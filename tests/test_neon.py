"""Neon readiness: engine options per backend, wake-up retries, TLS in provider URLs."""
import pathlib

import pytest
from sqlalchemy.exc import OperationalError

from app import config, db


def test_sqlite_engine_keeps_thread_check_off():
    options = db.engine_options("sqlite:////tmp/x.db")
    assert options["connect_args"] == {"check_same_thread": False}
    assert options["pool_pre_ping"] is True
    assert "pool_size" not in options


def test_postgres_engine_survives_suspended_compute_and_pgbouncer():
    options = db.engine_options("postgresql+psycopg://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db?sslmode=require")
    assert options["pool_pre_ping"] is True
    assert options["pool_recycle"] <= 300
    assert options["connect_args"]["connect_timeout"] >= 30
    # PgBouncer in transaction mode cannot reuse server-side prepared statements.
    assert options["connect_args"]["prepare_threshold"] is None


def test_postgres_engine_pins_ipv4_hostaddr_when_aaaa_would_win(monkeypatch):
    monkeypatch.setattr(db.socket, "getaddrinfo", lambda *a, **k: [
        (db.socket.AF_INET, db.socket.SOCK_STREAM, 6, "", ("203.0.113.10", 5432)),
    ])
    options = db.engine_options("postgresql+psycopg://u:p@ep-x.neon.tech/db?sslmode=require")
    assert options["connect_args"]["hostaddr"] == "203.0.113.10"


def test_postgres_engine_skips_hostaddr_without_ipv4(monkeypatch):
    monkeypatch.setattr(db.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("no A")))
    options = db.engine_options("postgresql+psycopg://u:p@ep-x.neon.tech/db?sslmode=require")
    assert "hostaddr" not in options["connect_args"]


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement):
        return None


class _FlakyEngine:
    """Fails like a Neon compute that is still waking up, then answers."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def connect(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise OperationalError("SELECT 1", {}, Exception("connection timeout expired\nendpoint is waking up"))
        return _Connection()


def test_wait_for_db_retries_until_the_compute_is_awake(monkeypatch):
    engine = _FlakyEngine(failures=2)
    pauses = []
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db.time, "sleep", pauses.append)
    db.wait_for_db(attempts=5, delay=2.0)
    assert engine.calls == 3
    assert pauses == [2.0, 4.0]


def test_wait_for_db_gives_up_with_the_original_error(monkeypatch):
    engine = _FlakyEngine(failures=99)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db.time, "sleep", lambda seconds: None)
    with pytest.raises(OperationalError):
        db.wait_for_db(attempts=3, delay=0.1)
    assert engine.calls == 3


def test_wait_for_db_is_instant_on_a_healthy_database():
    db.wait_for_db(attempts=1, delay=0)


@pytest.mark.parametrize("url, expected", [
    ("postgresql://u:p@ep-a-pooler.c-2.eu-central-1.aws.neon.tech/neondb",
     "postgresql+psycopg://u:p@ep-a-pooler.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require"),
    ("postgres://u:p@ep-a.neon.tech/neondb?channel_binding=require",
     "postgresql+psycopg://u:p@ep-a.neon.tech/neondb?channel_binding=require&sslmode=require"),
    ("postgresql://u:p@ep-a.neon.tech/neondb?sslmode=require",
     "postgresql+psycopg://u:p@ep-a.neon.tech/neondb?sslmode=require"),
    ("postgresql://u:p@localhost/test", "postgresql+psycopg://u:p@localhost/test"),
    ("postgresql+psycopg://u:p@db.internal/test", "postgresql+psycopg://u:p@db.internal/test"),
])
def test_neon_urls_get_tls_and_other_hosts_are_untouched(url, expected):
    assert config.database_url(url, pathlib.Path("/tmp")) == expected
