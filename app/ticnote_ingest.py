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
_last_scan = [0.0]


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
    nodes = ticnote.ready_recordings(appkey)
    queued = []
    for node in candidates(nodes, enabled_at):
        mid = import_recording(appkey, node)
        if mid:
            queued.append(mid)
    return queued
