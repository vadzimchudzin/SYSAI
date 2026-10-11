"""Accounts: nobody registers freely, a superuser approves each request, roles guard the admin sections."""
import pytest
from fastapi.testclient import TestClient

from app import accounts, config, db
from app.db import SessionLocal, User


@pytest.fixture(autouse=True)
def fast_login_failures(monkeypatch):
    monkeypatch.setattr("app.main.time.sleep", lambda seconds: None)
    accounts._attempts.clear()


@pytest.fixture
def visitor():
    """A second browser (own cookie jar), signed out."""
    from app.main import app
    with TestClient(app) as c:
        yield c


def sign_up(c, username="ivan", password="correct-horse", **extra):
    data = {"username": username, "display_name": "Иван", "password": password, "password2": password, **extra}
    return c.post("/register", data=data)


def user_row(username):
    with SessionLocal() as s:
        return s.query(User).filter_by(username=username).first()


def user_id(username):
    return user_row(username).id


def sign_in(c, username, password="correct-horse"):
    return c.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def approved_user(client, visitor, username="ivan", password="correct-horse"):
    sign_up(visitor, username, password)
    client.post(f"/admin/users/{user_id(username)}/approve")
    assert sign_in(visitor, username, password).headers["location"] == "/app"


# ----------------------------------------------------------------------------- passwords
def test_password_hash_round_trip_and_garbage():
    stored = accounts.hash_password("correct-horse")
    assert stored.startswith("scrypt$") and "correct-horse" not in stored
    assert accounts.verify_password("correct-horse", stored)
    assert not accounts.verify_password("wrong", stored)
    assert accounts.hash_password("correct-horse") != stored  # random salt
    for junk in ("", "plain", "scrypt$x$y", "md5$1$2$3$4$5"):
        assert not accounts.verify_password("correct-horse", junk)


# ----------------------------------------------------------------------------- superuser from the environment
def test_environment_superuser_exists_and_follows_password_rotation(monkeypatch):
    accounts.ensure_superuser()
    row = user_row("sysai")
    assert row.role == "superuser" and row.status == "approved"
    assert accounts.authenticate("SYSAI", "pw")[0] is not None
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "rotated-secret")
    accounts.ensure_superuser()
    assert accounts.authenticate("sysai", "pw")[0] is None
    assert accounts.authenticate("sysai", "rotated-secret")[0] is not None


def test_environment_superuser_is_restored_after_being_disabled():
    accounts.ensure_superuser()
    with SessionLocal() as s:
        row = s.query(User).filter_by(username="sysai").first()
        row.status, row.role = "disabled", "user"
        s.commit()
    accounts.ensure_superuser()
    assert user_row("sysai").status == "approved" and user_row("sysai").role == "superuser"


# ----------------------------------------------------------------------------- requests
def test_sign_up_creates_a_pending_request_that_cannot_sign_in(client, visitor):
    r = sign_up(visitor)
    assert r.status_code == 200 and "Заявка отправлена" in r.text
    assert user_row("ivan").status == "pending" and user_row("ivan").role == "user"
    assert user_row("ivan").password_hash != "correct-horse"
    r = sign_in(visitor, "ivan")
    assert r.headers["location"] == "/login?error=pending"
    assert visitor.get("/app", follow_redirects=False).headers["location"] == "/login"
    assert "ещё не подтверждена" in visitor.get("/login?error=pending").text


def test_wrong_password_and_unknown_login_look_the_same(client, visitor):
    sign_up(visitor)
    assert sign_in(visitor, "ivan", "nope-nope-nope").headers["location"] == "/login?error=1"
    assert sign_in(visitor, "ghost", "correct-horse").headers["location"] == "/login?error=1"


@pytest.mark.parametrize("fields, message", [
    ({"username": "Иван"}, "Логин"),
    ({"username": "ab"}, "Логин"),
    ({"username": "has space"}, "Логин"),
    ({"password": "short", "password2": "short"}, "Пароль не короче"),
    ({"password2": "different-one"}, "Пароли не совпадают"),
])
def test_sign_up_validation(client, visitor, fields, message):
    data = {"username": "ivan", "display_name": "", "password": "correct-horse", "password2": "correct-horse", **fields}
    r = visitor.post("/register", data=data)
    assert r.status_code == 400 and message in r.text
    assert user_row("ivan") is None


def test_duplicate_login_is_rejected_case_insensitively(client, visitor):
    sign_up(visitor, "ivan")
    r = sign_up(visitor, "IVAN")
    assert r.status_code == 400 and "занят" in r.text
    assert sign_up(visitor, "sysai").status_code == 400  # the environment superuser's login is taken too


def test_sign_up_attempts_are_rate_limited_per_address(client, visitor, monkeypatch):
    monkeypatch.setattr(accounts, "REGISTER_LIMIT", 2)
    assert sign_up(visitor, "first-one").status_code == 200
    assert sign_up(visitor, "second-one").status_code == 200
    r = sign_up(visitor, "third-one")
    assert r.status_code == 400 and "Слишком много попыток" in r.text
    assert user_row("third-one") is None


def test_pending_requests_are_capped(client, visitor, monkeypatch):
    monkeypatch.setattr(accounts, "MAX_PENDING", 1)
    sign_up(visitor, "first-one")
    r = sign_up(visitor, "second-one")
    assert r.status_code == 400 and "необработанных" in r.text


def test_closing_registration_stops_new_requests(client, visitor):
    assert client.post("/admin/users/registration", data={"open": ""}).status_code == 200
    assert "Приём заявок закрыт" in visitor.get("/register").text
    r = sign_up(visitor)
    assert r.status_code == 400 and user_row("ivan") is None
    client.post("/admin/users/registration", data={"open": "1"})
    assert sign_up(visitor).status_code == 200


# ----------------------------------------------------------------------------- approval and roles
def test_superuser_sees_the_request_and_approves_it(client, visitor):
    sign_up(visitor)
    page = client.get("/admin/users").text
    assert "Заявки на доступ (1)" in page and "ivan" in page and "Одобрить" in page
    r = client.post(f"/admin/users/{user_id('ivan')}/approve", follow_redirects=False)
    assert r.status_code == 303 and "/admin/users?msg=" in r.headers["location"]
    assert user_row("ivan").status == "approved" and user_row("ivan").decided_by == "sysai"
    assert sign_in(visitor, "ivan").headers["location"] == "/app"
    assert visitor.get("/app").status_code == 200


def test_regular_user_cannot_open_admin_sections(client, visitor):
    approved_user(client, visitor)
    for path in ("/settings", "/employees", "/admin/users"):
        r = visitor.get(path, follow_redirects=False)
        assert r.status_code == 403, path
        assert "суперпользователю" in r.text
    assert visitor.post("/admin/users/1/disable", follow_redirects=False).status_code == 403
    assert visitor.post("/settings", data={"company_name": "x"}, follow_redirects=False).status_code == 403
    nav = visitor.get("/app").text
    assert "/admin/users" not in nav and "/settings" not in nav and "/employees" not in nav
    assert "/admin/users" in client.get("/app").text


def test_anonymous_visitors_are_sent_to_the_login(client):
    from app.main import app
    with TestClient(app) as anonymous:
        for path in ("/admin/users", "/settings", "/employees", "/app"):
            assert anonymous.get(path, follow_redirects=False).headers["location"] == "/login"


def test_reject_blocks_sign_in_and_can_be_reversed(client, visitor):
    sign_up(visitor)
    client.post(f"/admin/users/{user_id('ivan')}/reject")
    assert sign_in(visitor, "ivan").headers["location"] == "/login?error=rejected"
    client.post(f"/admin/users/{user_id('ivan')}/approve")
    assert sign_in(visitor, "ivan").headers["location"] == "/app"


def test_disabling_an_account_ends_its_open_session(client, visitor):
    approved_user(client, visitor)
    assert visitor.get("/app", follow_redirects=False).status_code == 200
    client.post(f"/admin/users/{user_id('ivan')}/disable")
    assert visitor.get("/app", follow_redirects=False).headers["location"] == "/login"
    assert sign_in(visitor, "ivan").headers["location"] == "/login?error=disabled"
    client.post(f"/admin/users/{user_id('ivan')}/approve")
    assert sign_in(visitor, "ivan").headers["location"] == "/app"


def test_deleting_an_account_ends_its_session_and_frees_the_login(client, visitor):
    approved_user(client, visitor)
    client.post(f"/admin/users/{user_id('ivan')}/delete")
    assert user_row("ivan") is None
    assert visitor.get("/app", follow_redirects=False).headers["location"] == "/login"
    assert sign_up(visitor).status_code == 200


def test_promote_and_demote(client, visitor):
    approved_user(client, visitor)
    client.post(f"/admin/users/{user_id('ivan')}/promote")
    assert user_row("ivan").role == "superuser"
    assert visitor.get("/admin/users").status_code == 200
    client.post(f"/admin/users/{user_id('ivan')}/demote")
    assert user_row("ivan").role == "user"
    assert visitor.get("/admin/users", follow_redirects=False).status_code == 403


def test_pending_account_cannot_be_promoted(client, visitor):
    sign_up(visitor)
    client.post(f"/admin/users/{user_id('ivan')}/promote")
    assert user_row("ivan").role == "user"


def test_superuser_cannot_lock_themselves_out(client):
    me = user_id("sysai")
    for action in ("disable", "demote", "delete"):
        r = client.post(f"/admin/users/{me}/{action}", follow_redirects=False)
        assert r.status_code == 303
    row = user_row("sysai")
    assert row is not None and row.role == "superuser" and row.status == "approved"


def test_two_superusers_may_demote_each_other_but_the_last_one_stays(client, visitor):
    approved_user(client, visitor)
    client.post(f"/admin/users/{user_id('ivan')}/promote")
    assert visitor.post(f"/admin/users/{user_id('sysai')}/demote", follow_redirects=False).status_code == 303
    assert user_row("sysai").role == "user"
    with SessionLocal() as s:
        assert s.query(User).filter_by(role="superuser", status="approved").count() == 1
    with pytest.raises(accounts.AccountError):
        accounts.apply(user_id("ivan"), "demote", "sysai")
    assert user_row("ivan").role == "superuser"


def test_apply_refuses_to_remove_the_last_superuser():
    accounts.ensure_superuser()
    accounts.create_user("helper", "correct-horse", "user", "sysai")
    for action in ("disable", "demote", "delete"):
        with pytest.raises(accounts.AccountError):
            accounts.apply(user_id("sysai"), action, "helper")
    assert user_row("sysai").role == "superuser"


def test_superuser_creates_accounts_directly(client, visitor):
    r = client.post("/admin/users/create", data={"username": "anna", "display_name": "Анна", "password": "correct-horse",
                                                 "role": "superuser"}, follow_redirects=False)
    assert r.status_code == 303 and "создан" in client.get(r.headers["location"]).text
    row = user_row("anna")
    assert row.role == "superuser" and row.status == "approved" and row.decided_by == "sysai"
    assert sign_in(visitor, "anna").headers["location"] == "/app"
    assert visitor.get("/admin/users").status_code == 200
    bad = client.post("/admin/users/create", data={"username": "bob", "password": "x", "role": "user"})
    assert user_row("bob") is None and "Пароль не короче" in bad.text
    bad = client.post("/admin/users/create", data={"username": "bob", "password": "correct-horse", "role": "root"})
    assert user_row("bob") is None and "Неизвестная роль" in bad.text


def test_unknown_action_and_user_are_reported(client):
    assert "Неизвестное действие" in client.post(f"/admin/users/{user_id('sysai')}/explode").text
    assert "не найден" in client.post("/admin/users/9999/approve").text


def test_pending_badge_shows_in_the_navigation(client, visitor):
    sign_up(visitor)
    assert '<span class="pill red">1</span>' in client.get("/app").text
