"""SYSAI web app: upload form, meetings, draft editing/approval, settings, employees, Telegram webhook."""
import datetime as dt
import hmac
import json
import logging
import math
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import (accounts, agent, agent_checks, agent_skills, assistant, audio_archive, bitrix, cases, dialog_memory, diarization_eval, diarization_runs, diarize, doc_intake, doc_templates, doctext, entities, memory, transcribe, config, llm, deadlines, meeting_alerts, participants, pipeline, progress,
               progress_board, render, report_archive, settings_store, speaker_hints, speaker_id, voice_memory, voice_relabel,
               storage, telegram, transcript_edit, userbot, notion, voice, voice_diagram)
from .analyze import mmss
from .transcript_sections import group_transcript
from .ui_icons import ICONS
from .db import (AgentCheckCase, Artifact, Case, ConversationEscalation, Delivery, Document, DocumentTransfer, Note, PersonMemory, Skill, Department, Employee, EmployeeDepartment, OrgRelation, EmployeeVoiceSample, Meeting, Recording, ReportVersion, SessionLocal, Task, TelegramChat, User,
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


def stamp_seconds(stamp) -> int | None:
    """A task quote's «19:39» or «1:02:03» in seconds: where its ▶ starts the kept recording."""
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", str(stamp or "").strip())
    if not match:
        return None
    hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


tpl.env.globals.update(stamp_seconds=stamp_seconds)

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


def plural(count, one: str, few: str, many: str) -> str:
    """The Russian word form for a number: plural(5, 'реплика', 'реплики', 'реплик') → 'реплик'."""
    try:
        number = abs(int(count or 0))
    except (TypeError, ValueError):
        number = 0
    return _plural_ru(number, (one, few, many))


tpl.env.globals.update(plural=plural)


def action_line(action) -> str:
    """An action the model asked for, in one line: remind(to="me", topic="акт") — for the checking dialogs."""
    if not isinstance(action, dict):
        return str(action)[:200]
    rest = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" for k, v in action.items() if k != "type")
    line = f"{action.get('type')}({rest})"
    return line if len(line) <= 200 else line[:199] + "…"


tpl.env.filters["action_line"] = action_line


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
def meeting_view(request: Request, mid: int, voice_msg: str = "", voice_enroll: bool = False, speakers: str = "",
                 changed: str = "", recording: str = "", seg: str = "", ref: str = "", people: str = "",
                 relabel: str = ""):
    guard(request)
    with SessionLocal() as s:
        m = pipeline.load(s, mid)
        employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
        artifacts = [{"id": a.id, "title": a.title, "filename": a.filename, "svg": a.svg or ""} for a in
                     s.query(Artifact).filter_by(meeting_id=mid).order_by(Artifact.id.desc()).all()]
    diagram = voice_diagram.build(m.transcript, m.duration_sec, m.waveform, employees) if m.transcript else None
    speaker_hints.attach(diagram, m, employees)
    sections = group_transcript([dict(sg, seq=i) for i, sg in enumerate(m.transcript or []) if isinstance(sg, dict)],
                                (m.report or {}).get("topics") or [])
    count = int(changed) if changed.isascii() and changed.isdigit() and len(changed) < 7 else 0
    speaker_msg = (SPEAKER_MESSAGES.get(speakers, "") or RELABEL_MESSAGES.get(relabel, "")).format(n=count)
    with SessionLocal() as s:
        loaded = pipeline.load(s, mid)
        plan = (pipeline.review_plan(loaded, settings_store.for_meeting(loaded.options))
                if userbot.ai_mode() and loaded.status in ("awaiting_approval", "rejected") else None)
        may_listen = user_may_access_meeting_audio(request, loaded)
        voice_admin = current_user(request)["role"] == "superuser" and may_listen
        voice_clips, voice_debug, voice_ready = ([], False, {})
        if voice_admin:
            voice_clips, voice_debug, voice_ready = _meeting_voice_enrollment_context(s, loaded, manual=voice_enroll)
        kept = audio_archive.view(loaded)
        edits = transcript_edit.history(loaded)
        reference_admin = current_user(request)["role"] == "superuser"
        reference = diarization_eval.summary(loaded) if reference_admin else None
        if reference is not None:
            reference["runs"] = diarization_runs.brief(loaded.diarization_reference,
                                                       diarize.threshold_of(settings_store.all_settings()))
        present = participants.view(s, loaded)
        hint = participants.match_alert(s, loaded) if (loaded.participants or {}).get("source") != "alert" else None
    if not may_listen:  # recorded in someone else's browser: the same rule as for the voice clips
        kept = {"state": "none", "text": "Запись этого совещания доступна только тому, кто его записал."}
    client = voice_diagram.client_payload(diagram) if diagram else None
    if client is not None and kept.get("playable"):
        client["audio"] = {"src": kept["src"]}
    until = userbot.quiet_until()
    relabel_view = voice_relabel.view(m, settings_store.for_meeting(m.options)) if diagram and may_listen else None
    return page(request, "meeting.html", m=m, r=m.report or {}, employees=employees, artifacts=artifacts,
                plan=plan, quiet_at=userbot.local_time(until) if until is not None else "",
                case_by_task=cases.for_meeting(mid),
                voice_clips=voice_clips, voice_debug_mode=voice_debug, voice_readiness=voice_ready,
                voice_msg=voice_msg, voice_admin=voice_admin, diagram=diagram, transcript_sections=sections,
                diagram_client=client, speaker_msg=speaker_msg, recording_msg=RECORDING_MESSAGES.get(recording, ""),
                recording=kept, recording_admin=voice_admin,
                speakers_editable=bool(m.transcript) and m.status not in ("queued", "transcribing"),
                edits=edits, seg_msg=SEGMENT_MESSAGES.get(seg, ""), local_time=userbot.local_time,
                reference=reference, reference_admin=reference_admin, ref_msg=REFERENCE_MESSAGES.get(ref, ""),
                pct=diarization_eval.pct, label_list=diarization_eval.label_list,
                attendance=present, participant_ids=present["ids"], alert_hint=hint.title if hint is not None else "",
                people_msg=PARTICIPANT_MESSAGES.get(people, ""), diarization_note=_diarization_note(m),
                relabel=relabel_view)


def _diarization_note(meeting) -> dict | None:
    """How the voices of this meeting were labelled, when it is worth saying: another way than the parts, or a failure."""
    note = (meeting.options or {}).get("diarization") if isinstance(meeting.options, dict) else None
    if not isinstance(note, dict) or (note.get("method") == "chunks" and not note.get("error")):
        return None
    error = str(note.get("error") or "").strip()
    if error and error[-1] not in ".!?…":
        error += "."
    name = str(note.get("wanted") or note.get("method")) if error else str(note.get("method"))
    return {"title": diarize.METHODS.get(name, diarize.METHODS["chunks"]), "voices": note.get("voices"), "error": error,
            "seconds": note.get("seconds")}


PARTICIPANT_MESSAGES = {
    "saved": "Участники сохранены. Имена голосам теперь подбираются только из них; протокол учтёт список после «Пересобрать».",
    "alert": "Участники взяты из напоминания.",
    "cleared": "Список участников сброшен: имена голосам подбираются из всех сотрудников.",
    "noalert": "Для этой встречи не нашлось напоминания с участниками.",
}


@app.post("/meetings/{mid}/participants")
async def meeting_participants(request: Request, mid: int):
    """«Участники»: who was at the meeting. Names for its voices come only from them; the protocol lists them."""
    guard(request)
    form = await request.form()
    action = str(form.get("action") or "save")
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        if meeting is None:
            raise HTTPException(404, "Совещание не найдено.")
        if action == "save":
            guests = str(form.get("guests") or "")[:4000].splitlines()
            participants.set_people(s, meeting, form.getlist("ids"), guests, by=current_user(request)["username"])
            code = "saved"
        elif action == "clear":
            meeting.participants, code = None, "cleared"
        elif action == "alert":
            previous, meeting.participants = meeting.participants, None
            code = "alert" if participants.ensure(s, meeting) else "noalert"
            if code == "noalert":
                meeting.participants = previous
        else:
            raise HTTPException(400, "Неизвестное действие со списком участников.")
        s.commit()
    return RedirectResponse(f"/meetings/{mid}?people={code}#meeting-participants", 303)


def _named_present(meeting, who, by: str = "") -> None:
    """A person or a named guest chosen for a voice was at the meeting: the list of participants grows."""
    if not who:
        return
    name, employee_id, guest = who
    if employee_id is not None:
        participants.add(meeting, employee_id=employee_id, by=by)
    elif guest and not re.fullmatch(voice_diagram.GUEST + r"( \d+)?", str(name or "")):
        participants.add(meeting, guest=name, by=by)


RECORDING_MESSAGES = {"deleted": "Запись удалена из хранилища. Расшифровка, протокол и диаграмма остались."}


@app.get("/meetings/{mid}/recording")
def meeting_recording(request: Request, mid: int, download: bool = False):
    """The player's source: a short-lived presigned link to the private copy, never the keys or a public address."""
    guard(request)
    with SessionLocal() as s:
        meeting = _voice_meeting(s, request, mid)
        try:
            url = audio_archive.link(meeting, download=download)
        except storage.StorageError as exc:
            raise HTTPException(503, f"Хранилище записей недоступно: {exc}")
    if not url:
        raise HTTPException(404, "Записи этого совещания нет.")
    return RedirectResponse(url, 302, headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@app.post("/meetings/{mid}/recording/delete")
def meeting_recording_delete(request: Request, mid: int):
    """«Удалить запись»: the meeting, its transcript and report stay."""
    guard_admin(request)
    with SessionLocal() as s:
        _voice_meeting(s, request, mid)
    audio_archive.remove(mid)
    return RedirectResponse(f"/meetings/{mid}?recording=deleted#voice-diagram", 303)


SPEAKER_MESSAGES = {
    "assigned": "Готово: говорящий исправлен во всех его репликах ({n}). Протокол и задачи не пересобираются сами.",
    "reset": "Автоматическая разметка говорящих восстановлена ({n} реплик).",
}
RELABEL_MESSAGES = {
    "started": "Переразметка голосов запущена — обычно это несколько минут. Расшифровка не изменится, пока вы не сохраните результат.",
    "saved": "Новая разметка голосов сохранена ({n} реплик сменили говорящего). Протокол и задачи не пересобираются сами.",
    "reverted": "Прежняя разметка голосов возвращена ({n} реплик).",
}


def _speaker_meeting(session, mid):
    meeting = session.get(Meeting, mid)
    if meeting is None:
        raise HTTPException(404, "Совещание не найдено.")
    if not meeting.transcript or meeting.status in ("queued", "transcribing"):
        raise HTTPException(409, "Расшифровка ещё не готова.")
    return meeting


@app.post("/meetings/{mid}/speakers")
async def meeting_speaker_assign(request: Request, mid: int):
    """«Кто это?»: every replica of one diarization label goes to a person from the directory or to a guest."""
    guard(request)
    form = await request.form()
    label = str(form.get("speaker") or "").strip()
    choice = str(form.get("employee_id") or "").strip()
    with SessionLocal() as s:
        meeting = _speaker_meeting(s, mid)
        if label not in {voice_diagram.label_of(seg) for seg in meeting.transcript if isinstance(seg, dict)}:
            raise HTTPException(400, "Такого говорящего нет в расшифровке.")
        if choice == "guest":
            name, employee_id, guest = voice_diagram.guest_name(meeting.transcript, form.get("guest_name"), label), None, True
        else:
            employee = s.get(Employee, int(choice)) if choice.isascii() and choice.isdigit() and len(choice) < 10 else None
            if employee is None or not employee.active:
                raise HTTPException(400, "Выберите сотрудника из справочника или «Гость».")
            name, employee_id, guest = employee.name, employee.id, False
        by = current_user(request)["username"]
        before = meeting.transcript
        after, count = voice_diagram.assign(before, label, name, employee_id, guest, by=by)
        if count:
            transcript_edit.record(meeting, after, transcript_edit.assigned(before, after, label, name, by))
            _named_present(meeting, (name, employee_id, guest), by)
        s.commit()
    if count and employee_id is not None:
        voice_memory.remember_soon(mid, by=by)  # a confirmed voice, for the next meetings (with consent)
    return RedirectResponse(f"/meetings/{mid}?speakers=assigned&changed={count}#voice-diagram", 303)


@app.post("/meetings/{mid}/speakers/relabel")
def meeting_speaker_relabel(request: Request, mid: int):
    """«Переразметить голоса»: label the voices again from the kept recording; nothing changes until it is saved."""
    guard(request)
    with SessionLocal() as s:
        _voice_meeting(s, request, mid)
        _speaker_meeting(s, mid)
    try:
        voice_relabel.start(mid, by=current_user(request)["username"])
    except voice_relabel.RelabelError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse(f"/meetings/{mid}?relabel=started#voice-relabel", 303)


@app.get("/meetings/{mid}/speakers/relabel/status")
def meeting_speaker_relabel_status(request: Request, mid: int):
    guard(request)
    with SessionLocal() as s:
        meeting = _voice_meeting(s, request, mid)
        data = voice_relabel.state(meeting)
    return {"state": data.get("state") or "", "progress": data.get("progress") or ""}


def _relabel_decision(request: Request, mid: int, act) -> int:
    guard(request)
    with SessionLocal() as s:
        meeting = _voice_meeting(s, request, mid)
        try:
            count = act(meeting)
        except voice_relabel.RelabelError as exc:
            raise HTTPException(409, str(exc)) from None
        s.commit()
    return count


@app.post("/meetings/{mid}/speakers/relabel/save")
def meeting_speaker_relabel_save(request: Request, mid: int):
    guard(request)
    by = current_user(request)["username"]
    count = _relabel_decision(request, mid, lambda meeting: voice_relabel.save(meeting, by=by))
    voice_memory.remember_soon(mid, by=by)
    return RedirectResponse(f"/meetings/{mid}?relabel=saved&changed={count}#voice-diagram", 303)


@app.post("/meetings/{mid}/speakers/relabel/revert")
def meeting_speaker_relabel_revert(request: Request, mid: int):
    count = _relabel_decision(request, mid, voice_relabel.revert)
    return RedirectResponse(f"/meetings/{mid}?relabel=reverted&changed={count}#voice-diagram", 303)


@app.post("/meetings/{mid}/speakers/relabel/close")
def meeting_speaker_relabel_close(request: Request, mid: int):
    _relabel_decision(request, mid, voice_relabel.close)
    return RedirectResponse(f"/meetings/{mid}#voice-diagram", 303)


@app.post("/meetings/{mid}/speakers/reset")
def meeting_speaker_reset(request: Request, mid: int):
    """Undo every manual speaker correction of the meeting: the automatic labels come back."""
    guard(request)
    with SessionLocal() as s:
        meeting = _speaker_meeting(s, mid)
        count = transcript_edit.reset(meeting)
        s.commit()
    return RedirectResponse(f"/meetings/{mid}?speakers=reset&changed={count}#voice-diagram", 303)


SEGMENT_MESSAGES = {
    "speaker": "Готово: говорящий изменён только у этой реплики.",
    "split": "Готово: реплика разделена на две.",
    "merge": "Готово: реплики объединены.",
    "undo": "Последняя правка отменена.",
}


def _segment_seq(seq: str) -> int:
    if not (seq.isascii() and seq.isdigit() and len(seq) < 7):
        raise HTTPException(404, "Такой реплики нет в расшифровке.")
    return int(seq)


def _segment_choice(session, transcript, form, field="who"):
    """The speaker chosen in a correction form: (name, employee id, guest?) or None for «как было»."""
    choice = str(form.get(field) or "").strip()
    employee = None
    if choice.startswith("employee:"):
        raw = choice[len("employee:"):]
        employee = session.get(Employee, int(raw)) if raw.isascii() and raw.isdigit() and len(raw) < 10 else None
        if employee is None or not employee.active:
            raise transcript_edit.EditError("Выберите сотрудника из справочника.")
    return transcript_edit.resolve(transcript, choice, form.get("guest_name"), employee)


def _segment_redirect(mid: int, op: str, seq: int):
    return RedirectResponse(f"/meetings/{mid}?seg={op}#seg-{max(0, seq)}", 303)


def _segment_error(exc: transcript_edit.EditError):
    return HTTPException(409 if isinstance(exc, transcript_edit.Stale) else 400, str(exc))


@app.get("/meetings/{mid}/segments/{seq}", response_class=HTMLResponse)
def segment_editor(request: Request, mid: int, seq: str, fragment: bool = False):
    """One replica: another speaker only for it, a split in two, a merge with a neighbour. Opens under the replica on
    the meeting page (fragment) or as a page of its own without JavaScript."""
    guard(request)
    index = _segment_seq(seq)
    with SessionLocal() as s:
        meeting = _speaker_meeting(s, mid)
        transcript = meeting.transcript
        if not 0 <= index < len(transcript) or not isinstance(transcript[index], dict):
            raise HTTPException(404, "Такой реплики нет в расшифровке.")
        employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
        neighbour = lambda i: transcript[i] if 0 <= i < len(transcript) and isinstance(transcript[i], dict) else None
        segment = transcript[index]
        ctx = dict(m=meeting, seq=index, sg=segment, sig=transcript_edit.signature(segment),
                   labels=transcript_edit.labels(transcript), new_label=transcript_edit.new_label(transcript),
                   current=voice_diagram.label_of(segment), words=transcript_edit.words(segment.get("text")),
                   prev=neighbour(index - 1), next=neighbour(index + 1),
                   prev_sig=transcript_edit.signature(neighbour(index - 1)) if neighbour(index - 1) else "",
                   next_sig=transcript_edit.signature(neighbour(index + 1)) if neighbour(index + 1) else "",
                   employees=employees, participant_ids=participants.employee_ids(meeting))
        if fragment:
            return tpl.TemplateResponse(request, "_segment_editor.html", ctx)
        return page(request, "segment_edit.html", **ctx)


async def _segment_change(request: Request, mid: int, seq: str, change):
    guard(request)
    index = _segment_seq(seq)
    form = await request.form()
    by = current_user(request)["username"]
    with SessionLocal() as s:
        meeting = _speaker_meeting(s, mid)
        chosen = []
        try:
            transcript, entry = change(s, meeting.transcript, index, form, by, chosen)
        except transcript_edit.EditError as exc:
            raise _segment_error(exc)
        transcript_edit.record(meeting, transcript, entry)
        for who in chosen:
            _named_present(meeting, who, by)
        s.commit()
    if any(who and who[1] is not None for who in chosen):
        voice_memory.remember_soon(mid, by=by)
    return _segment_redirect(mid, entry["op"], entry["seq"])


@app.post("/meetings/{mid}/segments/{seq}/speaker")
async def segment_speaker(request: Request, mid: int, seq: str):
    def change(s, transcript, index, form, by, chosen):
        who = _segment_choice(s, transcript, form)
        chosen.append(who)
        return transcript_edit.set_speaker(transcript, index, form.get("sig"), who, by)
    return await _segment_change(request, mid, seq, change)


@app.post("/meetings/{mid}/segments/{seq}/split")
async def segment_split(request: Request, mid: int, seq: str):
    def change(s, transcript, index, form, by, chosen):
        who = _segment_choice(s, transcript, form) if str(form.get("who") or transcript_edit.SAME) != transcript_edit.SAME else None
        chosen.append(who)
        return transcript_edit.split(transcript, index, form.get("sig"), form.get("at"), who, by)
    return await _segment_change(request, mid, seq, change)


@app.post("/meetings/{mid}/segments/{seq}/merge")
async def segment_merge(request: Request, mid: int, seq: str):
    def change(s, transcript, index, form, by, chosen):
        return transcript_edit.merge(transcript, index, form.get("sig"), str(form.get("with") or ""),
                                     form.get("neighbour_sig"), by)
    return await _segment_change(request, mid, seq, change)


@app.post("/meetings/{mid}/transcript/undo")
def transcript_undo(request: Request, mid: int):
    """«Отменить последнюю правку» of the transcript, one at a time, newest first."""
    guard(request)
    with SessionLocal() as s:
        meeting = _speaker_meeting(s, mid)
        try:
            entry = transcript_edit.undo(meeting)
        except transcript_edit.EditError as exc:
            raise _segment_error(exc)
        s.commit()
    return _segment_redirect(mid, "undo", int(entry.get("seq") or 0))


REFERENCE_MESSAGES = {
    "saved": "Эталон сохранён: точность автоматической разметки посчитана по нему.",
    "deleted": "Эталон удалён. Расшифровка и правки остались.",
}


@app.post("/meetings/{mid}/reference")
def meeting_reference_save(request: Request, mid: int):
    """«Сохранить как эталон»: the corrected transcript becomes the answer key the automatic labelling is measured by."""
    guard_admin(request)
    with SessionLocal() as s:
        meeting = _speaker_meeting(s, mid)
        try:
            diarization_eval.save(meeting, by=current_user(request)["username"])
        except transcript_edit.EditError as exc:
            raise HTTPException(400, str(exc))
        s.commit()
    return RedirectResponse(f"/meetings/{mid}?ref=saved#diarization-reference", 303)


@app.post("/meetings/{mid}/reference/delete")
def meeting_reference_delete(request: Request, mid: int):
    guard_admin(request)
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        if meeting is None:
            raise HTTPException(404, "Совещание не найдено.")
        diarization_eval.remove(meeting)
        s.commit()
    return RedirectResponse(f"/meetings/{mid}?ref=deleted#diarization-reference", 303)


@app.get("/diarization", response_class=HTMLResponse)
def diarization_overview(request: Request, msg: str = ""):
    """«Точность разметки голосов»: every reference, its metrics and the totals."""
    guard_admin(request)
    rows = []
    with SessionLocal() as s:
        meetings = s.query(Meeting).filter(Meeting.diarization_reference.isnot(None)).order_by(Meeting.id.desc()).all()
        for meeting in meetings:
            info = diarization_eval.summary(meeting)
            if info is not None:
                rows.append({"id": meeting.id, "title": meeting.title or f"Совещание {meeting.id}",
                             "date": meeting.meeting_date, "duration": meeting.duration_sec, **info})
        references = [meeting.diarization_reference for meeting in meetings]
    total = diarization_eval.combine([row["metrics"] for row in rows])
    settings = settings_store.all_settings()
    method, threshold = diarize.method_of(settings), diarize.threshold_of(settings)
    return page(request, "diarization.html", rows=rows, total=total, pct=diarization_eval.pct,
                label_list=diarization_eval.label_list, local_time=userbot.local_time,
                compare=diarization_runs.overview(references, method, threshold), run=diarization_runs.status(),
                method=method, threshold=threshold, methods=diarize.METHODS,
                unavailable={name: diarize.unavailable(name) for name in diarize.METHODS},
                msg=DIARIZATION_MESSAGES.get(msg, ""), when=_when)


def _when(value) -> str:
    try:
        return userbot.local_time(dt.datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return ""


DIARIZATION_MESSAGES = {
    "started": "Сравнение запущено: способы по очереди размечают записи эталонов. Страница обновляется сама.",
    "busy": "Сравнение уже идёт.",
    "empty": "Нет эталонов: сначала сохраните хотя бы одну исправленную расшифровку как эталон.",
    "applied": "Способ разметки сохранён: он сработает на следующих записях.",
}


@app.post("/diarization/compare")
def diarization_compare(request: Request):
    """«Сравнить способы»: every way runs on the recording of every reference, in the background."""
    guard_admin(request)
    if diarization_runs.status()["running"]:
        code = "busy"
    else:
        code = "started" if diarization_runs.start() else "empty"
    return RedirectResponse(f"/diarization?msg={code}#compare", 303)


@app.post("/diarization/method")
async def diarization_method(request: Request):
    """«Применить»: the way (and, for the voice service, the threshold) the next recordings are labelled by."""
    guard_admin(request)
    form = await request.form()
    name = str(form.get("method") or "")
    if name not in diarize.METHODS:
        raise HTTPException(400, "Выберите способ разметки из списка.")
    updates = {"diarization_method": name}
    if form.get("threshold"):
        updates["diarization_voice_threshold"] = _voice_threshold(form.get("threshold"))
    settings_store.set_many(updates)
    return RedirectResponse("/diarization?msg=applied#compare", 303)


def _name_threshold(raw) -> float:
    """The confidence a name from the conversation needs: a percentage 50–95 (stored as a share)."""
    try:
        value = float(str(raw).strip().rstrip("%").strip().replace(",", "."))
    except ValueError:
        raise HTTPException(400, "Уверенность для имени укажите числом от 50 до 95 %.") from None
    if not 50 <= value <= 95:
        raise HTTPException(400, "Уверенность для имени укажите числом от 50 до 95 %.")
    return round(value / 100, 2)


def _memory_threshold(raw) -> float:
    """How alike a voice must be to the voice of a person remembered from past meetings: 0,4–0,95."""
    try:
        value = float(str(raw).strip().replace(",", "."))
    except ValueError:
        raise HTTPException(400, "Порог узнавания по прошлым встречам укажите числом от 0,4 до 0,95.") from None
    if not 0.4 <= value <= 0.95:
        raise HTTPException(400, "Порог узнавания по прошлым встречам укажите числом от 0,4 до 0,95.")
    return round(value, 2)


def _voice_threshold(raw) -> float:
    try:
        value = float(str(raw).strip().replace(",", "."))
    except ValueError:
        raise HTTPException(400, "Порог сходства голосов укажите числом от 0,2 до 0,95.") from None
    if not 0.2 <= value <= 0.95:
        raise HTTPException(400, "Порог сходства голосов укажите числом от 0,2 до 0,95.")
    return round(value, 2)


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
    elif action == "remind":  # «Напомнить ещё»: one more reminder now
        try:
            ok = cases.remind_again(case_id, who)
        except (cases.CaseError, cases.Retry, telegram.TelegramError) as exc:  # quiet hours and limits included
            raise HTTPException(409, f"Напоминание не ушло: {exc}") from None
    elif action == "take_over":  # «Займусь сам»: the assistant stops reminding
        ok = cases.take_over(case_id, who)
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
        memories = s.query(PersonMemory).filter_by(employee_id=eid).all()
        summaries = [m.summary for m in memories if m.summary]
        dialogs = dialog_memory.view(s, {m.chat_id for m in memories} | {e.telegram_user_id})
        card = {"id": e.id, "name": e.name, "aliases": e.aliases or "", "position": e.position or "",
                "departments": [f"{m.department.name}" + (f" — {m.role}" if m.role else "") for m in e.department_memberships
                                if m.department is not None],
                "responsibilities": e.responsibilities or "", "competencies": e.competencies or "",
                "context": e.context or "", "communication_style": getattr(e, "communication_style", "") or "",
                "telegram": e.telegram_chat_id or ""}
        documents = [{"id": d.id, "filename": d.filename, "at": d.created_at} for d in
                     s.query(Document).filter_by(employee_id=eid).order_by(Document.id.desc()).limit(20)]
    return page(request, "memory_person.html", card=card, notes=notes, archived=archived, summaries=summaries,
                dialogs=dialogs, documents=documents)


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
    """The assistant's own rolling summary about the person and the retelling of the older conversation are erased (the
    person asked, or it is wrong)."""
    guard_admin(request)
    with SessionLocal() as s:
        memories = s.query(PersonMemory).filter_by(employee_id=eid).all()
        for m in memories:
            m.summary = ""
        e = s.get(Employee, eid)
        dialog_memory.forget(s, {m.chat_id for m in memories} | {e.telegram_user_id if e else None})
        s.commit()
    return RedirectResponse(f"/memory/people/{eid}", 303)


def _builtin_skills() -> list:
    """The built-in skills with their switches and the uses of the last 30 days (companion skills follow their main one)."""
    from . import skill_stats
    modes, off, limits = agent_skills.modes(), agent_skills.switched_off(), agent_skills.limits()
    used = skill_stats.stats(30)
    rows = []
    for k in agent_skills.SKILLS.values():
        if k.name in agent_skills.PAIRED:
            continue
        counts = dict(used.get(k.name) or {})
        for companion, main in agent_skills.PAIRED.items():
            if main == k.name:
                for key, value in (used.get(companion) or {}).items():
                    if key != "last_error":
                        counts[key] = counts.get(key, 0) + value
                    elif value and not counts.get("last_error"):
                        counts["last_error"] = value
        limit_options = [x for x in agent_skills.LIMITS if not (x == "leaders" and k.who in ("reviewer", "assign"))]
        # the mode of a pair is the mode of the step that does the work (an order starts after «да»: confirm_order)
        mode_name = k.name if k.modes else next((c for c, m in agent_skills.PAIRED.items()
                                                 if m == k.name and agent_skills.SKILLS[c].modes), "")
        rows.append({"name": k.name, "title": k.title, "hint": k.hint, "who": agent_skills.WHO_RU[k.who],
                     "modes": bool(mode_name), "mode_name": mode_name, "mode": modes.get(mode_name, "auto"),
                     "on": k.name not in off, "limit": limits.get(k.name, ""), "limits": limit_options, "stats": counts})
    return rows


@app.get("/skills", response_class=HTMLResponse)
def skills_view(request: Request, edit: int = 0, saved: int = 0):
    guard_admin(request)
    with SessionLocal() as s:
        rows = [{"id": k.id, "title": k.title, "kind": k.kind, "when": k.when_to_use or "", "body": k.body, "active": k.active,
                 "uses": k.uses, "updated": k.updated_at, "by": k.updated_by or ""}
                for k in s.query(Skill).order_by(Skill.active.desc(), Skill.title)]
    return page(request, "skills.html", skills=rows, edit=edit, builtin=_builtin_skills(), saved=saved,
                modes_ru=agent_skills.MODE_RU, mode_hints=agent_skills.MODE_HINT, transport=agent_skills.transport(),
                transports=agent_skills.TRANSPORT_RU, agent_model=voice.model(), limit_ru=agent_skills.LIMIT_RU,
                director_set=bool(settings_store.get("director_chat_ids")))


@app.post("/skills/agent")
async def skills_agent_save(request: Request):
    """The modes of the built-in skills (at once / with the director's approval / only a suggestion) and the way the
    model asks for them."""
    guard_admin(request)
    f = await request.form()
    chosen, off, limits = {}, [], {}
    for name, skill in agent_skills.SKILLS.items():
        value = str(f.get(f"mode_{name}") or "auto")
        if skill.modes and value in agent_skills.MODES and value != "auto":
            chosen[name] = value
        if name in agent_skills.PAIRED:
            continue  # a companion skill follows the one it belongs to
        if f.get("present_switches") == "1" and not f.get(f"on_{name}"):
            off.append(name)
        limit = str(f.get(f"who_{name}") or "")
        if limit in agent_skills.LIMITS:
            limits[name] = limit
    way = str(f.get("transport") or "")
    updates = {"agent_skill_modes": chosen,
               "agent_transport": way if way in agent_skills.TRANSPORTS else agent_skills.transport()}
    if f.get("present_switches") == "1":  # an older form without the switches leaves them as they are
        updates.update({"agent_skills_off": off, "agent_skill_who": limits})
    settings_store.set_many(updates)
    return RedirectResponse("/skills?saved=1#agent-skills", 303)


CHECK_MESSAGES = {"started": "Прогон начался: модели отвечают на проверочные диалоги. Страница обновляется сама.",
                  "stopped": "Прогон останавливается: начатые ответы допишутся.",
                  "picked": "Модель и способ выбраны: ИИ-сотрудник отвечает так со следующего сообщения.",
                  "budget": "Бюджет сохранён.", "marked": "Ответ добавлен в проверочные диалоги — он войдёт в следующий прогон.",
                  "deleted": "Случай удалён.", "toggled": "Готово.",
                  "questions": "Вопросы добавлены — они войдут в следующий прогон (тема «Вопросы по компании»)."}


def _telegram_people() -> list:
    """People who can get a message from the assistant (they have Telegram)."""
    with SessionLocal() as s:
        return [{"id": e.id, "name": e.name, "position": e.position or ""} for e in
                s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name)
                if (e.telegram_chat_id or "").strip()]


def _active_people() -> list:
    with SessionLocal() as s:
        return [{"id": e.id, "name": e.name} for e in
                s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name)]


@app.get("/skills/checks", response_class=HTMLResponse)
def skill_checks_view(request: Request, run: int = 0, msg: str = "", error: str = ""):
    """Checking dialogs: about fifty situations sent to several models; quality and the price a month side by side."""
    guard_admin(request)
    history = agent_checks.runs()
    shown = run or next((r["id"] for r in history if r["done"]), 0)
    data = agent_checks.summary(shown) if shown else {}
    defaults = list(dict.fromkeys([voice.model(), *agent_checks.DEFAULT_MODELS]))
    return page(request, "skill_checks.html", data=data, history=history, running=agent_checks.running_id(),
                scenarios=agent_checks.scenarios(), cases=agent_checks.custom_cases(),
                questions=agent_checks.questions(), people=_active_people(),
                answers=agent_checks.recent_answers(), defaults=defaults, budget=agent_checks.budget(),
                month=agent_checks.monthly_messages(), model=voice.model(), transport=agent_skills.transport(),
                transports=agent_skills.TRANSPORT_RU, skills=agent_skills.SKILLS, role_ru=agent_checks.ROLE_RU,
                msg=CHECK_MESSAGES.get(msg, ""), error=error[:300], key=bool(config.openrouter_key()))


@app.post("/skills/checks")
async def skill_checks_action(request: Request):
    guard_admin(request)
    f = await request.form()
    do = str(f.get("do") or "")
    try:
        if do == "run":
            models = re.split(r"[\s,;]+", str(f.get("models") or ""))
            agent_checks.start(models, f.getlist("transport"), _who(request))
            return RedirectResponse("/skills/checks?msg=started", 303)
        if do == "stop":
            agent_checks.stop()
            return RedirectResponse("/skills/checks?msg=stopped", 303)
        if do == "pick":
            model, way = str(f.get("model") or "").strip(), str(f.get("transport") or "")
            if not re.fullmatch(r"[\w.\-]+/[\w.:\-]+", model) or way not in agent_skills.TRANSPORTS:
                raise agent_checks.CheckError("Неизвестная модель или способ")
            settings_store.set_many({"agent_model": model, "agent_transport": way})
            return RedirectResponse(f"/skills/checks?msg=picked&run={int(f.get('run') or 0)}", 303)
        if do == "budget":
            raw = str(f.get("budget") or "0").strip().replace(",", ".") or "0"
            try:
                value = float(raw)
            except ValueError:
                raise agent_checks.CheckError("Бюджет — число долларов в месяц, например 20") from None
            if not 0 <= value <= 100000:
                raise agent_checks.CheckError("Бюджет — от 0 до 100 000 долларов в месяц")
            settings_store.set_many({"agent_budget_month_usd": round(value, 2)})
            return RedirectResponse("/skills/checks?msg=budget#compare", 303)
        if do == "mark":
            agent_checks.add_case(int(str(f.get("message_id") or "0")), str(f.get("expected") or ""),
                                  [str(f.get("must") or "")], [str(f.get("must_not") or "")], _who(request))
            return RedirectResponse("/skills/checks?msg=marked#own", 303)
        if do == "questions":
            agent_checks.add_questions(str(f.get("lines") or ""), str(f.get("who") or "director"), _who(request))
            return RedirectResponse("/skills/checks?msg=questions#questions", 303)
        if do in ("delete", "toggle"):
            with SessionLocal() as s:
                case = s.get(AgentCheckCase, int(str(f.get("case_id") or "0")))
                if case is None:
                    raise agent_checks.CheckError("Случай не найден")
                if do == "delete":
                    s.delete(case)
                else:
                    case.active = not case.active
                s.commit()
            back = "questions" if str(f.get("back") or "") == "questions" else "own"
            return RedirectResponse(f"/skills/checks?msg={'deleted' if do == 'delete' else 'toggled'}#{back}", 303)
    except (agent_checks.CheckError, ValueError) as exc:
        return RedirectResponse("/skills/checks?error=" + quote(str(exc)[:300]), 303)
    raise HTTPException(404, "Неизвестное действие")


@app.get("/skills/checks/export")
def skill_checks_export(request: Request, days: int = 90):
    """The conversations as JSON lines: to pick the worst answers or to share them with the developer."""
    guard_admin(request)
    name = f"sysai-dialogs-{dt.date.today().isoformat()}.jsonl"
    return StreamingResponse(agent_checks.export_lines(max(1, min(days, 365))), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
        intake = doc_intake.card(d)
        cards = [{"id": e.id, "name": e.name} for e in s.query(entities.Entity).join(
            entities.EntityLink, entities.EntityLink.entity_id == entities.Entity.id).filter(
            entities.EntityLink.source_type == "document", entities.EntityLink.source_id == d.id)]
    answer = None
    if q.strip():
        try:
            answer = memory.answer_about_document(doc_id, q.strip()[:500])
        except llm.LLMError as exc:
            answer = {"answer": f"Модель недоступна: {exc}", "pieces": []}
    return page(request, "document.html", doc=doc, transfers=transfers, q=q, answer=answer, intake=intake, cards=cards,
                intake_on=doc_intake.enabled(), msg=INTAKE_MESSAGES.get(request.query_params.get("msg", ""), ""),
                error=str(request.query_params.get("error", ""))[:300])


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


INTAKE_MESSAGES = {"analyzing": "Разбираю заново — обновите страницу через полминуты.", "analyzed": "Разобран заново.",
                   "routed": "Готово: передан по правилам.", "rule": "Правило сохранено.", "rule_done": "Готово.",
                   "intake": "Готово.", "checked": "Отмечено — счёт проверенных виден на странице «Документы» в разделе «Разбор входящих»."}


@app.post("/documents/{doc_id}/analyze")
def document_analyze(request: Request, doc_id: int):
    """«Разобрать заново»: the requisites, the sums and the deadlines again (what was already sent stays as it was)."""
    guard_admin(request)
    with SessionLocal() as s:
        if s.get(Document, doc_id) is None:
            raise HTTPException(404, "Документ не найден")
    if config.TESTING:
        doc_intake.analyze(doc_id, force=True)
        return RedirectResponse(f"/documents/{doc_id}/view?msg=analyzed", 303)
    pipeline.POOL.submit(doc_intake.analyze, doc_id, True)
    return RedirectResponse(f"/documents/{doc_id}/view?msg=analyzing", 303)


@app.post("/documents/{doc_id}/route")
def document_route(request: Request, doc_id: int):
    """«Передать по правилам»: after a rule was added for a document nobody got."""
    guard_admin(request)
    try:
        route = doc_intake.route_again(doc_id, by=_who(request))
    except doc_intake.RuleError as exc:
        return RedirectResponse(f"/documents/{doc_id}/view?error=" + quote(str(exc)), 303)
    if route.get("status") not in ("sent", "self", "late"):
        reason = route.get("why") or doc_intake.ROUTE_RU.get(route.get("status") or "", "не передан")
        return RedirectResponse(f"/documents/{doc_id}/view?error=" + quote(reason), 303)
    return RedirectResponse(f"/documents/{doc_id}/view?msg=routed", 303)


@app.post("/documents/{doc_id}/check")
async def document_check(request: Request, doc_id: int):
    """«Да, всё верно» / «Есть ошибка» under the analysis: the Documents page counts how many are right."""
    guard_admin(request)
    f = await request.form()
    try:
        doc_intake.mark(doc_id, f.get("ok") == "1", str(f.get("note") or ""), by=_who(request))
    except doc_intake.RuleError as exc:
        return RedirectResponse(f"/documents/{doc_id}/view?error=" + quote(str(exc)), 303)
    return RedirectResponse(f"/documents/{doc_id}/view?msg=checked#intake", 303)


@app.post("/documents/rules")
async def doc_rule_save(request: Request):
    """A «что кому» rule: the type of the document and/or its words → a person or a department."""
    guard_admin(request)
    f = await request.form()
    raw_id = str(f.get("rule_id") or "").strip()
    try:
        doc_intake.save_rule(rule_id=int(raw_id) if raw_id.isdigit() else None, kind=str(f.get("kind") or ""),
                             keywords=str(f.get("keywords") or ""), to=str(f.get("to") or ""),
                             make_tasks=f.get("make_tasks") == "on", by=_who(request))
    except doc_intake.RuleError as exc:
        return RedirectResponse("/documents?error=" + quote(str(exc)) + "#rules", 303)
    return RedirectResponse("/documents?msg=rule#rules", 303)


@app.post("/documents/rules/{rule_id}/{action}")
def doc_rule_action(request: Request, rule_id: int, action: str):
    guard_admin(request)
    try:
        doc_intake.rule_action(rule_id, action)
    except doc_intake.RuleError as exc:
        raise HTTPException(404, str(exc)) from None
    return RedirectResponse("/documents?msg=rule_done#rules", 303)


@app.post("/documents/intake")
def doc_intake_switch(request: Request):
    guard_admin(request)
    settings_store.set_many({"doc_intake": not doc_intake.enabled()})
    return RedirectResponse("/documents?msg=intake#rules", 303)


# ----------------------------------------------------------------------------- objects and clients (entities.py)
ENTITY_MESSAGES = {"saved": "Карточка сохранена; упоминания в истории ищутся сами.", "merged": "Объединено.",
                   "deleted": "Карточка удалена.", "fact": "Факт добавлен.", "scanned": "Упоминания найдены заново.",
                   "archived": "Факт убран в архив."}


def _entity_form(f) -> dict:
    return {k: str(f.get(k) or "") for k in ("name", "kind", "aliases", "unp", "address", "contacts", "description")}


@app.get("/entities", response_class=HTMLResponse)
def entities_view(request: Request, q: str = "", kind: str = "", msg: str = "", error: str = "", added: int = 0,
                  found: int = 0):
    """Objects and clients: the cards, possible duplicates, a new card."""
    guard_admin(request)
    with SessionLocal() as s:
        rows, dups = entities.listing(s, q, kind if kind in entities.KINDS else ""), entities.duplicates(s)
    text = (f"Новых карточек: {added}, уже были: {found}. Упоминания в истории ищутся сами." if msg == "bulk"
            else ENTITY_MESSAGES.get(msg, ""))
    return page(request, "entities.html", rows=rows, dups=dups, q=q, kind=kind, kinds=entities.KINDS,
                source_types=entities.SOURCE_TYPES, msg=text, error=error[:300])


@app.post("/entities")
async def entity_create(request: Request):
    guard_admin(request)
    f = await request.form()
    try:
        entity_id = entities.save(None, **_entity_form(f), by=_who(request))
    except entities.EntityError as exc:
        return RedirectResponse("/entities?error=" + quote(str(exc)), 303)
    return RedirectResponse(f"/entities/{entity_id}?msg=saved", 303)


@app.post("/entities/bulk")
async def entity_bulk(request: Request):
    """The list of the main objects and clients pasted at once: one per line."""
    guard_admin(request)
    f = await request.form()
    added, found = entities.bulk_add(str(f.get("text") or ""), str(f.get("kind") or "client"), by=_who(request))
    return RedirectResponse(f"/entities?msg=bulk&added={added}&found={found}", 303)


@app.post("/entities/scan")
def entity_scan(request: Request):
    guard_admin(request)
    entities.rescan()
    return RedirectResponse("/entities?msg=scanned", 303)


@app.get("/entities/{entity_id}", response_class=HTMLResponse)
def entity_view(request: Request, entity_id: int, msg: str = "", error: str = ""):
    guard_admin(request)
    with SessionLocal() as s:
        try:
            e = entities.card(s, entity_id)
        except entities.EntityError:
            raise HTTPException(404, "Карточка не найдена") from None
    return page(request, "entity.html", e=e, kinds=entities.KINDS, source_types=entities.SOURCE_TYPES,
                msg=ENTITY_MESSAGES.get(msg, ""), error=error[:300])


@app.post("/entities/{entity_id}")
async def entity_save(request: Request, entity_id: int):
    guard_admin(request)
    f = await request.form()
    try:
        entities.save(entity_id, **_entity_form(f), by=_who(request))
    except entities.EntityError as exc:
        return RedirectResponse(f"/entities/{entity_id}?error=" + quote(str(exc)), 303)
    return RedirectResponse(f"/entities/{entity_id}?msg=saved", 303)


@app.post("/entities/{entity_id}/merge")
async def entity_merge(request: Request, entity_id: int):
    """The card `gone` is a duplicate of this one: everything moves here."""
    guard_admin(request)
    f = await request.form()
    gone = str(f.get("gone") or "")
    try:
        if not gone.isdigit():
            raise entities.EntityError("выберите карточку-дубль")
        entities.merge(entity_id, int(gone), by=_who(request))
    except entities.EntityError as exc:
        back = "/entities" if f.get("back") == "list" else f"/entities/{entity_id}"
        return RedirectResponse(back + "?error=" + quote(str(exc)), 303)
    return RedirectResponse("/entities?msg=merged" if f.get("back") == "list" else f"/entities/{entity_id}?msg=merged", 303)


@app.post("/entities/{entity_id}/delete")
def entity_delete(request: Request, entity_id: int):
    guard_admin(request)
    try:
        entities.delete(entity_id)
    except entities.EntityError as exc:
        raise HTTPException(404, str(exc)) from None
    return RedirectResponse("/entities?msg=deleted", 303)


@app.post("/entities/{entity_id}/facts")
async def entity_fact(request: Request, entity_id: int):
    guard_admin(request)
    f = await request.form()
    try:
        entities.add_fact(entity_id, str(f.get("text") or ""), by=_who(request))
    except entities.EntityError as exc:
        return RedirectResponse(f"/entities/{entity_id}?error=" + quote(str(exc)) + "#facts", 303)
    return RedirectResponse(f"/entities/{entity_id}?msg=fact#facts", 303)


@app.post("/entities/{entity_id}/facts/{note_id}/archive")
def entity_fact_archive(request: Request, entity_id: int, note_id: int):
    guard_admin(request)
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None or note.entity_id != entity_id:
            raise HTTPException(404, "Факт не найден")
    memory.edit_note(note_id, archived=True, by=_who(request))
    return RedirectResponse(f"/entities/{entity_id}?msg=archived#facts", 303)


def _document_sender(d) -> str:
    if d.chat_id == "generated":  # made from a template for the person who asked
        return f"{d.employee.name} (по шаблону)" if d.employee else "по шаблону"
    return d.employee.name if d.employee else d.chat_id


DOC_TEMPLATE_MESSAGES = {"saved": "Шаблон сохранён: ИИ-сотрудник видит его со следующего сообщения.",
                         "toggled": "Готово.", "deleted": "Шаблон удалён."}


@app.get("/documents", response_class=HTMLResponse)
def documents_view(request: Request, q: str = "", msg: str = "", error: str = ""):
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
                         "sender": _document_sender(d), "intake": doc_intake.brief(d),
                         "case": {"id": case.id, "title": case.title, "meeting_id": case.meeting_id} if case else None,
                         "transfers": [{"to": (s.get(Employee, t.to_employee_id).name if t.to_employee_id and
                                               s.get(Employee, t.to_employee_id) else t.to_ref), "status": t.status,
                                        "at": t.sent_at} for t in transfers]})
        templates, made = doc_templates.panel_list(s), doc_templates.recent(s, 15)
        rules, targets, checked = doc_intake.rules_view(s), doc_intake.targets(s), doc_intake.checked(s)
    return page(request, "documents.html", docs=rows, q=q, templates=templates, made=made,
                outputs=doc_templates.OUTPUTS, msg=DOC_TEMPLATE_MESSAGES.get(msg, "") or INTAKE_MESSAGES.get(msg, ""),
                error=error[:300], draft_ru=DRAFT_RU, rules=rules, targets=targets, doc_kinds=doc_intake.KINDS,
                intake_on=doc_intake.enabled(), checked=checked)


DRAFT_RU = {"draft": "ждёт «ок»", "replaced": "заменён исправленным", "cancelled": "отменён", "approved": "сохранён, отправляется",
            "sent": "сохранён и отправлен", "kept": "сохранён"}


@app.post("/documents/templates")
async def doc_template_save(request: Request):
    """A DOCX template with fields {{…}}: a new one, or the title, the hints and the file of an existing one."""
    guard_admin(request)
    me = current_user(request) or {}
    f = await request.form()
    upload = f.get("file")
    data = await upload.read() if upload is not None and hasattr(upload, "read") else b""
    raw_id = str(f.get("template_id") or "").strip()
    template_id = int(raw_id) if raw_id.isdigit() else 0
    hints = {}
    for key, value in f.multi_items():
        if key.startswith("field_"):
            hints[str(value)] = str(f.get("hint_" + key[len("field_"):]) or "")
    try:
        doc_templates.save_template(template_id=template_id or None, title=str(f.get("title") or ""),
                                    description=str(f.get("description") or ""),
                                    filename=getattr(upload, "filename", "") or "", data=data,
                                    output=str(f.get("output") or "docx"), hints=hints,
                                    by=me.get("name") or me.get("username") or "")
    except doc_templates.TemplateError as exc:
        return RedirectResponse("/documents?error=" + quote(str(exc)) + "#templates", 303)
    return RedirectResponse("/documents?msg=saved#templates", 303)


@app.post("/documents/templates/{template_id}/{action}")
def doc_template_action(request: Request, template_id: int, action: str):
    guard_admin(request)
    try:
        if action == "toggle":
            doc_templates.toggle(template_id)
        elif action == "delete":
            doc_templates.delete(template_id)
        else:
            raise HTTPException(404, "Неизвестное действие")
    except doc_templates.TemplateError as exc:
        raise HTTPException(404, str(exc)) from None
    return RedirectResponse(f"/documents?msg={'deleted' if action == 'delete' else 'toggled'}#templates", 303)


@app.get("/documents/templates/{template_id}/{what}")
def doc_template_file(request: Request, template_id: int, what: str):
    """The uploaded template (file) or the template with the names of its fields filled in (sample)."""
    guard_admin(request)
    try:
        if what == "sample":
            name, data = doc_templates.sample(template_id)
        elif what == "file":
            name, data = doc_templates.original(template_id)
        else:
            raise HTTPException(404, "Неизвестный файл")
    except doc_templates.TemplateError as exc:
        raise HTTPException(404, str(exc)) from None
    return Response(data, media_type=doc_templates.DOCX_MIME,
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})


@app.get("/documents/drafts/{draft_id}/{kind}")
def doc_draft_file(request: Request, draft_id: int, kind: str):
    guard_admin(request)
    if kind not in ("docx", "pdf"):
        raise HTTPException(404, "Неизвестный файл")
    try:
        name, mime, data = doc_templates.draft_file(draft_id, kind)
    except doc_templates.TemplateError as exc:
        raise HTTPException(404, str(exc)) from None
    return Response(data, media_type=mime, headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})


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
            t.title = " ".join(str(form.get(f"title_{t.id}") or "").split())[:500] or t.title  # the field wraps: no line breaks
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
            m.tasks.append(Task(title=" ".join(form["new_title"].split())[:500], description=(form.get("new_desc") or "").strip(),
                                assignee_id=int(a) if a else None,
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
            if not user_may_access_meeting_audio(request, m):
                raise HTTPException(404, "Совещание не найдено.")
            if m.status in ("queued", "transcribing", "analyzing", "sending", "delivery_queued", "delivery_retry"):
                raise HTTPException(409, "Дождитесь завершения обработки")
            if s.query(Delivery).filter(Delivery.meeting_id == mid, Delivery.status.in_(("pending", "sending"))).first():
                raise HTTPException(409, "Нельзя удалить совещание с незавершённой доставкой")
            if m.audio_path:
                pathlib.Path(m.audio_path).unlink(missing_ok=True)
            from .db import VoiceSampleCleanup
            for sample in s.query(EmployeeVoiceSample).filter_by(source_meeting_id=mid).all():
                if sample.sample_blob_path:
                    s.add(VoiceSampleCleanup(employee_id=sample.employee_id, storage_path=sample.sample_blob_path))
                s.delete(sample)
            voice_memory.forget_meeting(s, mid)  # the voices remembered from this meeting go with it
            audio_archive.forget(s, m, "meeting")
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
    env = {"openrouter_api_key": bool(config.OPENROUTER_API_KEY),
           "telegram_bot_token": bool(config.TELEGRAM_BOT_TOKEN), "bitrix_webhook_url": bool(config.BITRIX_WEBHOOK_URL),
           "ticnote_appkey": bool(config.TICNOTE_APPKEY)}
    for k in settings_store.SECRET_KEYS + settings_store.HIDDEN_KEYS:
        st.pop(k, None)  # secrets never go back to the browser, only a mask
    ai = bool(st.get("ai_employee"))
    from . import digest
    return page(request, "settings.html", s=st, chats=[] if ai else known_chats(), msg=msg, masked=masked, env=env,
                digest_frequencies=digest.FREQUENCY_RU, weekdays=digest.WEEKDAYS_RU, digest_people=_telegram_people(),
                account=userbot.status() if ai else None, persona_default=voice_default_persona(), spent=round(llm.spent_today(), 2),
                notion=notion.connection_status(), storage=storage.describe(), storage_usage=audio_archive.usage(),
                retention_days=config.RECORDING_RETENTION_DAYS, diarization_methods=diarize.METHODS,
                diarization_hints=diarize.HINTS, voice_service=config.voice_service_enabled(),
                diarization_unavailable={name: diarize.unavailable(name) for name in diarize.METHODS},
                voice_memory_days=speaker_id.VOICE_SAMPLE_RETENTION_DAYS, voice_memory_meetings=voice_memory.MIN_MEETINGS)


def voice_default_persona() -> str:
    from . import voice
    return voice.DEFAULT_PERSONA


def _chat_ids(form, name: str) -> list[str]:
    ids = [str(v) for v in form.getlist(name)]
    extra = form.get(name + "_extra", "")
    ids += [x.strip() for x in extra.replace("\n", ",").split(",") if x.strip()]
    return list(dict.fromkeys(ids))


def _catalog_items(url: str, headers: dict) -> list[dict]:
    response = httpx.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json()
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]


def _catalog_price(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _stt_usd_per_hour(pricing: dict) -> float | None:
    """Duration-priced STT bills per second of audio. Token-priced models and odd units get no hourly rate."""
    prompt, completion = _catalog_price(pricing.get("prompt")), _catalog_price(pricing.get("completion"))
    if prompt is None or not 0 < prompt < 0.01 or completion:  # $36+ per hour is not a per-second price
        return None
    return round(prompt * 3600, 2)


@app.get("/settings/models")
def settings_models(request: Request):
    guard_admin(request)
    headers = {}
    key = config.openrouter_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # Two independent requests: one failing catalog must not empty every model list.
    warnings, items, stt_items = [], [], []
    try:
        items = _catalog_items(f"{config.OPENROUTER_BASE_URL}/models", headers)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Could not load OpenRouter model catalog: %s", exc)
        warnings.append("общий каталог моделей не загрузился")
    try:
        stt_items = _catalog_items(f"{config.OPENROUTER_BASE_URL}/models?output_modalities=transcription", headers)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Could not load OpenRouter STT catalog: %s", exc)
        warnings.append("каталог моделей распознавания речи не загрузился")
    if not items and not stt_items:
        raise HTTPException(502, "Не удалось загрузить каталог OpenRouter. Проверьте подключение и повторите.")
    models, stt_from_main = [], []
    for item in items:
        model_id = item["id"][:200]
        architecture = item.get("architecture") or {}
        inputs = architecture.get("input_modalities") or item.get("input_modalities") or []
        outputs = architecture.get("output_modalities") or item.get("output_modalities") or []
        pricing = item.get("pricing") or {}
        prompt, completion = _catalog_price(pricing.get("prompt")), _catalog_price(pricing.get("completion"))
        if "transcription" in outputs:
            stt_from_main.append(item)  # the main catalog may list STT models too
        models.append({
            "id": model_id,
            "name": str(item.get("name") or model_id)[:200],
            "provider": model_id.lstrip("~").split("/", 1)[0],
            "audio_input": "audio" in inputs,
            "text_input": not inputs or "text" in inputs,
            "text_output": not outputs or "text" in outputs,
            "context_length": item.get("context_length"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
            "free": model_id == "openrouter/free" or model_id.endswith(":free") or (prompt == 0 and completion == 0),
            # :batch variants answer through the asynchronous batch queue, too slow for meetings and chats
            "batch": model_id.endswith(":batch"),
        })
    models.sort(key=lambda model: model["id"].casefold())
    transcription_models, seen = [], set()
    for item in stt_items + stt_from_main:
        model_id = item["id"][:200]
        if model_id in seen:
            continue
        seen.add(model_id)
        pricing = item.get("pricing") or {}
        transcription_models.append({
            "id": model_id, "name": str(item.get("name") or model_id)[:200], "kind": "stt",
            "provider": model_id.lstrip("~").split("/", 1)[0],
            "prompt_price": pricing.get("prompt"), "completion_price": pricing.get("completion"),
            "usd_per_hour": _stt_usd_per_hour(pricing),
            "description": " ".join(str(item.get("description") or "").split())[:300],
        })
    # Gemini hears words and voices in one pass (best on the speech benchmark, see scripts/speech_benchmark.py).
    for model in models:
        if (model["audio_input"] and model["text_output"] and not model["batch"]
                and transcribe.chat_model(model["id"]) and model["id"] not in seen):
            seen.add(model["id"])
            transcription_models.append({
                "id": model["id"], "name": model["name"], "kind": "chat", "provider": model["provider"],
                "prompt_price": model["prompt_price"], "completion_price": model["completion_price"],
                "usd_per_hour": None, "description": "",
            })
    transcription_models.sort(key=lambda model: (model["kind"] != "stt", model["name"].casefold(), model["id"]))
    return {"models": models, "count": len(models), "transcription_models": transcription_models,
            "transcription_count": len(transcription_models),
            "stt_count": sum(model["kind"] == "stt" for model in transcription_models), "warnings": warnings}


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
              "bitrix_enabled", "ai_employee", "ticnote_enabled", "voice_debug_mode", "voice_memory",
              "silence_working_hours"):
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
        if k in updates and (len(updates[k]) > 200 or not re.fullmatch(r"~?[A-Za-z0-9._:/-]+", updates[k])):
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
    if "diarization_method" in f:
        if f["diarization_method"] not in diarize.METHODS:
            raise HTTPException(400, "Выберите способ разметки голосов из списка")
        updates["diarization_method"] = f["diarization_method"]
    if "diarization_voice_threshold" in f:
        updates["diarization_voice_threshold"] = _voice_threshold(f["diarization_voice_threshold"])
    if "speaker_name_threshold" in f:
        updates["speaker_name_threshold"] = _name_threshold(f["speaker_name_threshold"])
    if "voice_memory_threshold" in f:
        updates["voice_memory_threshold"] = _memory_threshold(f["voice_memory_threshold"])
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
    if "digest_time" in f:  # «09:30» of the time field
        found = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(f["digest_time"]))
        if not found or int(found.group(1)) > 23 or int(found.group(2)) > 59:
            raise HTTPException(400, "Время сводки — часы и минуты, например 09:00")
        updates["digest_hour"], updates["digest_minute"] = int(found.group(1)), int(found.group(2))
    if f.get("present_digest_recipients") == "1":
        updates["digest_recipients"] = sorted({int(x) for x in f.getlist("digest_recipients") if str(x).isdigit()})
    for k, top, label in (("digest_hour", 23, "час сводки от 0 до 23"), ("digest_weekday", 6, "день недели сводки")):
        if k in f:
            try:
                value = int(str(f[k]).strip())
            except ValueError:
                raise HTTPException(400, f"Укажите {label}") from None
            if not 0 <= value <= top:
                raise HTTPException(400, f"Укажите {label}")
            updates[k] = value
    if f.get("present_quiet_days") == "1":
        days = sorted({int(x) for x in f.getlist("quiet_days") if str(x).isdigit() and 0 <= int(x) <= 6})
        if len(days) == 7:
            raise HTTPException(400, "Выходные: оставьте хотя бы один рабочий день")
        updates["quiet_days"] = days
    if "person_hourly_limit" in f:
        try:
            limit = int(str(f["person_hourly_limit"]).strip() or 0)
        except ValueError:
            raise HTTPException(400, "Лимит сообщений одному человеку: целое число") from None
        if not 0 <= limit <= 30:
            raise HTTPException(400, "Лимит сообщений одному человеку: от 0 до 30 в час")
        updates["person_hourly_limit"] = limit
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
        return RedirectResponse("/settings?msg=" + quote("Сводка: некому — выберите получателей или назначьте проверяющих "
                                                         "в карточке сотрудника"), 303)
    got = digest.send(mark=False)
    return RedirectResponse("/settings?msg=" + quote(f"Сводка отправлена: {got} из {len(digest.recipients())}"), 303)


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


@app.post("/settings/test-storage")
def test_storage(request: Request):
    """«Проверить хранилище»: writes, reads through a presigned link and deletes a small file."""
    guard_admin(request)
    try:
        text = storage.check()
    except storage.StorageError as e:
        text = str(e)
    except Exception as e:  # noqa: BLE001
        log.warning("storage check failed", exc_info=True)
        text = f"ошибка {type(e).__name__}"
    return RedirectResponse("/settings?msg=" + quote("Хранилище записей: " + text[:300]) + "#recording-storage", 303)


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
EMPLOYEE_TABS = ("profile", "departments", "contacts", "voice")
VOICE_STATUS_RU = {"ready": "признаки готовы", "pending": "ждёт сервиса признаков", "failed": "признаки не извлечены"}


def _plural_ru(count: int, forms: tuple) -> str:
    tens, ones = count % 100, count % 10
    if 11 <= tens <= 14:
        return forms[2]
    return forms[0] if ones == 1 else forms[1] if 2 <= ones <= 4 else forms[2]


def _initials(name: str) -> str:
    parts = [part for part in re.split(r"[\s\-]+", name or "") if part]
    return "".join(part[0] for part in parts[:2]).upper() or "?"


def _employee_main_group(employee):
    """A person is listed under the department they head, otherwise under the first one alphabetically."""
    memberships = sorted((m for m in employee.department_memberships if m.department),
                         key=lambda m: (not m.is_primary, m.department.name.lower()))
    if memberships:
        return f"d{memberships[0].department_id}", memberships[0].department.name
    legacy = (employee.department or "").strip()
    if legacy:
        return "t:" + legacy.lower(), legacy
    return "none", "Без подразделения"


def _employee_groups(employees):
    groups = {}
    for employee in employees:
        key, name = _employee_main_group(employee)
        groups.setdefault(key, {"key": key, "name": name, "items": []})["items"].append(employee)
    ordered = sorted(groups.values(), key=lambda g: (g["key"] == "none", g["name"].lower()))
    for group in ordered:
        group["items"].sort(key=lambda e: (not e.active, e.name.lower()))
    return ordered


def _employee_search_text(employee) -> str:
    parts = [employee.name, employee.aliases, employee.position, employee.department, employee.telegram_chat_id,
             (employee.competencies or "")[:400]]
    for membership in employee.department_memberships:
        parts += [membership.department.name if membership.department else "", membership.role]
    return " ".join(part for part in parts if part).lower().replace("ё", "е")


def _employee_voice_info(employees, readiness):
    """List badge from the collection heuristic: independent recordings out of the required minimum.

    It shows how far sample collection has got, not a calibrated recognition accuracy."""
    people = {p["employee_id"]: p for p in readiness.get("people", [])}
    need = int(readiness.get("min_independent_samples") or speaker_id.READY_INDEPENDENT_SAMPLES)
    info = {}
    for employee in employees:
        person = people.get(employee.id) or {}
        count = int(person.get("independent_samples") or 0)
        if not employee.active:
            state = "off"
        elif person.get("ready"):
            state = "ready"
        elif count >= need:
            state = "check"  # enough recordings, but they disagree or resemble a colleague
        elif count:
            state = "partial"
        else:
            state = "none"
        info[employee.id] = {
            "state": state, "count": count, "need": need,
            "pct": 100 if state == "ready" else (min(100, round(100 * count / need)) if need else 0),
            "samples": len(employee.voice_samples),
            "seconds": round(sum(float(sample.sample_duration or 0) for sample in employee.voice_samples)),
            "legacy": employee.voiceprint is not None,
        }
    return info


def _employee_summary(employees, voice_info) -> str:
    active = [e for e in employees if e.active]
    parts = [f"{len(active)} {_plural_ru(len(active), ('человек', 'человека', 'человек'))}",
             f"{sum(1 for e in active if e.telegram_chat_id)} с Telegram",
             f"голос готов у {sum(1 for e in active if voice_info[e.id]['state'] == 'ready')}"]
    inactive = len(employees) - len(active)
    if inactive:
        parts.append(f"неактивных: {inactive}")
    return " · ".join(parts)


def _employees_context(request, edit: int = 0, add: bool = False, voice_msg: str = "", tab: str = "",
                       saved: str = ""):
    owners = {f"user:{current_user(request)['id']}", request.session.get("recording_owner")}
    with SessionLocal() as s:
        emps = s.query(Employee).order_by(Employee.active.desc(), Employee.name).all()
        departments = s.query(Department).order_by(Department.active.desc(), Department.name).all()
        visible_voice_samples = {}
        for employee in emps:
            visible_voice_samples[employee.id] = [sample for sample in employee.voice_samples
                                                  if not sample.access_owner or sample.access_owner in owners]
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
    readiness = speaker_id.identification_readiness({e.id: list(e.voice_samples) for e in emps if e.active})
    voice_info = _employee_voice_info(emps, readiness)
    panel_employee = next((e for e in emps if e.id == edit), None) if edit else None
    voice_memory_card = None
    if panel_employee is not None:
        with SessionLocal() as s:
            voice_memory_card = voice_memory.card(s, panel_employee)
    if tab not in EMPLOYEE_TABS or (tab == "voice" and panel_employee is None):
        tab = "voice" if voice_msg and panel_employee is not None else "profile"
    return dict(
        employees=emps, employee_groups=_employee_groups(emps), chats=chats, linked=linked, rights=rights,
        rights_ru=agent.RIGHTS_RU, access_levels=access.LEVELS, access_hints=access.LEVEL_HINT,
        order_scopes=("",) + agent_skills.ORDER_SCOPES, scope_ru=agent_skills.SCOPE_RU, scope_hint=agent_skills.SCOPE_HINT,
        chat_skills=[x for x in agent_skills.SKILLS.values() if x.name not in agent_skills.PAIRED],
        skills_off_everywhere=agent_skills.switched_off(), strong_rights=access.STRONG, departments=departments, positions=settings_store.POSITIONS,
        edit=panel_employee.id if panel_employee else 0, add=bool(add) and panel_employee is None,
        panel_employee=panel_employee, panel_tab=tab, saved_name=saved[:200], voice_msg=voice_msg,
        voice_service_enabled=config.voice_service_enabled(),
        visible_voice_samples=visible_voice_samples, voice_readiness=readiness, voice_info=voice_info,
        voice_memory=voice_memory_card,
        voice_status_ru=VOICE_STATUS_RU, initials={e.id: _initials(e.name) for e in emps},
        search_text={e.id: _employee_search_text(e) for e in emps},
        employee_summary=_employee_summary(emps, voice_info),
        silence_people=[e for e in emps if e.active and e.telegram_chat_id], silence_defaults=_silence_defaults(),
    )


def _silence_defaults() -> dict:
    """The settings used when neither the card nor the department says how long to wait (placeholders)."""
    def number(key, default):
        try:
            return int(float(settings_store.get(key) or default))
        except (TypeError, ValueError):
            return default
    return {"hours": number("case_escalate_hours", 24), "reminders": number("case_max_reminders", 2)}


def _silence_fields(holder, form, own_id=None) -> None:
    """Whom to tell and when, if the person (or the department's people) does not answer: from a card form."""
    def number(key, low, high):
        raw = str(form.get(key) or "").strip()
        if not raw:
            return None
        try:
            value = int(float(raw))
        except ValueError:
            raise HTTPException(400, "Часы и число напоминаний — целые числа") from None
        return max(low, min(high, value))
    raw = str(form.get("silence_contact_id") or "").strip()
    contact_id = None
    if raw:
        try:
            contact_id = int(raw)
        except ValueError:
            raise HTTPException(400, "Некорректный контакт на случай молчания") from None
        if own_id is not None and contact_id == own_id:
            raise HTTPException(400, "Нельзя сообщать о молчании самому молчащему")
    holder.silence_contact_id = contact_id
    holder.silence_hours = number("silence_hours", 1, 336)
    holder.silence_reminders = number("silence_reminders", 0, 10)


def _render_fragment(request, name: str, **ctx) -> str:
    return page(request, name, **ctx).body.decode("utf-8")


@app.get("/employees", response_class=HTMLResponse)
def employees_view(request: Request, edit: int = 0, add: int = 0, voice_msg: str = "", tab: str = "",
                   saved: str = ""):
    guard_admin(request)
    return page(request, "employees.html", **_employees_context(request, edit, bool(add), voice_msg, tab, saved))


@app.get("/employees/panel", response_class=HTMLResponse)
def employees_panel(request: Request, edit: int = 0, add: int = 0, tab: str = ""):
    """The side card alone: the directory swaps it in without reloading the list."""
    guard_admin(request)
    ctx = _employees_context(request, edit, bool(add), tab=tab)
    if edit and ctx["panel_employee"] is None:
        raise HTTPException(404, "Сотрудник не найден")
    return page(request, "_employee_panel.html", **ctx)


def _employee_store(form, id, name, aliases, position, department, competencies, responsibilities, context,
                    channel, telegram_chat_id, telegram_manual, bitrix_user_id, active) -> int:
    if not name.strip():
        raise HTTPException(400, "Укажите имя и фамилию сотрудника")
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
        if form.get("skills_form"):  # whom they may give orders to and which skills are on (not for the director)
            scope = str(form.get("order_scope") or "").strip()
            e.order_scope = scope if scope in agent_skills.ORDER_SCOPES else ""
            on = {str(x) for x in form.getlist("skill_allowed")}
            names = [n for n in agent_skills.SKILLS if n not in agent_skills.PAIRED]
            e.skills_allowed = None if all(n in on for n in names) else [n for n in names if n in on]
        if form.get("talk_form"):  # how to address the person, and whom to tell when they do not answer
            e.address_as = str(form.get("address_as") or "").strip()[:120]
            address_form = str(form.get("address_form") or "").strip()
            e.address_form = address_form if address_form in voice.ADDRESS_FORMS else ""
            _silence_fields(e, form, own_id=e.id if id else None)
            if e.silence_contact_id is not None:
                contact = s.get(Employee, e.silence_contact_id)
                if contact is None or not contact.active or not contact.telegram_chat_id:
                    raise HTTPException(400, "Сообщать о молчании можно только активному сотруднику с Telegram")
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
    return saved_employee_id


@app.post("/employees")
async def employee_save(request: Request, id: int = Form(0), name: str = Form(""), aliases: str = Form(""),
                  position: str = Form(""), department: str = Form(""), competencies: str = Form(""),
                  responsibilities: str = Form(""), context: str = Form(""),
                  channel: str = Form("telegram"), telegram_chat_id: str = Form(""),
                  telegram_manual: str = Form(""), bitrix_user_id: str = Form(""), active: str = Form("")):
    guard_admin(request)
    form = await request.form()
    # The directory saves the card in place (fetch) and gets the refreshed list and card back;
    # without JavaScript the same form posts normally and is redirected.
    wants_json = request.headers.get("x-requested-with") == "fetch"
    add_next = str(form.get("next") or "") == "add"
    try:
        saved_employee_id = _employee_store(form, id, name, aliases, position, department, competencies,
                                            responsibilities, context, channel, telegram_chat_id, telegram_manual,
                                            bitrix_user_id, active)
    except HTTPException as exc:
        if wants_json:
            return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)
        raise
    agent.invalidate_roster()
    if wants_json:
        ctx = _employees_context(request, 0 if add_next else saved_employee_id, add_next,
                                 tab=str(form.get("tab") or ""), saved=name.strip() if add_next else "")
        message = (f"Сохранено: {name.strip()}. Можно заполнять следующую карточку." if add_next
                   else "Сотрудник добавлен." if not id else "Изменения сохранены.")
        return JSONResponse({
            "ok": True, "id": saved_employee_id, "created": not id, "next": "add" if add_next else "",
            "message": message, "summary": ctx["employee_summary"],
            "list_html": _render_fragment(request, "_employee_list.html", **ctx),
            "panel_html": _render_fragment(request, "_employee_panel.html", **ctx),
        })
    if add_next:
        return RedirectResponse(f"/employees?add=1&saved={quote(name.strip()[:200], safe='')}#employee-create", 303)
    target = (
        f"/employees?edit={saved_employee_id}#employee-{saved_employee_id}"
        if id else f"/employees#employee-{saved_employee_id}"
    )
    return RedirectResponse(target, 303)


def _voice_redirect(employee_id: int, message: str):
    # Back to the same card with the voice tab open, not to the top of the directory.
    return RedirectResponse(f"/employees?edit={employee_id}&tab=voice&voice_msg={quote(message, safe='')}#voice", 303)


def _meeting_redirect(mid: int, message: str):
    return RedirectResponse(f"/meetings/{mid}?voice_msg={quote(message, safe='')}", 303)


def _voice_write_guard(request):
    guard_admin(request)
    from urllib.parse import urlsplit
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlsplit(origin).netloc != request.url.netloc):
        raise HTTPException(403, "Запрос должен исходить из этой панели.")


def _meeting_audio_owner(session, meeting):
    # Match both the explicit link and reused audio paths. No superuser bypass:
    # /recordings/{rid}/audio enforces ownership even for other superusers.
    rows = session.query(Recording).filter(
        (Recording.meeting_id == meeting.id) | (Recording.path == (meeting.audio_path or ""))
    ).all()
    return [r for r in rows if r.meeting_id == meeting.id or (meeting.audio_path and r.path == meeting.audio_path)]


def user_may_access_meeting_audio(request, meeting):
    guard(request)
    with SessionLocal() as session:
        rows = _meeting_audio_owner(session, meeting)
    return all(r.owner == recording_owner(request, r.id) for r in rows)


def _voice_meeting(session, request, mid):
    meeting = session.get(Meeting, mid)
    if meeting is None or not user_may_access_meeting_audio(request, meeting):
        raise HTTPException(404, "Совещание не найдено.")
    return meeting


def _candidate(meeting, start, end, speaker=None):
    start, end = speaker_id._finite(start), speaker_id._finite(end)
    if start is None or end is None or start < 0 or not 5 <= end - start <= 30:
        raise HTTPException(400, "Некорректный интервал фрагмента.")
    for clip in speaker_id.propose_clear_speaker_clips(meeting.transcript or []):
        if abs(clip["start"] - start) < 0.0005 and abs(clip["end"] - end) < 0.0005:
            if speaker is None or clip["speaker"] == speaker:
                return clip
    raise HTTPException(400, "Фрагмент не входит в доступные кандидаты или содержит наложение реплик.")


def _meeting_voice_enrollment_context(session, meeting, manual=False):
    debug = settings_store.get("voice_debug_mode") is True
    active = session.query(Employee).filter(Employee.active.is_(True)).all()
    by_employee = {e.id: list(e.voice_samples) for e in active}
    readiness = speaker_id.identification_readiness(by_employee)
    if not (debug or manual) or meeting.status in ("queued", "transcribing", "analyzing"):
        return [], debug, readiness
    if not meeting.audio_path or not pathlib.Path(meeting.audio_path).is_file():
        return [], debug, readiness
    # Stop collecting for sufficiently sampled known people, never hide an unknown
    # just because another subset of employees has enough samples.
    per_person = {p["employee_id"]: p["ready"] for p in readiness.get("people", [])}
    pending = []
    for clip in speaker_id.propose_clear_speaker_clips(meeting.transcript or []):
        if speaker_id.overlapping_confirmed_sample(session, meeting.id, clip["start"], clip["end"]):
            continue
        known_id = clip.get("speaker_employee_id")
        if not manual and known_id is not None and per_person.get(known_id):
            continue
        pending.append(clip)
    return pending[:8], debug, readiness


@app.get("/meetings/{mid}/speaker-clip")
def meeting_speaker_clip(request: Request, mid: int, start: float, end: float):
    guard(request)
    with SessionLocal() as session:
        meeting = _voice_meeting(session, request, mid)
        clip = _candidate(meeting, start, end)
        source = meeting.audio_path
        if not source or not pathlib.Path(source).is_file():
            raise HTTPException(404, "Аудиозапись недоступна.")
    with tempfile.TemporaryDirectory(prefix="sysai_clip_") as tmp:
        target = pathlib.Path(tmp) / "clip.wav"
        if speaker_id.extract_interval(source, target, clip["start"], clip["end"]) is None:
            raise HTTPException(400, "Не удалось вырезать полный фрагмент.")
        if not speaker_id.clip_has_speech(target):
            raise HTTPException(400, "Фрагмент слишком тихий, перегружен или содержит слишком много пауз.")
        raw = target.read_bytes()
    return Response(raw, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@app.post("/meetings/{mid}/speaker-clips/confirm")
def meeting_confirm_speaker_clip(request: Request, mid: int, speaker: str = Form(...),
                                 start: float = Form(...), end: float = Form(...),
                                 employee_id: int = Form(...), consent: str = Form(""),
                                 listened: str = Form("")):
    _voice_write_guard(request)
    with SessionLocal() as session:
        meeting = _voice_meeting(session, request, mid)
        clip = _candidate(meeting, start, end, speaker)
        if consent not in ("yes", "1", "on", "true") or listened != "yes":
            return _meeting_redirect(mid, "Нужно прослушать фрагмент, подтвердить один голос и отдельное согласие сотрудника.")
        source = pathlib.Path(meeting.audio_path or "")
        if not meeting.audio_path or not source.is_file():
            return _meeting_redirect(mid, "Аудиозапись недоступна.")
        # Lock employee before provider work so a simultaneous revoke wins serially.
        employee = session.query(Employee).filter_by(id=employee_id, active=True).with_for_update().one_or_none()
        if employee is None:
            return _meeting_redirect(mid, "Активный сотрудник не найден.")
        owners = _meeting_audio_owner(session, meeting)
        access_owner = owners[0].owner if owners else ""
        import hashlib
        source_hash = hashlib.sha256()
        before = source.stat()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                source_hash.update(block)
        with tempfile.TemporaryDirectory(prefix="sysai_confirm_clip_") as tmp:
            target = pathlib.Path(tmp) / "clip.wav"
            extracted = speaker_id.extract_interval(source, target, clip["start"], clip["end"])
            after = source.stat()
            if extracted is None or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return _meeting_redirect(mid, "Запись изменилась или фрагмент неполный; прослушайте заново.")
            actual_start, duration = extracted
            try:
                row = speaker_id.store_confirmed_meeting_sample(
                    session, employee_id=employee_id, meeting_id=mid,
                    start=actual_start, end=actual_start + duration, wav_path=target,
                    confirmed_by=str(current_user(request)["id"]), access_owner=access_owner,
                    source_audio_sha256=source_hash.hexdigest(),
                )
                session.commit()
                status = row.embedding_status
            except speaker_id.SpeakerIdentityError as exc:
                session.rollback()
                return _meeting_redirect(mid, str(exc)[:300])
        message = {"ready": "Зашифрованный образец и голосовые признаки сохранены. Точность ещё не проверена.",
                   "pending": "Аудио сохранено в БД. Сервис признаков не настроен: образец пока не участвует в идентификации.",
                   "failed": "Аудио сохранено в БД, извлечение признаков не удалось. Можно повторить в карточке сотрудника."}
        return _meeting_redirect(mid, message[status])


def _voice_sample(session, request, eid, sid):
    guard_admin(request)
    row = session.query(EmployeeVoiceSample).filter_by(employee_id=eid, id=sid).with_for_update().one_or_none()
    if row is None or not speaker_id._not_expired(row):
        raise HTTPException(404, "Образец не найден или срок хранения истёк.")
    if row.access_owner and row.access_owner not in {f"user:{current_user(request)['id']}", request.session.get("recording_owner")}:
        raise HTTPException(404, "Образец не найден.")
    if row.source_meeting_id is not None:
        _voice_meeting(session, request, row.source_meeting_id)
    return row


@app.get("/employees/{eid}/voice-samples/{sid}/audio")
def employee_voice_sample_audio(request: Request, eid: int, sid: int):
    guard_admin(request)
    with SessionLocal() as session:
        row = _voice_sample(session, request, eid, sid)
        try:
            raw = speaker_id.decrypt_sample_audio(row)
        except speaker_id.SpeakerIdentityError:
            raise HTTPException(409, "Образец не удалось расшифровать. Проверьте постоянный SECRET_KEY.")
    return Response(raw, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@app.post("/employees/{eid}/voice-samples/{sid}/retry")
def employee_voice_sample_retry(request: Request, eid: int, sid: int):
    _voice_write_guard(request)
    with SessionLocal() as session:
        row = _voice_sample(session, request, eid, sid)
        try:
            ready = speaker_id.embed_pending_sample(session, row)
        except speaker_id.SpeakerIdentityError as exc:
            return _voice_redirect(eid, str(exc))
        session.commit()
    return _voice_redirect(eid, "Признаки получены." if ready else "Признаки пока недоступны; аудио сохранено в БД.")


@app.post("/employees/{eid}/voice-samples/{sid}/delete")
def employee_voice_sample_delete_one(request: Request, eid: int, sid: int):
    _voice_write_guard(request)
    from .db import VoiceSampleCleanup
    with SessionLocal() as session:
        row = _voice_sample(session, request, eid, sid)
        if row.sample_blob_path:
            session.add(VoiceSampleCleanup(employee_id=eid, storage_path=row.sample_blob_path))
        session.delete(row)
        session.commit()
    return _voice_redirect(eid, "Образец и голосовые признаки удалены из БД.")


@app.post("/employees/{eid}/voice-sample/delete")
def employee_voice_sample_delete(request: Request, eid: int):
    _voice_write_guard(request)
    with SessionLocal() as session:
        employee = session.query(Employee).filter_by(id=eid).with_for_update().one_or_none()
        if employee is None:
            return _voice_redirect(eid, "Сотрудник не найден.")
        removed = speaker_id.revoke_employee_voice_data(session, eid)
        session.commit()
    # New samples have no external copy. Legacy cleanup is durable and can be retried.
    with SessionLocal() as session:
        speaker_id.retry_sample_cleanup(session)
        session.commit()
    return _voice_redirect(eid, f"Образцы и признаки отозваны ({removed}). Старые внешние копии при ошибке остаются в очереди очистки.")


@app.post("/employees/{eid}/voice-memory")
async def employee_voice_memory(request: Request, eid: int):
    """The employee's consent to remember the voice between meetings: given (the latest confirmed meetings are
    remembered at once) or withdrawn (every print is deleted)."""
    _voice_write_guard(request)
    form = await request.form()
    agreed = str(form.get("consent") or "") == "yes"
    by = _who(request)
    with SessionLocal() as session:
        employee = session.query(Employee).filter_by(id=eid).with_for_update().one_or_none()
        if employee is None:
            return _voice_redirect(eid, "Сотрудник не найден.")
        if agreed and not employee.active:
            return _voice_redirect(eid, "Сотрудник не активен — голос не запоминается.")
        removed = voice_memory.set_consent(session, employee, agreed, by=by)
        session.commit()
    if not agreed:
        return _voice_redirect(eid, f"Согласие отозвано, запомненный голос удалён ({removed}).")
    if not voice_memory.enabled():
        return _voice_redirect(eid, "Согласие отмечено. Голос начнёт запоминаться, когда в настройках включат "
                                    "«Запоминать голоса между встречами» и подключат свой сервис голосов.")
    voice_memory.remember_history(eid, by=by)
    return _voice_redirect(eid, "Согласие отмечено. Голос запоминается по последним подтверждённым совещаниям "
                                "и дальше — после каждого «Кто это?».")


@app.post("/employees/{eid}/voice-memory/forget")
def employee_voice_memory_forget(request: Request, eid: int):
    _voice_write_guard(request)
    with SessionLocal() as session:
        removed = voice_memory.forget(session, eid)
        session.commit()
    return _voice_redirect(eid, f"Запомненный голос удалён ({removed}). Согласие осталось: голос снова начнёт "
                                "запоминаться с подтверждённых совещаний.")


@app.post("/employees/voice-samples/cleanup")
def employee_voice_samples_cleanup(request: Request):
    _voice_write_guard(request)
    with SessionLocal() as session:
        speaker_id.purge_expired_samples(session)
        speaker_id.retry_sample_cleanup(session)
        session.commit()
    return RedirectResponse("/employees", 303)


@app.get("/departments", response_class=HTMLResponse)
def departments_view(request: Request, edit: int = 0, msg: str = "",
                     view: str = "map", open_add: int = 0):
    guard_admin(request)
    view = "list" if view == "list" else "map"
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
            if relation.source_type not in {"company", "employee", "department"}:
                continue
            if relation.target_type not in {"employee", "department"}:
                continue
            if relation.source_type == "employee" and relation.source_id not in valid_employee_ids:
                continue
            if relation.source_type == "department" and relation.source_id not in valid_department_ids:
                continue
            if relation.target_type == "employee" and relation.target_id not in valid_employee_ids:
                continue
            if relation.target_type == "department" and relation.target_id not in valid_department_ids:
                continue
            source_key = f"{relation.source_type}:{relation.source_id}"
            target_key = f"{relation.target_type}:{relation.target_id}"
            kind = "block" if relation.target_type == "department" else "reporting"
            map_edges.append({
                "source": source_key, "target": target_key,
                "kind": kind, "label": relation.label or relation.relation_type,
                "relation_id": relation.id,
            })
            if relation.target_type == "employee":
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
        silence_people=[{"id": e.id, "name": e.name} for e in employees if e.telegram_chat_id],
        silence_defaults=_silence_defaults(),
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


def _org_cycle_exists(session, source_type: str, source_id: int, target_type: str, target_id: int) -> bool:
    """True when adding source -> target would close a directed cycle among people and departments."""
    children = {}
    for edge in session.query(OrgRelation).all():
        children.setdefault((edge.source_type, edge.source_id), set()).add((edge.target_type, edge.target_id))
    pending, visited = [(target_type, target_id)], set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if current == (source_type, source_id):
            return True
        pending.extend(children.get(current, ()))
    return False


@app.post("/departments/map/relation")
async def department_map_relation(request: Request):
    """Create or update a directed line between people, departments, or the company root."""
    guard_admin(request)
    try:
        data = await request.json()
        source_type = str(data.get("source_type") or "")
        source_id = int(data.get("source_id") or 0)
        target_type = str(data.get("target_type") or "employee")
        target_id = int(data.get("target_id") or 0)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(400, "Некорректные параметры связи")
    label = str(data.get("label") or "").strip()[:200]
    relation_type = str(data.get("relation_type") or "reports_to").strip()[:40] or "reports_to"
    allowed = {"company", "employee", "department"}
    if source_type not in allowed or target_type not in {"employee", "department"}:
        raise HTTPException(400, "Можно связать компанию, отдел или сотрудника")
    if source_type == "company" and source_id != 1:
        raise HTTPException(400, "Некорректный узел компании")
    if source_type != "company" and source_id <= 0:
        raise HTTPException(400, "Некорректный исходный блок")
    if target_id <= 0:
        raise HTTPException(400, "Некорректный целевой блок")
    if source_type == target_type and source_id == target_id:
        raise HTTPException(400, "Нельзя связать блок с самим собой")

    with SessionLocal() as s:
        if source_type == "employee":
            parent = s.get(Employee, source_id)
            if parent is None or not parent.active:
                raise HTTPException(404, "Исходный сотрудник не найден")
        if source_type == "department":
            parent_dept = s.get(Department, source_id)
            if parent_dept is None:
                raise HTTPException(404, "Исходное подразделение не найдено")
        if target_type == "employee":
            target = s.get(Employee, target_id)
            if target is None or not target.active:
                raise HTTPException(404, "Целевой сотрудник не найден")
        else:
            target_dept = s.get(Department, target_id)
            if target_dept is None:
                raise HTTPException(404, "Целевое подразделение не найдено")
        if _org_cycle_exists(s, source_type, source_id, target_type, target_id):
            raise HTTPException(409, "Эта связь создаст замкнутый цикл")
        relation = s.query(OrgRelation).filter_by(
            source_type=source_type, source_id=source_id,
            target_type=target_type, target_id=target_id, relation_type=relation_type,
        ).one_or_none()
        if relation is None:
            relation = OrgRelation(
                source_type=source_type, source_id=source_id,
                target_type=target_type, target_id=target_id,
                relation_type=relation_type,
            )
            s.add(relation)
        if label:
            relation.label = label
        elif target_type == "department" and source_type == "department":
            relation.label = "Связь отделов"
        elif target_type == "department" and source_type == "company":
            relation.label = "Подразделение"
        elif source_type == "company":
            relation.label = "Директор"
        else:
            relation.label = "Подчинение"
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
                    return_view: str = Form("list"), silence_form: str = Form(""),
                    silence_contact_id: str = Form(""), silence_hours: str = Form(""),
                    silence_reminders: str = Form("")):
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
        if silence_form:  # whom to tell when somebody of the department does not answer (their own card wins)
            _silence_fields(department, {"silence_contact_id": silence_contact_id, "silence_hours": silence_hours,
                                         "silence_reminders": silence_reminders})
            if department.silence_contact_id is not None:
                contact = s.get(Employee, department.silence_contact_id)
                if contact is None or not contact.active or not contact.telegram_chat_id:
                    raise HTTPException(400, "Сообщать о молчании можно только активному сотруднику с Telegram")
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
            speaker_id.revoke_employee_voice_data(s, eid)
            s.flush()
            s.expire(e, ["voice_samples", "voiceprint"])
            s.delete(e)
        s.commit()
    with SessionLocal() as s:
        speaker_id.retry_sample_cleanup(s)
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
