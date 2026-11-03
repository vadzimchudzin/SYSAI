"""Meeting recordings kept in the private storage (storage.py) for the player on the meeting page.

When the transcription starts the original is hard-linked into DATA_DIR/archive (so the pipeline still deletes it as
before), compressed to a speech MP3 (mono, 16 kHz, 32 kbit/s: ~14 MB per hour, what the speech model hears) and
uploaded next to the transcription; then the local copies are removed. A failed upload is retried by the background
loop while the compressed file is on disk. A restart of a free instance wipes that disk: a TicNote recording is then
taken from the TicNote cloud again (by itself, a few times, or with «Скачать из TicNote заново»), any other one is
reported as not kept, never as silently missing. Copies are deleted after RECORDING_RETENTION_DAYS (a lifecycle rule
of the bucket does the same on the storage side), together with their meeting or on request, always through the
StorageCleanup outbox.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from . import audio, config, storage
from .db import Meeting, SessionLocal, StorageCleanup, now

log = logging.getLogger("sysai.archive")
PREFIX = "recordings/"
MIME = "audio/mpeg"
RETRIES = (60, 300, 1800, 7200)              # seconds before the 2nd…5th upload attempt
CLEANUP_BACKOFF = (60, 300, 1800, 7200, 21600)
CLEANUP_GIVE_UP = 40                         # the bucket's lifecycle rule removes what could not be deleted
PLAY_SECONDS = 3 * 3600                      # a player link: long enough for one listening session
STALE = dt.timedelta(minutes=30)             # a copy without a local file is lost after this (a restart)
LOST = "сервер перезапустился раньше, чем она загрузилась в хранилище"
RESTORE_PAUSE = (0, 900, 7200)               # seconds after a failure before the 1st…3rd download from TicNote by itself
MAX_RESTORES = len(RESTORE_PAUSE)
BUSY = ("queued", "transcribing", "analyzing")
_OLD_PREFIX = re.compile(r"^\s*запись не сохранилась:\s*", re.IGNORECASE)  # stored by earlier versions: said twice
_ACTIVE: set[int] = set()                    # meetings whose copy is being compressed or uploaded by this process
_LOCK = threading.Lock()
_last_purge = [0.0]


def enabled() -> bool:
    return storage.configured()


def archive_dir() -> pathlib.Path:
    return config.DATA_DIR / "archive"


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _local_date(moment: dt.datetime) -> str:
    try:
        zone = ZoneInfo(config.TIMEZONE)
    except Exception:  # noqa: BLE001 - an unknown zone name must not break the page
        zone = dt.timezone.utc
    return moment.astimezone(zone).strftime("%d.%m.%Y")


def _claim(meeting_id: int) -> bool:
    with _LOCK:
        if meeting_id in _ACTIVE:
            return False
        _ACTIVE.add(meeting_id)
        return True


def _release(meeting_id: int) -> None:
    with _LOCK:
        _ACTIVE.discard(meeting_id)


def _own_file(value) -> pathlib.Path | None:
    """A local copy recorded in the database, only if it really is inside the archive folder."""
    if not value:
        return None
    path = pathlib.Path(str(value))
    try:
        return path if path.resolve().is_relative_to(archive_dir().resolve()) else None
    except OSError:
        return None


def _save(meeting_id: int, status: str | None = None, **fields) -> bool:
    """Updates the copy's state; False when the meeting is gone or its copy was removed meanwhile."""
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None or m.recording_status != "pending":
            return False
        m.recording = {**(m.recording or {}), **fields}
        if status:
            m.recording_status = status
        s.commit()
    return True


def _fail(meeting_id: int, reason: str) -> None:
    log.warning("meeting %s: recording not kept: %s", meeting_id, reason)
    _save(meeting_id, "failed", error=reason[:300], local="", failed_at=_iso(now()))


# ---- keeping a new recording ---------------------------------------------------------------------------------------
def start(meeting_id: int, audio_path: str, source: str = "") -> threading.Thread | None:
    """Called when the transcription starts (and again once the transcript is saved: then it is usually kept already).
    The original is linked at once; compression and upload run in the background (synchronously under tests).
    Returns the job, or None when there is nothing to keep."""
    if source == "demo" or not audio_path or not enabled():
        return None
    original = pathlib.Path(audio_path)
    if not original.is_file() or not _claim(meeting_id):
        return None
    try:
        with SessionLocal() as s:
            m = s.get(Meeting, meeting_id)
            status = m.recording_status or "" if m is not None else None
            if status == "pending":  # a resumed processing: keep it again unless its compressed copy is still here
                local = _own_file((m.recording or {}).get("local"))
                status = "" if local is None or not local.is_file() else status
            if status not in ("", "failed"):
                _release(meeting_id)  # already kept (a retry of the processing) or removed on purpose
                return None
            m.recording_status = "pending"
            m.recording = {"attempts": 0, "since": _iso(now())}
            s.commit()
        folder = archive_dir()
        folder.mkdir(parents=True, exist_ok=True)
        link = folder / f"m{meeting_id}-{secrets.token_hex(4)}.src{original.suffix.lower()[:10]}"
        try:
            os.link(original, link)  # instant and free: the pipeline may delete the original right away
        except OSError:
            shutil.copyfile(original, link)  # another filesystem
    except Exception as exc:  # noqa: BLE001 - keeping a copy must never break the processing
        _fail(meeting_id, f"не удалось взять запись для хранения ({type(exc).__name__})")
        _release(meeting_id)
        return None
    if config.TESTING:
        _compress_and_upload(meeting_id, link)
        return None
    job = threading.Thread(target=_compress_and_upload, args=(meeting_id, link), daemon=True, name=f"archive-{meeting_id}")
    job.start()
    return job


def compress(source: pathlib.Path, target: pathlib.Path, timeout: float) -> float:
    """Speech MP3 of the first audio stream, without metadata; returns its duration in seconds."""
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0",
               "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
               "-map_metadata", "-1", str(target)]
    if shutil.which("nice"):
        command = ["nice", "-n", "10"] + command  # the web pages stay responsive on a tenth of a CPU
    subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    return audio.duration(str(target))


def _length(path: pathlib.Path) -> float:
    """Seconds of audio by the file's header (cheap: nothing is decoded), 0 when it does not say."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return max(float(out), 0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _compress_and_upload(meeting_id: int, link: pathlib.Path) -> None:
    target = link.with_name(link.name.split(".src")[0] + ".mp3")
    try:
        with SessionLocal() as s:
            m = s.get(Meeting, meeting_id)
            hint = float((m.duration_sec if m else 0) or 0)
        hint = hint or _length(link)  # before the transcription the length is not known yet: a long one needs time
        try:
            seconds = compress(link, target, timeout=600 + hint)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            target.unlink(missing_ok=True)
            detail = (getattr(exc, "stderr", b"") or b"")[-300:]
            log.warning("meeting %s: compression failed: %s %s", meeting_id, type(exc).__name__,
                        detail.decode("utf-8", "replace") if isinstance(detail, bytes) else detail)
            _fail(meeting_id, "не удалось сжать запись для хранения")
            return
        finally:
            link.unlink(missing_ok=True)
        key = f"{PREFIX}{now():%Y/%m}/m{meeting_id}-{secrets.token_hex(6)}.mp3"
        if not _save(meeting_id, local=str(target), key=key, duration=round(seconds, 2)):
            target.unlink(missing_ok=True)  # the meeting was deleted while compressing
            return
        _upload(meeting_id)
    except Exception:  # noqa: BLE001
        log.exception("meeting %s: keeping the recording failed", meeting_id)
        _fail(meeting_id, "внутренняя ошибка при сохранении записи")
    finally:
        _release(meeting_id)


def _upload(meeting_id: int) -> bool:
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None or m.recording_status != "pending":
            return False
        rec = dict(m.recording or {})
    local, key = _own_file(rec.get("local")), str(rec.get("key") or "")
    if local is None or not local.is_file() or not key.startswith(PREFIX):
        _fail(meeting_id, LOST)
        return False
    try:
        size = storage.put_file(key, local, MIME)
    except OSError:
        _fail(meeting_id, "локальная копия записи пропала до загрузки в хранилище")  # removed while uploading
        return False
    except storage.StorageError as exc:
        attempts = int(rec.get("attempts") or 0) + 1
        log.warning("meeting %s: upload attempt %s failed: %s", meeting_id, attempts, exc)
        if attempts > len(RETRIES):
            local.unlink(missing_ok=True)
            with SessionLocal() as s:  # a PUT that the storage finished while the answer was lost must not stay forever
                s.add(StorageCleanup(key=key, reason="failed"))
                s.commit()
            _fail(meeting_id, f"не удалось загрузить в хранилище: {exc}")
        else:
            _save(meeting_id, attempts=attempts, error=str(exc)[:300],
                  next_at=_iso(now() + dt.timedelta(seconds=RETRIES[attempts - 1])))
        return False
    stored = now()
    kept = {"key": key, "size": size, "duration": rec.get("duration") or 0, "mime": MIME, "stored_at": _iso(stored),
            "expires_at": _iso(stored + dt.timedelta(days=config.RECORDING_RETENTION_DAYS))}
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None or m.recording_status != "pending":
            s.add(StorageCleanup(key=key, reason="orphan"))  # deleted during the upload
        else:
            m.recording_status, m.recording = "stored", kept
        s.commit()
    local.unlink(missing_ok=True)
    log.info("meeting %s: recording kept (%s bytes)", meeting_id, size)
    return True


# ---- taking a TicNote recording again ------------------------------------------------------------------------------
def restorable(meeting: Meeting) -> bool:
    """A TicNote meeting whose recording is not kept can take it from the TicNote cloud again: TicNote keeps the WAV."""
    if meeting.source != "ticnote" or not enabled() or (meeting.recording_status or "") not in ("", "failed"):
        return False
    if meeting.status in BUSY:
        return False  # the processing keeps the recording itself
    created = meeting.created_at
    if created is not None:
        created = created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc)
        if now() - created > dt.timedelta(days=config.RECORDING_RETENTION_DAYS):
            return False  # a copy must not outlive the retention period
    return bool(config.ticnote_appkey())


def restore(meeting_id: int) -> bool:
    """«Скачать из TicNote заново»: the WAV is downloaded from TicNote again, compressed and kept like a new recording
    (in the background; synchronously under tests). False when this meeting cannot take it now."""
    from . import ticnote_ingest  # it imports the pipeline, which imports this module
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None or not restorable(m):
            return False
    record_id = ticnote_ingest.record_of(meeting_id)
    if not record_id or not _claim(meeting_id):
        return False
    try:
        with SessionLocal() as s:
            m = s.get(Meeting, meeting_id)
            if m is None or (m.recording_status or "") not in ("", "failed"):
                _release(meeting_id)
                return False
            m.recording_status = "pending"
            m.recording = {"attempts": 0, "since": _iso(now()), "from": "ticnote",
                           "restores": int((m.recording or {}).get("restores") or 0) + 1}
            s.commit()
    except Exception:
        _release(meeting_id)
        raise
    if config.TESTING:
        _take_from_ticnote(meeting_id, record_id)
        return True
    threading.Thread(target=_take_from_ticnote, args=(meeting_id, record_id), daemon=True,
                     name=f"archive-{meeting_id}").start()
    return True


def _take_from_ticnote(meeting_id: int, record_id: str) -> None:
    from . import ticnote_ingest
    folder = archive_dir()
    link = folder / f"m{meeting_id}-{secrets.token_hex(4)}.src.wav"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        ticnote_ingest.download_again(record_id, link)
    except Exception as exc:  # noqa: BLE001 - TicNote is down, the recording was deleted there or the key is gone
        link.unlink(missing_ok=True)
        log.warning("meeting %s: TicNote recording %s not downloaded again: %s", meeting_id, record_id, exc)
        _fail(meeting_id, f"не удалось скачать её из TicNote заново ({str(exc).strip().rstrip('.')[:200] or 'ошибка'})")
        _release(meeting_id)
        return
    _compress_and_upload(meeting_id, link)  # releases the meeting


def _recover_lost() -> None:
    """A TicNote meeting whose copy did not make it (a restart, the storage was down) takes its recording from TicNote
    again by itself, MAX_RESTORES times with growing pauses; one recording at a time on a tenth of a CPU."""
    with _LOCK:
        if _ACTIVE:
            return
    if not config.ticnote_appkey():
        return
    moment = now()
    with SessionLocal() as s:
        rows = (s.query(Meeting.id, Meeting.recording)
                .filter(Meeting.recording_status == "failed", Meeting.source == "ticnote",
                        Meeting.created_at >= moment - dt.timedelta(days=config.RECORDING_RETENTION_DAYS))
                .order_by(Meeting.id.desc()).limit(100).all())
    for meeting_id, rec in rows:
        rec = rec or {}
        done = int(rec.get("restores") or 0)
        if done >= MAX_RESTORES:
            continue
        failed = _parse(rec.get("failed_at")) if rec.get("failed_at") else None
        if failed is not None and moment - failed < dt.timedelta(seconds=RESTORE_PAUSE[done]):
            continue
        if restore(meeting_id):
            return


# ---- removing ------------------------------------------------------------------------------------------------------
def forget(session, meeting: Meeting, reason: str) -> None:
    """Queues the kept copy for deletion in the caller's transaction (a deleted meeting, a removed recording)."""
    rec = meeting.recording or {}
    key = str(rec.get("key") or "")
    if key.startswith(PREFIX):
        session.add(StorageCleanup(key=key, reason=reason))
    local = _own_file(rec.get("local"))
    if local is not None:
        local.unlink(missing_ok=True)


def remove(meeting_id: int) -> bool:
    """«Удалить запись»: the meeting stays, its recording goes. False when there was nothing to remove."""
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None or m.recording_status not in ("stored", "pending", "failed"):
            return False
        forget(s, m, "removed")
        m.recording_status, m.recording = "deleted", {"deleted_at": _iso(now())}
        s.commit()
    return True


# ---- background loop -----------------------------------------------------------------------------------------------
def run_due() -> None:
    """Retries uploads, takes lost TicNote recordings again, performs queued deletions and, once an hour, removes
    copies past their retention period."""
    if not enabled():
        return
    _retry_uploads()
    _recover_lost()
    _clean_up()
    if time.monotonic() - _last_purge[0] >= 3600:
        purge_expired()
        _sweep()
        _last_purge[0] = time.monotonic()


def _retry_uploads() -> None:
    with SessionLocal() as s:
        rows = s.query(Meeting.id, Meeting.recording).filter(Meeting.recording_status == "pending").all()
    for meeting_id, rec in rows:
        rec = rec or {}
        due = _parse(rec.get("next_at")) if rec.get("next_at") else None
        if due and due > now():
            continue
        if not _claim(meeting_id):
            continue  # this process is compressing or uploading it right now
        try:
            local = _own_file(rec.get("local"))
            if local is not None and local.is_file():
                _upload(meeting_id)
                continue
            started = _parse(rec.get("since")) if rec.get("since") else None
            if started and now() - started < STALE:
                continue  # during a deploy the previous instance may still be compressing it on its own disk
            _fail(meeting_id, LOST)  # a TicNote recording is taken again right after (_recover_lost)
        finally:
            _release(meeting_id)


def _clean_up(limit: int = 50) -> None:
    with SessionLocal() as s:
        ids = [r.id for r in s.query(StorageCleanup.id).filter(StorageCleanup.next_at <= now())
               .order_by(StorageCleanup.id).limit(limit).all()]
    for row_id in ids:
        with SessionLocal() as s:
            row = s.get(StorageCleanup, row_id)
            if row is None:
                continue
            if not row.key.startswith(PREFIX) or ".." in row.key:
                log.error("storage cleanup %s: refusing an unexpected key", row_id)
                s.delete(row)
                s.commit()
                continue
            try:
                storage.delete(row.key)
            except storage.StorageError as exc:
                row.attempts = (row.attempts or 0) + 1
                row.error = str(exc)[:300]
                row.next_at = now() + dt.timedelta(seconds=CLEANUP_BACKOFF[min(row.attempts, len(CLEANUP_BACKOFF)) - 1])
                if row.attempts >= CLEANUP_GIVE_UP:
                    log.error("storage cleanup %s: giving up after %s attempts: %s", row_id, row.attempts, exc)
                    s.delete(row)
            else:
                s.delete(row)
            s.commit()


def _sweep() -> int:
    """Local copies nobody waits for any more: a restart in the middle of compression on a disk that survives restarts."""
    folder = archive_dir()
    if not folder.is_dir():
        return 0
    with SessionLocal() as s:
        wanted = {str((rec or {}).get("local")) for (rec,) in
                  s.query(Meeting.recording).filter(Meeting.recording_status == "pending").all()}
    with _LOCK:
        active = set(_ACTIVE)
    limit = time.time() - STALE.total_seconds()
    removed = 0
    for path in folder.iterdir():
        owner = re.match(r"m(\d+)-", path.name)
        if str(path) in wanted or (owner and int(owner.group(1)) in active):
            continue
        try:
            info = path.stat()
            if not path.is_file() or max(info.st_mtime, info.st_ctime) > limit:
                continue  # a hard link gets a new ctime: a fresh copy of an old upload is not swept
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("recordings archive: removed %s forgotten local copies", removed)
    return removed


def purge_expired() -> int:
    """Copies past RECORDING_RETENTION_DAYS: queued for deletion, the meeting says so."""
    moment = now()
    with SessionLocal() as s:
        rows = s.query(Meeting.id, Meeting.recording).filter(Meeting.recording_status == "stored").all()
        due = [mid for mid, rec in rows if (_parse((rec or {}).get("expires_at")) or moment + dt.timedelta(days=1)) <= moment]
        for mid in due:
            m = s.get(Meeting, mid)
            forget(s, m, "expired")
            m.recording_status, m.recording = "expired", {"expired_at": _iso(moment)}
        s.commit()
    return len(due)


# ---- the meeting page and settings ---------------------------------------------------------------------------------
def view(meeting: Meeting) -> dict:
    """What the page says about the recording; "playable" only for a copy that is in the storage and not expired."""
    status, rec = meeting.recording_status or "", meeting.recording or {}
    days = config.RECORDING_RETENTION_DAYS
    if status == "stored":
        until = _parse(rec.get("expires_at"))
        if until is None or until > now():
            size = int(rec.get("size") or 0)
            return {"state": "stored", "playable": True, "src": f"/meetings/{meeting.id}/recording",
                    "download": f"/meetings/{meeting.id}/recording?download=1",
                    "size": f"{size / 1_000_000:.1f}".replace(".", ",") + " МБ",
                    "text": f"Запись хранится до {_local_date(until)}" if until else "Запись хранится"}
        status = "expired"
    if status == "expired":
        return {"state": "expired", "text": f"Запись удалена: прошло {days} дней хранения."}
    if status == "deleted":
        return {"state": "deleted", "text": "Запись этого совещания удалена."}
    if status == "pending":
        if rec.get("attempts"):
            text = "Запись пока не загрузилась в хранилище, попробуем ещё раз автоматически."
        elif rec.get("from") == "ticnote":
            text = "Скачиваем запись из TicNote заново и сохраняем в хранилище, это займёт несколько минут."
        else:
            text = "Запись сохраняется в хранилище, обычно это занимает пару минут."
        return {"state": "pending", "text": text}
    again = {"restorable": True} if restorable(meeting) else {}
    if status == "failed":
        reason = _OLD_PREFIX.sub("", str(rec.get("error") or "")).strip().rstrip(".") or "ошибка"
        text = f"Запись не сохранилась: {reason}."
        if again and int(rec.get("restores") or 0) < MAX_RESTORES:
            text += " Скачаем её из TicNote заново автоматически."
        return {"state": "failed", "text": text, **again}
    if meeting.source == "demo":
        return {"state": "none", "text": "У демо нет записи, слушать нечего."}
    if meeting.source == "notion":
        return {"state": "none", "text": "Совещание импортировано текстом, записи нет."}
    if not enabled():
        return {"state": "none", "text": "Чтобы слушать записи, подключите хранилище: Настройки → Хранилище записей."}
    if meeting.status in BUSY:
        return {"state": "none", "text": "Запись сохранится в хранилище во время обработки."}
    return {"state": "none", "text": "Запись этого совещания не сохранялась: оно обработано до подключения хранилища.", **again}


def link(meeting: Meeting, download: bool = False) -> str | None:
    """Presigned link to the kept copy (None when there is none). Raises storage.StorageError on broken settings."""
    key = str((meeting.recording or {}).get("key") or "")
    if not view(meeting).get("playable") or not key.startswith(PREFIX):
        return None
    filename = ""
    if download:
        when = meeting.meeting_date.strftime("%d.%m.%Y") if meeting.meeting_date else ""
        filename = " ".join(part for part in ((meeting.title or "Совещание").strip()[:80], when) if part) + ".mp3"
    return storage.download_url(key, PLAY_SECONDS, filename=filename)


def usage() -> dict:
    with SessionLocal() as s:
        rows = s.query(Meeting.recording_status, Meeting.recording).filter(
            Meeting.recording_status.in_(("stored", "pending", "failed"))).all()
    stored = [int((rec or {}).get("size") or 0) for status, rec in rows if status == "stored"]
    return {"count": len(stored), "megabytes": round(sum(stored) / 1_000_000, 1),
            "pending": sum(1 for status, _ in rows if status == "pending"),
            "failed": sum(1 for status, _ in rows if status == "failed")}
