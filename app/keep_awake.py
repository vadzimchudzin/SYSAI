"""Keeps a sleeping free instance awake while work is in progress (Render free: asleep after 15 idle minutes).

A free Render web service stops after 15 minutes without incoming requests and starts again with an empty disk on the
next one. A TicNote recording or a long transcription is processed with nobody on the site, so the instance fell asleep
in the middle of it: the processing started over after the wake-up, and a recording waiting for its upload to the
storage was lost. While something is in progress the background loop asks the service's own public address
(/health, no database) every EVERY seconds: for the platform that is an incoming request. With nothing to do it stays
quiet and the instance sleeps as before. KEEP_AWAKE=0 switches it off; on by default on Render.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import config
from .db import Meeting, Recording, SessionLocal, TicnoteImport

log = logging.getLogger("sysai.awake")
EVERY = 10 * 60        # seconds between the requests: well inside the 15 idle minutes
CHECK = 60             # seconds between the looks at the database while nothing is going on
LONGEST = 3 * 3600     # busy for longer means something is stuck: let the instance sleep, the work resumes on wake-up
_STATE = {"pinged": 0.0, "checked": 0.0, "since": 0.0, "stuck": False}


def _clock() -> float:
    return time.monotonic()


def enabled() -> bool:
    return config.KEEP_AWAKE and config.PUBLIC_URL.startswith("https://")


def busy() -> str:
    """What is still in progress, "" when nothing is."""
    with SessionLocal() as s:
        if s.query(Meeting.id).filter(Meeting.status.in_(("queued", "transcribing", "analyzing"))).first():
            return "обработка совещания"
        if s.query(Meeting.id).filter(Meeting.recording_status == "pending").first():
            return "сохранение записи в хранилище"
        if s.query(TicnoteImport.record_id).filter(TicnoteImport.status == "downloading").first():
            return "скачивание записи с TicNote"
        if s.query(Recording.id).filter(Recording.status == "finishing").first():
            return "сборка записи из браузера"
    return ""


def run_due() -> None:
    """Background step: one request to /health every EVERY seconds while busy() says something is in progress."""
    if not enabled():
        return
    moment = _clock()
    if moment - _STATE["checked"] < CHECK or (_STATE["pinged"] and moment - _STATE["pinged"] < EVERY):
        return
    _STATE["checked"] = moment
    reason = busy()
    if not reason:
        _STATE.update(since=0.0, stuck=False, pinged=0.0)
        return
    if not _STATE["since"]:
        _STATE["since"] = moment
        log.info("keep awake: %s — asking %s/health every %s min", reason, config.PUBLIC_URL, EVERY // 60)
    if moment - _STATE["since"] > LONGEST:
        if not _STATE["stuck"]:
            _STATE["stuck"] = True
            log.warning("keep awake: busy for %s h (%s), letting the instance sleep", LONGEST // 3600, reason)
        return
    _STATE["pinged"] = moment
    try:
        httpx.get(f"{config.PUBLIC_URL}/health", timeout=30)
    except httpx.HTTPError as exc:
        log.info("keep awake: %s/health did not answer: %s", config.PUBLIC_URL, exc)
