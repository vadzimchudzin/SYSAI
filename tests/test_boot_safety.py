"""Safe boot: /health needs no database, strangers never reach the database, one instance owns the Telegram account."""
import json
import time

import pytest

from app import agent, config, settings_store, userbot
from app.db import Employee, SessionLocal, Setting
from test_ai_mode import FakeBackend, tgacc  # noqa: F401  (fixture)


def holder():
    with SessionLocal() as s:
        row = s.get(Setting, "userbot_lease")
        return json.loads(row.value) if row else None


def other_instance(monkeypatch, name="other"):
    monkeypatch.setattr(userbot, "INSTANCE", name)


# ----------------------------------------------------------------------------- /health
def test_health_does_not_touch_the_database(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("the database must not be used by /health")
    monkeypatch.setattr(config, "openrouter_key", boom)
    monkeypatch.setattr("app.settings_store.SessionLocal", boom)
    body = client.get("/health").json()
    assert body["ok"] is True
    assert set(body) >= {"commit", "uptime_s", "telegram_account"}


def test_health_names_the_running_commit(client, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a8e40e8f1a53b0191c87e10ee80f742c6d6ff933")
    assert client.get("/health").json()["commit"] == "a8e40e8"


def test_health_details_keeps_the_old_flags(client):
    body = client.get("/health/details").json()
    assert {"llm", "telegram", "telegram_account", "database"} <= set(body)


def test_the_boot_delay_is_configured():
    assert config.STARTUP_DELAY >= 0


# ----------------------------------------------------------------------------- one instance owns the account
def test_the_first_instance_takes_the_account_and_renews_the_lease(tgacc):
    assert userbot.take_lease() is True
    first = holder()
    assert first["id"] == userbot.INSTANCE and first["until"] > time.time()
    assert userbot.take_lease() is True  # renewing is allowed


def test_a_second_instance_waits_while_the_first_one_holds_the_account(tgacc, monkeypatch):
    assert userbot.take_lease()
    mine = userbot.INSTANCE
    other_instance(monkeypatch)
    assert userbot.take_lease() is False
    assert holder()["id"] == mine


def test_start_does_not_connect_while_another_instance_holds_the_account(tgacc, monkeypatch):
    userbot.stop()
    assert userbot.take_lease()
    tgacc.connects = 0
    other_instance(monkeypatch)
    assert userbot.start() is False
    assert tgacc.connects == 0
    assert "занят" in userbot.status()["error"]


def test_an_expired_lease_can_be_taken_over(tgacc, monkeypatch):
    with SessionLocal() as s:
        s.merge(Setting(key="userbot_lease", value=json.dumps({"id": "dead", "until": time.time() - 1})))
        s.commit()
    assert userbot.take_lease() is True
    assert holder()["id"] == userbot.INSTANCE


def test_stopping_releases_the_account_for_the_next_instance(tgacc, monkeypatch):
    assert userbot.take_lease()
    userbot.stop()
    other_instance(monkeypatch)
    assert userbot.take_lease() is True  # no waiting for the lease to run out


def test_the_watchdog_steps_aside_when_somebody_else_holds_the_account(tgacc, monkeypatch):
    assert userbot.ready()
    with SessionLocal() as s:
        s.merge(Setting(key="userbot_lease", value=json.dumps({"id": "intruder", "until": time.time() + 60})))
        s.commit()
    userbot.watchdog()
    assert not userbot.ready()
    assert tgacc.connected is None


def test_the_watchdog_renews_the_lease(tgacc):
    before = holder()["until"] if holder() else 0
    time.sleep(0.01)
    userbot.watchdog()
    assert holder()["until"] > before


def test_start_hands_the_sender_filter_to_the_backend(tgacc):
    userbot.stop()
    assert userbot.start() is True
    assert tgacc.wants is agent.wants


# ----------------------------------------------------------------------------- strangers are dropped early
@pytest.fixture
def team():
    agent.invalidate_roster()
    with SessionLocal() as s:
        s.add(Employee(name="Иван Петров", telegram_chat_id="@ivan", position="Менеджер"))
        s.add(Employee(name="Бывший", telegram_chat_id="@gone", position="Менеджер", active=False))
        s.add(Employee(name="Анна", telegram_chat_id="@anna", telegram_user_id="43", position="Менеджер"))
        s.commit()
    settings_store.set_many({"approver_chat_ids": ["@boss"], "full_access_chat_ids": ["+375291112233"]})
    agent.invalidate_roster()
    yield
    agent.invalidate_roster()


def test_the_team_is_served_and_strangers_are_not(team):
    assert agent.wants({"user_id": 1, "username": "ivan"})
    assert agent.wants({"user_id": 43, "username": ""})              # known by the numeric id
    assert agent.wants({"user_id": 7, "username": "boss"})           # reviewer
    assert agent.wants({"user_id": 9, "username": "", "phone": "375291112233"})  # full access by phone
    assert not agent.wants({"user_id": 1000, "username": "stranger"})
    assert not agent.wants({"user_id": 5, "username": "gone"})       # a dismissed employee


def test_the_roster_is_cached_and_follows_changes_after_invalidation(team):
    assert not agent.wants({"user_id": 11, "username": "newbie"})
    with SessionLocal() as s:
        s.add(Employee(name="Новичок", telegram_chat_id="@newbie", position="Менеджер"))
        s.commit()
    assert not agent.wants({"user_id": 11, "username": "newbie"})    # still the cached list
    agent.invalidate_roster()
    assert agent.wants({"user_id": 11, "username": "newbie"})


def test_the_roster_refreshes_by_itself_after_its_lifetime(team, monkeypatch):
    assert not agent.wants({"user_id": 12, "username": "late"})
    with SessionLocal() as s:
        s.add(Employee(name="Поздний", telegram_chat_id="@late", position="Менеджер"))
        s.commit()
    monkeypatch.setattr(agent, "ROSTER_TTL", -1)
    assert agent.wants({"user_id": 12, "username": "late"})


def test_the_backend_drops_strangers_before_any_database_work():
    import asyncio

    class Person:
        def __init__(self, uid, username):
            self.id, self.username, self.phone, self.bot = uid, username, "", False
            self.first_name, self.last_name = "X", ""

    class Message:
        raw_text, id = "привет", 5

    handed = []
    backend = userbot.TelethonBackend()
    backend._on_message = handed.append
    backend.wants = lambda probe: probe["username"] == "ivan"

    async def go():
        await backend._dispatch(Person(1, "ivan"), Message())
        await backend._dispatch(Person(2, "stranger"), Message())
    asyncio.run(go())
    assert [m["username"] for m in handed] == ["ivan"]
