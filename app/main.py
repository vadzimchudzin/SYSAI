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

from sqlalchemy import func
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import (accounts, agent, bitrix, cases, doctext, memory, transcribe, config, llm, deadlines, meeting_alerts, pipeline, progress, progress_board, render, report_archive,
               settings_store, speaker_id, telegram, userbot, notion)
from .analyze import mmss
from .transcript_sections import group_transcript
from .ui_icons import ICONS
from .db import (Artifact, Case, ConversationEscalation, Delivery, Document, DocumentTransfer, Note, PersonMemory, Skill, Department, Employee, EmployeeDepartment, OrgRelation, EmployeeVoiceprint, Meeting, Recording, ReportVersion, SessionLocal, Task, TelegramChat, User,
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
    notion.start_worker()
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
    notion.stop_worker()
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
    if source and source not in ("web", "microphone", "demo", "api", "notion", "ticnote"):
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
    if source not in ("file", "mic", "ticnote"):
        source = "file"
    return page(request, "new.html", s=settings_store.all_settings(), chats=known_chats(), source=source,
                today=deadlines.today().isoformat(), mic_meetings=mic, audio_ext=sorted(pipeline.AUDIO_EXT))


@app.get("/ticnote/status")
def ticnote_status(request: Request):
    guard(request)
    from . import ticnote_ingest
    data = ticnote_ingest.status()
    for item in data["recent"]:
        item["label"], item["color"] = STATUS.get(item["state"], ("Скачивание записи", "blue") if item["state"] == "downloading"
                                                  else (item["state"], "gray"))
    return JSONResponse(data)


@app.post("/ticnote/scan")
def ticnote_scan_now(request: Request):
    """'Проверить сейчас' on the TicNote tab: poll TicNote without waiting for the next minute."""
    guard(request)
    from . import ticnote_ingest
    if not settings_store.get("ticnote_enabled") or not config.ticnote_appkey():
        raise HTTPException(409, "Включите режим TicNote и сохраните ключ в настройках")

    def job():
        try:
            ticnote_ingest.scan(force=True)
        except Exception:  # noqa: BLE001
            log.exception("TicNote scan failed")
    job() if config.TESTING else pipeline.POOL.submit(job)
    return JSONResponse({"ok": True})


@app.post("/ticnote/imports/{record_id}/{action}")
def ticnote_import_action(request: Request, record_id: str, action: str):
    """Остановить / Повторить for one TicNote recording on the TicNote tab."""
    guard(request)
    from . import ticnote_ingest
    if action not in ("stop", "retry"):
        raise HTTPException(404, "Неизвестное действие")
    done = (ticnote_ingest.stop if action == "stop" else ticnote_ingest.retry)(record_id)
    if not done:
        raise HTTPException(409, "Запись уже в другом состоянии, обновите страницу")
    if action == "retry" and settings_store.get("ticnote_enabled") and config.ticnote_appkey() and not config.TESTING:
        pipeline.POOL.submit(lambda: ticnote_ingest.scan(force=True))
    return JSONResponse({"ok": True})


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
                plan=plan, quiet_at=userbot.local_time(until) if until is not None else "",
                case_by_task=cases.for_meeting(mid))


@app.post("/cases/{case_id}/{action}")
def case_action(request: Request, case_id: int, action: str):
    """The director's decisions on a case: allow the plan or a transfer, repeat a stopped step, cancel."""
    guard_admin(request)
    who = (current_user(request) or {}).get("username") or "панель"
    done = {"approve": cases.approve, "retry": cases.retry_step, "cancel": cases.cancel}.get(action)
    if done is None:
        raise HTTPException(404, "Неизвестное действие")
    if not done(case_id, f"{who} (панель)"):
        raise HTTPException(409, "Действие сейчас невозможно: дело уже в другом состоянии")
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        target = f"/meetings/{case.meeting_id}" if case and case.meeting_id else "/documents"
    return RedirectResponse(target, status_code=303)


def _progress_data(request: Request, scope: str) -> dict:
    with SessionLocal() as s:
        return progress.build(s, scope or "last")


@app.get("/progress", response_class=HTMLResponse)
def progress_view(request: Request, scope: str = "last"):
    """Every case of the assistant and every task after a meeting: what is done, who is awaited, what needs a decision."""
    guard(request)
    me = current_user(request) or {}
    return page(request, "progress.html", data=_progress_data(request, scope), admin=me.get("role") == "superuser")


@app.get("/progress/feed", response_class=HTMLResponse)
def progress_feed(request: Request, scope: str = "last"):
    guard(request)
    me = current_user(request) or {}
    return tpl.TemplateResponse(request, "_progress_feed.html", {"request": request, "data": _progress_data(request, scope),
                                                                 "admin": me.get("role") == "superuser"},
                                headers={"Cache-Control": "private, no-store"})


@app.get("/progress/version")
def progress_version(request: Request, scope: str = "last"):
    guard(request)
    with SessionLocal() as s:
        return JSONResponse({"version": progress.version(s, scope or "last")}, headers={"Cache-Control": "private, no-store"})


@app.post("/progress/cases/{case_id}/{action}")
async def progress_case_action(request: Request, case_id: int, action: str):
    """A person steps into a case: approve, repeat, pause, resume, enter an answer, upload a document, write from the
    assistant's account, hand over to an employee, close."""
    guard_admin(request)
    me = current_user(request) or {}
    who = f"{me.get('name') or me.get('username') or 'руководитель'} (панель)"
    form = await request.form()
    if action == "upload":
        upload = form.get("file")
        data = await upload.read() if upload is not None and hasattr(upload, "read") else b""
        ok = cases.attach_document(case_id, getattr(upload, "filename", "") or "файл",
                                   getattr(upload, "content_type", "") or "", data, who)
    elif action == "answer":
        ok = cases.record_answer(case_id, str(form.get("text") or ""), who)
    elif action == "message":
        ok = cases.send_manual(case_id, str(form.get("text") or ""), who)
    elif action == "handover":
        try:
            employee_id = int(str(form.get("employee_id") or ""))
        except ValueError:
            raise HTTPException(400, "Выберите сотрудника")
        ok = cases.hand_over(case_id, employee_id, who)
    elif action == "close":
        ok = cases.close_manually(case_id, who, str(form.get("note") or ""))
    elif action == "link":
        try:
            ok = cases.link_document(case_id, int(str(form.get("document_id") or "")), who)
        except ValueError:
            raise HTTPException(400, "Выберите документ")
    else:
        simple = {"approve": cases.approve, "retry": cases.retry_step, "pause": cases.pause, "resume": cases.resume,
                  "cancel": cases.cancel}.get(action)
        if simple is None:
            raise HTTPException(404, "Неизвестное действие")
        ok = simple(case_id, who)
    if not ok:
        raise HTTPException(409, "Действие сейчас невозможно: дело в другом состоянии или не хватает данных")
    back = request.headers.get("referer") or "/progress"
    if not back.split("://", 1)[-1].split("/", 1)[-1].startswith(("progress", "meetings")):
        back = "/progress"
    return RedirectResponse(back.split("#")[0] + f"#case-{case_id}", status_code=303)


def _who(request: Request) -> str:
    me = current_user(request) or {}
    return str(me.get("name") or me.get("username") or "панель")[:120]


def _note_view(n, people, departments) -> dict:
    return {"id": n.id, "text": n.text, "quote": n.quote or "", "confidence": n.confidence or "medium",
            "confidence_ru": memory.CONFIDENCE_RU.get(n.confidence or "medium", n.confidence),
            "source": memory.SOURCE_RU.get(n.source, n.source), "meeting_id": n.meeting_id, "at": n.created_at,
            "author": n.author or "", "edited": n.edited_by or "", "kind": n.kind,
            "about": people.get(n.employee_id) or (f"отдел {departments[n.department_id]}" if n.department_id in departments else "компания")}


@app.get("/memory", response_class=HTMLResponse)
def memory_view(request: Request, q: str = "", msg: str = ""):
    """What the AI employee knows: the company, departments, people (cards and notes), with search."""
    guard_admin(request)
    with SessionLocal() as s:
        people = {e.id: e.name for e in s.query(Employee).all()}
        departments = {d.id: d.name for d in s.query(Department).all()}
        company_notes = [_note_view(n, people, departments) for n in
                         s.query(Note).filter(Note.employee_id.is_(None), Note.archived.is_(False),
                                              Note.kind.in_(("company", "fact"))).order_by(Note.id.desc()).limit(200)]
        depts = [{"id": d.id, "name": d.name, "description": d.description or "", "regulations": d.regulations or "",
                  "members": [f"{m.employee.name}" + (f" — {m.role}" if m.role else "") for m in d.memberships
                              if m.employee is not None and m.employee.active]}
                 for d in s.query(Department).filter(Department.active.is_(True)).order_by(Department.name)]
        counts = dict(s.query(Note.employee_id, func.count(Note.id)).filter(Note.archived.is_(False),
                                                                            Note.employee_id.isnot(None))
                      .group_by(Note.employee_id).all())
        staff = [{"id": e.id, "name": e.name, "position": e.position or "", "notes": counts.get(e.id, 0),
                  "style": bool(getattr(e, "communication_style", ""))}
                 for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name)]
        results = []
        if q.strip():
            for h in memory.search(q, k=15, s=s):
                results.append({"type": memory.TYPE_RU[h["type"]], "title": h["title"], "text": h["text"][:400],
                                "url": h.get("url") or "/memory", "score": h["score"], "meta": h.get("meta") or {}})
    return page(request, "memory.html", q=q, msg=msg, results=results, company_notes=company_notes, departments=depts,
                staff=staff, profile=settings_store.get("company_profile") or "",
                autolearn=bool(settings_store.get("memory_autolearn")))


@app.post("/memory/company")
async def memory_company_save(request: Request):
    guard_admin(request)
    f = await request.form()
    settings_store.set_many({"company_profile": str(f.get("company_profile") or "")[:20000],
                             "memory_autolearn": f.get("memory_autolearn") == "on"})
    return RedirectResponse("/memory?msg=saved", 303)


@app.post("/memory/departments/{did}")
async def memory_department_save(request: Request, did: int):
    guard_admin(request)
    f = await request.form()
    with SessionLocal() as s:
        d = s.get(Department, did)
        if d is None:
            raise HTTPException(404, "Отдел не найден")
        d.description = str(f.get("description") or "").strip()[:1000]
        d.regulations = str(f.get("regulations") or "").strip()[:10000]
        s.commit()
    return RedirectResponse("/memory?msg=saved#departments", 303)


def _back(request: Request, default: str) -> str:
    back = request.headers.get("referer") or default
    path = "/" + back.split("://", 1)[-1].split("/", 1)[-1] if "://" in back else back
    return path if path.startswith(("/memory", "/skills")) else default


@app.post("/memory/notes")
async def memory_note_add(request: Request):
    guard_admin(request)
    f = await request.form()

    def num(key):
        value = str(f.get(key) or "").strip()
        return int(value) if value.isdigit() else None
    try:
        memory.add_note(text=str(f.get("text") or ""), employee_id=num("employee_id"), department_id=num("department_id"),
                        confidence=str(f.get("confidence") or "high"), author=_who(request))
    except ValueError as exc:
        raise HTTPException(400, f"Заметка не сохранена: {exc}")
    return RedirectResponse(_back(request, "/memory"), 303)


@app.post("/memory/notes/{nid}")
async def memory_note_edit(request: Request, nid: int):
    guard_admin(request)
    f = await request.form()
    action = str(f.get("action") or "save")
    ok = memory.edit_note(nid, archived=True, by=_who(request)) if action == "archive" else \
        memory.edit_note(nid, text=str(f.get("text") or ""), confidence=str(f.get("confidence") or ""), by=_who(request))
    if not ok:
        raise HTTPException(400, "Заметка не изменена")
    return RedirectResponse(_back(request, "/memory"), 303)


@app.get("/memory/people/{eid}", response_class=HTMLResponse)
def memory_person(request: Request, eid: int):
    """A person's card: role, duties, how to talk to them, and every note with its source."""
    guard_admin(request)
    with SessionLocal() as s:
        e = s.get(Employee, eid)
        if e is None:
            raise HTTPException(404, "Сотрудник не найден")
        people = {x.id: x.name for x in s.query(Employee).all()}
        departments = {d.id: d.name for d in s.query(Department).all()}
        notes = [_note_view(n, people, departments) for n in
                 s.query(Note).filter(Note.employee_id == eid, Note.archived.is_(False)).order_by(Note.id.desc()).limit(300)]
        archived = s.query(Note).filter(Note.employee_id == eid, Note.archived.is_(True)).count()
        summaries = [m.summary for m in s.query(PersonMemory).filter_by(employee_id=eid).all() if m.summary]
        card = {"id": e.id, "name": e.name, "aliases": e.aliases or "", "position": e.position or "",
                "departments": [f"{m.department.name}" + (f" — {m.role}" if m.role else "") for m in e.department_memberships
                                if m.department is not None],
                "responsibilities": e.responsibilities or "", "competencies": e.competencies or "",
                "context": e.context or "", "communication_style": getattr(e, "communication_style", "") or "",
                "telegram": e.telegram_chat_id or ""}
        documents = [{"id": d.id, "filename": d.filename, "at": d.created_at} for d in
                     s.query(Document).filter_by(employee_id=eid).order_by(Document.id.desc()).limit(20)]
    return page(request, "memory_person.html", card=card, notes=notes, archived=archived, summaries=summaries,
                documents=documents)


@app.post("/memory/people/{eid}")
async def memory_person_save(request: Request, eid: int):
    guard_admin(request)
    f = await request.form()
    with SessionLocal() as s:
        e = s.get(Employee, eid)
        if e is None:
            raise HTTPException(404, "Сотрудник не найден")
        for key, limit in (("responsibilities", 4000), ("competencies", 4000), ("context", 4000), ("communication_style", 2000)):
            if key in f:
                setattr(e, key, str(f.get(key) or "").strip()[:limit])
        s.commit()
    agent.invalidate_roster()
    return RedirectResponse(f"/memory/people/{eid}?saved=1", 303)


@app.post("/memory/people/{eid}/forget-summary")
def memory_person_forget(request: Request, eid: int):
    """The assistant's own rolling summary about the person is erased (the person asked, or it is wrong)."""
    guard_admin(request)
    with SessionLocal() as s:
        for m in s.query(PersonMemory).filter_by(employee_id=eid).all():
            m.summary = ""
        s.commit()
    return RedirectResponse(f"/memory/people/{eid}", 303)


@app.get("/skills", response_class=HTMLResponse)
def skills_view(request: Request, edit: int = 0):
    guard_admin(request)
    with SessionLocal() as s:
        rows = [{"id": k.id, "title": k.title, "kind": k.kind, "when": k.when_to_use or "", "body": k.body, "active": k.active,
                 "uses": k.uses, "updated": k.updated_at, "by": k.updated_by or ""}
                for k in s.query(Skill).order_by(Skill.active.desc(), Skill.title)]
    return page(request, "skills.html", skills=rows, edit=edit)


@app.post("/skills")
async def skill_save(request: Request):
    guard_admin(request)
    f = await request.form()
    title, body = str(f.get("title") or "").strip()[:200], str(f.get("body") or "").strip()[:8000]
    if not title or not body:
        raise HTTPException(400, "Нужны название и текст навыка")
    kind = f.get("kind") if f.get("kind") in ("scenario", "template", "tone") else "scenario"
    with SessionLocal() as s:
        sid = str(f.get("id") or "").strip()
        k = s.get(Skill, int(sid)) if sid.isdigit() else None
        if k is None:
            k = Skill()
            s.add(k)
        k.title, k.body, k.kind = title, body, kind
        k.when_to_use = str(f.get("when_to_use") or "").strip()[:2000]
        k.active, k.updated_by = f.get("active", "on") == "on", _who(request)
        s.commit()
    return RedirectResponse("/skills", 303)


@app.post("/skills/{sid}/{action}")
def skill_action(request: Request, sid: int, action: str):
    guard_admin(request)
    with SessionLocal() as s:
        k = s.get(Skill, sid)
        if k is None:
            raise HTTPException(404, "Навык не найден")
        if action == "toggle":
            k.active = not k.active
        elif action == "delete":
            s.delete(k)
        else:
            raise HTTPException(404, "Неизвестное действие")
        s.commit()
    return RedirectResponse("/skills", 303)


@app.get("/documents/{doc_id}/view", response_class=HTMLResponse)
def document_view(request: Request, doc_id: int, q: str = ""):
    """A document's card: what it is, its text, which case it belongs to, where it was passed on, questions about it."""
    guard_admin(request)
    with SessionLocal() as s:
        d = s.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "Документ не найден")
        case = s.get(Case, d.case_id) if d.case_id else None
        transfers = [{"to": (s.get(Employee, t.to_employee_id).name if t.to_employee_id and s.get(Employee, t.to_employee_id)
                             else t.to_ref), "status": t.status, "at": t.sent_at or t.created_at, "by": t.approved_by or ""}
                     for t in s.query(DocumentTransfer).filter_by(document_id=d.id).order_by(DocumentTransfer.id)]
        doc = {"id": d.id, "filename": d.filename, "kind": d.kind, "size": d.size, "sha": d.sha256, "at": d.created_at,
               "sender": d.employee.name if d.employee else d.chat_id, "caption": d.caption or "", "sensitive": d.sensitive,
               "status": d.text_status or "pending", "summary": d.summary or "", "text": (d.text or "")[:20000],
               "truncated": len(d.text or "") > 20000,
               "case": {"id": case.id, "title": case.title, "meeting_id": case.meeting_id} if case else None}
    answer = None
    if q.strip():
        try:
            answer = memory.answer_about_document(doc_id, q.strip()[:500])
        except llm.LLMError as exc:
            answer = {"answer": f"Модель недоступна: {exc}", "pieces": []}
    return page(request, "document.html", doc=doc, transfers=transfers, q=q, answer=answer)


@app.post("/documents/{doc_id}/reread")
def document_reread(request: Request, doc_id: int):
    guard_admin(request)
    with SessionLocal() as s:
        d = s.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "Документ не найден")
        d.text_status = "pending"
        s.commit()
    doctext.process(doc_id) if config.TESTING else pipeline.POOL.submit(doctext.process, doc_id)
    return RedirectResponse(f"/documents/{doc_id}/view", 303)


@app.get("/documents", response_class=HTMLResponse)
def documents_view(request: Request, q: str = ""):
    """The library of files people sent to the AI employee, with the case and where each file was passed on."""
    guard_admin(request)
    with SessionLocal() as s:
        query = s.query(Document).order_by(Document.id.desc())
        found = query.limit(200).all()
        if q.strip():  # case-insensitive in Russian on every database (SQLite's LIKE folds only Latin letters)
            needle = q.strip().casefold()
            found = [d for d in query.limit(2000).all()
                     if needle in f"{d.filename} {d.caption} {d.text or ''} {d.summary or ''}".casefold()][:200]
        rows = []
        for d in found:
            case = s.get(Case, d.case_id) if d.case_id else None
            transfers = s.query(DocumentTransfer).filter_by(document_id=d.id).order_by(DocumentTransfer.id).all()
            rows.append({"id": d.id, "filename": d.filename, "kind": d.kind, "size": d.size, "sha": d.sha256[:12],
                         "sensitive": d.sensitive, "caption": d.caption, "at": d.created_at,
                         "summary": d.summary or "", "status": d.text_status or "pending",
                         "sender": d.employee.name if d.employee else d.chat_id,
                         "case": {"id": case.id, "title": case.title, "meeting_id": case.meeting_id} if case else None,
                         "transfers": [{"to": (s.get(Employee, t.to_employee_id).name if t.to_employee_id and
                                               s.get(Employee, t.to_employee_id) else t.to_ref), "status": t.status,
                                        "at": t.sent_at} for t in transfers]})
    return page(request, "documents.html", docs=rows, q=q)


@app.get("/documents/{doc_id}")
def document_download(request: Request, doc_id: int):
    guard_admin(request)
    with SessionLocal() as s:
        d = s.get(Document, doc_id)
        if d is None or d.data is None:
            raise HTTPException(404, "Документ не найден")
        data, mime, name = bytes(d.data), d.mime or "application/octet-stream", d.filename or f"document-{doc_id}"
    return Response(data, media_type=mime, headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})


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


@app.post("/meetings/{mid}/cancel-processing")
def meeting_cancel_processing(request: Request, mid: int):
    guard(request)
    if not pipeline.cancel_processing(mid):
        raise HTTPException(409, "Совещание уже не обрабатывается")
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
           "telegram_bot_token": bool(config.TELEGRAM_BOT_TOKEN), "bitrix_webhook_url": bool(config.BITRIX_WEBHOOK_URL),
           "ticnote_appkey": bool(config.TICNOTE_APPKEY)}
    for k in settings_store.SECRET_KEYS + settings_store.HIDDEN_KEYS:
        st.pop(k, None)  # secrets never go back to the browser, only a mask
    ai = bool(st.get("ai_employee"))
    from . import digest
    return page(request, "settings.html", s=st, chats=[] if ai else known_chats(), msg=msg, masked=masked, env=env,
                digest_frequencies=digest.FREQUENCY_RU, weekdays=digest.WEEKDAYS_RU,
                account=userbot.status() if ai else None, persona_default=voice_default_persona(), spent=round(llm.spent_today(), 2),
                notion=notion.connection_status())


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
    # Gemini hears words and voices in one pass (best on the speech benchmark, see scripts/speech_benchmark.py).
    known = {m["id"] for m in transcription_models}
    for model in models:
        if model["audio_input"] and transcribe.chat_model(model["id"]) and model["id"] not in known:
            transcription_models.append({"id": model["id"], "name": model["name"] + " — слова и голоса одним проходом",
                                         "prompt_price": model["prompt_price"], "completion_price": model["completion_price"]})
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
              "bitrix_enabled", "ai_employee", "ticnote_enabled"):
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
    if "case_autonomy" in f:
        if f["case_autonomy"] not in cases.AUTONOMY:
            raise HTTPException(400, "Выберите уровень самостоятельности из списка")
        updates["case_autonomy"] = f["case_autonomy"]
    for key, low, high in (("case_reminder_hours", 0.5, 168), ("case_max_reminders", 0, 10), ("case_escalate_hours", 1, 720)):
        if key in f:
            try:
                value = float(str(f[key]).replace(",", "."))
            except ValueError:
                raise HTTPException(400, "Сроки дел укажите числом")
            if not low <= value <= high:
                raise HTTPException(400, f"Значение вне диапазона {low}–{high}")
            updates[key] = int(value) if key == "case_max_reminders" else value
    if "sensitive_keywords" in f:
        updates["sensitive_keywords"] = str(f["sensitive_keywords"])[:1000]
    # Secrets are never echoed in hidden inputs. A value alone (including a
    # legacy hidden value) is not an instruction to replace a credential.
    # Require the explicit action selected in the visible credential control.
    if "daily_budget_usd" in f:
        try:
            updates["daily_budget_usd"] = max(0.0, float(str(f["daily_budget_usd"]).replace(",", ".") or 0))
        except ValueError:
            raise HTTPException(400, "Лимит расходов укажите числом в долларах")
    if "digest_frequency" in f:
        from . import digest
        if f["digest_frequency"] not in digest.FREQUENCIES:
            raise HTTPException(400, "Сводка: выберите «каждый день», «раз в неделю» или «не присылать»")
        updates["digest_frequency"] = f["digest_frequency"]
    for k, top, label in (("digest_hour", 23, "час сводки от 0 до 23"), ("digest_weekday", 6, "день недели сводки")):
        if k in f:
            try:
                value = int(str(f[k]).strip())
            except ValueError:
                raise HTTPException(400, f"Укажите {label}") from None
            if not 0 <= value <= top:
                raise HTTPException(400, f"Укажите {label}")
            updates[k] = value
    for k in ("quiet_from_hour", "quiet_to_hour"):
        if k in f:
            try:
                hour = int(str(f[k]).strip())
            except ValueError:
                raise HTTPException(400, "Тихие часы: целые часы от 0 до 23")
            if not 0 <= hour <= 23:
                raise HTTPException(400, "Тихие часы: целые часы от 0 до 23")
            updates[k] = hour
    if "notion_parent_id" in f:
        raw_parent = str(f.get("notion_parent_id") or "").strip()
        normalized_parent = notion.normalise_parent_setting(raw_parent) if raw_parent else ""
        if raw_parent and not normalized_parent:
            raise HTTPException(400, "Укажите корректный URL или UUID страницы/базы Notion")
        updates["notion_parent_id"] = normalized_parent
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
    if updates.get("ticnote_enabled") and not settings_store.get("ticnote_enabled"):
        # Mode switched on: take only what reaches TicNote from now on, not the whole history.
        updates["ticnote_enabled_at"] = int(time.time() * 1000)
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


@app.post("/settings/test-digest")
def test_digest(request: Request):
    """«Прислать сводку сейчас»: the same text the reviewers get on schedule, without moving the schedule."""
    guard_admin(request)
    from . import digest
    if not digest.recipients():
        return RedirectResponse("/settings?msg=" + quote("Сводка: нет проверяющих — назначьте их в карточке сотрудника"), 303)
    got = digest.send(mark=False)
    return RedirectResponse("/settings?msg=" + quote(f"Сводка отправлена проверяющим: {got} из {len(digest.recipients())}"), 303)


@app.post("/settings/test-ticnote")
def test_ticnote(request: Request):
    guard_admin(request)
    from . import ticnote
    key = config.ticnote_appkey()
    if not key:
        return RedirectResponse("/settings?msg=" + quote("TicNote: сначала сохраните AppKey"), 303)
    try:
        return RedirectResponse("/settings?msg=" + quote("TicNote: " + ticnote.check_key(key)), 303)
    except ticnote.TicNoteError as e:
        return RedirectResponse("/settings?msg=" + quote(f"TicNote: {str(e)[:200]}"), 303)


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



# ----------------------------------------------------------------------------- Notion / TicNote export
@app.get("/settings/notion/connect")
def notion_connect(request: Request):
    guard_admin(request)
    state = secrets.token_urlsafe(32)
    try:
        callback_uri = notion.redirect_uri()
        url = notion.authorization_url(state, callback_uri)
    except notion.NotionError as exc:
        return RedirectResponse("/settings?msg=" + quote(str(exc)[:220]), 303)
    request.session["notion_oauth_state"] = state
    request.session["notion_oauth_callback_uri"] = callback_uri
    return RedirectResponse(url, 303)


@app.get("/settings/notion/callback")
def notion_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    guard_admin(request)
    expected = str(request.session.pop("notion_oauth_state", "") or "")
    callback_uri = str(request.session.pop("notion_oauth_callback_uri", "") or "")
    if error:
        return RedirectResponse("/settings?msg=" + quote("Notion отклонил авторизацию: " + error[:100]), 303)
    if not expected or not state or not hmac.compare_digest(expected, state):
        return RedirectResponse("/settings?msg=" + quote("Не прошла проверка state OAuth Notion. Подключите аккаунт заново."), 303)
    try:
        notion.save_connection(notion.exchange_code(code, callback_uri or notion.redirect_uri()))
    except notion.NotionError as exc:
        log.warning("Notion OAuth failed: %s", str(exc)[:180])
        return RedirectResponse("/settings?msg=" + quote("Notion: " + str(exc)[:200]), 303)
    return RedirectResponse("/settings?msg=" + quote("Рабочее пространство Notion подключено."), 303)


@app.post("/settings/notion/sync")
def notion_sync(request: Request):
    guard_admin(request)
    try:
        result = notion.sync_existing_pages()
        summary = (f"Импорт Notion: найдено {result['found']}, в очереди {result['queued']}, "
                   f"уже импортировано {result['duplicate']}, вне выбранной страницы {result['ignored']}, "
                   f"ошибок {result['failed']}.")
    except notion.NotionError as exc:
        summary = "Notion: " + str(exc)[:220]
    return RedirectResponse("/settings?msg=" + quote(summary), 303)


@app.post("/settings/notion/disconnect")
def notion_disconnect(request: Request):
    guard_admin(request)
    notion.disconnect()
    return RedirectResponse("/settings?msg=" + quote("OAuth-токены Notion удалены. Настройка webhook сохранена."), 303)


@app.post("/settings/notion/reset-webhook-token")
def notion_reset_webhook_token(request: Request):
    guard_admin(request)
    notion.reset_webhook_verification_token()
    return RedirectResponse("/settings?msg=" + quote("Verification token очищен. Повторно выполните проверку webhook в Notion."), 303)


@app.get("/settings/notion/webhook-token")
def notion_webhook_token(request: Request):
    guard_admin(request)
    token = notion.webhook_verification_token()
    if not token:
        raise HTTPException(404, "Notion пока не отправил verification_token. Запустите проверку webhook.")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(token, headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"})


@app.post("/webhooks/notion")
async def notion_webhook(request: Request):
    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise HTTPException(413, "Слишком большой Notion webhook.")
    try:
        result = notion.receive_webhook(raw_body, request.headers.get("x-notion-signature", ""))
    except notion.WebhookError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    return JSONResponse(result)


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
    from . import access
    rights = {e.id: agent.rights_of(e) for e in emps}
    return page(request, "employees.html", employees=emps, chats=chats, linked=linked, rights=rights,
                rights_ru=agent.RIGHTS_RU, access_levels=access.LEVELS, access_hints=access.LEVEL_HINT,
                strong_rights=access.STRONG, departments=departments, positions=settings_store.POSITIONS, edit=edit, add=bool(add),
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
        from . import access
        level = str(form.get("access_level") or "").strip()
        if level and (level not in access.LEVELS or access.level_of(e) == "director"):
            level = ""  # unknown value, or the director (set in the settings, not in the card)
        if level and level != "employee" and not e.telegram_chat_id:
            s.rollback()
            raise HTTPException(400, "Сначала укажите Telegram сотрудника: права привязываются к его аккаунту")
        s.commit()
        saved_employee_id = e.id
    if level and level != access.level_of(e):
        access.set_level(e, level)  # after the card is saved: the settings are written in their own transaction
    agent.invalidate_roster()
    target = (
        f"/employees?edit={saved_employee_id}#employee-{saved_employee_id}"
        if id else f"/employees#employee-{saved_employee_id}"
    )
    return RedirectResponse(target, 303)


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
def departments_view(request: Request, edit: int = 0, msg: str = "",
                     view: str = "list", open_add: int = 0):
    guard_admin(request)
    view = "map" if view == "map" else "list"
    with SessionLocal() as s:
        departments = s.query(Department).order_by(Department.active.desc(), Department.name).all()
        employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name, Employee.id).all()
        from . import access
        access_levels = {employee.id: access.level_of(employee) for employee in employees}

        department_positions = {}
        department_heads = {}
        for index, department in enumerate(departments):
            department_positions[department.id] = (
                max(0, min(20000, round(float(department.map_x)))) if department.map_x is not None
                else 480 + (index // 5) * 760,
                max(0, min(20000, round(float(department.map_y)))) if department.map_y is not None
                else 120 + (index % 5) * 315,
            )
            department_heads[department.id] = next(
                (membership for membership in department.memberships
                 if membership.is_primary and membership.employee is not None and membership.employee.active),
                None,
            )

        visible_department_ids = {d.id for d in departments}
        employee_memberships = {}
        for employee in employees:
            employee_memberships[employee.id] = [
                membership for membership in employee.department_memberships
                if membership.department_id in visible_department_ids
                and membership.department is not None
            ]

        available_employees_by_department = {}
        for department in departments:
            assigned = {m.employee_id for m in department.memberships}
            available_employees_by_department[department.id] = [
                employee for employee in employees if employee.id not in assigned
            ]

        graph_relations = s.query(OrgRelation).all()
        map_departments = []
        furthest_right, furthest_bottom = 1400, 1100
        for department in departments:
            x, y = department_positions[department.id]
            members = [
                m for m in department.memberships
                if m.employee is not None and m.employee.active
            ]
            map_departments.append({
                "id": department.id, "name": department.name,
                "description": department.description or "",
                "active": bool(department.active),
                "members": members,
                "head_name": department_heads[department.id].employee.name
                    if department_heads[department.id] else "",
                "map_x": x, "map_y": y,
            })
            furthest_right = max(furthest_right, x + 340)
            furthest_bottom = max(furthest_bottom, y + 190)

        # A person is rendered exactly once, regardless of how many departments
        # they belong to or lead. Legacy membership coordinates are used only as
        # a one-time fallback until the employee is next moved on the canvas.
        map_employees = []
        map_edges = []
        slots_by_department = {}
        for index, employee in enumerate(employees):
            memberships = employee_memberships.get(employee.id, [])
            first_membership = next(
                (m for m in memberships if m.map_x is not None and m.map_y is not None),
                None,
            )
            anchor = next((m for m in memberships if m.is_primary), memberships[0] if memberships else None)
            if employee.map_x is not None and employee.map_y is not None:
                employee_x, employee_y = float(employee.map_x), float(employee.map_y)
            elif first_membership is not None:
                employee_x, employee_y = float(first_membership.map_x), float(first_membership.map_y)
            elif anchor is not None:
                dept_x, dept_y = department_positions[anchor.department_id]
                slot = slots_by_department.get(anchor.department_id, 0)
                slots_by_department[anchor.department_id] = slot + 1
                employee_x = dept_x + 390 + (slot // 7) * 330
                employee_y = dept_y + 12 + (slot % 7) * 108
            else:
                employee_x = 420 + (index // 8) * 330
                employee_y = 980 + (index % 8) * 110

            employee_x = max(0, min(20000, round(employee_x)))
            employee_y = max(0, min(20000, round(employee_y)))
            position = employee.position or ""
            manager_departments = [
                m.department.name for m in memberships
                if m.is_primary and m.department is not None
            ]
            department_names = [
                m.department.name for m in memberships if m.department is not None
            ]
            department_roles = [
                f"{m.department.name}: {m.role}" if m.role else m.department.name
                for m in memberships if m.department is not None
            ]
            map_employees.append({
                "id": employee.id, "name": employee.name, "position": position,
                "access_level": access_levels.get(employee.id, "employee"),
                "is_director": " ".join((employee.name or "").split()).casefold()
                    == "корнюшко сергей михайлович",
                "map_x": employee_x, "map_y": employee_y,
                "department_summary": ", ".join(department_names),
                "role_summary": " · ".join(department_roles),
                "manager_summary": ", ".join(manager_departments),
                "department_ids": [m.department_id for m in memberships],
            })
            furthest_right = max(furthest_right, employee_x + 330)
            furthest_bottom = max(furthest_bottom, employee_y + 150)

            for membership in memberships:
                if membership.is_primary:
                    map_edges.append({
                        "source": f"employee:{employee.id}",
                        "target": f"department:{membership.department_id}",
                        "kind": "manager", "label": membership.role or "Руководитель",
                    })
                else:
                    map_edges.append({
                        "source": f"department:{membership.department_id}",
                        "target": f"employee:{employee.id}",
                        "kind": "membership", "label": membership.role or "Сотрудник",
                    })

        valid_employee_ids = {e.id for e in employees}
        valid_department_ids = {d.id for d in departments}
        employee_parent_ids = set()
        company_linked_employee_ids = set()
        for relation in graph_relations:
            if relation.target_type != "employee" or relation.target_id not in valid_employee_ids:
                continue
            if relation.source_type == "employee" and relation.source_id not in valid_employee_ids:
                continue
            if relation.source_type == "department" and relation.source_id not in valid_department_ids:
                continue
            if relation.source_type not in {"company", "employee", "department"}:
                continue
            source_key = f"{relation.source_type}:{relation.source_id}"
            target_key = f"employee:{relation.target_id}"
            map_edges.append({
                "source": source_key, "target": target_key,
                "kind": "reporting", "label": relation.label or relation.relation_type,
                "relation_id": relation.id,
            })
            employee_parent_ids.add(relation.target_id)
            if relation.source_type == "company":
                company_linked_employee_ids.add(relation.target_id)

        # The designated director is the initial root link until the graph is
        # explicitly configured in the constructor. This does not grant access.
        director = next(
            (e for e in employees
             if " ".join((e.name or "").split()).casefold() == "корнюшко сергей михайлович"),
            None,
        )
        if director is not None and director.id not in company_linked_employee_ids:
            map_edges.append({
                "source": "company:1", "target": f"employee:{director.id}",
                "kind": "director", "label": "Директор",
            })
            employee_parent_ids.add(director.id)

        # Staff without a department or explicit reporting line remain visible
        # as direct company reports; no active employee is dropped from the map.
        for employee in employees:
            if not employee_memberships.get(employee.id) and employee.id not in employee_parent_ids:
                map_edges.append({
                    "source": "company:1", "target": f"employee:{employee.id}",
                    "kind": "company-member", "label": "Прямое подчинение",
                })

        map_root_x = max(0, min(20000, round(float(settings_store.get("org_map_root_x") or 85))))
        map_root_y = max(0, min(20000, round(float(settings_store.get("org_map_root_y") or 735))))
        map_board_width = max(3200, furthest_right + 520, map_root_x + 750)
        map_board_height = max(2200, furthest_bottom + 420, map_root_y + 600)

    return page(
        request, "departments.html", departments=departments, edit=edit, msg=msg, view=view,
        open_add=open_add, map_departments=map_departments, map_employees=map_employees,
        map_unassigned=[e for e in map_employees if not e["department_ids"]],
        map_edges=map_edges, map_board_width=map_board_width, map_board_height=map_board_height,
        map_root_x=map_root_x, map_root_y=map_root_y, map_member_count=len(map_employees),
        available_employees_by_department=available_employees_by_department,
        department_heads=department_heads,
    )


@app.post("/departments/map/position")
async def department_map_position(request: Request):
    """Save the free position of one graph node without changing its relationships."""
    guard_admin(request)
    try:
        data = await request.json()
        node_type = str(data.get("node_type") or "")
        map_x, map_y = float(data.get("map_x")), float(data.get("map_y"))
        if not (0 <= map_x <= 20000 and 0 <= map_y <= 20000):
            raise ValueError("coordinates out of bounds")
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Некорректные координаты карточки")

    if node_type == "company":
        settings_store.set_many({"org_map_root_x": map_x, "org_map_root_y": map_y})
        return JSONResponse({"ok": True})
    with SessionLocal() as s:
        if node_type == "department":
            try:
                node_id = int(data.get("department_id") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "Некорректное подразделение")
            node = s.get(Department, node_id)
        elif node_type == "employee":
            try:
                node_id = int(data.get("employee_id") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "Некорректный сотрудник")
            node = s.get(Employee, node_id)
            if node is not None and not node.active:
                raise HTTPException(404, "Активный сотрудник не найден")
        else:
            raise HTTPException(400, "Неизвестный тип карточки")
        if node is None:
            raise HTTPException(404, "Карточка не найдена")
        node.map_x, node.map_y = map_x, map_y
        s.commit()
    return JSONResponse({"ok": True})


@app.post("/departments/map/assign")
async def department_map_assign(request: Request):
    """Add/update an employee's membership in a department without removing other memberships."""
    guard_admin(request)
    try:
        data = await request.json()
        employee_id = int(data.get("employee_id") or 0)
        department_id = int(data.get("department_id") or 0)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Некорректный сотрудник или подразделение")
    role = str(data.get("role") or "").strip()[:200]
    responsibilities = str(data.get("responsibilities") or "").strip()[:4000]
    is_primary = bool(data.get("is_primary"))
    try:
        map_x = float(data.get("map_x")) if data.get("map_x") is not None else None
        map_y = float(data.get("map_y")) if data.get("map_y") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "Некорректные координаты карточки")
    with SessionLocal() as s:
        employee = s.get(Employee, employee_id)
        department = s.get(Department, department_id)
        if employee is None or not employee.active:
            raise HTTPException(404, "Активный сотрудник не найден")
        if department is None or not department.active:
            raise HTTPException(400, "Подразделение не найдено или неактивно")
        membership = s.query(EmployeeDepartment).filter_by(
            employee_id=employee_id, department_id=department_id
        ).one_or_none()
        if membership is None:
            membership = EmployeeDepartment(employee_id=employee_id, department_id=department_id)
            s.add(membership)
            s.flush()
        if is_primary:
            s.query(EmployeeDepartment).filter(
                EmployeeDepartment.department_id == department_id,
                EmployeeDepartment.id != membership.id,
            ).update({EmployeeDepartment.is_primary: False}, synchronize_session=False)
        membership.role = role
        membership.responsibilities = responsibilities
        membership.is_primary = is_primary
        if map_x is not None and map_y is not None:
            employee.map_x, employee.map_y = map_x, map_y
        s.commit()
    agent.invalidate_roster()
    return JSONResponse({"ok": True})


@app.post("/departments/map/relation")
async def department_map_relation(request: Request):
    """Create or update a directed reporting line between people or the company and a person."""
    guard_admin(request)
    try:
        data = await request.json()
        source_type = str(data.get("source_type") or "")
        source_id = int(data.get("source_id") or 0)
        target_id = int(data.get("target_id") or 0)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Некорректные параметры связи")
    label = str(data.get("label") or "").strip()[:200]
    relation_type = str(data.get("relation_type") or "reports_to").strip()[:40]
    if source_type not in {"company", "employee"} or target_id <= 0:
        raise HTTPException(400, "Поддерживаются связи от SYSAT или сотрудника к сотруднику")
    if source_type == "company" and source_id != 1:
        raise HTTPException(400, "Некорректный узел компании")
    if source_type == "employee" and source_id <= 0:
        raise HTTPException(400, "Некорректный руководитель")
    if source_type == "employee" and source_id == target_id:
        raise HTTPException(400, "Нельзя связать сотрудника с самим собой")

    with SessionLocal() as s:
        target = s.get(Employee, target_id)
        if target is None or not target.active:
            raise HTTPException(404, "Подчинённый сотрудник не найден")
        if source_type == "employee":
            parent = s.get(Employee, source_id)
            if parent is None or not parent.active:
                raise HTTPException(404, "Руководитель не найден")
            # Prevent circular reporting chains.
            children_by_parent = {}
            for edge in s.query(OrgRelation).filter_by(
                source_type="employee", target_type="employee"
            ).all():
                children_by_parent.setdefault(edge.source_id, set()).add(edge.target_id)
            pending, visited = [target_id], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                if current == source_id:
                    raise HTTPException(409, "Эта связь создаст замкнутый цикл подчинения")
                pending.extend(children_by_parent.get(current, ()))
        relation = s.query(OrgRelation).filter_by(
            source_type=source_type, source_id=source_id,
            target_type="employee", target_id=target_id, relation_type=relation_type,
        ).one_or_none()
        if relation is None:
            relation = OrgRelation(
                source_type=source_type, source_id=source_id,
                target_type="employee", target_id=target_id,
                relation_type=relation_type,
            )
            s.add(relation)
        relation.label = label or ("Директор" if source_type == "company" else "Подчинение")
        s.commit()
    return JSONResponse({"ok": True})


@app.post("/departments/map/relation/{relation_id}/delete")
def department_map_relation_delete(request: Request, relation_id: int):
    guard_admin(request)
    with SessionLocal() as s:
        relation = s.get(OrgRelation, relation_id)
        if relation is None:
            raise HTTPException(404, "Связь не найдена")
        s.delete(relation)
        s.commit()
    return JSONResponse({"ok": True})


@app.post("/departments")
def department_save(request: Request, id: int = Form(0), name: str = Form(...),
                    description: str = Form(""), active: str = Form(""), regulations: str = Form(None),
                    return_view: str = Form("list")):
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
        if regulations is not None:
            department.regulations = regulations.strip()[:10000]
        department.active = bool(active) if id else True
        s.add(department)
        s.commit()
    safe_view = "map" if return_view == "map" else "list"
    if id:
        target = f"/departments?view={safe_view}#department-{department.id}"
    else:
        target = f"/departments?view={safe_view}&create=1#department-create-form"
    return RedirectResponse(target, 303)


@app.post("/departments/{department_id}/head")
def department_set_head(request: Request, department_id: int, employee_id: int = Form(0),
                        view: str = Form("list")):
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
    safe_view = "map" if view == "map" else "list"
    return RedirectResponse(f"/departments?view={safe_view}&msg=" + quote(message) + f"#department-{department_id}", 303)


@app.post("/departments/{department_id}/members")
def department_add_member(request: Request, department_id: int,
                          employee_id: int = Form(0), role: str = Form(""),
                          responsibilities: str = Form(""), view: str = Form("list")):
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
    safe_view = "map" if view == "map" else "list"
    return RedirectResponse(
        f"/departments?view={safe_view}&open_add={department_id}&msg=" + quote(message)
        + f"#department-{department_id}-add", 303,
    )


@app.post("/departments/{department_id}/members/create")
async def department_create_member(
    request: Request, department_id: int, name: str = Form(...),
    aliases: str = Form(""), position: str = Form(""), competencies: str = Form(""),
    employee_responsibilities: str = Form(""), context: str = Form(""),
    telegram_contact: str = Form(""), bitrix_user_id: str = Form(""),
    role: str = Form(""), responsibilities: str = Form(""),
    view: str = Form("list"), access_level: str = Form("employee"),
    is_primary: str = Form(""),
):
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
    from . import access
    selected_level = access_level.strip() or "employee"
    if selected_level not in access.LEVELS:
        raise HTTPException(400, "Неизвестный уровень доступа")
    if selected_level != "employee" and not contact:
        raise HTTPException(400, "Для повышенных прав сначала укажите Telegram сотрудника")
    primary = is_primary.strip().lower() in {"1", "true", "on", "yes"}
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(400, "Подразделение не найдено или неактивно")
        duplicate = next((item for item in s.query(Employee).all()
                          if (item.name or "").strip().casefold() == clean_name.casefold()), None)
        if duplicate is not None:
            raise HTTPException(
                409,
                f"Сотрудник «{duplicate.name}» уже есть в общем справочнике. Перетащите его карточку в подразделение или добавьте через «Существующий сотрудник»."
            )
        employee = Employee(
            name=clean_name, aliases=aliases.strip()[:500], position=position.strip()[:200],
            competencies=competencies.strip()[:4000],
            responsibilities=employee_responsibilities.strip()[:4000],
            context=context.strip()[:4000], channel="telegram",
            telegram_chat_id=contact, bitrix_user_id=bitrix_user_id.strip()[:40], active=True,
        )
        s.add(employee)
        s.flush()
        membership = EmployeeDepartment(
            employee_id=employee.id, department_id=department.id,
            role=role.strip()[:200], responsibilities=responsibilities.strip()[:4000],
            is_primary=primary,
        )
        s.add(membership)
        if primary:
            s.query(EmployeeDepartment).filter(
                EmployeeDepartment.department_id == department.id,
                EmployeeDepartment.employee_id != employee.id,
            ).update({EmployeeDepartment.is_primary: False}, synchronize_session=False)
        employee_id = employee.id
        department_name = department.name
        s.commit()
    if selected_level != "employee":
        access.set_level(employee, selected_level)
    agent.invalidate_roster()
    message = f"Создан сотрудник «{clean_name}» и добавлен в «{department_name}». Все данные профиля сохранены."
    safe_view = "map" if view == "map" else "list"
    return RedirectResponse(
        f"/departments?view={safe_view}&msg=" + quote(message) + f"#employee-{employee_id}", 303,
    )


@app.post("/departments/{department_id}/members/{employee_id}/remove")
def department_remove_member(request: Request, department_id: int, employee_id: int,
                             view: str = Form("list")):
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
    safe_view = "map" if view == "map" else "list"
    return RedirectResponse(f"/departments?view={safe_view}&msg=" + quote(message) + f"#department-{department_id}", 303)


@app.post("/departments/{department_id}/delete")
def department_delete(request: Request, department_id: int, view: str = Form("list")):
    guard_admin(request)
    with SessionLocal() as s:
        department = s.get(Department, department_id)
        if department:
            s.delete(department)
            s.commit()
    safe_view = "map" if view == "map" else "list"
    return RedirectResponse(f"/departments?view={safe_view}#departments-list", 303)


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
