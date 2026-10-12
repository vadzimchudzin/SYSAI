"""Sign-in guard: wrong passwords are counted, the public defaults from the README close the login, /health/details is private."""
import pytest
from fastapi.testclient import TestClient

from app import accounts, config


@pytest.fixture(autouse=True)
def fast_login_failures(monkeypatch):
    monkeypatch.setattr("app.main.time.sleep", lambda seconds: None)
    accounts._failures.clear()


@pytest.fixture
def visitor():
    from app.main import app
    with TestClient(app) as c:
        yield c


def attempt(c, username="sysai", password="pw"):
    return c.post("/login", data={"username": username, "password": password}, follow_redirects=False)


# ----------------------------------------------------------------------------- counting wrong passwords
def test_wrong_passwords_lock_the_address_even_for_the_right_one(visitor):
    for _ in range(accounts.LOGIN_LIMIT):
        assert attempt(visitor, password="nope").headers["location"] == "/login?error=1"
    answer = attempt(visitor, password="pw")          # the right password, but the address has used up its tries
    assert answer.headers["location"] == "/login?error=throttled"
    assert visitor.get("/app", follow_redirects=False).status_code == 303   # and it did not sign in


def test_right_passwords_never_fill_the_counter(visitor):
    for _ in range(accounts.LOGIN_LIMIT + 5):
        assert attempt(visitor).headers["location"] == "/app"
        visitor.get("/logout")
    assert accounts._failures == {}


def test_the_lock_ends_with_the_window(visitor, monkeypatch):
    for _ in range(accounts.LOGIN_LIMIT):
        attempt(visitor, password="nope")
    assert attempt(visitor).headers["location"] == "/login?error=throttled"
    later = accounts.time.monotonic() + accounts.LOGIN_WINDOW + 1
    monkeypatch.setattr(accounts.time, "monotonic", lambda: later)
    assert attempt(visitor).headers["location"] == "/app"


def test_one_address_does_not_lock_another():
    for _ in range(accounts.LOGIN_LIMIT):
        accounts.note_login_failure("10.0.0.1", "someone")
    assert accounts.login_blocked("10.0.0.1", "other-name")
    assert not accounts.login_blocked("10.0.0.2", "other-name")


def test_a_flood_on_one_login_name_from_many_addresses_is_stopped_later():
    for i in range(accounts.LOGIN_NAME_LIMIT):
        assert not accounts.login_blocked(f"10.1.0.{i}", "sysai")
        accounts.note_login_failure(f"10.1.0.{i}", "sysai")
    assert accounts.login_blocked("10.1.9.9", "SYSAI")      # a new address, same name (case does not matter)


def test_other_account_states_are_not_counted(visitor):
    visitor.post("/register", data={"username": "ivan", "display_name": "Иван", "password": "correct-horse",
                                    "password2": "correct-horse", "consent": "1"})
    for _ in range(accounts.LOGIN_LIMIT + 3):
        assert attempt(visitor, "ivan", "correct-horse").headers["location"] == "/login?error=pending"
    assert accounts._failures == {}


def test_the_login_page_explains_the_lock(visitor):
    assert "Подождите 10 минут" in visitor.get("/login?error=throttled").text


# ----------------------------------------------------------------------------- the public defaults
def test_the_default_admin_password_closes_the_superuser_login(visitor, monkeypatch):
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-long-random-secret")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "SYSAI")
    assert accounts.insecure_reason() == "ADMIN_PASSWORD"
    assert attempt(visitor, "SYSAI", "SYSAI").headers["location"] == "/login?error=insecure"
    assert accounts.authenticate("sysai", "pw") == (None, "insecure")        # whatever the password is
    assert "ADMIN_PASSWORD" in visitor.get("/login?error=insecure").text


def test_the_default_password_does_not_close_other_accounts(monkeypatch):
    accounts.register("ivan", "correct-horse")
    with accounts.SessionLocal() as s:
        user = s.query(accounts.User).filter_by(username="ivan").one()
        user.status = "approved"
        s.commit()
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-long-random-secret")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "SYSAI")
    user, problem = accounts.authenticate("ivan", "correct-horse")
    assert user is not None and problem == ""


def test_a_real_admin_password_keeps_the_login_open(visitor, monkeypatch):
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(config, "SECRET_KEY", "a-real-long-random-secret")
    assert accounts.insecure_reason() == ""
    assert attempt(visitor).headers["location"] == "/app"


def test_the_default_secret_key_closes_every_login_and_kills_old_cookies(client, monkeypatch):
    assert client.get("/app", follow_redirects=False).status_code == 200       # signed in (by the fixture)
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(config, "SECRET_KEY", config.DEFAULT_SECRET_KEY)
    assert accounts.insecure_reason() == "SECRET_KEY"
    assert client.get("/app", follow_redirects=False).status_code == 303       # the cookie is worth nothing now
    assert accounts.authenticate("ivan", "whatever") == (None, "insecure")
    assert attempt(client).headers["location"] == "/login?error=insecure"


def test_an_empty_secret_key_counts_as_the_default(monkeypatch):
    monkeypatch.setattr(config, "TESTING", False)
    monkeypatch.setattr(config, "SECRET_KEY", "  ")
    assert config.weak_secret_key()


def test_the_testing_mode_never_trips_the_guard():
    assert config.TESTING and not config.weak_secret_key() and not config.weak_admin_password()


# ----------------------------------------------------------------------------- /health/details
def test_health_details_needs_a_signed_in_person(visitor):
    assert visitor.get("/health/details").status_code == 401
    attempt(visitor)
    assert {"llm", "telegram", "telegram_account", "database"} <= set(visitor.get("/health/details").json())


def test_plain_health_stays_open(visitor):
    assert visitor.get("/health").json()["ok"] is True
