"""«Сервер» (№29): is the service up all the time, and is its memory enough? The facts behind the move off Render free,
and afterwards the proof that the new place works without breaks.

Every process writes a ServerRun row: its start, a heartbeat every minute, its clean stop (shutdown), its memory peak.
A gap between one run's last sign of life and the next run's start is downtime. After a clean stop it was a restart, a
sleep of Render free or an update; without a clean stop the process was killed, most often for memory. Runs of a deploy
overlap (the new one starts before the old one stops): that is no break.

The nightly dumps of the own server (deploy/compose.yaml writes them to ./backups and mounts it read-only at BACKUP_DIR)
are copied to the recordings storage (backups/…): a dead disk must not take the only copy."""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import os
import pathlib
import re
import shutil
import threading
import time
import uuid

from sqlalchemy import text

from . import config, reminders, settings_store, storage
from .db import Meeting, ServerRun, SessionLocal, engine, now

log = logging.getLogger("sysai.server")

INSTANCE = uuid.uuid4().hex[:12]
BEAT_EVERY = 60            # seconds between heartbeats
BREAK_MIN = 120            # a clean restart shorter than this is no downtime (docker compose up, a quick reboot)
WINDOW_DAYS = 7
KEEP_DAYS = 40             # runs older than this are deleted
BACKUP_EVERY = 1800        # how often the backup folder is looked at
NEON_FREE_CU_HOURS = 100   # the Free plan of Neon, per month (neon.com/pricing, 2026)
NEON_MIN_CU = 0.25         # the smallest compute: it runs while the service runs (the service asks the database often)
NEON_FREE_STORAGE_MB = 1024
CGROUP = pathlib.Path("/sys/fs/cgroup")
PROC = pathlib.Path("/proc")
KINDS = {"crash": "Сбой: процесс оборвался без остановки",
         "update": "Обновление версии",
         "stop": "Остановка или перезапуск",
         "sleep": "Сон или перезапуск Render",
         "move": "Переезд на новое место"}
_state = {"beat": 0.0, "backup": 0.0}
_thread: threading.Thread | None = None
_stop = threading.Event()


def _aware(moment: dt.datetime | None) -> dt.datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment


def _local(moment: dt.datetime | None) -> dt.datetime | None:
    return _aware(moment).astimezone(reminders.zone()) if moment else None


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def duration(seconds: float) -> str:
    """«2 ч 15 мин», «40 мин», «35 с», «3 дн 4 ч»."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} дн {hours} ч" if hours else f"{days} дн"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{minutes} мин" if minutes else f"{secs} с"


def _span(start: dt.datetime, end: dt.datetime) -> str:
    """«02.11 08:05–12:05», «01.11 23:50 – 02.11 00:10»."""
    first, last = _local(start), _local(end)
    if first.date() == last.date():
        return f"{first.strftime('%d.%m %H:%M')}–{last.strftime('%H:%M')}"
    return f"{first.strftime('%d.%m %H:%M')} – {last.strftime('%d.%m %H:%M')}"


def platform() -> str:
    if os.getenv("RENDER"):
        return "render"
    return "docker" if pathlib.Path("/.dockerenv").exists() else "server"


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return ""


def _mb(raw: str) -> int | None:
    try:
        return int(raw) // 1048576
    except (TypeError, ValueError):
        return None


def memory() -> dict:
    """MB: the process now and at its peak, its biggest child (ffmpeg) at its peak, the limit (the container's, else
    the machine's memory), the container now, and the processes the kernel killed for memory. None when unknown."""
    import resource
    out = {"rss": None, "peak": None, "child": None, "limit": None, "used": None, "oom_kills": 0}
    found = re.search(r"VmRSS:\s+(\d+) kB", _read(PROC / "self/status"))
    if found:
        out["rss"] = int(found.group(1)) // 1024
    try:  # kB on Linux
        out["peak"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        out["child"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
    except (OSError, ValueError, AttributeError):
        pass
    limit = _read(CGROUP / "memory.max") or _read(CGROUP / "memory/memory.limit_in_bytes")
    if limit.isdigit() and int(limit) < 1 << 50:  # cgroup v1 says «no limit» with a huge number, v2 with «max»
        out["limit"] = _mb(limit)
    else:
        total = re.search(r"MemTotal:\s+(\d+) kB", _read(PROC / "meminfo"))
        out["limit"] = int(total.group(1)) // 1024 if total else None
    out["used"] = _mb(_read(CGROUP / "memory.current") or _read(CGROUP / "memory/memory.usage_in_bytes"))
    killed = re.search(r"oom_kill (\d+)", _read(CGROUP / "memory.events"))
    if killed:
        out["oom_kills"] = int(killed.group(1))
    return out


def _busy(s) -> str:
    ids = [mid for (mid,) in s.query(Meeting.id).filter(Meeting.status.in_(("transcribing", "analyzing")))
           .order_by(Meeting.id).limit(3)]
    return ("обработка совещания " + ", ".join(f"№{i}" for i in ids)) if ids else ""


# ----------------------------------------------------------------------------- the runs
def on_start(moment: dt.datetime | None = None, instance: str | None = None) -> None:
    moment = moment or now()
    mem = memory()
    with SessionLocal() as s:
        s.add(ServerRun(instance=instance or INSTANCE, platform=platform(),
                        commit=(os.getenv("RENDER_GIT_COMMIT") or "")[:12], started_at=moment, beat_at=moment,
                        limit_mb=mem["limit"], peak_mb=mem["peak"], child_mb=mem["child"], oom_kills=mem["oom_kills"]))
        s.query(ServerRun).filter(ServerRun.started_at < moment - dt.timedelta(days=KEEP_DAYS)).delete()
        s.commit()


def beat(moment: dt.datetime | None = None, instance: str | None = None, mem: dict | None = None) -> None:
    """The heartbeat: this run is alive; its memory peak and what it is busy with."""
    moment = moment or now()
    mem = mem or memory()
    with SessionLocal() as s:
        run = s.query(ServerRun).filter_by(instance=instance or INSTANCE).first()
        if run is None:
            return
        run.beat_at = moment
        run.peak_mb = max(filter(None, (run.peak_mb, mem.get("peak"))), default=None)
        run.child_mb = max(filter(None, (run.child_mb, mem.get("child"))), default=None)
        run.limit_mb = mem.get("limit") or run.limit_mb
        run.oom_kills = max(run.oom_kills or 0, mem.get("oom_kills") or 0)
        run.busy = _busy(s)[:200]
        s.commit()


def on_stop(moment: dt.datetime | None = None, instance: str | None = None) -> None:
    moment = moment or now()
    with SessionLocal() as s:
        run = s.query(ServerRun).filter_by(instance=instance or INSTANCE).first()
        if run is not None:
            run.beat_at = run.stopped_at = moment
            s.commit()


def _end(run) -> dt.datetime:
    return _aware(run.stopped_at or run.beat_at)


def breaks(runs: list, moment: dt.datetime | None = None) -> list[dict]:
    """The downtime between runs, oldest first: {"start", "end", "seconds", "kind", "busy", "peak", "child", "limit"}.
    kind: crash (the run before ended without a clean stop), update (another version started after a clean stop),
    sleep (a clean stop on Render: free instances sleep), stop (a clean stop elsewhere: a restart, a reboot), move
    (the next run is on another platform: the database came along to a new place).
    Overlapping runs (a deploy) leave no break."""
    out = []
    alive = None  # the run alive the longest so far
    for run in sorted(runs, key=lambda r: _aware(r.started_at)):
        start = _aware(run.started_at)
        if alive is not None:
            gap = (start - _end(alive)).total_seconds()
            crashed = alive.stopped_at is None
            if gap > 0 and (crashed or gap >= BREAK_MIN):
                kind = "crash" if crashed else (
                    "update" if alive.commit and run.commit and alive.commit != run.commit else "stop")
                if not crashed and alive.platform and run.platform and alive.platform != run.platform:
                    kind = "move"
                elif kind == "stop" and alive.platform == "render":
                    kind = "sleep"
                out.append({"start": _end(alive), "end": start, "seconds": gap, "kind": kind, "busy": alive.busy or "",
                            "peak": alive.peak_mb, "child": alive.child_mb, "limit": alive.limit_mb})
        if alive is None or _end(run) >= _end(alive):
            alive = run
    return out


def _awake(runs: list, first: dt.datetime, last: dt.datetime, live: str = "") -> float:
    """Seconds between first and last when some run was up (the live run is up till last)."""
    spans = []
    for run in runs:
        start = max(_aware(run.started_at), first)
        end = min(last if run.instance == live else _end(run), last)
        if end > start:
            spans.append((start, end))
    total, edge = 0.0, None
    for start, end in sorted(spans):
        if edge is not None:
            start = max(start, edge)
        if end > start:
            total += (end - start).total_seconds()
            edge = end
    return total


def neon_estimate(runs: list, moment: dt.datetime, live: str = "") -> dict | None:
    """Neon's compute runs while the service runs: CU-hours used this month and by its end (at the smallest compute),
    and the day the free hours run out. None while the measure is too short."""
    local = _local(moment)
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_hours = calendar.monthrange(local.year, local.month)[1] * 24
    first = min((_aware(r.started_at) for r in runs), default=None)
    if first is None:
        return None
    tracked_from = max(month_start, first)
    tracked = (moment - tracked_from).total_seconds()
    if tracked < 6 * 3600:
        return None
    ratio = min(1.0, _awake(runs, tracked_from, moment, live) / tracked)
    elapsed_hours = (moment - month_start).total_seconds() / 3600
    used = NEON_MIN_CU * ratio * elapsed_hours
    projected = NEON_MIN_CU * ratio * month_hours
    out = {"used": round(used), "projected": round(projected), "ratio": ratio, "limit": NEON_FREE_CU_HOURS,
           "runs_out": None, "exhausted": used >= NEON_FREE_CU_HOURS}
    if projected > NEON_FREE_CU_HOURS and not out["exhausted"] and ratio > 0:
        out["runs_out"] = local + dt.timedelta(hours=(NEON_FREE_CU_HOURS - used) / (NEON_MIN_CU * ratio))
    return out


def _neon() -> bool:
    return "neon.tech" in (engine.url.host or "")


def database_size() -> int | None:
    try:
        if engine.dialect.name == "postgresql":
            with SessionLocal() as s:
                return int(s.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0)
        if engine.dialect.name == "sqlite" and engine.url.database and os.path.exists(engine.url.database):
            return os.path.getsize(engine.url.database)
    except Exception:  # noqa: BLE001 - a size is a nicety, the page must open anyway
        log.exception("server: database size")
    return None


# ----------------------------------------------------------------------------- the nightly dumps
def dumps() -> list[dict]:
    folder = config.BACKUP_DIR
    if not folder or not folder.is_dir():
        return []
    out = []
    for path in folder.glob("sysai_*.dump"):
        try:
            info = path.stat()
        except OSError:
            continue
        out.append({"name": path.name, "path": path, "size": info.st_size,
                    "at": dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)})
    return sorted(out, key=lambda d: d["at"])


def copy_backups(moment: dt.datetime | None = None, force: bool = False) -> str:
    """Copies the newest dump to the storage (backups/<name>) and deletes there the copies older than BACKUP_KEEP_DAYS.
    Returns what was done, for the page."""
    moment = moment or now()
    files = dumps()
    if not files or not (force or settings_store.get("backup_copy")):
        return ""
    state = dict(settings_store.get("backup_state") or {})
    copied = list(state.get("copied") or [])
    state["checked_at"] = moment.isoformat(timespec="seconds")
    if not storage.configured():
        state["error"] = "хранилище записей не подключено (S3_ENDPOINT, S3_BUCKET и ключи)"
        settings_store.set_many({"backup_state": state})
        return state["error"]
    done = ""
    newest = files[-1]
    try:
        if newest["name"] not in {c.get("name") for c in copied}:
            size = storage.put_file("backups/" + newest["name"], newest["path"])
            copied.append({"name": newest["name"], "size": size, "at": moment.isoformat(timespec="seconds")})
            done = f"скопирован {newest['name']}"
        cutoff = moment - dt.timedelta(days=config.BACKUP_KEEP_DAYS)
        keep = []
        for item in copied:
            try:
                old = dt.datetime.fromisoformat(item.get("at") or "") < cutoff
            except ValueError:
                old = False
            if old and item.get("name") != newest["name"]:
                storage.delete("backups/" + item["name"])
            else:
                keep.append(item)
        copied = keep[-60:]
        state["error"] = ""
    except storage.StorageError as exc:
        state["error"] = str(exc)[:300]
        done = state["error"]
    state["copied"] = copied
    settings_store.set_many({"backup_state": state})
    return done or "копия уже есть"


def backups(moment: dt.datetime | None = None) -> dict:
    moment = moment or now()
    files = dumps()
    state = settings_store.get("backup_state") or {}
    names = {c.get("name") for c in state.get("copied") or []}
    out = {"here": bool(config.BACKUP_DIR and config.BACKUP_DIR.is_dir()), "count": len(files), "latest": None,
           "copy_on": bool(settings_store.get("backup_copy")), "storage": storage.configured(),
           "error": state.get("error") or "", "copied": len(names), "keep_days": config.BACKUP_KEEP_DAYS}
    if files:
        last = files[-1]
        age = (moment - last["at"]).total_seconds() / 3600
        size = last["size"]
        out["latest"] = {"name": last["name"], "size": f"{size / 1048576:.1f} МБ" if size >= 1048576 else f"{max(1, size // 1024)} КБ",
                         "at": _local(last["at"]).strftime("%d.%m %H:%M"), "age_h": round(age), "stale": age > 30,
                         "copied": last["name"] in names}
    return out


# ----------------------------------------------------------------------------- the page
def summary(moment: dt.datetime | None = None, live: str | None = None) -> dict:
    moment = moment or now()
    live = INSTANCE if live is None else live
    since = moment - dt.timedelta(days=WINDOW_DAYS)
    with SessionLocal() as s:
        runs = s.query(ServerRun).filter(ServerRun.started_at >= moment - dt.timedelta(days=WINDOW_DAYS + 31)).all()
        current = next((r for r in runs if r.instance == live), None)
    window = [b for b in breaks(runs, moment) if b["end"] > since]
    for b in window:
        b["seconds"] = (b["end"] - max(b["start"], since)).total_seconds()
        b["label"] = KINDS[b["kind"]]
        b["when"] = _span(b["start"], b["end"])
        b["length"] = duration(b["seconds"])
    first = min((_aware(r.started_at) for r in runs), default=None)
    tracked = (moment - max(since, first)).total_seconds() if first else 0
    down = sum(b["seconds"] for b in window)
    crashes = [b for b in window if b["kind"] == "crash"]
    longest = max(window, key=lambda b: b["seconds"], default=None)
    mem = memory()
    if mem.get("rss") and mem.get("peak"):
        mem["peak"] = max(mem["peak"], mem["rss"])
    recent = [r for r in runs if _end(r) >= since]
    peaks = [r.peak_mb for r in recent if r.peak_mb]
    # the tightest run: the highest peak against the memory that run had (a move changes the limit)
    rated = [(r.peak_mb / r.limit_mb, r.peak_mb, r.limit_mb) for r in recent if r.peak_mb and r.limit_mb]
    if current is not None and mem.get("peak") and mem.get("limit"):
        rated.append((mem["peak"] / mem["limit"], mem["peak"], mem["limit"]))
    tightest = max(rated, default=None)
    disk = None
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        disk = {"free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}
    except OSError:
        pass
    size = database_size()
    out = {
        "platform": {"render": "Render", "docker": "свой сервер (Docker)", "server": "сервер без Docker"}[platform()],
        "started": _local(_aware(current.started_at)).strftime("%d.%m %H:%M") if current else "",
        "uptime": duration((moment - _aware(current.started_at)).total_seconds()) if current else "",
        "commit": (current.commit if current else "") or "",
        "breaks": list(reversed(window))[:30], "count": len(window), "down": duration(down), "down_s": down,
        "crashes": len(crashes), "longest": longest, "tracked_h": round(tracked / 3600, 1),
        "availability": round(100 * (1 - down / tracked), 1) if tracked > 0 else None,
        "memory": mem, "peak_week": max(peaks, default=None), "disk": disk,
        "tightest": {"peak": tightest[1], "limit": tightest[2]} if tightest else None,
        "db": {"kind": engine.dialect.name, "neon": _neon(), "size_mb": round(size / 1048576) if size else None},
        "neon": neon_estimate(runs, moment, live) if _neon() else None,
        "storage": storage.configured(), "backups": backups(moment),
    }
    out["verdict"] = verdict(out)
    return out


def verdict(w: dict) -> list[tuple[str, str]]:
    """The few lines that say whether the place is good enough: (ok | warn | bad, text)."""
    lines: list[tuple[str, str]] = []
    if w["tracked_h"] < 24:
        lines.append(("warn", "Замер только начался: честная картина будет через сутки-двое работы."))
    if w["count"]:
        longest = w["longest"]
        lines.append(("bad" if w["down_s"] > 3600 else "warn",
                      f"За {WINDOW_DAYS} дней сервис не работал {w['count']} {_plural(w['count'], 'раз', 'раза', 'раз')}, "
                      f"всего {w['down']} (доступность {str(w['availability']).replace('.', ',')}%); самый долгий перерыв — {longest['length']}, "
                      f"{longest['when']}. Пока сервис стоит, не уходят напоминания и сводки, не идут почта и синхронизации."))
    elif w["tracked_h"] >= 24:
        lines.append(("ok", f"За {WINDOW_DAYS} дней перерывов не было."))
    mem = w["memory"]
    if w["crashes"]:
        last = next((b for b in w["breaks"] if b["kind"] == "crash"), {})
        hint = f", пик памяти {last['peak']} МБ из {last['limit']}" if last.get("peak") and last.get("limit") else ""
        doing = f" во время: {last['busy']}" if last.get("busy") else ""
        when = f" Последняя — {last['when']}{doing}{hint}." if last else ""
        lines.append(("bad", f"Аварийных остановок: {w['crashes']}.{when} Процесс обрывается без остановки чаще всего "
                             "тогда, когда кончается память."))
    tight = w.get("tightest")
    if tight:
        peak, limit = tight["peak"], tight["limit"]
        if peak >= 0.85 * limit or mem.get("oom_kills"):
            killed = f"; ядро уже убивало процессы из-за памяти: {mem['oom_kills']}" if mem.get("oom_kills") else ""
            lines.append(("bad", f"Память на пределе: пик {peak} МБ из {limit} МБ{killed}."))
        else:
            lines.append(("ok", f"Памяти хватает: пик {peak} МБ из {limit} МБ."))
    neon = w.get("neon")
    if neon:
        if neon["exhausted"]:
            lines.append(("bad", f"База Neon работает всё время, пока работает сервис: по оценке, {NEON_FREE_CU_HOURS} "
                                 "бесплатных CU-часов этого месяца уже израсходованы — Neon может остановить базу до "
                                 "следующего месяца."))
        elif neon["runs_out"]:
            lines.append(("bad", f"База Neon работает всё время, пока работает сервис: в этом месяце ≈ {neon['used']} "
                                 f"CU-часов, к концу ≈ {neon['projected']} при {NEON_FREE_CU_HOURS} бесплатных. Часы кончатся "
                                 f"около {neon['runs_out'].strftime('%d.%m')}, и Neon остановит базу до следующего месяца."))
        else:
            lines.append(("ok", f"Часов Neon хватает: в этом месяце ≈ {neon['used']} CU-часов, к концу ≈ "
                                f"{neon['projected']} из {NEON_FREE_CU_HOURS} бесплатных."))
    db = w["db"]
    if db["neon"] and db["size_mb"] and db["size_mb"] >= 0.8 * NEON_FREE_STORAGE_MB:
        lines.append(("bad", f"База занимает {db['size_mb']} МБ из {NEON_FREE_STORAGE_MB} МБ бесплатного Neon."))
    b = w["backups"]
    if b["here"] and b["latest"] and b["latest"]["stale"]:
        lines.append(("bad", f"Ночной бэкап не обновлялся {b['latest']['age_h']} ч: смотрите docker compose logs backup."))
    elif b["here"] and not b["latest"]:
        lines.append(("bad", "Папка бэкапов пуста: ночной бэкап не работает (docker compose logs backup)."))
    if b["here"] and b["copy_on"] and b["error"]:
        lines.append(("warn", f"Копия бэкапа в хранилище не сделана: {b['error']}"))
    return lines


# ----------------------------------------------------------------------------- the watcher
def _watch() -> None:
    while not _stop.wait(BEAT_EVERY):
        try:
            beat()
        except Exception:  # noqa: BLE001 - the database may be waking up: the next beat will do
            log.warning("server: heartbeat failed", exc_info=True)
        if time.time() - _state["backup"] >= BACKUP_EVERY:
            _state["backup"] = time.time()
            try:
                done = copy_backups()
                if done:
                    log.info("server: backup copy: %s", done)
            except Exception:  # noqa: BLE001
                log.exception("server: backup copy failed")


def start() -> None:
    """Called at startup (not in tests): the run is written and a heartbeat thread keeps it alive."""
    global _thread
    try:
        on_start()
    except Exception:  # noqa: BLE001 - the measure must never keep the service from starting
        log.exception("server: could not record the start")
        return
    _stop.clear()
    _thread = threading.Thread(target=_watch, daemon=True, name="sysai-watch")
    _thread.start()


def stop() -> None:
    _stop.set()
    try:
        on_stop()
    except Exception:  # noqa: BLE001
        log.warning("server: could not record the stop", exc_info=True)
