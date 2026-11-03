import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="sysai_test_")
os.environ.update(SYSAI_TESTING="1", DATA_DIR=_tmp,
                  DATABASE_URL=os.getenv("SYSAI_TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db",
                  ADMIN_USERNAME="SYSAI", ADMIN_PASSWORD="pw", SECRET_KEY="test-secret", OPENROUTER_API_KEY="", TELEGRAM_BOT_TOKEN="",
                  TELEGRAM_WEBHOOK_SECRET="hook")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from app import config, db, telegram  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    from app import accounts
    accounts._failures.clear()  # wrong-password counters are per process: one test must not lock out the next
    db.Base.metadata.drop_all(db.engine)
    db.init_db()
    from app import settings_store, userbot
    # the hourly limit of one person, the days off and the working-time count of silence are tested on their own
    # (otherwise a test that gives a moment would depend on the day of the week it runs)
    settings_store.set_many({"person_hourly_limit": 0, "silence_working_hours": False, "quiet_days": []})
    userbot._sent_to.clear()
    yield


@pytest.fixture
def tg(monkeypatch):
    """Fake Telegram: records every Bot API call."""
    calls = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake")

    def fake_call(method, data=None, files=None, timeout=60):
        calls.append((method, data or {}, files))
        return {"message_id": len(calls)}
    monkeypatch.setattr(telegram, "call", fake_call)
    return calls


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "sysai", "password": "pw"})
        yield c


# ----------------------------------------------------------------------------- the agent network (app/net)
@pytest.fixture
def net_on():
    from app import settings_store
    settings_store.set_many({"agents_on": True, "openrouter_api_key": "test-openrouter-key"})
    from app.net import mcp
    mcp._calls.clear()
    mcp._notes.clear()
    return True


@pytest.fixture
def net_agent(client, net_on):
    """A connected ОПО agent of the admin: {"id", "name", "token", "headers", "user_id", "department_id"}."""
    from app.db import Department, SessionLocal, User
    from app.net import agents
    with SessionLocal() as s:
        admin = s.query(User).filter(User.role == "superuser").first()
        dept = Department(name="ОПО")
        s.add(dept)
        s.commit()
        admin_id, dept_id = admin.id, dept.id
    agent = agents.create(admin_id, "opo-sysai", dept_id, None, "hermes", "", by="test")
    code = agents.issue_code(agent.id)
    _, token = agents.exchange(code, host="PC-OPO-1", harness="hermes", versions={"hermes": "0.9"})
    return {"id": agent.id, "name": agent.name, "token": token, "headers": {"Authorization": f"Bearer {token}"},
            "user_id": admin_id, "department_id": dept_id}
