"""TicNote mode: device recordings are picked up from the TicNote cloud and run through our pipeline.

Only audio is taken (the WAV TicNote makes after upload); TicNote's own transcript and summary are ignored.
Runs from the background loop. Each recordId becomes at most one meeting (ticnote_imports primary key).
"""
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from . import config, pipeline, settings_store, ticnote
from .db import SessionLocal, TicnoteImport, now

log = logging.getLogger("sysai.ticnote")

POLL_SECONDS = 60
MAX_ATTEMPTS = 3
STALE_MINUTES = 15   # a download this old was cut off (restart, crash): it is free to be taken again
_last_scan = [0.0]
STATE = {"last_scan_at": 0.0, "last_error": ""}  # shown on the TicNote tab


def _ms(node: dict, *keys) -> int:
    for k in keys:
        try:
            v = int(node.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return 0


def _meeting_date(node: dict) -> dt.date | None:
    ms = _ms(node, "createTimestamp")
    if not ms:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, ZoneInfo(config.TIMEZONE)).date()


def _title(node: dict) -> str:
    name = (node.get("name") or "").strip()
    if name:
        return name[:300]
    ms = _ms(node, "createTimestamp")
    when = dt.datetime.fromtimestamp(ms / 1000, ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M") if ms else ""
    return f"TicNote {when}".strip()


def candidates(nodes: list[dict], enabled_at_ms: int) -> list[dict]:
    """Ready device recordings that reached the TicNote cloud after the mode was switched on, oldest first."""
    out = []
    for node in nodes:
        if str(node.get("fileType") or "") != "recording_file" or not ticnote.is_ready_recording(node):
            continue
        if not node.get("id"):
            continue
        if _ms(node, "addTimestamp", "createTimestamp") < enabled_at_ms:
            continue
        out.append(node)
    return sorted(out, key=lambda n: _ms(n, "addTimestamp", "createTimestamp"))


def _stale(row) -> bool:
    updated = row.updated_at or row.created_at
    if row.status != "downloading" or updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=dt.timezone.utc)
    return now() - updated > dt.timedelta(minutes=STALE_MINUTES)


def _claim(record_id: str, title: str) -> bool:
    """Reserve a recording for import. False when it is already imported, in progress or out of retries."""
    with SessionLocal() as s:
        row = s.get(TicnoteImport, record_id)
        if row is None:
            s.add(TicnoteImport(record_id=record_id, title=title, status="downloading", attempts=1))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                return False
            return True
        if _stale(row):
            row.status = "error"
        if row.status != "error" or row.attempts >= MAX_ATTEMPTS:
            return False
        row.status, row.attempts, row.error = "downloading", row.attempts + 1, ""
        s.commit()
        return True


def _finish(record_id: str, **fields):
    with SessionLocal() as s:
        row = s.get(TicnoteImport, record_id)
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_at = now()
        s.commit()


def _release(record_id: str):
    with SessionLocal() as s:
        row = s.get(TicnoteImport, record_id)
        if row is not None and row.status == "downloading":
            if row.attempts <= 1:
                s.delete(row)
            else:
                row.status, row.attempts = "error", row.attempts - 1
            s.commit()


def import_recording(appkey: str, node: dict) -> int | None:
    """Download one recording's WAV and queue it as a meeting. Returns the meeting id."""
    record_id, title = str(node["id"]), _title(node)
    if not _claim(record_id, title):
        return None
    dst = config.DATA_DIR / "uploads" / f"ticnote_{record_id}.wav"
    try:
        detail = ticnote.file_detail(appkey, record_id)
        if not (detail.get("formatUrl") or "").strip() and detail.get("transcodeStatus") not in ("fail", "no_rights"):
            _release(record_id)  # TicNote is still converting the audio: take it on a later pass
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        ticnote.download_wav(ticnote.audio_url(detail), dst)
        # The user may press Stop while the cloud download is in progress.
        # Re-check the durable import row before creating a meeting or queueing
        # paid transcription work, so a late download cannot restart processing.
        with SessionLocal() as s:
            latest = s.get(TicnoteImport, record_id)
            stopped = latest is None or (
                latest.status == "error"
                and (latest.error or "") == "Скачивание остановлено вручную"
            )
        if stopped:
            dst.unlink(missing_ok=True)
            log.info("TicNote import %s stopped by user during download", record_id)
            return None
        mid = pipeline.create_meeting(f"{title}.wav", str(dst), source="ticnote", title=title,
                                      meeting_date=_meeting_date(node))
    except Exception as e:  # noqa: BLE001
        log.warning("TicNote import %s failed: %s", record_id, e)
        dst.unlink(missing_ok=True)
        _finish(record_id, status="error", error=str(e)[:1000])
        return None
    _finish(record_id, status="queued", meeting_id=mid)
    pipeline.submit(mid)
    log.info("TicNote recording %s -> meeting %s", record_id, mid)
    return mid


def scan(force: bool = False) -> list[int]:
    """Background step: when TicNote mode is on, queue every new ready recording."""
    if not settings_store.get("ticnote_enabled"):
        return []
    appkey = config.ticnote_appkey()
    if not appkey:
        return []
    if not force and time.time() - _last_scan[0] < POLL_SECONDS:
        return []
    _last_scan[0] = time.time()
    enabled_at = int(settings_store.get("ticnote_enabled_at") or 0)
    if not enabled_at:  # turned on outside the settings page: start from now, never import the whole history
        enabled_at = int(time.time() * 1000)
        settings_store.set_many({"ticnote_enabled_at": enabled_at})
    try:
        nodes = ticnote.ready_recordings(appkey)
    except Exception as e:
        STATE.update(last_scan_at=time.time(), last_error=str(e)[:300])
        raise
    STATE.update(last_scan_at=time.time(), last_error="")
    queued = []
    for node in candidates(nodes, enabled_at):
        mid = import_recording(appkey, node)
        if mid:
            queued.append(mid)
    return queued


def status(limit: int = 6) -> dict:
    """What the TicNote tab shows: mode, key, last poll and the latest device recordings with their meetings."""
    from .db import Meeting
    enabled = bool(settings_store.get("ticnote_enabled"))
    has_key = bool(config.ticnote_appkey())
    recent, busy = [], False
    with SessionLocal() as s:
        rows = s.query(TicnoteImport).order_by(TicnoteImport.created_at.desc()).limit(limit).all()
        for row in rows:
            m = s.get(Meeting, row.meeting_id) if row.meeting_id else None
            if _stale(row):
                state = "error"
            else:
                state = "downloading" if row.status == "downloading" else (m.status if m else row.status)
            if state in ("downloading", "queued", "transcribing", "analyzing"):
                busy = True
            can_stop = state == "downloading" or (m is not None and state in pipeline.STOPPABLE)
            can_retry = state == "error"
            recent.append({"record_id": row.record_id, "can_stop": can_stop, "can_retry": can_retry,
                           "title": row.title or "Запись TicNote", "state": state,
                           "progress": (m.progress if m else "") or "", "error": (row.error or (m.error if m else "") or "")[:300],
                           "meeting_id": row.meeting_id,
                           "at": row.created_at.isoformat() if row.created_at else ""})
    mode = "off" if not enabled else ("nokey" if not has_key else ("busy" if busy else "waiting"))
    return {"mode": mode, "enabled": enabled, "has_key": has_key, "last_scan_at": STATE["last_scan_at"],
            "last_error": STATE["last_error"], "recent": recent}


def stop(record_id: str) -> bool:
    """«Остановить» on the TicNote tab: a stuck download is dropped (no automatic retry), a run in our pipeline is stopped."""
    with SessionLocal() as s:
        row = s.get(TicnoteImport, record_id)
        if row is None:
            return False
        if row.meeting_id:
            mid = row.meeting_id
        elif row.status == "downloading":
            row.status, row.attempts, row.error = "error", MAX_ATTEMPTS, "Скачивание остановлено вручную"
            s.commit()
            return True
        else:
            return False
    return pipeline.cancel_processing(mid)


def retry(record_id: str) -> bool:
    """«Повторить»: a failed download is taken again on the next pass, a failed meeting is processed again."""
    with SessionLocal() as s:
        row = s.get(TicnoteImport, record_id)
        if row is None:
            return False
        if row.meeting_id:
            mid = row.meeting_id
        elif row.status == "error" or _stale(row):
            row.status, row.attempts, row.error = "error", 0, ""
            s.commit()
            _last_scan[0] = 0  # do not wait for the next minute
            return True
        else:
            return False
    return pipeline.retry_processing(mid)


def restore_audio(meeting_id: int) -> str:
    """Download the WAV of a TicNote meeting again (the server disk is wiped on restart). Returns the new path."""
    with SessionLocal() as s:
        row = s.query(TicnoteImport).filter_by(meeting_id=meeting_id).first()
        record_id = row.record_id if row else None
    if not record_id:
        return ""
    appkey = config.ticnote_appkey()
    if not appkey:
        raise ticnote.TicNoteError("Нет ключа TicNote: не могу скачать запись заново")
    dst = config.DATA_DIR / "uploads" / f"ticnote_{record_id}.wav"
    dst.parent.mkdir(parents=True, exist_ok=True)
    ticnote.download_wav(ticnote.audio_url(ticnote.file_detail(appkey, record_id)), dst)
    pipeline._set(meeting_id, audio_path=str(dst))
    log.info("TicNote recording %s downloaded again for meeting %s", record_id, meeting_id)
    return str(dst)
