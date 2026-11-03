"""Google Drive ↔ SYSAI (№28): the documents and the templates in a folder of the company.

Connected with a service account of Google Cloud (its JSON key; the folder is shared with its address as an editor).
In the folder three folders are made (or found):
- «Входящие»: a file put there comes to «Документы» — it is read, analyzed and passed on like a file sent in Telegram
  or by mail (a changed file comes again as its new version);
- «Документы»: the documents that came to SYSAI (Telegram, mail, made by a template) are put there by month, the
  confidential ones are not;
- «Шаблоны»: a DOCX (or a Google document) with fields {{…}} becomes a template of «Документы по шаблону»; a changed
  file updates the template, a removed one switches it off; a template added in the panel is put there, its new file
  replaces the one in Drive.
Google gives a service account no space of its own: new files can be put only into a shared drive (Google Workspace);
in an ordinary «Мой диск» reading works, and the page says that putting does not."""
import base64
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import threading
import time

import httpx

from . import cases, config, doc_templates, reminders, settings_store
from .db import DocTemplate, Document, DriveFile, SessionLocal, now

log = logging.getLogger("sysai.gdrive")

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER = "application/vnd.google-apps.folder"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORT = {"application/vnd.google-apps.document": (DOCX, ".docx"),
          "application/vnd.google-apps.spreadsheet": (XLSX, ".xlsx"),
          "application/vnd.google-apps.presentation": ("application/pdf", ".pdf")}
FOLDERS = {"inbox": "Входящие", "docs": "Документы", "templates": "Шаблоны"}
FILE_FIELDS = "id, name, mimeType, md5Checksum, modifiedTime, size, version, webViewLink"
POLL_MINUTES = 10
RETRY_MINUTES = 30
MAX_IMPORT = 20         # files taken from «Входящие» by one run
MAX_UPLOAD = 10         # documents put into Drive by one run
MAX_TRIES = 3
LOG_KEEP = 30
TOGGLES = ("gdrive_inbox", "gdrive_upload", "gdrive_templates")
KIND_RU = {"inbox": "из «Входящих»", "upload": "в «Документы»", "template": "шаблон"}
_LOCK = threading.Lock()
_TOKEN = {"value": "", "until": 0.0, "email": ""}
_transport = None  # tests put an httpx.MockTransport here


class DriveError(RuntimeError):
    def __init__(self, text: str, reason: str = "", code: int = 0):
        super().__init__(text)
        self.reason, self.code = reason, code


class Abort(RuntimeError):
    pass


QUOTA_TEXT = ("Google не даёт сервисному аккаунту места на обычном «Моём диске»: класть файлы можно только в общий "
              "диск (Google Workspace). Переложите папку в общий диск и добавьте туда сервисный аккаунт "
              "(«Менеджер контента»), затем нажмите «Проверить». Чтение «Входящих» и шаблонов работает и так.")


# ----------------------------------------------------------------------------------------------- the connection
def _key() -> dict:
    from . import mail
    raw = mail.unseal(str(settings_store.get("gdrive_key") or ""))
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def configured() -> bool:
    return bool(settings_store.get("gdrive_key")) and bool(settings_store.get("gdrive_folder"))


def active() -> bool:
    return configured() and any(settings_store.get(k) for k in TOGGLES)


def folder_id(raw: str) -> str:
    """The id of a folder from its link («…/drive/folders/1AbC…?usp=sharing», «open?id=…») or the id itself."""
    raw = str(raw or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", raw) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", raw)
    if m:
        return m.group(1)
    return raw if re.fullmatch(r"[A-Za-z0-9_-]{10,}", raw) else ""


def check_key(text: str) -> dict:
    try:
        data = json.loads(str(text or "").strip())
    except ValueError as exc:
        raise DriveError("Ключ не похож на JSON: вставьте содержимое скачанного файла ключа целиком.") from exc
    if not isinstance(data, dict) or data.get("type") != "service_account" or not data.get("client_email") \
            or "PRIVATE KEY" not in str(data.get("private_key") or ""):
        raise DriveError("Нужен JSON-ключ сервисного аккаунта (в нём есть \"type\": \"service_account\", "
                         "client_email и private_key).")
    return data


def save(key_text: str = "", folder: str = "", by: str = "") -> None:
    """The form of «Интеграции»: an empty key field keeps the saved key."""
    from . import mail
    updates = {}
    if str(key_text or "").strip():
        data = check_key(key_text)
        updates["gdrive_key"] = mail.seal(json.dumps(data))
        _TOKEN.update(value="", until=0.0, email="")
    elif not settings_store.get("gdrive_key"):
        raise DriveError("Вставьте JSON-ключ сервисного аккаунта.")
    if str(folder or "").strip():
        value = folder_id(folder)
        if not value:
            raise DriveError("Не понял ссылку на папку: откройте папку в Google Drive и скопируйте адрес из браузера.")
        if value != settings_store.get("gdrive_folder"):
            updates["gdrive_folder"] = value
            updates["gdrive_state"] = {"by": str(by)[:120]}
    elif not settings_store.get("gdrive_folder"):
        raise DriveError("Вставьте ссылку на папку компании в Google Drive.")
    if updates:
        settings_store.set_many(updates)


def switched(updates: dict) -> None:
    """The switches were saved: documents are put into Drive from now on (not the whole library)."""
    state = dict(settings_store.get("gdrive_state") or {})
    if updates.get("gdrive_upload") and (not settings_store.get("gdrive_upload") or "upload_after" not in state):
        state["upload_after"] = _last_document()
    settings_store.set_many({**updates, "gdrive_state": state})


def forget() -> None:
    settings_store.set_many({"gdrive_key": "", "gdrive_folder": "", "gdrive_state": {}})
    _TOKEN.update(value="", until=0.0, email="")
    with SessionLocal() as s:
        s.query(DriveFile).delete()
        s.commit()


def _last_document() -> int:
    with SessionLocal() as s:
        row = s.query(Document.id).order_by(Document.id.desc()).first()
        return int(row[0]) if row else 0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _assertion(key: dict) -> str:
    """A JWT signed with the key of the service account (RS256): what Google exchanges for an access token."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    issued = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": key["client_email"], "scope": SCOPE, "aud": key.get("token_uri") or TOKEN_URI,
                              "iat": issued, "exp": issued + 3600}).encode())
    private = serialization.load_pem_private_key(str(key["private_key"]).encode(), password=None)
    signature = private.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{claims}.{_b64(signature)}"


def _client() -> httpx.Client:
    return httpx.Client(timeout=60, transport=_transport) if _transport is not None else httpx.Client(timeout=60)


def _token() -> str:
    key = _key()
    if not key:
        raise DriveError("Ключ сервисного аккаунта не сохранён.")
    if _TOKEN["value"] and _TOKEN["until"] > time.time() + 60 and _TOKEN["email"] == key.get("client_email"):
        return _TOKEN["value"]
    try:
        assertion = _assertion(key)
    except (ValueError, TypeError, KeyError) as exc:
        raise DriveError("Ключ сервисного аккаунта повреждён — скачайте новый в Google Cloud.") from exc
    try:
        with _client() as client:
            reply = client.post(key.get("token_uri") or TOKEN_URI,
                                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                      "assertion": assertion})
    except httpx.HTTPError as exc:
        raise DriveError(f"Google не ответил ({type(exc).__name__}).") from exc
    data = _json(reply)
    if reply.status_code != 200 or not data.get("access_token"):
        raise DriveError("Google не принял ключ: " + str(data.get("error_description") or data.get("error")
                                                         or reply.status_code)
                         + ". Проверьте, что ключ не удалён и в проекте включён Google Drive API.")
    _TOKEN.update(value=data["access_token"], until=time.time() + int(data.get("expires_in") or 3600),
                  email=key.get("client_email"))
    return _TOKEN["value"]


def _json(reply) -> dict:
    try:
        data = reply.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _request(method: str, url: str, *, params=None, json_body=None, content=None, headers=None, raw: bool = False):
    """One call of the Drive API; its error as DriveError (with Google's reason: storageQuotaExceeded, notFound…)."""
    params = {**(params or {}), "supportsAllDrives": "true"}
    head = {"Authorization": f"Bearer {_token()}", **(headers or {})}
    try:
        with _client() as client:
            reply = client.request(method, url, params=params, json=json_body, content=content, headers=head)
    except httpx.HTTPError as exc:
        raise DriveError(f"Google Drive не ответил ({type(exc).__name__}).") from exc
    if reply.status_code >= 400:
        data = _json(reply).get("error") or {}
        reasons = [str(e.get("reason") or "") for e in (data.get("errors") or []) if isinstance(e, dict)]
        reason = reasons[0] if reasons else str(data.get("status") or "")
        text = str(data.get("message") or reply.text[:200] or reply.status_code)
        if reason == "storageQuotaExceeded" or "storage quota" in text.lower():
            raise DriveError(QUOTA_TEXT, "storageQuotaExceeded", reply.status_code)
        if reason == "accessNotConfigured" or "has not been used in project" in text:
            raise DriveError("В проекте Google Cloud не включён Google Drive API: включите его («APIs & Services» → "
                             "«Library» → «Google Drive API») и через пару минут нажмите «Проверить».", reason,
                             reply.status_code)
        if reply.status_code == 404:
            raise DriveError("Файл или папка не найдены — или не открыты для сервисного аккаунта.", "notFound", 404)
        if reply.status_code in (401, 403) and reason in ("insufficientFilePermissions", "forbidden", ""):
            raise DriveError("Нет доступа: откройте папку для сервисного аккаунта с правом «Редактор».", reason,
                             reply.status_code)
        raise DriveError(f"Google Drive: {text[:200]}", reason, reply.status_code)
    if raw:
        return reply.content
    return _json(reply)


def _q(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


def children(parent: str, folders: bool = False, name: str = "") -> list:
    """The files (or the folders) right in a folder, all pages."""
    q = [f"'{_q(parent)}' in parents", "trashed = false",
         f"mimeType {'=' if folders else '!='} '{FOLDER}'"]
    if name:
        q.append(f"name = '{_q(name)}'")
    out, page = [], None
    for _ in range(20):
        params = {"q": " and ".join(q), "fields": f"nextPageToken, files({FILE_FIELDS})", "pageSize": 200,
                  "includeItemsFromAllDrives": "true", "orderBy": "modifiedTime"}
        if page:
            params["pageToken"] = page
        data = _request("GET", f"{API}/files", params=params)
        out.extend(data.get("files") or [])
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def _folder(parent: str, name: str, create: bool = True) -> str:
    found = children(parent, folders=True, name=name)
    if found:
        return found[0]["id"]
    if not create:
        return ""
    made = _request("POST", f"{API}/files", params={"fields": "id"},
                    json_body={"name": name, "mimeType": FOLDER, "parents": [parent]})
    return made.get("id") or ""


def _upload(name: str, mime: str, data: bytes, parent: str) -> dict:
    boundary = "sysai" + secrets.token_hex(8)
    meta = json.dumps({"name": name, "parents": [parent]}, ensure_ascii=False)
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: {mime or 'application/octet-stream'}\r\n\r\n").encode() + data + \
        f"\r\n--{boundary}--".encode()
    return _request("POST", f"{UPLOAD}/files", params={"uploadType": "multipart", "fields": FILE_FIELDS},
                    content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})


def _replace(file_id: str, mime: str, data: bytes) -> dict:
    return _request("PATCH", f"{UPLOAD}/files/{file_id}", params={"uploadType": "media", "fields": FILE_FIELDS},
                    content=data, headers={"Content-Type": mime or "application/octet-stream"})


def _download(f: dict) -> tuple:
    """(bytes, mime, filename): a Google document comes as DOCX, a table as XLSX, a presentation as PDF."""
    mime, name = f.get("mimeType") or "", f.get("name") or "файл"
    if mime in EXPORT:
        target, ext = EXPORT[mime]
        data = _request("GET", f"{API}/files/{f['id']}/export", params={"mimeType": target}, raw=True)
        return data, target, name if name.lower().endswith(ext) else name + ext
    return _request("GET", f"{API}/files/{f['id']}", params={"alt": "media"}, raw=True), mime, name


def _sig(f: dict) -> str:
    return str(f.get("md5Checksum") or (f"v{f['version']}" if f.get("version") else "") or f.get("modifiedTime") or "")


def check() -> dict:
    """«Проверить»: the key works, the folder opens, its three folders are there, and whether files can be put."""
    with _LOCK:
        state = dict(settings_store.get("gdrive_state") or {})
        root = str(settings_store.get("gdrive_folder") or "")
        meta = _request("GET", f"{API}/files/{root}", params={"fields": "id, name, mimeType, driveId, capabilities"})
        if meta.get("mimeType") != FOLDER:
            raise DriveError("По ссылке не папка: нужна ссылка на папку Google Drive.")
        caps = meta.get("capabilities") or {}
        if caps and caps.get("canAddChildren") is False:
            raise DriveError("Папка открыта только для чтения: дайте сервисному аккаунту право «Редактор».")
        folders, missing, problem = {}, [], None
        for k, name in FOLDERS.items():
            try:
                folders[k] = _folder(root, name)
            except DriveError as exc:
                missing.append(f"«{name}»")
                problem = exc
        if missing:
            raise DriveError(f"Не получилось создать папки {', '.join(missing)} ({problem}). Создайте их в папке "
                             "компании сами и нажмите «Проверить».")
        state.update(root_name=meta.get("name") or "", shared=bool(meta.get("driveId")), folders=folders,
                     email=_key().get("client_email", ""), checked_at=_iso(), ok=True, error="")
        state.setdefault("upload_after", _last_document())
        try:  # can files be put here? (a service account has no space in «Мой диск»)
            probe = _upload("sysai-проверка.txt", "text/plain", b"SYSAI", root)
            try:
                _request("DELETE", f"{API}/files/{probe['id']}", raw=True)
            except DriveError:
                pass
            state["upload_blocked"] = ""
        except DriveError as exc:
            state["upload_blocked"] = str(exc)[:500]
        settings_store.set_many({"gdrive_state": state})
        return state


# ----------------------------------------------------------------------------------------------- the three ways
def _iso(moment=None) -> str:
    return (moment or now()).isoformat(timespec="seconds")


def _local(moment):
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(reminders.zone())


def _import_inbox(s, state: dict, counts: dict, notes: list) -> None:
    inbox = (state.get("folders") or {}).get("inbox")
    if not inbox:
        return
    taken = 0
    for f in children(inbox):
        if taken >= MAX_IMPORT:
            state["inbox_more"] = True
            break
        row = s.query(DriveFile).filter_by(file_id=f["id"]).first()
        sig = _sig(f)
        if row is not None and (row.kind != "inbox" or row.sig == sig):
            continue
        name, mime = (f.get("name") or "файл")[:255], f.get("mimeType") or ""
        if row is None:
            row = DriveFile(file_id=f["id"], kind="inbox", origin="drive", name=name)
            s.add(row)
            s.flush()
        row.name, row.sig = name, sig
        if mime.startswith("application/vnd.google-apps.") and mime not in EXPORT:
            row.status, row.error = "skipped", "такой файл Google не выгружается"
            s.commit()
            continue
        if int(f.get("size") or 0) > cases.MAX_DOCUMENT_BYTES:
            row.status, row.error = "skipped", "больше 20 МБ — откройте в Google Drive"
            s.commit()
            continue
        taken += 1
        try:
            data, mime, filename = _download(f)
        except DriveError as exc:
            row.attempts += 1
            row.status, row.error = "failed", str(exc)[:300]
            row.sig = "" if row.attempts < MAX_TRIES else sig  # tried again on the next run (three times)
            s.commit()
            continue
        if len(data) > cases.MAX_DOCUMENT_BYTES:
            row.status, row.error = "skipped", "больше 20 МБ — откройте в Google Drive"
            s.commit()
            continue
        row.version += 1
        caption = f"Google Drive, «{FOLDERS['inbox']}»: {filename}"[:2000]
        doc = Document(chat_id="drive", message_id=row.id * 1000 + row.version % 1000, filename=filename[:255],
                       mime=mime[:120], size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                       kind=cases._classify(mime, filename), caption=caption,
                       sensitive=cases.is_sensitive(filename, ""), data=data)
        s.add(doc)
        s.flush()
        row.document_id, row.status, row.error, row.attempts = doc.id, "ok", "", 0
        s.commit()
        counts["imported"] = counts.get("imported", 0) + 1


def _month_folder(state: dict, moment) -> str:
    month = (_local(moment) or _local(now())).strftime("%Y-%m")
    months = dict(state.get("months") or {})
    if not months.get(month):
        months[month] = _folder((state.get("folders") or {})["docs"], month)
        state["months"] = months
    return months[month]


def _upload_documents(s, state: dict, counts: dict, notes: list) -> None:
    if state.get("upload_blocked") or not (state.get("folders") or {}).get("docs"):
        return
    after = int(state.get("upload_after") or 0)
    rows = {r.document_id: r for r in s.query(DriveFile).filter(DriveFile.kind == "upload")}
    ids = [r.id for r in s.query(Document.id).filter(Document.id > after, Document.sensitive.is_(False),
                                                     Document.chat_id != "drive", Document.data.isnot(None))
           .order_by(Document.id).limit(300)]
    todo = [i for i in ids if i not in rows or rows[i].status == "failed"][:MAX_UPLOAD]
    for document_id in todo:
        doc = s.get(Document, document_id)
        name = (doc.filename or f"документ-{doc.id}")[:255]
        row = rows.get(document_id)
        try:
            f = _upload(name, doc.mime or "application/octet-stream", bytes(doc.data),
                        _month_folder(state, doc.created_at))
        except DriveError as exc:
            if exc.reason == "storageQuotaExceeded":
                state["upload_blocked"] = str(exc)[:500]
                return
            if row is None:
                row = DriveFile(kind="upload", origin="panel", name=name, document_id=doc.id, status="failed")
                s.add(row)
            row.attempts = (row.attempts or 0) + 1
            row.error = str(exc)[:300]
            row.status = "skipped" if row.attempts >= MAX_TRIES else "failed"  # tried again on the next runs
            s.commit()
            notes.append(f"«{name}» не положен в Google Drive: {str(exc)[:160]}")
            continue
        if row is None:
            row = DriveFile(kind="upload", origin="panel", document_id=doc.id)
            s.add(row)
        row.file_id, row.name, row.sig, row.status, row.error = f.get("id"), name, _sig(f), "ok", ""
        s.commit()
        counts["uploaded"] = counts.get("uploaded", 0) + 1


def _sync_templates(s, state: dict, counts: dict, notes: list) -> None:
    folder = (state.get("folders") or {}).get("templates")
    if not folder:
        return
    files = children(folder)
    present = {f["id"] for f in files}
    for f in files:  # Drive → here
        row = s.query(DriveFile).filter_by(file_id=f["id"]).first()
        sig = _sig(f)
        if row is not None and (row.kind != "template" or row.sig == sig):
            continue
        mime, name = f.get("mimeType") or "", f.get("name") or "шаблон"
        if row is None:
            row = DriveFile(file_id=f["id"], kind="template", origin="drive", name=name[:300])
            s.add(row)
        row.name, row.sig = name[:300], sig
        if mime != DOCX and mime != "application/vnd.google-apps.document" and not name.lower().endswith(".docx"):
            row.status, row.error = "skipped", "не DOCX и не документ Google"
            s.commit()
            continue
        s.commit()  # the template is saved by its own session: nothing may wait here
        try:
            data, _, filename = _download(f)
            t = s.get(DocTemplate, row.template_id) if row.template_id else None
            if t is not None:
                tid = doc_templates.save_template(template_id=t.id, title=t.title, description=t.description or "",
                                                  filename=filename, data=data, output=t.output or "docx",
                                                  by="Google Drive")
                counts["templates_in"] = counts.get("templates_in", 0) + 1
            else:
                title = re.sub(r"\.docx$", "", name, flags=re.I).strip() or "Шаблон"
                tid = doc_templates.save_template(title=title, filename=filename, data=data, by="Google Drive")
                counts["templates_new"] = counts.get("templates_new", 0) + 1
            s.expire_all()
            row = s.get(DriveFile, row.id)
            row.template_id, row.local_sha, row.status, row.error = tid, hashlib.sha256(data).hexdigest(), "ok", ""
        except (DriveError, doc_templates.TemplateError) as exc:
            s.expire_all()
            row = s.get(DriveFile, row.id)
            row.status, row.error = "failed", str(exc)[:300]
            notes.append(f"шаблон «{name}» из Google Drive не взят: {str(exc)[:160]}")
        s.commit()
    for row in s.query(DriveFile).filter(DriveFile.kind == "template", DriveFile.status == "ok").all():
        if row.file_id and row.file_id not in present:  # removed from «Шаблоны»: switched off here
            t = s.get(DocTemplate, row.template_id) if row.template_id else None
            if t is not None and t.active:
                t.active = False
                counts["templates_off"] = counts.get("templates_off", 0) + 1
            row.status, row.error = "gone", "файл убран из «Шаблонов»"
    s.commit()
    if state.get("upload_blocked"):
        return
    rows = {r.template_id: r for r in s.query(DriveFile).filter(DriveFile.kind == "template",
                                                                DriveFile.template_id.isnot(None))}
    for t in s.query(DocTemplate).filter(DocTemplate.active.is_(True)).order_by(DocTemplate.id).all():
        data = bytes(t.data or b"")
        sha = hashlib.sha256(data).hexdigest()
        row = rows.get(t.id)
        try:
            if row is None:  # made in the panel → «Шаблоны»
                name = t.filename if (t.filename or "").lower().endswith(".docx") else f"{t.title}.docx"
                f = _upload(name[:255], DOCX, data, folder)
                s.add(DriveFile(file_id=f.get("id"), kind="template", origin="panel", name=name[:300], sig=_sig(f),
                                local_sha=sha, template_id=t.id))
                counts["templates_out"] = counts.get("templates_out", 0) + 1
            elif row.status == "ok" and row.local_sha != sha and row.file_id in present:  # a new file in the panel
                f = _replace(row.file_id, DOCX, data)
                row.sig, row.local_sha = _sig(f), sha
                counts["templates_out"] = counts.get("templates_out", 0) + 1
            s.commit()
        except DriveError as exc:
            s.rollback()
            if exc.reason == "storageQuotaExceeded":
                state["upload_blocked"] = str(exc)[:500]
                return
            notes.append(f"шаблон «{t.title}» не положен в Google Drive: {str(exc)[:160]}")


def _log(state: dict, lines: list) -> None:
    stamp = now().astimezone(reminders.zone()).strftime("%d.%m %H:%M")
    state["log"] = ([{"at": stamp, "text": t[:300]} for t in lines] + list(state.get("log") or []))[:LOG_KEEP]


def _summary(counts: dict) -> str:
    parts = [(counts.get("imported"), "файлов из «Входящих»"), (counts.get("uploaded"), "положено документов в Drive"),
             (counts.get("templates_new"), "новых шаблонов"), (counts.get("templates_in"), "обновлено шаблонов"),
             (counts.get("templates_out"), "положено шаблонов в Drive"),
             (counts.get("templates_off"), "выключено шаблонов")]
    return ", ".join(f"{label}: {n}" for n, label in parts if n) or "изменений нет"


def sync() -> dict:
    """One sync now (the background loop and «Синхронизировать сейчас»)."""
    if not configured():
        raise DriveError("Google Drive не подключён.")
    state = dict(settings_store.get("gdrive_state") or {})
    if not (state.get("folders") or {}).get("inbox"):
        state = check()
    with _LOCK:
        counts, notes = {}, []
        state["checked_at"], state["inbox_more"] = _iso(), False
        blocked = state.get("upload_blocked")
        try:
            with SessionLocal() as s:
                if settings_store.get("gdrive_inbox"):
                    _import_inbox(s, state, counts, notes)
                if settings_store.get("gdrive_upload"):
                    _upload_documents(s, state, counts, notes)
                if settings_store.get("gdrive_templates"):
                    _sync_templates(s, state, counts, notes)
            state.update(ok=True, error="")
        except DriveError as exc:
            state.update(ok=False, error=str(exc)[:300])
            log.info("drive sync stopped: %s", exc)
        if state.get("upload_blocked") and not blocked:
            notes.append("класть файлы в эту папку нельзя: " + state["upload_blocked"][:200])
        state["counts"], state["summary"] = counts, _summary(counts)
        if counts or notes or state.get("ok") is False:
            _log(state, ([state["summary"]] if counts else []) + notes[:10] +
                 ([f"ошибка: {state['error']}"] if state.get("ok") is False else []))
        settings_store.set_many({"gdrive_state": state})
        return state


def run_due() -> None:
    if not active():
        return
    state = settings_store.get("gdrive_state") or {}
    try:
        last = dt.datetime.fromisoformat(str(state.get("checked_at"))) if state.get("checked_at") else None
    except ValueError:
        last = None
    wait = RETRY_MINUTES if state.get("ok") is False else POLL_MINUTES
    if last is not None and now() - last < dt.timedelta(minutes=wait):
        return
    try:
        sync()
    except DriveError as exc:
        state = dict(settings_store.get("gdrive_state") or {})
        state.update(ok=False, error=str(exc)[:300], checked_at=_iso())
        settings_store.set_many({"gdrive_state": state})


def status() -> dict:
    state = dict(settings_store.get("gdrive_state") or {})
    state["configured"] = configured()
    state["has_key"] = bool(settings_store.get("gdrive_key"))
    state["folder"] = str(settings_store.get("gdrive_folder") or "")
    if state["has_key"] and not state.get("email"):
        state["email"] = _key().get("client_email", "")
    try:
        moment = dt.datetime.fromisoformat(str(state["checked_at"])) if state.get("checked_at") else None
    except ValueError:
        moment = None
    state["checked_local"] = _local(moment).strftime("%d.%m %H:%M") if moment else ""
    with SessionLocal() as s:
        state["files"] = [{"kind": KIND_RU.get(r.kind, r.kind), "name": r.name, "status": r.status, "error": r.error,
                           "document_id": r.document_id, "template_id": r.template_id,
                           "at": _local(r.updated_at or r.created_at).strftime("%d.%m %H:%M") if (r.updated_at or r.created_at) else ""}
                          for r in s.query(DriveFile).order_by(DriveFile.updated_at.desc(), DriveFile.id.desc()).limit(20)]
    return state
