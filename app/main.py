"""SYSAI web app: upload form, meetings, draft editing/approval, settings, employees, Telegram webhook."""
import datetime as dt
import hmac
import logging
import mimetypes
import os
import pathlib
import re
import secrets
import shutil
import tempfile
import threading
import time

import httpx
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import (accounts, agent, bitrix, config, llm, deadlines, meeting_alerts, pipeline, progress_board, render, report_archive,
               settings_store, speaker_id, telegram, userbot)
from .analyze import mmss
from .transcript_sections import group_transcript
from .ui_icons import ICONS
from .db import (Artifact, ConversationEscalation, Delivery, Department, Employee, EmployeeDepartment, EmployeeVoiceprint, Meeting, Recording, ReportVersion, SessionLocal, Task, TelegramChat, User,
                 init_db, now)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sysai")
mimetypes.add_type("font/woff2", ".woff2")  # slim images lack it: fonts are served by us, not Google
BASE = pathlib.Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    startup()
    yield
    shutdown()


app = FastAPI(title="SYSAI", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=14 * 86400)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.globals.update(mmss=mmss, fmt=deadlines.fmt, priority=render.PRIORITY, config=config, group_transcript=group_transcript)

STATUS = {"queued": ("В очереди", "gray"), "transcribing": ("Расшифровка", "blue"), "analyzing": ("Анализ", "blue"),
          "awaiting_approval": ("Ждёт проверки", "amber"), "sending": ("Отправка", "blue"), "done": ("Отправлено", "green"),
          "error": ("Ошибка", "red"), "rejected": ("Отклонено", "gray")}
STATUS.update(delivery_queued=("В очереди отправки", "blue"), delivery_retry=("Повтор доставки", "amber"),
              delivery_failed=("Ошибка доставки", "red"), ready=("Отчёт готов", "green"))
tpl.env.globals.update(STATUS=STATUS, ICONS=ICONS)

_ASSET_VERSIONS: dict = {}


def asset(name: str) -> str:
    """/static/<name>?v=<content hash>. Render's CDN lets browsers keep static files for 4 hours, so without the version a
    deploy shows new pages with old styles; the hash changes only when the file does."""
    version = _ASSET_VERSIONS.get(name)
    if version is None:
        import hashlib
        version = _ASSET_VERSIONS[name] = hashlib.md5((BASE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={version}"


tpl.env.globals.update(asset=asset)


def legal_info() -> dict:
    """Who runs the site: footer, /legal and the privacy policy read the same settings."""
    s = settings_store.all_settings()
    return {k: (s.get(k) or "") for k in settings_store.LEGAL_KEYS}


tpl.env.globals.update(legal_info=legal_info, POLICY_DATE="12.10.2026")


def startup():
    init_db()
    accounts.ensure_superuser()
    if accounts.insecure_reason():
        log.error("SIGN-IN IS CLOSED: %s is still the public default from the README. Set a real value in the service "
                  "environment and restart; until then nobody (SECRET_KEY) or the superuser (ADMIN_PASSWORD) can sign in.",
                  accounts.insecure_reason())
    (config.DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    config.MIC_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    if config.TESTING:
        return
    pipeline.start_background()
    if userbot.ai_mode():
        # The AI employee answers from the Telegram account, the bot webhook stays off. The connection waits a little so
        # the platform's port check passes before the heavy Telegram library loads.
        timer = threading.Timer(config.STARTUP_DELAY, userbot.sync, args=(agent.handle_incoming,))
        timer.daemon = True
        timer.start()
    elif config.telegram_enabled():
        try:
            if config.TELEGRAM_MODE == "polling":
                telegram.start_polling(pipeline.handle_update)
            else:
                telegram.set_webhook()
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram setup failed: %s", e)


def shutdown():
    """The platform stops the old instance after the new one is up: hand the Telegram account over cleanly so the
    new instance can take it at once instead of waiting for the lease to run out."""
    if config.TESTING:
        return
    try:
        userbot.stop()
    except Exception:  # noqa: BLE001
        log.exception("shutdown: could not stop the Telegram account")


def current_user(request: Request) -> dict | None:
    """The signed-in account behind the session cookie, only while it is still approved (cached per request)."""
    cached = getattr(request.state, "user", False)
    if cached is not False:
        return cached
    user = None
    if config.weak_secret_key():  # the signing key is public: no cookie can be trusted
        request.state.user = None
        return None
    uid = request.session.get("uid")
    if uid is not None:
        with SessionLocal() as s:
            row = s.get(User, uid)
            if row is not None and row.status == "approved":
                user = {"id": row.id, "username": row.username, "name": row.display_name or row.username, "role": row.role}
        if user is None:
            request.session.clear()  # the account was disabled or removed: the cookie dies with it
    request.state.user = user
    return user


def authed(request: Request) -> bool:
    return current_user(request) is not None


def guard(request: Request):
    if not authed(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def guard_admin(request: Request):
    """Settings, the employee directory and user management belong to superusers."""
    guard(request)
    if current_user(request)["role"] != "superuser":
        raise HTTPException(status_code=403, detail="Раздел доступен только суперпользователю.")


def page(request: Request, name: str, **ctx):
    me = current_user(request)
    pending_users = accounts.pending_count() if me and me["role"] == "superuser" else 0
    ai = userbot.ai_mode()
    return tpl.TemplateResponse(request, name, {"request": request, "llm": config.llm_enabled(), "ai": ai,
                                                "tg": userbot.ready() if ai else config.telegram_enabled(),
                                                "ephemeral": EPHEMERAL,
                                                "me": me, "pending_users": pending_users, **ctx})


EPHEMERAL = config.DATABASE_URL.startswith("sqlite") and bool(os.getenv("RENDER"))


# ----------------------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if authed(request):
        return RedirectResponse("/app", 303)
    return tpl.TemplateResponse(request, "login.html", {"request": request, "error": error, "username": ""})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if accounts.login_blocked(ip, username):  # too many wrong passwords lately: do not even check this one
        time.sleep(1)
        return RedirectResponse("/login?error=throttled", 303)
    user, problem = accounts.authenticate(username, password)
    if user is not None:
        request.session.update(auth=True, uid=user.id, user=user.display_name or user.username, role=user.role)
        return RedirectResponse("/app", 303)
    if problem == "1":
        accounts.note_login_failure(ip, username)
    time.sleep(1)
    return RedirectResponse("/login?error=" + problem, 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


STARTED = time.time()


@app.get("/health")
async def health():
    """No database and no thread pool: the platform's port and health checks must pass even while Neon wakes up or
    Telegram is slow. `commit` says which version really runs (Render sets RENDER_GIT_COMMIT)."""
    return {"ok": True, "commit": (os.getenv("RENDER_GIT_COMMIT") or "")[:7], "uptime_s": int(time.time() - STARTED),
            "telegram_account": userbot.ready()}


@app.get("/health/details")
def health_details(request: Request):
    if not authed(request):  # it says whether the model key and the Telegram account are connected
        raise HTTPException(status_code=401, detail="Нужен вход")
    return {"ok": True, "llm": config.llm_enabled(), "telegram": config.telegram_enabled(),
            "telegram_account": userbot.ready(), "database": config.DATABASE_URL.split(":")[0].split("+")[0]}


# ----------------------------------------------------------------------------- meetings
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return tpl.TemplateResponse(request, "landing.html", {"request": request, "authed": authed(request),
                                                          "year": dt.date.today().year})


def _meetings(limit: int = 100):
    with SessionLocal() as s:
        meetings = s.query(Meeting).order_by(Meeting.id.desc()).limit(limit).all()
        return meetings, {m.id: len(m.tasks) for m in meetings}


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request):
    guard(request)
    meetings, counts = _meetings(8)
    with SessionLocal() as s:
        stats = dict(meetings=s.query(Meeting).count(), tasks=s.query(Task).count(),
                     pending=s.query(Meeting).filter_by(status="awaiting_approval").count(),
                     employees=s.query(Employee).filter(Employee.active.is_(True)).count())
    return page(request, "dashboard.html", meetings=meetings, counts=counts, stats=stats,
                settings=settings_store.all_settings(), employees_count=stats["employees"])


@app.get("/meetings", response_class=HTMLResponse)
def meetings_list(request: Request, q: str = "", status: str = "", source: str = "", date_from: str = "", date_to: str = "", p: int = 1):
    guard(request)
    q = q.strip()[:200]
    if status and status not in STATUS:
        raise HTTPException(400, "Некорректный статус")
    if source and source not in ("web", "microphone", "demo", "api"):
        raise HTTPException(400, "Некорректный источник")
    try:
        first = dt.date.fromisoformat(date_from) if date_from else None
        last = dt.date.fromisoformat(date_to) if date_to else None
    except ValueError:
        raise HTTPException(400, "Некорректная дата фильтра") from None
    if first and last and first > last:
        raise HTTPException(400, "Начальная дата не должна быть позже конечной")
    with SessionLocal() as s:
        query = s.query(Meeting)
        if q:
            # Python casefold keeps Cyrillic search consistent across SQLite and PostgreSQL.
            matches = [mid for mid, title, filename in s.query(Meeting.id, Meeting.title, Meeting.filename)
                       if q.casefold() in (title or "").casefold() or q.casefold() in (filename or "").casefold()]
            query = query.filter(Meeting.id.in_(matches))
        if status:
            query = query.filter(Meeting.status == status)
        if source:
            query = query.filter(Meeting.source == source)
        if first:
            query = query.filter(Meeting.meeting_date >= first)
        if last:
            query = query.filter(Meeting.meeting_date <= last)
        total = query.count()
        pages = max(1, (total + 23) // 24)
        number = max(1, min(p, pages))
        meetings = query.order_by(Meeting.id.desc()).offset((number - 1) * 24).limit(24).all()
        counts = {m.id: len(m.tasks) for m in meetings}
        versions = {m.id: report_archive.latest(s, m.id) for m in meetings}
    filters = dict(q=q, status=status, source=source, date_from=date_from, date_to=date_to)
    link = lambda n: "/meetings?" + urlencode(dict(filters, p=n))
    return page(request, "meetings.html", meetings=meetings, counts=counts, versions=versions, **filters,
                page_number=number, total_pages=pages, total_count=total, archive_error="",
                prev_url=link(number - 1) if number > 1 else None, next_url=link(number + 1) if number < pages else None)


@app.get("/app/new", response_class=HTMLResponse)
def new_meeting(request: Request, source: str = "file"):
    guard(request)
    with SessionLocal() as s:
        mic = s.query(Meeting).filter_by(source="microphone").order_by(Meeting.id.desc()).limit(3).all()
    return page(request, "new.html", s=settings_store.all_settings(), chats=known_chats(), source=source,
                today=deadlines.today().isoformat(), mic_meetings=mic, audio_ext=sorted(pipeline.AUDIO_EXT))


def _options(form) -> dict:
    """Per-meeting overrides from the upload page."""
    if "deadline_mode" not in form:
        return {}
    return {"approval_required": bool(form.get("approval_required")),
            "send_tasks_to_assignees": bool(form.get("send_tasks_to_assignees")),
            "include_transcript_in_pdf": bool(form.get("include_transcript_in_pdf")),
            **({"report_style": form.get("report_style")} if form.get("report_style") in ("balanced", "executive", "detailed", "technical") and form.get("report_style") != (settings_store.get("report_style") or "balanced") else {}),
            "deadline_mode": form.get("deadline_mode", "default"),
            "report_chat_ids": _chat_ids(form, "report_chat_ids")}


@app.post("/settings/mic")
async def mic_toggle(request: Request):
    guard(request)
    data = await request.json()
    settings_store.set_many({"auto_ingest": bool(data.get("on"))})
    return {"auto_ingest": settings_store.get("auto_ingest")}


def recording_owner(request, rid=None):
    guard(request)
    user = current_user(request)
    shared_owner = f"user:{user['id']}"
    # Keep an in-progress recording created before this upgrade working in its original browser session.
    legacy_owner = request.session.get("recording_owner")
    if rid and legacy_owner:
        with SessionLocal() as session:
            old = session.get(Recording, rid)
            if old is not None and old.owner == legacy_owner:
                return legacy_owner
    return shared_owner


@app.post("/recordings")
async def recording_start(request: Request):
    from . import recording
    owner = recording_owner(request)
    form = await request.form()
    try:
        date = dt.date.fromisoformat(form.get("meeting_date")) if form.get("meeting_date") else deadlines.today()
    except ValueError:
        raise HTTPException(400, "Некорректная дата совещания") from None
    return recording.start(owner, form.get("mime_type", ""), (form.get("title") or "").strip()[:300], date,
                           _options(form), (form.get("device_label") or "").strip()[:120])


@app.get("/recordings/active")
def recordings_active(request: Request):
    from . import recording
    return {"recordings": recording.list_active(recording_owner(request), request.session.get("recording_owner"))}


@app.get("/recordings/{rid}/status")
def recording_status(request: Request, rid: str):
    from . import recording
    return recording.get_status(recording_owner(request, rid), rid)


@app.get("/recordings/{rid}/monitor", response_class=HTMLResponse)
def recording_monitor(request: Request, rid: str):
    from . import recording
    data = recording.get_status(recording_owner(request, rid), rid)
    return page(request, "recording_monitor.html", recording=data)


@app.post("/recordings/{rid}/chunks/{sequence}")
async def recording_chunk(request: Request, rid: str, sequence: int):
    from . import recording
    owner = recording_owner(request, rid)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8 * 1024 * 1024:
            raise HTTPException(413, "Одна часть записи превышает 8 МБ")
        body.extend(chunk)
    return recording.append(owner, rid, sequence, bytes(body))


@app.post("/recordings/{rid}/finish")
def recording_finish(request: Request, rid: str):
    from . import recording
    return recording.finish(recording_owner(request, rid), rid)


@app.post("/recordings/{rid}/cancel")
def recording_cancel(request: Request, rid: str):
    from . import recording
    return recording.cancel(recording_owner(request, rid), rid)


@app.get("/recordings/{rid}/audio")
def recording_download(request: Request, rid: str):
    from . import recording
    path, mime = recording.audio_file(recording_owner(request, rid), rid)
    return FileResponse(path, media_type=mime, filename=f"recording{path.suffix}")


@app.post("/upload")
async def upload(request: Request):
    guard(request)
    form = await request.form()
    file, title, meeting_date = form.get("file"), form.get("title", ""), form.get("meeting_date", "")
    if not getattr(file, "filename", ""):
        raise HTTPException(400, "Выберите аудиофайл")
    ext = pathlib.Path(file.filename or "").suffix.lower()
    if ext not in pipeline.AUDIO_EXT:
        raise HTTPException(400, f"Неподдерживаемый формат {ext}. Нужен аудиофайл: " + ", ".join(sorted(pipeline.AUDIO_EXT)))
    dst = config.DATA_DIR / "uploads" / f"{int(time.time() * 1000)}{ext}"
    size = 0
    with dst.open("wb") as out:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                out.close(); dst.unlink(missing_ok=True)
                raise HTTPException(413, f"Файл больше {config.MAX_UPLOAD_MB} МБ")
            out.write(chunk)
    d = dt.date.fromisoformat(meeting_date) if meeting_date else None
    mid = pipeline.create_meeting(file.filename or dst.name, str(dst), "web", title.strip(), d, _options(form))
    pipeline.submit(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/demo")
async def demo(request: Request):
    """Demo run without audio (bundled transcript). source=microphone simulates an auto-ingested recording."""
    guard(request)
    form = await request.form()
    mic = form.get("source") == "microphone"
    mid = pipeline.create_meeting("demo.mp3", "", "demo",
                                  "Запись с микрофона (имитация)" if mic else "", options=_options(form))
    pipeline.submit(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.get("/meetings/{mid}", response_class=HTMLResponse)
def meeting_view(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        m = pipeline.load(s, mid)
        employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
        artifacts = [{"id": a.id, "title": a.title, "filename": a.filename, "svg": a.svg or ""} for a in
                     s.query(Artifact).filter_by(meeting_id=mid).order_by(Artifact.id.desc()).all()]
    with SessionLocal() as s:
        loaded = pipeline.load(s, mid)
        plan = (pipeline.review_plan(loaded, settings_store.for_meeting(loaded.options))
                if userbot.ai_mode() and loaded.status in ("awaiting_approval", "rejected") else None)
    until = userbot.quiet_until()
    return page(request, "meeting.html", m=m, r=m.report or {}, employees=employees, artifacts=artifacts,
                plan=plan, quiet_at=userbot.local_time(until) if until is not None else "")


@app.get("/artifacts/{aid}")
def artifact_download(request: Request, aid: int):
    """A document the AI employee made itself (chart, report)."""
    guard(request)
    with SessionLocal() as s:
        artifact = s.get(Artifact, aid)
        if artifact is None or artifact.data is None:
            raise HTTPException(404, "Материал не найден")
        data, mime, name = bytes(artifact.data), artifact.mime, artifact.filename
    return Response(data, media_type=mime, headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(name)})


@app.get("/meetings/{mid}/status")
def meeting_status(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        return JSONResponse({"status": m.status, "progress": m.progress, "error": m.error,
                             "dialogue_version": [[x.id, x.status] for x in m.dialogue] +
                                                [[x.id, x.status] for x in m.escalations] +
                                                [[x.id, x.status] for x in m.deliveries if x.phase == "dialogue"]})


@app.get("/meetings/{mid}/board")
def meeting_board(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        if not meeting:
            raise HTTPException(404, "Совещание не найдено")
        board = progress_board.build(s, meeting, settings_store.for_meeting(meeting.options), STATUS)
    return JSONResponse(board, headers={"Cache-Control": "private, no-store"})


def _apply_form(mid: int, form, approve=False):
    with SessionLocal() as s:
        values = {Meeting.progress: Meeting.progress}
        if approve:
            values = {Meeting.status: "delivery_queued", Meeting.approved_at: now(), Meeting.error: ""}
        changed = s.query(Meeting).filter(Meeting.id == mid, Meeting.status.in_(("awaiting_approval", "rejected"))).update(
            values, synchronize_session=False)
        if not changed:
            return False
        m = s.get(Meeting, mid)
        r = dict(m.report or {})
        r["title"] = form.get("title", r.get("title", ""))
        r["summary"] = form.get("summary", r.get("summary", ""))
        m.report, m.title = r, r["title"]
        for t in list(m.tasks):
            if f"title_{t.id}" not in form:  # task not on the submitted form: leave untouched
                continue
            if form.get(f"del_{t.id}"):
                m.tasks.remove(t)
                continue
            t.title = (form.get(f"title_{t.id}") or t.title).strip()
            t.description = form.get(f"desc_{t.id}", t.description)
            a = form.get(f"assignee_{t.id}", "")
            if a or t.executor != "assistant":  # an empty choice keeps a task the assistant does itself
                t.assignee_id = int(a) if a else None
            if a:
                t.executor = "person"
            dl = form.get(f"deadline_{t.id}", "")
            new = dt.date.fromisoformat(dl) if dl else None
            if new != t.deadline:
                t.deadline, t.deadline_source = new, ("manual" if new else "none")
            t.priority = form.get(f"priority_{t.id}", t.priority)
        if (form.get("new_title") or "").strip():
            a = form.get("new_assignee", "")
            dl = form.get("new_deadline", "")
            m.tasks.append(Task(title=form["new_title"].strip(), assignee_id=int(a) if a else None,
                                deadline=dt.date.fromisoformat(dl) if dl else None,
                                deadline_source="manual" if dl else "none", priority=form.get("new_priority", "medium")))
        s.query(Delivery).filter_by(meeting_id=mid, phase="draft", status="pending").update(
            {Delivery.status: "cancelled"}, synchronize_session=False)
        if not approve:
            m.options = dict(m.options or {}, approval_revision=int((m.options or {}).get("approval_revision", 1)) + 1)
        else:
            s.flush()
            report_archive.ensure(s, m, settings_store.for_meeting(m.options))
        s.commit()
        return True


@app.post("/meetings/{mid}/save")
async def meeting_save(request: Request, mid: int):
    guard(request)
    if not _apply_form(mid, await request.form()):
        raise HTTPException(409, "Редактирование доступно только до утверждения отчёта")
    pipeline.request_approval(mid)
    return RedirectResponse(f"/meetings/{mid}?saved=1", 303)


@app.post("/meetings/{mid}/approve")
async def meeting_approve(request: Request, mid: int):
    """Saves the edited draft and sends it."""
    guard(request)
    form = await request.form()
    if form:
        if _apply_form(mid, form, approve=True):
            pipeline.dispatch_delivery(mid)
    else:
        pipeline.submit_deliver(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/retry-delivery")
def meeting_retry_delivery(request: Request, mid: int):
    guard(request)
    pipeline.retry_delivery(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/retry-processing")
def meeting_retry_processing(request: Request, mid: int):
    guard(request)
    pipeline.retry_processing(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/escalations/{escalation_id}/resolve")
def meeting_resolve_escalation(request: Request, mid: int, escalation_id: int):
    guard(request)
    with SessionLocal() as s:
        s.query(ConversationEscalation).filter_by(id=escalation_id, meeting_id=mid, status="open").update(
            {ConversationEscalation.status: "resolved"}, synchronize_session=False)
        s.commit()
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/dialogue-deliveries/{delivery_id}/retry")
async def meeting_retry_dialogue_delivery(request: Request, mid: int, delivery_id: int):
    guard(request)
    form = await request.form()
    with SessionLocal() as s:
        row = s.query(Delivery).filter_by(id=delivery_id, meeting_id=mid, phase="dialogue").first()
        if not row or row.status not in ("failed", "uncertain"):
            raise HTTPException(409, "Отправка уже обработана или недоступна")
        if row.status == "uncertain" and form.get("confirm") != "yes":
            raise HTTPException(400, "Сначала проверьте чат и подтвердите риск повторной отправки")
        s.query(Delivery).filter(Delivery.id == row.id, Delivery.status.in_(("failed", "uncertain"))).update(
            {Delivery.status: "pending", Delivery.next_attempt_at: None, Delivery.cycle_attempts: 0},
            synchronize_session=False)
        s.commit()
    from . import conversations, delivery
    delivery.send(delivery_id)
    conversations.run_pending()
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/deliveries/{delivery_id}/resend")
async def meeting_resend_uncertain(request: Request, mid: int, delivery_id: int):
    guard(request)
    form = await request.form()
    if form.get("confirm") != "yes":
        raise HTTPException(400, "Подтвердите риск повторной отправки")
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        if not m or m.status not in ("delivery_failed", "awaiting_approval"):
            raise HTTPException(409, "Рассылка уже обрабатывается")
        changed = s.query(Delivery).filter_by(id=delivery_id, meeting_id=mid, status="uncertain").update(
            {Delivery.status: "pending", Delivery.next_attempt_at: None, Delivery.cycle_attempts: 0}, synchronize_session=False)
        s.commit()
    if changed:
        pipeline.retry_delivery(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/reject")
def meeting_reject(request: Request, mid: int):
    guard(request)
    pipeline.reject(mid)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/reanalyze")
def meeting_reanalyze(request: Request, mid: int):
    """Rebuild the report from the saved transcript (e.g. after adding employees)."""
    guard(request)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        changed = s.query(Meeting).filter(Meeting.id == mid, Meeting.status.in_(("awaiting_approval", "rejected", "error"))).update(
            {Meeting.status: "analyzing", Meeting.progress: "Повторный анализ"}, synchronize_session=False)
        if not changed:
            raise HTTPException(409, "Нельзя пересобрать утверждённый отчёт")
        if not m.transcript:
            raise HTTPException(400, "Нет расшифровки")
        m.options = dict(m.options or {}, approval_revision=int((m.options or {}).get("approval_revision", 1)) + 1)
        m.status, m.progress = "analyzing", "Повторный анализ"
        s.commit()

    def job():
        try:
            settings = pipeline.meeting_settings(mid)
            with SessionLocal() as s:
                m = s.get(Meeting, mid)
                emps = s.query(Employee).filter(Employee.active.is_(True)).all()
                segs, mdate, source = m.transcript, m.meeting_date or deadlines.today(), m.source
            from . import analyze
            report = analyze.mock_report(emps, mdate) if source == "demo" else analyze.analyze(
                segs, emps, settings, mdate, progress=lambda p: pipeline._set(mid, progress=p),
                checkpoint=pipeline.checkpoints.Store(mid))
            pipeline._save_report(mid, report, settings)
            pipeline.request_approval(mid)
        except Exception as e:  # noqa: BLE001
            pipeline._set(mid, status="error", error=str(e)[:2000])
    job() if config.TESTING else pipeline.POOL.submit(job)
    return RedirectResponse(f"/meetings/{mid}", 303)


@app.post("/meetings/{mid}/tasks/{task_id}/retry-assistant-message")
def retry_assistant_message(request: Request, mid: int, task_id: int):
    guard_admin(request)
    if not assistant.retry_uncertain_message(task_id, mid):
        raise HTTPException(409, "Повтор невозможен: задача не требует проверки доставки или совещание не подтверждено")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@app.post("/meetings/{mid}/delete")
def meeting_delete(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        s.query(Meeting).filter_by(id=mid).update({Meeting.progress: Meeting.progress}, synchronize_session=False)
        m = s.get(Meeting, mid)
        if m:
            if m.status in ("queued", "transcribing", "analyzing", "sending", "delivery_queued", "delivery_retry"):
                raise HTTPException(409, "Дождитесь завершения обработки")
            if s.query(Delivery).filter(Delivery.meeting_id == mid, Delivery.status.in_(("pending", "sending"))).first():
                raise HTTPException(409, "Нельзя удалить совещание с незавершённой доставкой")
            if m.audio_path:
                pathlib.Path(m.audio_path).unlink(missing_ok=True)
            s.delete(m)
            s.commit()
    return RedirectResponse("/meetings", 303)


@app.get("/meetings/{mid}/pdf")
def meeting_pdf(request: Request, mid: int, download: bool = False):
    guard(request)
    with SessionLocal() as s:
        if not s.get(Meeting, mid):
            raise HTTPException(404, "Совещание не найдено")
        m = pipeline.load(s, mid)
        if not m.report:
            raise HTTPException(409, "Отчёт ещё не готов")
        archived = report_archive.latest(s, mid)
        if not archived and m.status in report_archive.APPROVED_STATUSES:
            s.query(Meeting).filter_by(id=mid).update({Meeting.progress: Meeting.progress}, synchronize_session=False)
            archived = report_archive.ensure(s, m, settings_store.for_meeting(m.options), origin="legacy")
            s.commit()
        pdf = archived.pdf if archived else render.report_pdf(m, settings_store.for_meeting(m.options))
        filename = archived.filename if archived else render.pdf_name(m)
    return pdf_response(pdf, filename, download)


def pdf_response(pdf, filename, download=False):
    disposition = "attachment" if download else "inline"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store",
    })


@app.get("/meetings/{mid}/reports/{version_id}/pdf")
def archived_pdf(request: Request, mid: int, version_id: int, download: bool = False):
    guard(request)
    with SessionLocal() as s:
        version = s.query(ReportVersion).filter_by(id=version_id, meeting_id=mid).first()
        if not version:
            raise HTTPException(404, "Версия отчёта не найдена")
        return pdf_response(version.pdf, version.filename, download)


# ----------------------------------------------------------------------------- settings
def known_chats():
    with SessionLocal() as s:
        return s.query(TelegramChat).filter(TelegramChat.active.is_(True)).order_by(TelegramChat.kind, TelegramChat.title).all()


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, msg: str = ""):
    guard_admin(request)
    st = settings_store.all_settings()
    masked = {k: bool(st.get(k)) for k in settings_store.SECRET_KEYS}
    env = {"openrouter_api_key": bool(config.OPENROUTER_API_KEY), "pyannoteai_api_key": bool(config.PYANNOTEAI_API_KEY),
           "telegram_bot_token": bool(config.TELEGRAM_BOT_TOKEN), "bitrix_webhook_url": bool(config.BITRIX_WEBHOOK_URL)}
    for k in settings_store.SECRET_KEYS + settings_store.HIDDEN_KEYS:
        st.pop(k, None)  # secrets never go back to the browser, only a mask
    ai = bool(st.get("ai_employee"))
    return page(request, "settings.html", s=st, chats=[] if ai else known_chats(), msg=msg, masked=masked, env=env,
                account=userbot.status() if ai else None, persona_default=voice_default_persona(), spent=round(llm.spent_today(), 2))


def voice_default_persona() -> str:
    from . import voice
    return voice.DEFAULT_PERSONA


def _chat_ids(form, name: str) -> list[str]:
    ids = [str(v) for v in form.getlist(name)]
    extra = form.get(name + "_extra", "")
    ids += [x.strip() for x in extra.replace("\n", ",").split(",") if x.strip()]
    return list(dict.fromkeys(ids))


@app.get("/settings/models")
def settings_models(request: Request):
    guard_admin(request)
    headers = {}
    key = config.openrouter_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = httpx.get(f"{config.OPENROUTER_BASE_URL}/models", headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        stt_response = httpx.get(f"{config.OPENROUTER_BASE_URL}/models?output_modalities=transcription",
                                 headers=headers, timeout=20)
        stt_response.raise_for_status()
        stt_payload = stt_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Could not load OpenRouter model catalog: %s", exc)
        raise HTTPException(502, "Не удалось загрузить каталог OpenRouter. Проверьте подключение и повторите.") from None
    items = payload.get("data", []) if isinstance(payload, dict) else []
    models = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        architecture = item.get("architecture") or {}
        inputs = architecture.get("input_modalities") or item.get("input_modalities") or []
        outputs = architecture.get("output_modalities") or item.get("output_modalities") or []
        pricing = item.get("pricing") or {}
        models.append({
            "id": item["id"][:200],
            "name": str(item.get("name") or item["id"])[:200],
            "audio_input": "audio" in inputs,
            "text_input": not inputs or "text" in inputs,
            "text_output": not outputs or "text" in outputs,
            "context_length": item.get("context_length"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
        })
    models.sort(key=lambda model: (model["id"].casefold()))
    stt_items = stt_payload.get("data", []) if isinstance(stt_payload, dict) else []
    transcription_models = []
    for item in stt_items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        pricing = item.get("pricing") or {}
        transcription_models.append({"id": item["id"][:200],
                                     "name": str(item.get("name") or item["id"])[:200],
                                     "prompt_price": pricing.get("prompt"),
                                     "completion_price": pricing.get("completion")})
    transcription_models.sort(key=lambda model: model["id"].casefold())
    return {"models": models, "count": len(models), "transcription_models": transcription_models,
            "transcription_count": len(transcription_models)}


@app.post("/settings")
async def settings_save(request: Request):
    guard_admin(request)
    f = await request.form()
    # Only update submitted controls. Omitted settings belong to other sections
    # or clients and must not be reset to defaults. Presence markers distinguish
    # an unchecked checkbox / empty chat selection from an omitted control.
    updates = {}
    for k in ("auto_ingest", "approval_required", "send_tasks_to_assignees",
              "include_transcript_in_pdf", "dialogue_enabled", "allow_deadline_proposals",
              "bitrix_enabled", "ai_employee"):
        if k in f or f.get("present_" + k) == "1":
            updates[k] = bool(f.get(k))
    for k in ("approver_chat_ids", "report_chat_ids", "secretary_chat_ids", "full_access_chat_ids", "director_chat_ids"):
        if k in f or k + "_extra" in f or f.get("present_" + k) == "1":
            updates[k] = _chat_ids(f, k)
    for k in settings_store.LEGAL_KEYS:
        if k in f:
            updates[k] = str(f[k]).strip()[:300]
    for k in ("transcribe_model", "diarization_model", "report_model", "agent_model", "conversation_model", "conversation_tone",
              "company_name", "accent_color", "glossary", "persona"):
        if k in f:
            value = str(f[k]).strip()
            updates[k] = value if k == "glossary" else value or settings_store.DEFAULTS[k]
    for k in ("transcribe_model", "diarization_model", "report_model", "agent_model", "conversation_model"):
        if k in updates and (len(updates[k]) > 200 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", updates[k])):
            raise HTTPException(400, "Некорректный идентификатор модели")
    if "conversation_tone" in updates:
        updates["conversation_tone"] = updates["conversation_tone"][:1000]
    if "conversation_model" in updates:
        model = updates["conversation_model"]
        if model != "openrouter/free" and not model.endswith(":free"):
            raise HTTPException(400, "Для переписки выберите openrouter/free или модель с суффиксом :free")
    if "deadline_mode" in f:
        if f["deadline_mode"] not in ("default", "ask", "none"):
            raise HTTPException(400, "Выберите режим сроков из списка")
        updates["deadline_mode"] = f["deadline_mode"]
    if "default_deadline_days" in f:
        try:
            updates["default_deadline_days"] = max(1, int(f["default_deadline_days"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "Укажите целое число рабочих дней")
    if "ask_timeout_hours" in f:
        updates["ask_timeout_hours"] = _num(f["ask_timeout_hours"], 24)
    # Secrets are never echoed in hidden inputs. A value alone (including a
    # legacy hidden value) is not an instruction to replace a credential.
    # Require the explicit action selected in the visible credential control.
    if "daily_budget_usd" in f:
        try:
            updates["daily_budget_usd"] = max(0.0, float(str(f["daily_budget_usd"]).replace(",", ".") or 0))
        except ValueError:
            raise HTTPException(400, "Лимит расходов укажите числом в долларах")
    for k in ("quiet_from_hour", "quiet_to_hour"):
        if k in f:
            try:
                hour = int(str(f[k]).strip())
            except ValueError:
                raise HTTPException(400, "Тихие часы: целые часы от 0 до 23")
            if not 0 <= hour <= 23:
                raise HTTPException(400, "Тихие часы: целые часы от 0 до 23")
            updates[k] = hour
    old_token = config.telegram_token()
    for k in settings_store.SECRET_KEYS:
        action = f.get("action_" + k, "keep")
        if action == "clear":
            updates[k] = ""
        elif action == "replace":
            value = str(f.get(k) or "").strip()
            if not value:
                raise HTTPException(400, "Введите новый ключ или выберите «Не менять»")
            updates[k] = value
        elif action != "keep":
            raise HTTPException(400, "Неизвестное действие с ключом")
    ai_after = bool(updates["ai_employee"]) if "ai_employee" in updates else userbot.ai_mode()
    if ai_after:  # in AI-employee mode people are addressed by @username, +phone or numeric id
        for k in ("approver_chat_ids", "report_chat_ids", "full_access_chat_ids", "director_chat_ids"):
            if k in updates:
                try:
                    updates[k] = [userbot.normalize_ref(x) for x in updates[k]]
                except userbot.RefError as e:
                    raise HTTPException(400, str(e))
    if updates:
        settings_store.set_many(updates)
    msg = "Сохранено"
    if "ai_employee" in updates and not config.TESTING:
        userbot.sync(agent.handle_incoming)
        if ai_after and not userbot.configured():
            msg += ". Теперь подключите Telegram-аккаунт"
    if config.telegram_token() != old_token and config.telegram_enabled() and not config.TESTING and not ai_after:
        try:
            msg += ". Telegram подключён" if telegram.set_webhook() else ". Токен сохранён, но адрес сайта не известен"
        except Exception as e:  # noqa: BLE001
            msg += f". Telegram: {str(e)[:150]}"
    return RedirectResponse("/settings?msg=" + msg, 303)


def _num(v, default):
    try:
        x = max(1.0, float(v))
    except (TypeError, ValueError):
        return default
    return int(x) if x.is_integer() else x


@app.post("/settings/test-openrouter")
def test_openrouter(request: Request):
    guard_admin(request)
    from . import llm
    try:
        return RedirectResponse("/settings?msg=OpenRouter: " + llm.check_key(), 303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?msg=OpenRouter: {str(e)[:200]}", 303)


@app.post("/settings/test-telegram")
def test_telegram(request: Request):
    guard_admin(request)
    s = settings_store.all_settings()
    chats = list(dict.fromkeys((s["report_chat_ids"] or []) + (s["approver_chat_ids"] or [])))
    if not config.telegram_enabled():
        return RedirectResponse("/settings?msg=Telegram не настроен: вставьте токен бота в «Подключения»", 303)
    try:
        me = telegram.call("getMe")
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?msg=Telegram отклонил токен: {str(e)[:150]}", 303)
    if not chats:
        return RedirectResponse(f"/settings?msg=Бот @{me.get('username')} работает. Выберите чаты для отчёта или проверки", 303)
    ok, bad = 0, []
    for c in chats:
        try:
            telegram.send_message(c, "✅ Тестовое сообщение SYSAI: доставка работает.")
            ok += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"{c}: {e}")
    return RedirectResponse(f"/settings?msg=Бот @{me.get('username')}: отправлено {ok} из {len(chats)}. " + "; ".join(bad)[:300], 303)


@app.post("/settings/test-bitrix")
def test_bitrix(request: Request):
    guard_admin(request)
    try:
        return RedirectResponse(f"/settings?msg=Bitrix24 подключён: {bitrix.check()}", 303)
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(f"/settings?msg=Bitrix24: {str(e)[:200]}", 303)


# ----------------------------------------------------------------------------- telegram account of the AI employee
def _tg_redirect(message: str):
    return RedirectResponse("/settings/telegram?msg=" + quote(message), 303)


def _tg_step(action, success: str):
    """Runs one login step and turns any failure into a message in the panel."""
    try:
        return _tg_redirect(action() or success)
    except userbot.LoginError as e:
        return _tg_redirect(str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("telegram account step failed")
        return _tg_redirect(f"Не удалось связаться с Telegram: {str(e)[:200]}")


@app.get("/settings/telegram", response_class=HTMLResponse)
def telegram_account_view(request: Request, msg: str = ""):
    guard_admin(request)
    return page(request, "telegram_account.html", account=userbot.status(), msg=msg)


@app.post("/settings/telegram/start")
def telegram_account_start(request: Request, api_id: str = Form(""), api_hash: str = Form(""), phone: str = Form("")):
    guard_admin(request)
    return _tg_step(lambda: userbot.login_start(api_id, api_hash, phone),
                    "Код отправлен в Telegram: он придёт сообщением от «Telegram» в приложении.")


@app.post("/settings/telegram/code")
def telegram_account_code(request: Request, code: str = Form("")):
    guard_admin(request)
    return _tg_step(lambda: "Нужен пароль двухэтапной проверки." if userbot.login_code(code) == "password" else
                    "Аккаунт подключён.", "")


@app.post("/settings/telegram/password")
def telegram_account_password(request: Request, password: str = Form("")):
    guard_admin(request)
    return _tg_step(lambda: userbot.login_password(password), "Аккаунт подключён.")


@app.post("/settings/telegram/logout")
def telegram_account_logout(request: Request):
    guard_admin(request)
    return _tg_step(userbot.logout, "Вышли из аккаунта.")


# ----------------------------------------------------------------------------- employees
@app.get("/employees", response_class=HTMLResponse)
def employees_view(request: Request, edit: int = 0, add: int = 0, voice_msg: str = ""):
    guard_admin(request)
    with SessionLocal() as s:
        emps = s.query(Employee).order_by(Employee.active.desc(), Employee.name).all()
        departments = s.query(Department).order_by(Department.active.desc(), Department.name).all()
        for employee in emps:
            for membership in employee.department_memberships:
                _ = membership.department.name if membership.department else ""
            _ = employee.voiceprint.created_at if employee.voiceprint else None
        for department in departments:
            _ = len(department.memberships)
    linked = {e.telegram_chat_id for e in emps if e.telegram_chat_id}
    ai = userbot.ai_mode()
    chats = [] if ai else [c for c in known_chats() if c.kind == "private"]
    return page(request, "employees.html", employees=emps, chats=chats, linked=linked,
                departments=departments, positions=settings_store.POSITIONS, edit=edit, add=bool(add),
                voice_msg=voice_msg, voice_identity_enabled=bool(config.pyannoteai_key()))


@app.post("/employees")
async def employee_save(request: Request, id: int = Form(0), name: str = Form(...), aliases: str = Form(""),
                  position: str = Form(""), department: str = Form(""), competencies: str = Form(""),
                  responsibilities: str = Form(""), context: str = Form(""),
                  channel: str = Form("telegram"), telegram_chat_id: str = Form(""),
                  telegram_manual: str = Form(""), bitrix_user_id: str = Form(""), active: str = Form("")):
    guard_admin(request)
    form = await request.form()
    try:
        selected_ids = {int(value) for value in form.getlist("department_ids")}
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректное подразделение") from None
    with SessionLocal() as s:
        e = s.get(Employee, id) if id else Employee()
        if e is None:
            raise HTTPException(404, "Сотрудник не найден")
        e.name, e.aliases, e.position, e.channel = name.strip(), aliases.strip(), position.strip(), channel
        e.department = department.strip()
        e.competencies = competencies.strip()
        e.responsibilities = responsibilities.strip()[:4000]
        e.context = context.strip()
        ref = (telegram_manual.strip() or telegram_chat_id).strip()
        if ref and userbot.ai_mode():
            try:
                ref = userbot.normalize_ref(ref)
            except userbot.RefError as exc:
                raise HTTPException(400, str(exc))
        if (e.telegram_chat_id or "") != ref:
            e.telegram_user_id = ""
        e.telegram_chat_id = ref
        e.bitrix_user_id = bitrix_user_id.strip()
        e.active = bool(active) if id else True
        s.add(e)
        s.flush()
        departments = s.query(Department).filter(Department.id.in_(selected_ids)).all() if selected_ids else []
        if {d.id for d in departments} != selected_ids:
            raise HTTPException(400, "Одно из подразделений не найдено")
        existing_ids = {m.department_id for m in e.department_memberships}
        if any(not d.active and d.id not in existing_ids for d in departments):
            raise HTTPException(400, "Нельзя добавить сотрудника в неактивное подразделение")
        s.query(EmployeeDepartment).filter_by(employee_id=e.id).delete(synchronize_session=False)
        for d in departments:
            role = str(form.get(f"department_role_{d.id}") or "").strip()[:200]
            duties = str(form.get(f"department_responsibilities_{d.id}") or "").strip()[:4000]
            s.add(EmployeeDepartment(employee_id=e.id, department_id=d.id, role=role, responsibilities=duties))
        s.commit()
    return RedirectResponse("/employees", 303)


def _voice_redirect(employee_id: int, message: str):
    return RedirectResponse(f"/employees?edit={employee_id}&voice_msg={quote(message, safe='')}", 303)


@app.post("/employees/{eid}/voice-sample")
def employee_voice_sample(request: Request, eid: int, sample: UploadFile = File(...), consent: str = Form("")):
    guard_admin(request)
    if consent not in ("yes", "1", "on", "true"):
        return _voice_redirect(eid, "Нужно отдельное подтверждённое согласие сотрудника на голосовой шаблон.")
    if not config.pyannoteai_key():
        return _voice_redirect(eid, "Сначала добавьте ключ pyannoteAI в Настройки → Идентификация по голосу.")
    if config.weak_secret_key():
        return _voice_redirect(eid, "Перед сохранением голосового шаблона задайте надёжный SECRET_KEY на сервере.")
    extension = pathlib.Path(sample.filename or "").suffix.lower()
    if extension not in {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".flac", ".aac", ".mp4"}:
        return _voice_redirect(eid, "Поддерживаются WAV, MP3, M4A, WEBM, OGG, FLAC, AAC или MP4.")
    with SessionLocal() as session:
        employee = session.get(Employee, eid)
        if employee is None or not employee.active:
            return _voice_redirect(eid, "Активный сотрудник не найден.")
        employee_name = employee.name
    try:
        with tempfile.TemporaryDirectory(prefix="sysai_voice_sample_") as tmp:
            source_path = pathlib.Path(tmp) / ("upload" + extension)
            normalized_path = pathlib.Path(tmp) / "voice-sample.wav"
            total = 0
            with source_path.open("wb") as destination:
                while True:
                    block = sample.file.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > speaker_id.MAX_SAMPLE_BYTES:
                        raise speaker_id.SpeakerIdentityError("Файл образца превышает 16 МБ.")
                    destination.write(block)
            if total == 0:
                raise speaker_id.SpeakerIdentityError("Файл образца пуст.")
            duration = speaker_id.normalize_sample(source_path, normalized_path)
            template = speaker_id.create_voiceprint(normalized_path, eid)
            encrypted = speaker_id.encrypt_voiceprint(template)
        with SessionLocal() as session:
            employee = session.get(Employee, eid)
            if employee is None:
                raise speaker_id.SpeakerIdentityError("Сотрудник был удалён во время регистрации.")
            profile = session.query(EmployeeVoiceprint).filter_by(employee_id=eid).one_or_none()
            if profile is None:
                profile = EmployeeVoiceprint(employee_id=eid)
            profile.encrypted_voiceprint = encrypted
            profile.provider = "pyannoteAI"
            profile.sample_duration = duration
            profile.consent_at = now()
            profile.consent_version = speaker_id.CONSENT_VERSION
            profile.updated_at = now()
            session.add(profile)
            session.commit()
        return _voice_redirect(eid, f"Голосовой шаблон для «{employee_name}» создан. Исходный файл образца не сохранялся в SYSAI.")
    except speaker_id.SpeakerIdentityError as exc:
        return _voice_redirect(eid, str(exc)[:300])
    except Exception:
        log.exception("Could not enroll voiceprint for employee id %s", eid)
        return _voice_redirect(eid, "Не удалось создать голосовой шаблон. Проверьте ключ pyannoteAI и логи сервера.")
    finally:
        try:
            sample.file.close()
        except Exception:
            pass


@app.post("/employees/{eid}/voice-sample/delete")
def employee_voice_sample_delete(request: Request, eid: int):
    guard_admin(request)
    with SessionLocal() as session:
        employee = session.get(Employee, eid)
        if employee is None:
            return _voice_redirect(eid, "Сотрудник не найден.")
        profile = session.query(EmployeeVoiceprint).filter_by(employee_id=eid).one_or_none()
        if profile is not None:
            session.delete(profile)
            session.commit()
        employee_name = employee.name
    return _voice_redirect(eid, f"Локальный голосовой шаблон «{employee_name}» удалён.")


@app.get("/departments", response_class=HTMLResponse)
def departments_view(request: Request, edit: int = 0, msg: str = ""):
    guard_admin(request)
    with SessionLocal() as s:
        departments = s.query(Department).order_by(Department.active.desc(), Department.name).all()
        employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
        for department in departments:
            for membership in department.memberships:
                if membership.employee is not None:
                    _ = membership.employee.name, membership.employee.position, membership.employee.active
            _ = len(department.memberships)
        for employee in employees:
            for membership in employee.department_memberships:
                if membership.department is not None:
                    _ = membership.department.name
        department_heads = {
            department.id: next(
                (membership for membership in department.memberships
                 if membership.is_primary and membership.employee is not None and membership.employee.active),
                None,
            )
            for department in departments
        }
        member_ids_by_department = {
            department.id: {membership.employee_id for membership in department.memberships}
            for department in departments
        }
        available_employees_by_department = {
            department.id: [employee for employee in employees
                            if employee.id not in member_ids_by_department[department.id]]
            for department in departments
        }
    return page(request, "departments.html", departments=departments, edit=edit, msg=msg,
                available_employees_by_department=available_employees_by_department, department_heads=department_heads)


@app.post("/departments")
def department_save(request: Request, id: int = Form(0), name: str = Form(...),
                    description: str = Form(""), active: str = Form("")):
    guard_admin(request)
    clean_name = name.strip()[:100]
    if not clean_name:
        raise HTTPException(400, "Укажите название подразделения")
    with SessionLocal() as s:
        duplicate = s.query(Department).filter(Department.name.ilike(clean_name))
        if id:
            duplicate = duplicate.filter(Department.id != id)
        if duplicate.first():
            raise HTTPException(409, "Подразделение с таким названием уже существует")
        department = s.get(Department, id) if id else Department()
        if department is None:
            raise HTTPException(404, "Подразделение не найдено")
        department.name = clean_name
        department.description = description.strip()[:1000]
        department.active = bool(active) if id else True
        s.add(department)
        s.commit()
    return RedirectResponse("/departments", 303)


@app.post("/departments/{department_id}/head")
def department_set_head(request: Request, department_id: int, employee_id: int = Form(0)):
    guard_admin(request)
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        if department is None:
            raise HTTPException(404, "Подразделение не найдено")
        memberships = s.query(EmployeeDepartment).filter_by(department_id=department_id).all()
        by_employee = {membership.employee_id: membership for membership in memberships}
        chosen = by_employee.get(employee_id) if employee_id else None
        if employee_id and (
            chosen is None or chosen.employee is None or not chosen.employee.active
        ):
            raise HTTPException(400, "Начальника можно выбрать только из активных сотрудников этого подразделения")
        for membership in memberships:
            membership.is_primary = bool(chosen and membership.employee_id == chosen.employee_id)
        department_name = department.name
        chosen_name = chosen.employee.name if chosen and chosen.employee else ""
        s.commit()
    message = (
        f"Назначен начальник подразделения «{department_name}»: {chosen_name}."
        if chosen_name else f"Назначение начальника подразделения «{department_name}» снято."
    )
    return RedirectResponse("/departments?msg=" + quote(message), 303)


@app.post("/departments/{department_id}/members")
def department_add_member(request: Request, department_id: int,
                          employee_id: int = Form(0), role: str = Form(""),
                          responsibilities: str = Form("")):
    guard_admin(request)
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        if department is None:
            raise HTTPException(404, "Подразделение не найдено")
        if not department.active:
            raise HTTPException(400, "Сначала активируйте подразделение")
        employee = s.get(Employee, employee_id)
        if employee is None or not employee.active:
            raise HTTPException(404, "Активный сотрудник не найден")
        membership = s.query(EmployeeDepartment).filter_by(
            employee_id=employee.id, department_id=department.id
        ).first()
        if membership is None:
            membership = EmployeeDepartment(employee_id=employee.id, department_id=department.id)
            s.add(membership)
        membership.role = role.strip()[:200]
        membership.responsibilities = responsibilities.strip()[:4000]
        s.commit()
        message = f"{employee.name} добавлен в подразделение «{department.name}». Он сохранён в общем справочнике сотрудников."
    return RedirectResponse("/departments?msg=" + quote(message), 303)


@app.post("/departments/{department_id}/members/create")
def department_create_member(request: Request, department_id: int, name: str = Form(...),
                             aliases: str = Form(""), position: str = Form(""),
                             telegram_contact: str = Form(""), role: str = Form(""),
                             responsibilities: str = Form("")):
    guard_admin(request)
    clean_name = name.strip()[:200]
    if not clean_name:
        raise HTTPException(400, "Укажите имя сотрудника")
    contact = telegram_contact.strip()[:200]
    if contact and userbot.ai_mode():
        try:
            contact = userbot.normalize_ref(contact)
        except userbot.RefError as exc:
            raise HTTPException(400, str(exc))
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        if department is None:
            raise HTTPException(404, "Подразделение не найдено")
        if not department.active:
            raise HTTPException(400, "Сначала активируйте подразделение")
        duplicate = next((item for item in s.query(Employee).all()
                          if (item.name or "").strip().casefold() == clean_name.casefold()), None)
        if duplicate is not None:
            membership = s.query(EmployeeDepartment).filter_by(
                employee_id=duplicate.id, department_id=department.id
            ).first()
            if membership is None:
                raise HTTPException(
                    409,
                    f"Сотрудник «{duplicate.name}» уже есть в общем справочнике. Добавьте существующую запись."
                )
            raise HTTPException(409, f"«{duplicate.name}» уже состоит в этом подразделении.")
        employee = Employee(
            name=clean_name, aliases=aliases.strip()[:500], position=position.strip()[:200],
            channel="telegram", telegram_chat_id=contact, active=True,
        )
        s.add(employee)
        s.flush()
        s.add(EmployeeDepartment(
            employee_id=employee.id, department_id=department.id,
            role=role.strip()[:200], responsibilities=responsibilities.strip()[:4000],
        ))
        s.commit()
        message = f"Создан сотрудник «{employee.name}» и добавлен в «{department.name}». Он отображается и в общем справочнике."
    return RedirectResponse("/departments?msg=" + quote(message), 303)


@app.post("/departments/{department_id}/members/{employee_id}/remove")
def department_remove_member(request: Request, department_id: int, employee_id: int):
    guard_admin(request)
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        employee = s.get(Employee, employee_id)
        membership = s.query(EmployeeDepartment).filter_by(
            department_id=department_id, employee_id=employee_id
        ).first()
        if membership is not None:
            s.delete(membership)
            s.commit()
        department_name = department.name if department is not None else "подразделение"
        employee_name = employee.name if employee is not None else "Сотрудник"
    message = f"{employee_name} исключён из «{department_name}». Запись в общем справочнике сотрудников сохранена."
    return RedirectResponse("/departments?msg=" + quote(message), 303)


@app.post("/departments/{department_id}/delete")
def department_delete(request: Request, department_id: int):
    guard_admin(request)
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        if department:
            s.delete(department)
            s.commit()
    return RedirectResponse("/departments", 303)


# ----------------------------------------------------------------------------- meeting reminders
@app.get("/reminders", response_class=HTMLResponse)
def reminders_view(request: Request, edit: int = 0, msg: str = ""):
    guard_admin(request)
    return page(request, "reminders.html", msg=msg, **meeting_alerts.page_data(edit))


@app.post("/reminders")
async def reminder_save(request: Request):
    guard_admin(request)
    f = await request.form()
    values = {k: f.get(k, "") for k in ("title", "details", "meet_time", "lead_minutes", "once_date")}
    values["weekdays"], values["recipients"] = f.getlist("weekdays"), f.getlist("recipients")
    try:
        alert_id = int(f.get("id") or 0)
    except ValueError:
        alert_id = 0
    try:
        meeting_alerts.save(values, alert_id)
    except meeting_alerts.AlertError as exc:
        target = f"/reminders?edit={alert_id}&" if alert_id else "/reminders?"
        return RedirectResponse(target + "msg=" + quote(str(exc)), 303)
    return RedirectResponse("/reminders?msg=" + quote("Сохранено"), 303)


@app.post("/reminders/{alert_id}/toggle")
def reminder_toggle(request: Request, alert_id: int):
    guard_admin(request)
    try:
        on = meeting_alerts.toggle(alert_id)
    except meeting_alerts.AlertError as exc:
        return RedirectResponse("/reminders?msg=" + quote(str(exc)), 303)
    return RedirectResponse("/reminders?msg=" + quote("Включено" if on else "Выключено"), 303)


@app.post("/reminders/{alert_id}/delete")
def reminder_delete(request: Request, alert_id: int):
    guard_admin(request)
    meeting_alerts.delete(alert_id)
    return RedirectResponse("/reminders?msg=" + quote("Удалено"), 303)


@app.post("/employees/{eid}/delete")
def employee_delete(request: Request, eid: int):
    guard_admin(request)
    with SessionLocal() as s:
        for t in s.query(Task).filter_by(assignee_id=eid):
            t.assignee_id = None
        s.query(Task).filter_by(target_employee_id=eid).update({Task.target_employee_id: None}, synchronize_session=False)
        e = s.get(Employee, eid)
        if e:
            s.delete(e)
        s.commit()
    return RedirectResponse("/employees", 303)


# ----------------------------------------------------------------------------- sign-up requests and users
def _register_page(request: Request, status_code: int = 200, **ctx):
    base = {"request": request, "open": accounts.registration_open(), "sent": False, "error": "", "username": "",
            "display_name": ""}
    return tpl.TemplateResponse(request, "register.html", {**base, **ctx}, status_code=status_code)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return tpl.TemplateResponse(request, "privacy.html", {"request": request, "authed": authed(request), "year": dt.date.today().year})


@app.get("/legal", response_class=HTMLResponse)
def legal(request: Request):
    return tpl.TemplateResponse(request, "legal.html", {"request": request, "authed": authed(request), "year": dt.date.today().year})


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if authed(request):
        return RedirectResponse("/app", 303)
    return _register_page(request)


@app.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, username: str = Form(""), display_name: str = Form(""),
                    password: str = Form(""), password2: str = Form(""), consent: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    try:
        if not accounts.allow_registration(ip):
            raise accounts.AccountError("Слишком много попыток с вашего адреса. Попробуйте позже.")
        if password != password2:
            raise accounts.AccountError("Пароли не совпадают.")
        accounts.register(username, password, display_name, consent=bool(consent))
    except accounts.AccountError as e:
        return _register_page(request, 400, error=str(e), username=username.strip(), display_name=display_name.strip())
    return _register_page(request, sent=True)


def _users_redirect(message: str):
    return RedirectResponse("/admin/users?msg=" + quote(message), 303)


@app.get("/admin/users", response_class=HTMLResponse)
def users_view(request: Request, msg: str = ""):
    guard_admin(request)
    return page(request, "users.html", users=accounts.list_users(), registration_open=accounts.registration_open(),
                msg=msg)


@app.post("/admin/users/create")
def users_create(request: Request, username: str = Form(""), display_name: str = Form(""), password: str = Form(""),
                 role: str = Form("user")):
    guard_admin(request)
    try:
        accounts.create_user(username, password, role, current_user(request)["username"], display_name)
    except accounts.AccountError as e:
        return _users_redirect(str(e))
    return _users_redirect(f"{accounts.normalize(username)}: аккаунт создан")


@app.post("/admin/users/registration")
def users_registration(request: Request, open: str = Form("")):
    guard_admin(request)
    settings_store.set_many({"registration_open": open == "1"})
    return _users_redirect("Приём заявок открыт." if open == "1" else "Приём заявок закрыт.")


@app.post("/admin/users/{uid}/{action}")
def users_action(request: Request, uid: int, action: str):
    guard_admin(request)
    try:
        return _users_redirect(accounts.apply(uid, action, current_user(request)["username"]))
    except accounts.AccountError as e:
        return _users_redirect(str(e))


# ----------------------------------------------------------------------------- telegram webhook
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403)
    update = await request.json()
    try:
        pipeline.handle_update(update)
    except Exception:  # never make Telegram retry forever
        log.exception("update failed")
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], 303)
    if request.url.path.startswith("/telegram") or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": exc.detail}, exc.status_code)
    resp = page(request, "error.html", code=exc.status_code, detail=exc.detail)
    resp.status_code = exc.status_code
    return resp
