"""Small durable task supervisor; never a Telegram consumer or independent sender.

No schema creation, network work or background thread at import. Each tick is bounded,
returns while waiting for a reply, and shares one DB lease with other tick callers.
Unknown external-send outcomes are held forever for review, never automatically retried.
"""
import datetime as dt
import json
import os
import re
import uuid

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from .. import db, llm, report_archive, settings_store, userbot
from .models import Action, Event, Lease, Run

KEY = "coordinator_pilot_v1"
DEFAULTS = {"enabled": False, "dry_run": True, "allowed_employee_ids": [],
            "daily_actions": 10, "max_actions_per_task": 3, "reply_timeout_hours": 24}
TERMINAL = ("completed", "failed")



class Hold(ValueError):
    """Safe, user-visible policy or input failure."""


def options():
    return {**DEFAULTS, **(settings_store.get(KEY) or {})}


def configure(values):
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise Hold("Unknown configuration field")
    cfg = {**options(), **values}
    if type(cfg["enabled"]) is not bool or type(cfg["dry_run"]) is not bool:
        raise Hold("enabled/dry_run must be booleans")
    ids = cfg["allowed_employee_ids"]
    if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
        raise Hold("Allowlist must contain explicit employee IDs")
    for key, high in (("daily_actions", 100), ("max_actions_per_task", 3), ("reply_timeout_hours", 168)):
        if type(cfg[key]) is not int or not 1 <= cfg[key] <= high:
            raise Hold(f"Invalid {key}")
    with db.SessionLocal() as s:
        row = s.get(db.Setting, KEY)
        if row is None:
            row = db.Setting(key=KEY)
            s.add(row)
        row.value = json.dumps(cfg)
        s.commit()
    return cfg


def schema_ready():
    return all(inspect(db.engine).has_table(name) for name in
               ("coordinator_runs", "coordinator_actions", "coordinator_events", "coordinator_leases"))


def event(s, run, kind, data=None, key=None):
    s.add(Event(run_id=run.id, kind=kind, data=data or {}, external_key=key))
    run.updated_at = db.now()


def hold(s, run, reason):
    run.state, run.reason = "needs_review", reason
    event(s, run, "held", {"reason": reason})


def authorized(s, run):
    owner = s.get(db.User, run.owner_id)
    task = s.get(db.Task, run.task_id)
    if not owner or owner.status != "approved" or owner.role != "superuser":
        raise Hold("Initiator no longer has execution rights")
    if not task or task.executor != "assistant" or task.status != "coordinator_owned":
        raise Hold("Task no longer belongs to this pilot")
    meeting = s.get(db.Meeting, task.meeting_id)
    if not meeting or not meeting.approved_at or meeting.status not in report_archive.APPROVED_STATUSES:
        raise Hold("Meeting must remain explicitly approved")
    return task


def recipient(s, task, cfg):
    employee = s.get(db.Employee, task.target_employee_id) if task.target_employee_id else None
    if not employee or not employee.active or employee.id not in cfg["allowed_employee_ids"]:
        raise Hold("Explicit active target must be in the current recipient allowlist")
    # Stable numeric identity only: never resolve a model-produced username/phone.
    ref = str(employee.telegram_user_id or employee.telegram_chat_id or "")
    if not re.fullmatch(r"[1-9]\d{4,19}", ref):
        raise Hold("Target needs a verified numeric Telegram identity")
    return employee, ref


def enroll(task_id, owner_id):
    if not options()["enabled"] or not schema_ready():
        raise Hold("Enable pilot and apply reviewed additive migration first")
    with db.SessionLocal() as s:
        existing = s.query(Run).filter_by(task_id=task_id).first()
        if existing:
            return existing.id
        task = s.get(db.Task, task_id)
        if not task or task.executor != "assistant" or task.status not in ("draft", "assistant_todo"):
            raise Hold("Only an unsent assistant task can be enrolled")
        run = Run(id=uuid.uuid4().hex, task_id=task_id, owner_id=owner_id, state="queued")
        # Atomic ownership handoff competes with the legacy assistant's own claim.
        claimed = s.query(db.Task).filter(db.Task.id == task_id, db.Task.status == task.status).update(
            {db.Task.status: "coordinator_owned"}, synchronize_session=False)
        if not claimed:
            raise Hold("Task already claimed")
        s.expire(task)
        authorized(s, run)
        s.add(run)
        event(s, run, "enrolled")
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return s.query(Run).filter_by(task_id=task_id).one().id
        return run.id


def artifact_bytes(s, task, artifact_id):
    artifact = s.get(db.Artifact, artifact_id) if type(artifact_id) is int else None
    if (not artifact or artifact.task_id != task.id or artifact.meeting_id != task.meeting_id or
            artifact.mime != "application/pdf" or not artifact.data or len(artifact.data) > 2 * 1024 * 1024):
        raise Hold("Only this task's stored PDF artifact, at most 2 MiB, may be transferred")
    # Never accept a filesystem path, URL, supplied bytes, or model-produced filename.
    return f"sysai-task-{task.id}-artifact-{artifact.id}.pdf", artifact.data


def context(s, run, task):
    staff = [{"id": e.id, "name": e.name, "position": e.position,
              "department": e.department, "competencies": (e.competencies or "")[:1000],
              "responsibilities": (e.responsibilities or "")[:1000], "notes": (e.context or "")[:1000],
              "departments": [{"name": m.department.name, "role": m.role,
                               "responsibilities": m.responsibilities} for m in e.department_memberships
                              if m.department and m.department.active]}
             for e in s.query(db.Employee).filter_by(active=True).order_by(db.Employee.id).limit(100)]
    notes = [{"kind": n.kind, "text": n.text[:1500]} for n in
             s.query(db.Note).filter_by(task_id=task.id).order_by(db.Note.id.desc()).limit(10)]
    replies = [{"event_id": e.id, **e.data} for e in s.query(Event).filter_by(
        run_id=run.id, kind="reply").order_by(Event.id.desc()).limit(6)]
    return {"task": {"id": task.id, "title": task.title, "description": (task.description or "")[:4000],
                     "target_employee_id": task.target_employee_id, "source_quote": (task.source_quote or "")[:2000]},
            "staff": staff, "memory": notes, "replies": replies,
            "artifacts": [{"id": a.id, "title": a.title} for a in s.query(db.Artifact).filter_by(task_id=task.id, meeting_id=task.meeting_id).limit(20)],
            "previous_plan": run.plan, "actions_used": s.query(Action).filter_by(run_id=run.id).count()}


def apply_plan(s, run, task, data, cfg):
    kind = data["kind"]
    if kind == "result":
        evidence = s.get(Event, data.get("evidence_event_id")) if type(data.get("evidence_event_id")) is int else None
        quote = data.get("evidence_quote")
        if (not evidence or evidence.run_id != run.id or evidence.kind != "reply" or
                not isinstance(quote, str) or len(quote.strip()) < 5 or quote not in evidence.data["text"]):
            raise Hold("Completion needs exact evidence from a linked employee reply")
        # Keep provenance; employee report is evidence, not independent verification or ownership.
        run.summary = f"Employee-reported result (reply event {evidence.id}): {quote[:4000]}"
        run.state, run.reason = "completed", ""
        s.add(db.Note(task_id=task.id, meeting_id=task.meeting_id, employee_id=task.target_employee_id,
                      kind="fact", source="conversation", author="coordinator",
                      text=run.summary))
        event(s, run, "completed", {"evidence_event_id": evidence.id})
        return
    if kind in ("delegate", "hold"):
        suggested = s.get(db.Employee, data.get("employee_id")) if type(data.get("employee_id")) is int else None
        run.summary = ("Suggested competent employee (not confirmed owner): " + suggested.name +
                       ". Reviewer acceptance required.") if kind == "delegate" and suggested and suggested.active else "Reviewer decision required."
        hold(s, run, "Complex or insufficiently specified task; accept proposal or clarify")
        return
    if kind not in ("ask", "clarify", "file"):
        raise Hold("Unsupported plan kind")
    artifact_id = data.get("artifact_id") if kind == "file" else None
    if kind == "file":
        artifact_bytes(s, task, artifact_id)
    message = data.get("message")
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
        raise Hold("Question must contain 1–2000 characters")
    count = s.query(Action).filter_by(run_id=run.id).count()
    if count >= cfg["max_actions_per_task"]:
        raise Hold("Task clarification budget exhausted")
    if kind == "clarify" and not s.query(Event).filter_by(run_id=run.id, kind="reply").first():
        raise Hold("Clarification requires a correlated reply")
    employee, ref = recipient(s, task, cfg)
    s.add(Action(run_id=run.id, sequence=count + 1, employee_id=employee.id,
                 recipient=ref, artifact_id=artifact_id, body=f"[SYSAI:{run.id}] {message.strip()}"))
    run.state, run.reason = "planned", ""
    event(s, run, "planned", {"kind": kind, "sequence": count + 1})


def dispatch(run_id):
    """Final execution-time policy check and durable outbox claim, then existing sender."""
    with db.SessionLocal() as s:
        run = s.get(Run, run_id)
        cfg = options()  # no cached grants, including after a slow planning call
        task = authorized(s, run)
        employee, ref = recipient(s, task, cfg)
        action = s.query(Action).filter_by(run_id=run.id, status="prepared").order_by(Action.id).first()
        if not action:
            return
        if action.employee_id != employee.id or action.recipient != ref:
            raise Hold("Recipient changed since planning; review required")
        if not cfg["enabled"] or cfg["dry_run"] or os.getenv("SYSAI_COORDINATOR_LIVE") != "1":
            run.reason = "Dry-run: question prepared; no message sent"
            s.commit()
            return
        if userbot.quiet_until() is not None:
            run.reason = "Quiet hours: send deferred"
            s.commit()
            return
        llm.check_budget()
        midnight = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
        used = s.query(Action).filter(Action.attempted_at >= midnight).count()
        if used >= cfg["daily_actions"]:
            raise Hold("Daily durable action budget exhausted")
        if not userbot.ai_mode() or not userbot.ready():
            raise Hold("Existing Telegram USER account is required and must be ready")
        claimed = s.query(Action).filter_by(id=action.id, status="prepared").update(
            {Action.status: "sending", Action.attempted_at: db.now()}, synchronize_session=False)
        if not claimed:
            return
        file_data = artifact_bytes(s, task, action.artifact_id) if action.artifact_id else None
        action_id, body = action.id, action.body
        event(s, run, "send_started", {"action_id": action_id})
        s.commit()  # external side effect NEVER precedes durable claim
    from ..assistant import send_task_message, send_task_file
    try:
        receipt = send_task_file(ref, *file_data, body) if file_data else send_task_message(ref, body)
        if not isinstance(receipt, dict) or type(receipt.get("message_id")) is not int or receipt["message_id"] <= 0:
            raise Hold("Missing delivery receipt")
    except Exception:  # unknown outcome, including timeout after remote success
        with db.SessionLocal() as s:
            action, run = s.get(Action, action_id), s.get(Run, run_id)
            action.status = "uncertain"
            hold(s, run, "Uncertain send: inspect Telegram; automatic resend prohibited")
            s.commit()
        return
    with db.SessionLocal() as s:
        action, run = s.get(Action, action_id), s.get(Run, run_id)
        action.status, action.message_id = "sent", receipt["message_id"]
        run.state, run.reason = "awaiting_reply", ""
        event(s, run, "sent", {"action_id": action_id, "message_id": action.message_id})
        s.commit()


def acquire():
    token = uuid.uuid4().hex
    with db.SessionLocal() as s:
        row = s.get(Lease, "tick")
        if row is None:
            s.add(Lease(name="tick", token=token, expires_at=db.now() + dt.timedelta(minutes=5)))
            try:
                s.commit()
                return token
            except IntegrityError:
                return None
        claimed = s.query(Lease).filter(Lease.name == "tick", Lease.expires_at < db.now()).update(
            {Lease.token: token, Lease.expires_at: db.now() + dt.timedelta(minutes=5)}, synchronize_session=False)
        s.commit()
        return token if claimed else None


def tick():
    """At most one prepared send per invocation; never sleep waiting for replies."""
    if not options()["enabled"] or not schema_ready():
        return {"processed": 0, "reason": "disabled or migration missing"}
    token = acquire()
    if not token:
        return {"processed": 0, "reason": "another tick owns the lease"}
    processed = 0
    try:
        with db.SessionLocal() as s:
            recover()
            ids = [r.id for r in s.query(Run).filter(Run.state.in_(("planned",))).order_by(Run.created_at).limit(1)]
        for run_id in ids:
            try:
                with db.SessionLocal() as s:
                    run = s.get(Run, run_id)
                    task = authorized(s, run)
                    state = run.state
                if state == "planned":
                    dispatch(run_id)
            except Hold as exc:
                with db.SessionLocal() as s:
                    hold(s, s.get(Run, run_id), str(exc))
                    s.commit()
            except Exception:
                # Provider/internal errors must not log secrets or replay an external send.
                with db.SessionLocal() as s:
                    run = s.get(Run, run_id)
                    run.state, run.reason = "failed", "Planner/internal failure; inspect diagnostics before review"
                    event(s, run, "failed")
                    s.commit()
            processed += 1
    finally:
        with db.SessionLocal() as s:
            s.query(Lease).filter_by(name="tick", token=token).update({Lease.expires_at: db.now()})
            s.commit()
    return {"processed": processed}


def inbound(msg):
    """Consume only an explicitly correlated reply by the exact current numeric target.

    Multiple open tasks are NEVER guessed from recency. References and reply_to must
    agree. A handled duplicate stays consumed and cannot fall through to legacy agent.
    """
    if not schema_ready():
        return False
    uid, mid = str(msg.get("user_id", "")), msg.get("message_id")
    text = msg.get("text") or ""
    refs = set(re.findall(r"\[SYSAI:([a-f0-9]{32})\]", text))
    reply_to = msg.get("reply_to")
    if not refs and not reply_to:
        return False
    key = f"telegram:{uid}:{mid}"
    with db.SessionLocal() as s:
        if s.query(Event).filter_by(external_key=key).first():
            return True
        candidates = s.query(Action).filter_by(recipient=uid, status="sent")
        if reply_to:
            candidates = candidates.filter_by(message_id=reply_to)
        if refs:
            candidates = candidates.filter(Action.run_id.in_(refs))
        run_ids = {a.run_id for a in candidates.all()}
        if len(run_ids) != 1 or len(refs) > 1 or type(mid) is not int or mid <= 0 or not text.strip():
            return bool(refs)  # addressed pilot data never becomes free-form legacy orders
        run = s.get(Run, run_ids.pop())
        if not run or run.state not in ("awaiting_reply", "queued"):
            return True
        try:
            task = authorized(s, run)
            _, ref = recipient(s, task, options())
            if ref != uid:
                return True
        except Hold:
            return True
        event(s, run, "reply", {"text": text[:8000], "message_id": mid, "user_id": uid}, key)
        s.add(db.Note(task_id=task.id, meeting_id=task.meeting_id, employee_id=task.target_employee_id,
                      kind="fact", source="conversation", author=uid,
                      text=f"Employee reply for SYSAI:{run.id} (untrusted): {text[:8000]}"))
        run.state, run.reason = "queued", ""
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
        return True


def review(run_id, decision):
    """No resend override. Reviewer can accept a delegation suggestion or stop work."""
    with db.SessionLocal() as s:
        run = s.get(Run, run_id)
        if not run or run.state != "needs_review":
            raise Hold("Task is not waiting for review")
        task = authorized(s, run)
        if decision == "accept_delegate" and run.plan.get("kind") == "delegate":
            employee = s.get(db.Employee, run.plan.get("employee_id"))
            if not employee or not employee.active:
                raise Hold("Suggested employee is no longer active")
            # Human acceptance, not model inference, establishes ownership; delivery remains existing manual flow.
            task.assignee_id, task.assignee_name, task.executor, task.status = employee.id, employee.name, "person", "in_progress"
            run.state, run.reason = "completed", "Reviewer accepted handoff; employee work is NOT completed"
            event(s, run, "handoff_accepted", {"employee_id": employee.id})
        elif decision == "stop":
            run.state, run.reason = "failed", "Stopped by reviewer; no further sends"
            event(s, run, "stopped")
        else:
            raise Hold("Unsupported review decision; uncertain sends cannot be retried here")
        s.commit()


def snapshot(run_id=None):
    if not schema_ready():
        return {"config": options(), "schema_ready": False, "runtime": runtime_readiness(), "runs": []}
    with db.SessionLocal() as s:
        query = s.query(Run).order_by(Run.created_at.desc())
        if run_id:
            query = query.filter_by(id=run_id)
        runs = []
        for r in query.limit(100):
            wake = s.query(Event).filter_by(external_key="wake-state:"+r.id).first()
            wake_lease = s.get(Lease, "wake:"+r.id)
            runs.append({"id": r.id, "task_id": r.task_id, "state": r.state, "plan": r.plan,
                         "wake": {"status": wake.data.get("status"), "attempts": wake.data.get("attempts"),
                                  "error": wake.data.get("error"),
                                  "next_at": wake_lease.expires_at.isoformat() if wake_lease else None} if wake else None,
                         "summary": r.summary, "reason": r.reason,
                         "actions": [{"sequence": a.sequence, "status": a.status, "recipient": a.recipient,
                                      "body": a.body, "message_id": a.message_id} for a in
                                     s.query(Action).filter_by(run_id=r.id).order_by(Action.sequence)],
                         "events": [{"id": e.id, "kind": e.kind, "data": e.data} for e in
                                    s.query(Event).filter_by(run_id=r.id).order_by(Event.id)]})
        return {"config": options(), "schema_ready": True, "runtime": runtime_readiness(), "runs": runs}


def planning_context(run_id, owner_id):
    with db.SessionLocal() as s:
        run = s.get(Run, run_id)
        if not run or run.owner_id != owner_id:
            raise Hold("Run not found for runtime principal")
        task = authorized(s, run)
        latest = s.query(Event.id).filter(Event.run_id == run.id, Event.kind.notin_(("wake_started", "wake_finished", "wake_state"))).order_by(Event.id.desc()).first()
        return {"run_id": run.id, "state": run.state, "revision": latest[0] if latest else 0,
                "data": context(s, run, task)}


def submit_plan(run_id, owner_id, revision, data):
    if not options()["enabled"]:
        raise Hold("Pilot disabled")
    token = acquire()
    if not token:
        raise Hold("Another operation owns the execution lease")
    try:
        with db.SessionLocal() as s:
            run = s.get(Run, run_id)
            if not run or run.owner_id != owner_id or run.state != "queued":
                raise Hold("Run is not queued for this principal")
            task = authorized(s, run)
            latest = s.query(Event.id).filter(Event.run_id == run.id, Event.kind.notin_(("wake_started", "wake_finished", "wake_state"))).order_by(Event.id.desc()).first()
            if revision != (latest[0] if latest else 0):
                raise Hold("Stale planning revision; fetch context again")
            # Strict allowlist of fields: ignore no executable or permission-bearing input.
            allowed = {"kind", "message", "employee_id", "summary", "evidence_event_id", "evidence_quote", "artifact_id"}
            if not isinstance(data, dict) or set(data) - allowed:
                raise Hold("Unknown plan fields")
            run.plan = data
            apply_plan(s, run, task, data, options())
            s.commit()
            return {"run_id": run.id, "state": run.state}
    finally:
        release(token)


def release(token):
    with db.SessionLocal() as s:
        s.query(Lease).filter_by(name="tick", token=token).update({Lease.expires_at: db.now()})
        s.commit()


def reserve_planning_turn(run_id, owner_id):
    """Durable call-count budget: runtime cannot spend unbounded LLM turns on restarts."""
    if not options()["enabled"]:
        raise Hold("Pilot disabled")
    token = acquire()
    if not token:
        raise Hold("Another operation owns the execution lease")
    try:
        with db.SessionLocal() as s:
            run = s.get(Run, run_id)
            if not run or run.owner_id != owner_id or run.state != "queued":
                raise Hold("Run is not queued for this principal")
            authorized(s, run)
            midnight = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
            daily = s.query(Event).filter(Event.kind == "planner_reserved", Event.created_at >= midnight).count()
            total = s.query(Event).filter_by(run_id=run.id, kind="planner_reserved").count()
            if daily >= options()["daily_actions"] * 3 or total >= 6:
                hold(s, run, "Durable planning call budget exhausted")
                s.commit()
                raise Hold("Durable planning call budget exhausted")
            event(s, run, "planner_reserved")
            s.commit()
        return planning_context(run_id, owner_id)
    finally:
        release(token)


def recover():
    with db.SessionLocal() as s:
        stale = db.now() - dt.timedelta(minutes=5)
        for action in s.query(Action).filter(Action.status == "sending", Action.attempted_at < stale):
            action.status = "uncertain"
            hold(s, s.get(Run, action.run_id), "Interrupted send: inspect Telegram; no automatic retry")
        for run in s.query(Run).filter_by(state="awaiting_reply"):
            updated = run.updated_at.replace(tzinfo=dt.timezone.utc) if run.updated_at.tzinfo is None else run.updated_at
            if db.now() - updated > dt.timedelta(hours=options()["reply_timeout_hours"]):
                hold(s, run, "Reply overdue; reviewer follow-up required")
        s.commit()


def maybe_enroll_task(task_id):
    """Approval/recovery handoff only, before the legacy assistant claims a task.

    Global UI opt-in authorizes this handoff, not a live send. It does not scan or
    bulk-enroll unrelated history. Already-owned tasks never return to legacy even
    after disabling the pilot. Policy/configuration failures are fail-closed.
    """
    with db.SessionLocal() as s:
        task = s.get(db.Task, task_id)
        if task and task.status == "coordinator_owned":
            return True
        if not options()["enabled"]:
            return False
        if not task or task.executor != "assistant":
            return False
        if task.status not in ("draft", "assistant_todo"):
            return True
    if not schema_ready():
        raise Hold("OpenClaw handoff held: reviewed pilot schema is missing")
    try:
        owner_id = int(os.getenv("SYSAI_MCP_USER_ID", "0"))
    except ValueError:
        raise Hold("OpenClaw handoff held: service principal is not configured") from None
    try:
        enroll(task_id, owner_id)
    except Hold:
        # An overlapping approval/recovery call may have won the same task CAS.
        with db.SessionLocal() as s:
            task = s.get(db.Task, task_id)
            if task and task.status not in ("draft", "assistant_todo"):
                return True
        raise
    return True


def runtime_readiness():
    """Authenticated diagnostics only; never return gateway/model/session tokens."""
    from urllib.parse import urlsplit
    value = os.getenv("SYSAI_OPENCLAW_URL", "").rstrip("/")
    parsed = urlsplit(value)
    origin = value if parsed.scheme == "https" and parsed.hostname and not any(
        (parsed.username, parsed.password, parsed.path, parsed.query, parsed.fragment)) else ""
    return {"enabled": os.getenv("SYSAI_OPENCLAW_ENABLED", "0") == "1",
            "origin": origin, "gateway_token_configured": len(os.getenv("SYSAI_OPENCLAW_TOKEN", "")) >= 32,
            "live_gate": os.getenv("SYSAI_COORDINATOR_LIVE", "0") == "1"}
