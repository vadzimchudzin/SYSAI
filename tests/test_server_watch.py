"""№29 «Сервер»: every process start, its heartbeat, its clean stop and its memory peak are written down; the gaps
between runs are the downtime (a crash when the run before ended without a clean stop, a sleep of Render, an update);
a deploy where the new run starts before the old one stops leaves no gap. Neon's free compute hours are estimated
from the time the service is up, and the nightly dumps of the own server are copied to the recordings storage."""
import datetime as dt
from types import SimpleNamespace

import pytest

from app import config, server_watch, settings_store, storage
from app.db import Meeting, ServerRun, SessionLocal

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def run(instance, start, end, clean=True, commit="abc1234", platform="docker", peak=None, limit=None, busy=""):
    return SimpleNamespace(instance=instance, started_at=start, beat_at=end, stopped_at=end if clean else None,
                           commit=commit, platform=platform, peak_mb=peak, child_mb=None, limit_mb=limit, busy=busy)


def hours(n):
    return dt.timedelta(hours=n)


def test_gaps_between_runs_are_downtime_and_a_deploy_overlap_is_not():
    runs = [
        run("a", T0, T0 + hours(2)),                                             # clean stop, back in 30 min: a stop
        run("b", T0 + hours(2.5), T0 + hours(5), clean=False, peak=505, limit=512, busy="обработка совещания №3"),
        run("c", T0 + hours(5) + dt.timedelta(minutes=3), T0 + hours(8)),       # b was killed: a crash
        run("d", T0 + hours(7.9), T0 + hours(12), commit="def5678"),             # started before c stopped: a deploy
        run("e", T0 + hours(12) + dt.timedelta(seconds=40), T0 + hours(13)),     # a quick restart: no downtime
        run("f", T0 + hours(14), T0 + hours(15), commit="0000000"),              # another version after a stop
    ]
    gaps = server_watch.breaks(runs)
    assert [g["kind"] for g in gaps] == ["stop", "crash", "update"]
    assert gaps[0]["seconds"] == 1800
    crash = gaps[1]
    assert crash["busy"] == "обработка совещания №3" and crash["peak"] == 505 and crash["limit"] == 512
    assert crash["seconds"] == 180
    # a killed run is a crash however quickly the next one comes; on Render a clean stop is a sleep
    quick = server_watch.breaks([run("a", T0, T0 + hours(1), clean=False), run("b", T0 + hours(1) + dt.timedelta(seconds=20), T0 + hours(2))])
    assert [g["kind"] for g in quick] == ["crash"]
    asleep = server_watch.breaks([run("a", T0, T0 + hours(1), platform="render"),
                                  run("b", T0 + hours(3), T0 + hours(4), platform="render")])
    assert [g["kind"] for g in asleep] == ["sleep"] and asleep[0]["seconds"] == 7200
    # the database moved from Render to the own server along with these rows: a move, not a sleep
    moved = server_watch.breaks([run("a", T0, T0 + hours(1), platform="render"),
                                 run("b", T0 + hours(1.5), T0 + hours(4), commit="", platform="docker")])
    assert [g["kind"] for g in moved] == ["move"]


def test_runs_are_recorded_and_the_page_tells_the_downtime_and_the_crash(client, monkeypatch):
    monkeypatch.setattr(server_watch, "memory", lambda: {"rss": 210, "peak": 480, "child": 120, "limit": 512, "used": 300,
                                                         "oom_kills": 0})
    with SessionLocal() as s:
        s.add(Meeting(title="Планёрка", status="transcribing"))
        s.commit()
    start = dt.datetime.now(UTC) - hours(50)
    server_watch.on_start(start, instance="old1")
    server_watch.beat(start + hours(20), instance="old1")
    server_watch.on_start(start + hours(23), instance="old2")  # old1 never stopped: killed
    server_watch.beat(start + hours(30), instance="old2")
    server_watch.on_stop(start + hours(30), instance="old2")
    server_watch.on_start(start + hours(31), instance="live")
    with SessionLocal() as s:
        first = s.query(ServerRun).filter_by(instance="old1").one()
        assert first.stopped_at is None and first.peak_mb == 480 and first.child_mb == 120 and first.limit_mb == 512
        assert first.busy == "обработка совещания №1"
        assert s.query(ServerRun).filter_by(instance="old2").one().stopped_at is not None
    w = server_watch.summary(live="live")
    assert [b["kind"] for b in w["breaks"]] == ["stop", "crash"]  # newest first
    assert w["count"] == 2 and w["crashes"] == 1 and w["down_s"] == pytest.approx(4 * 3600, abs=2)
    text = " ".join(line for _, line in w["verdict"])
    assert "не работал 2 раза, всего 4 ч" in text
    assert "Аварийных остановок: 1" in text and "обработка совещания №1" in text and "пик памяти 480 МБ из 512" in text
    assert "Память на пределе: пик 480 МБ из 512 МБ" in text
    page = client.get("/server")
    assert page.status_code == 200
    assert "Итог за 7 дней" in page.text and "Сбой: процесс оборвался без остановки" in page.text
    assert "Остановка или перезапуск" in page.text and "deploy/HOSTING.md" in page.text
    assert client.get("/settings").text.count('href="/server"') == 1


def test_a_quiet_week_says_so_and_old_runs_are_deleted(monkeypatch):
    monkeypatch.setattr(server_watch, "memory", lambda: {"rss": 150, "peak": 200, "child": None, "limit": 4096,
                                                         "used": 250, "oom_kills": 0})
    moment = dt.datetime.now(UTC)
    server_watch.on_start(moment - dt.timedelta(days=60), instance="ancient")
    server_watch.on_start(moment - dt.timedelta(days=9), instance="live")
    with SessionLocal() as s:
        assert [r.instance for r in s.query(ServerRun)] == ["live"]
    w = server_watch.summary(moment, live="live")
    assert w["count"] == 0 and w["availability"] == 100.0
    assert ("ok", "За 7 дней перерывов не было.") in w["verdict"]
    assert ("ok", "Памяти хватает: пик 200 МБ из 4096 МБ.") in w["verdict"]
    assert not [line for level, line in w["verdict"] if level != "ok"]


def test_neon_free_hours_run_out_when_the_service_never_sleeps(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "UTC")
    month = dt.datetime(2026, 11, 1, tzinfo=UTC)
    moment = month + dt.timedelta(days=10)
    always = [SimpleNamespace(instance="live", started_at=month, beat_at=moment, stopped_at=None)]
    est = server_watch.neon_estimate(always, moment, live="live")
    assert est["used"] == 60 and est["projected"] == 180 and est["ratio"] == 1.0
    assert est["runs_out"].strftime("%d.%m %H:%M") == "17.11 16:00"  # 400 hours from the 1st
    # up a third of the time (Render free sleeping): enough
    third = [SimpleNamespace(instance=f"r{d}", started_at=month + dt.timedelta(days=d), beat_at=month + dt.timedelta(days=d, hours=8),
                             stopped_at=month + dt.timedelta(days=d, hours=8)) for d in range(10)]
    est = server_watch.neon_estimate(third, moment)
    assert est["projected"] == 60 and est["runs_out"] is None and not est["exhausted"]
    # too short a measure: no guess
    assert server_watch.neon_estimate([SimpleNamespace(instance="x", started_at=moment - hours(2), beat_at=moment,
                                                       stopped_at=None)], moment, live="x") is None
    # the line on the page
    lines = server_watch.verdict({"tracked_h": 240, "count": 0, "crashes": 0, "breaks": [], "memory": {}, "peak_week": None,
                                  "neon": server_watch.neon_estimate(always, moment, live="live"),
                                  "db": {"neon": True, "size_mb": 900}, "backups": {"here": False}})
    text = " ".join(line for _, line in lines)
    assert "к концу ≈ 180 при 100 бесплатных" in text and "около 17.11" in text
    assert "База занимает 900 МБ из 1024 МБ" in text


def test_memory_reads_the_container_limit_and_the_kills(tmp_path, monkeypatch):
    cg, proc = tmp_path / "cg", tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    cg.mkdir()
    (cg / "memory.max").write_text("536870912\n")
    (cg / "memory.current").write_text(str(300 * 1048576))
    (cg / "memory.events").write_text("low 0\nhigh 0\nmax 4\noom 2\noom_kill 2\n")
    (proc / "self/status").write_text("Name:\tpython\nVmRSS:\t  204800 kB\n")
    (proc / "meminfo").write_text("MemTotal:       16384000 kB\n")
    monkeypatch.setattr(server_watch, "CGROUP", cg)
    monkeypatch.setattr(server_watch, "PROC", proc)
    mem = server_watch.memory()
    assert mem["limit"] == 512 and mem["used"] == 300 and mem["rss"] == 200 and mem["oom_kills"] == 2
    assert mem["peak"] and mem["peak"] > 10  # this test process
    (cg / "memory.max").write_text("max\n")  # no container limit: the machine's memory
    assert server_watch.memory()["limit"] == 16000


def test_the_newest_nightly_dump_is_copied_to_the_storage_and_old_copies_go(tmp_path, monkeypatch, client):
    import os
    folder = tmp_path / "backups"
    folder.mkdir()
    old = folder / "sysai_2026-11-01_0300.dump"
    new = folder / "sysai_2026-11-02_0300.dump"
    old.write_bytes(b"old" * 100)
    new.write_bytes(b"new" * 200)
    (folder / "sysai_2026-11-03_0300.dump.tmp").write_bytes(b"half")  # being written: not a dump yet
    stamp = dt.datetime.now(UTC).timestamp()
    os.utime(old, (stamp - 86400 * 2, stamp - 86400 * 2))
    os.utime(new, (stamp - 3600, stamp - 3600))
    monkeypatch.setattr(config, "BACKUP_DIR", folder)
    monkeypatch.setattr(config, "BACKUP_KEEP_DAYS", 14)
    calls = []
    monkeypatch.setattr(storage, "configured", lambda: True)
    monkeypatch.setattr(storage, "put_file", lambda key, path, content_type="": calls.append(("put", key)) or 600)
    monkeypatch.setattr(storage, "delete", lambda key: calls.append(("delete", key)))
    assert server_watch.copy_backups() == "скопирован sysai_2026-11-02_0300.dump"
    assert server_watch.copy_backups() == "копия уже есть"
    assert calls == [("put", "backups/sysai_2026-11-02_0300.dump")]
    b = server_watch.backups()
    assert b["here"] and b["count"] == 2 and b["latest"]["copied"] and not b["latest"]["stale"] and b["copied"] == 1
    # a copy older than BACKUP_KEEP_DAYS is deleted from the storage
    state = settings_store.get("backup_state")
    state["copied"].insert(0, {"name": "sysai_2026-10-01_0300.dump", "size": 5, "at": "2026-10-01T03:00:00+00:00"})
    settings_store.set_many({"backup_state": state})
    server_watch.copy_backups()
    assert calls[-1] == ("delete", "backups/sysai_2026-10-01_0300.dump")
    assert [c["name"] for c in settings_store.get("backup_state")["copied"]] == ["sysai_2026-11-02_0300.dump"]
    # the storage refuses: the error is kept and shown, the page offers the copy again
    def refuse(key, path, content_type=""):
        raise storage.StorageError("загрузка: нет доступа к бакету")
    monkeypatch.setattr(storage, "put_file", refuse)
    newer = folder / "sysai_2026-11-03_0300.dump"
    newer.write_bytes(b"x")
    assert server_watch.copy_backups() == "загрузка: нет доступа к бакету"
    page = client.get("/server")
    assert "Копия бэкапа в хранилище не сделана: загрузка: нет доступа к бакету" in page.text
    assert "Скопировать сейчас" in page.text
    # switched off: nothing is copied by the watcher; the button still copies
    client.post("/server/backup", data={})
    assert settings_store.get("backup_copy") is False
    assert server_watch.copy_backups() == ""
    monkeypatch.setattr(storage, "put_file", lambda key, path, content_type="": calls.append(("put", key)) or 1)
    answer = client.post("/server/backup", data={"action": "copy"}, follow_redirects=False)
    assert "msg=" in answer.headers["location"] and calls[-1] == ("put", "backups/sysai_2026-11-03_0300.dump")


def test_without_dumps_the_page_says_who_makes_them(client, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", None)
    page = client.get("/server")
    assert "Здесь ночных дампов нет" in page.text
    assert server_watch.copy_backups(force=True) == ""
