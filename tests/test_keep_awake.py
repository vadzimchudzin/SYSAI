"""A free Render instance is kept awake while a meeting is processed or a recording uploaded (keep_awake.py)."""
import httpx
import pytest

from app import config, keep_awake
from app.db import Meeting, SessionLocal


@pytest.fixture
def awake(monkeypatch):
    monkeypatch.setattr(config, "KEEP_AWAKE", True)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://sysai.example.com")
    monkeypatch.setattr(keep_awake, "_STATE", {"pinged": 0.0, "checked": 0.0, "since": 0.0, "stuck": False})
    clock, asked = [100_000.0], []
    monkeypatch.setattr(keep_awake, "_clock", lambda: clock[0])

    def get(url, **_kwargs):
        asked.append(url)
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(httpx, "get", get)
    return clock, asked


def add(**fields):
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", **fields)
        s.add(m)
        s.commit()
        return m.id


def set_status(mid, status):
    with SessionLocal() as s:
        s.get(Meeting, mid).status = status
        s.commit()


def test_nothing_in_progress_lets_the_instance_sleep(awake):
    clock, asked = awake
    add(status="done")
    for _ in range(5):
        keep_awake.run_due()
        clock[0] += keep_awake.EVERY
    assert asked == []


def test_a_meeting_in_progress_asks_the_own_address_every_ten_minutes(awake):
    clock, asked = awake
    mid = add(status="transcribing")
    keep_awake.run_due()
    assert asked == ["https://sysai.example.com/health"]
    clock[0] += keep_awake.EVERY - 30
    keep_awake.run_due()
    assert len(asked) == 1  # not yet
    clock[0] += 30
    keep_awake.run_due()
    assert len(asked) == 2
    set_status(mid, "awaiting_approval")
    clock[0] += keep_awake.EVERY
    keep_awake.run_due()
    clock[0] += keep_awake.EVERY
    keep_awake.run_due()
    assert len(asked) == 2 and keep_awake._STATE["since"] == 0.0


def test_a_recording_waiting_for_the_storage_keeps_it_awake_too(awake):
    clock, asked = awake
    add(status="done", recording_status="pending", recording={"attempts": 1})
    keep_awake.run_due()
    assert keep_awake.busy() == "сохранение записи в хранилище" and len(asked) == 1


def test_a_stuck_job_does_not_hold_the_instance_forever(awake):
    clock, asked = awake
    add(status="queued")
    keep_awake.run_due()
    clock[0] += keep_awake.LONGEST + 1
    keep_awake.run_due()
    clock[0] += keep_awake.EVERY
    keep_awake.run_due()
    assert len(asked) == 1 and keep_awake._STATE["stuck"]


def test_a_failed_request_is_only_logged(awake, monkeypatch):
    clock, asked = awake
    add(status="analyzing")

    def down(url, **_kwargs):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "get", down)
    keep_awake.run_due()  # must not raise: the background loop goes on
    assert keep_awake._STATE["pinged"] == clock[0]


@pytest.mark.parametrize("on, url", [(False, "https://sysai.example.com"), (True, ""), (True, "http://localhost:8000")])
def test_off_without_the_setting_or_a_public_https_address(awake, monkeypatch, on, url):
    clock, asked = awake
    monkeypatch.setattr(config, "KEEP_AWAKE", on)
    monkeypatch.setattr(config, "PUBLIC_URL", url)
    add(status="transcribing")
    keep_awake.run_due()
    assert asked == []
