"""How often each built-in skill of the AI employee worked, was cancelled or failed (the Skills page)."""
import datetime as dt
import logging

from sqlalchemy import func

from .db import SessionLocal, SkillRun, now

log = logging.getLogger("sysai.agent")

OUTCOMES = ("done", "approval", "suggested", "cancelled", "error")


def record(name: str, outcome: str, detail: str = "", chat: str = "") -> None:
    """Never raises: the statistics must not break the work they count."""
    if outcome not in OUTCOMES:
        return
    try:
        with SessionLocal() as s:
            s.add(SkillRun(name=str(name)[:40], outcome=outcome, detail=str(detail or "")[:500], chat=str(chat or "")[:64]))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("skill %s: the use was not counted: %s", name, exc)


def stats(days: int = 30) -> dict:
    """{skill: {"done", "approval", "suggested", "cancelled", "error", "last_error"}} for the last days."""
    since = now() - dt.timedelta(days=days)
    out = {}
    with SessionLocal() as s:
        rows = (s.query(SkillRun.name, SkillRun.outcome, func.count(SkillRun.id))
                .filter(SkillRun.created_at >= since).group_by(SkillRun.name, SkillRun.outcome).all())
        for name, outcome, count in rows:
            out.setdefault(name, dict.fromkeys(OUTCOMES, 0))[outcome] = count
        for name, item in out.items():
            if item["error"]:
                last = (s.query(SkillRun.detail).filter(SkillRun.name == name, SkillRun.outcome == "error",
                                                        SkillRun.created_at >= since)
                        .order_by(SkillRun.id.desc()).first())
                item["last_error"] = (last[0] if last else "") or ""
    return out
