"""Notion OAuth + webhook importer for TicNote-exported text.

This imports Notion page text into SYSAI's analysis pipeline. It does not download
original audio and cannot redo acoustic diarization.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import IntegrityError

from . import config, settings_store
from .db import Meeting, NotionConnection, NotionImportedPage, NotionWebhookEvent, SessionLocal, now

log = logging.getLogger("sysai.notion")
API = "https://api.notion.com/v1"
API_VERSION = "2026-03-11"
CONNECTION_ID = 1
SETTLE_SECONDS = 75
WORKER_SECONDS = 15
MAX_TEXT = 200_000
MAX_BLOCKS = 2_000
MAX_DEPTH = 5
_TOKEN_LOCK = threading.Lock()
_STOP = threading.Event()
_WORKER: threading.Thread | None = None


class NotionError(RuntimeError):
    """Notion integration, permission or API error."""


class WebhookError(NotionError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("notion:" + config.SECRET_KEY).encode()).digest())
    return Fernet(key)


def _seal(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode("ascii")


def _unseal(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode()
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def client_configured() -> bool:
    return bool(config.NOTION_CLIENT_ID and config.NOTION_CLIENT_SECRET)


def redirect_uri() -> str:
    if config.NOTION_REDIRECT_URI:
        return config.NOTION_REDIRECT_URI
    if config.PUBLIC_URL:
        return config.PUBLIC_URL.rstrip("/") + "/settings/notion/callback"
    raise NotionError("Задайте PUBLIC_URL или NOTION_REDIRECT_URI на сервере.")


def authorization_url(state: str, callback_uri: str | None = None) -> str:
    if not client_configured():
        raise NotionError("На сервере не заданы NOTION_CLIENT_ID и NOTION_CLIENT_SECRET.")
    return "https://api.notion.com/v1/oauth/authorize?" + urlencode({
        "client_id": config.NOTION_CLIENT_ID, "redirect_uri": callback_uri or redirect_uri(),
        "response_type": "code", "owner": "user", "state": state,
    })


def exchange_code(code: str, callback_uri: str) -> dict:
    if not client_configured() or not code or len(code) > 2_000:
        raise NotionError("Не удалось начать OAuth Notion: проверьте конфигурацию приложения.")
    try:
        response = httpx.post(API + "/oauth/token",
            auth=(config.NOTION_CLIENT_ID, config.NOTION_CLIENT_SECRET),
            json={"grant_type": "authorization_code", "code": code, "redirect_uri": callback_uri},
            headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise NotionError(f"Не удалось обменять код Notion: {type(exc).__name__}.") from None
    if response.status_code >= 400:
        raise NotionError(f"Notion OAuth ({response.status_code}): "
                          f"{str(payload.get('message') or payload.get('error') or 'запрос отклонён')[:160]}")
    if not payload.get("access_token") or not payload.get("workspace_id"):
        raise NotionError("Notion вернул неполный OAuth-ответ.")
    return payload


def _snapshot() -> dict | None:
    with SessionLocal() as s:
        row = s.get(NotionConnection, CONNECTION_ID)
        if row is None:
            return None
        return {key: getattr(row, key) for key in (
            "access_token_encrypted", "refresh_token_encrypted", "token_expires_at", "workspace_id",
            "workspace_name", "owner_name", "bot_id", "webhook_verification_token_encrypted",
            "last_sync_at", "connected_at")}


def save_connection(payload: dict) -> None:
    owner = payload.get("owner") or {}
    owner_user = owner.get("user") if isinstance(owner, dict) else {}
    try:
        expiry = now() + dt.timedelta(seconds=max(60, int(payload["expires_in"]))) if payload.get("expires_in") else None
    except (TypeError, ValueError):
        expiry = None
    with SessionLocal() as s:
        row = s.get(NotionConnection, CONNECTION_ID)
        if row is None:
            row = NotionConnection(id=CONNECTION_ID)
            s.add(row)
        row.access_token_encrypted = _seal(str(payload["access_token"]))
        row.refresh_token_encrypted = _seal(str(payload.get("refresh_token") or ""))
        row.token_expires_at = expiry
        row.workspace_id = str(payload.get("workspace_id") or "")[:100]
        row.workspace_name = str(payload.get("workspace_name") or "")[:200]
        row.owner_name = str((owner_user or {}).get("name") or "")[:200]
        row.bot_id = str(payload.get("bot_id") or "")[:100]
        row.connected_at = now()
        row.updated_at = now()
        s.commit()
    log.info("Notion workspace connected: %s", str(payload.get("workspace_name") or "")[:100])


def disconnect() -> None:
    with SessionLocal() as s:
        row = s.get(NotionConnection, CONNECTION_ID)
        if row is None:
            return
        row.access_token_encrypted = row.refresh_token_encrypted = ""
        row.token_expires_at = row.connected_at = None
        row.workspace_id = row.workspace_name = row.owner_name = row.bot_id = ""
        row.updated_at = now()
        s.commit()


def webhook_verification_token() -> str:
    row = _snapshot()
    return _unseal(row["webhook_verification_token_encrypted"]) if row else ""


def reset_webhook_verification_token() -> None:
    with SessionLocal() as s:
        row = s.get(NotionConnection, CONNECTION_ID)
        if row:
            row.webhook_verification_token_encrypted = ""
            row.updated_at = now()
            s.commit()


def connection_status() -> dict:
    row = _snapshot()
    try:
        callback = redirect_uri()
    except NotionError:
        callback = ""
    return {
        "client_configured": client_configured(),
        "connected": bool(row and row["access_token_encrypted"] and row["workspace_id"]),
        "workspace_name": row["workspace_name"] if row else "",
        "workspace_id": row["workspace_id"] if row else "",
        "owner_name": row["owner_name"] if row else "",
        "last_sync_at": row["last_sync_at"].isoformat() if row and row["last_sync_at"] else "",
        "webhook_token_received": bool(row and _unseal(row["webhook_verification_token_encrypted"])),
        "webhook_url": config.PUBLIC_URL.rstrip("/") + "/webhooks/notion" if config.PUBLIC_URL else "",
        "redirect_uri": callback,
    }


def _access_token(force_refresh: bool = False) -> str:
    with _TOKEN_LOCK:
        row = _snapshot()
        if not row:
            raise NotionError("Сначала подключите рабочее пространство Notion.")
        access = _unseal(row["access_token_encrypted"])
        refresh = _unseal(row["refresh_token_encrypted"])
        if not access:
            raise NotionError("Нет активного подключения Notion. Подключите аккаунт заново.")
        expiry = row["token_expires_at"]
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.timezone.utc)
        due = force_refresh or (expiry is not None and
            expiry <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=90))
        if not due:
            return access
        if not refresh or not client_configured():
            if force_refresh:
                raise NotionError("Токен Notion истёк. Подключите аккаунт заново.")
            return access
        try:
            response = httpx.post(API + "/oauth/token",
                auth=(config.NOTION_CLIENT_ID, config.NOTION_CLIENT_SECRET),
                json={"grant_type": "refresh_token", "refresh_token": refresh},
                headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotionError(f"Не удалось обновить токен Notion: {type(exc).__name__}.") from None
        if response.status_code >= 400 or not payload.get("access_token"):
            raise NotionError(f"Обновление токена Notion ({response.status_code}): "
                              f"{str(payload.get('message') or payload.get('error') or 'отклонено')[:140]}")
        try:
            expiry = now() + dt.timedelta(seconds=max(60, int(payload["expires_in"]))) if payload.get("expires_in") else None
        except (TypeError, ValueError):
            expiry = None
        access, refresh = str(payload["access_token"]), str(payload.get("refresh_token") or refresh)
        with SessionLocal() as s:
            saved = s.get(NotionConnection, CONNECTION_ID)
            if saved is None:
                raise NotionError("Подключение Notion удалено во время обновления.")
            saved.access_token_encrypted, saved.refresh_token_encrypted = _seal(access), _seal(refresh)
            saved.token_expires_at, saved.updated_at = expiry, now()
            s.commit()
        return access


def api_request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
                retry_auth: bool = True) -> dict:
    if not path.startswith("/") or ".." in path:
        raise NotionError("Некорректный путь Notion API.")
    headers = {"Authorization": "Bearer " + _access_token(), "Notion-Version": API_VERSION,
               "Accept": "application/json", "Content-Type": "application/json"}
    try:
        response = httpx.request(method.upper(), API + path, headers=headers, params=params, json=json_body, timeout=30)
        if response.status_code == 401 and retry_auth:
            headers["Authorization"] = "Bearer " + _access_token(force_refresh=True)
            response = httpx.request(method.upper(), API + path, headers=headers, params=params, json=json_body, timeout=30)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise NotionError(f"Ошибка обращения к Notion API: {type(exc).__name__}.") from None
    if response.status_code >= 400:
        raise NotionError(f"Notion API ({response.status_code}): "
                          f"{str(payload.get('message') or payload.get('code') or 'запрос отклонён')[:160]}")
    if not isinstance(payload, dict):
        raise NotionError("Notion API вернул неожиданный ответ.")
    return payload


def normalise_parent_setting(value: str | None) -> str:
    raw = (value or "").strip().split("?", 1)[0].rstrip("/").split("/")[-1].replace("-", "")
    match = re.search(r"([0-9a-fA-F]{32})$", raw)
    return match.group(1).lower() if match else ""


def _parent(page: dict) -> tuple[str, str]:
    value = page.get("parent") or {}
    kind = str(value.get("type") or "") if isinstance(value, dict) else ""
    return kind, str(value.get(kind) or "") if kind else ""


def _belongs_to_parent(page: dict, root_id: str) -> bool:
    target, current, seen = normalise_parent_setting(root_id), page, set()
    for _ in range(MAX_DEPTH):
        kind, parent_id = _parent(current)
        if not parent_id:
            return False
        normalised = normalise_parent_setting(parent_id)
        if normalised and hmac.compare_digest(normalised, target):
            return True
        if parent_id in seen or not normalised:
            return False
        seen.add(parent_id)
        if kind == "page_id":
            current = api_request("GET", "/pages/" + normalised)
        elif kind == "database_id":
            current = api_request("GET", "/databases/" + normalised)
        elif kind == "data_source_id":
            current = api_request("GET", "/data_sources/" + normalised)
        else:
            return False
    return False


def _rich_text(items) -> str:
    if not isinstance(items, list):
        return ""
    return "".join(str(item.get("plain_text") or (item.get("text") or {}).get("content") or "")
                   for item in items if isinstance(item, dict))


def _block_text(block: dict) -> str:
    kind = str(block.get("type") or "")
    data = block.get(kind) or {}
    if not isinstance(data, dict):
        return ""
    if kind in ("child_page", "child_database"):
        return str(data.get("title") or "")
    if kind == "table_row":
        return " | ".join(_rich_text(c) for c in (data.get("cells") or []) if isinstance(c, list))
    for key in ("rich_text", "title", "caption"):
        text = _rich_text(data.get(key))
        if text:
            return text
    return str(data.get("url") or "") if kind == "bookmark" else ""


def _collect_text(root_id: str) -> str:
    root = normalise_parent_setting(root_id)
    if not root:
        return ""
    output, budget = [], [0, 0]

    def visit(parent_id: str, depth: int = 0):
        if depth > MAX_DEPTH or budget[0] >= MAX_BLOCKS or budget[1] >= MAX_TEXT:
            return
        cursor = None
        while budget[0] < MAX_BLOCKS and budget[1] < MAX_TEXT:
            params = {"page_size": min(100, MAX_BLOCKS - budget[0])}
            if cursor:
                params["start_cursor"] = cursor
            payload = api_request("GET", "/blocks/" + parent_id + "/children", params=params)
            blocks = payload.get("results") or []
            if not blocks:
                break
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                budget[0] += 1
                text = _block_text(block).strip()
                if text:
                    text = text[:MAX_TEXT - budget[1]]
                    output.append(text)
                    budget[1] += len(text)
                if block.get("has_children") and depth < MAX_DEPTH:
                    child = normalise_parent_setting(str(block.get("id") or ""))
                    if child:
                        visit(child, depth + 1)
                if budget[0] >= MAX_BLOCKS or budget[1] >= MAX_TEXT:
                    break
            if not payload.get("has_more") or not payload.get("next_cursor"):
                break
            cursor = payload["next_cursor"]

    visit(root)
    return "\n".join(output).strip()[:MAX_TEXT]


def _title(page: dict) -> str:
    props = page.get("properties") or {}
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and (prop.get("type") == "title" or isinstance(prop.get("title"), list)):
                value = _rich_text(prop.get("title")).strip()
                if value:
                    return value[:300]
    return str(page.get("title") or "Совещание из Notion").strip()[:300]


def _page_date(page: dict):
    for key in ("created_time", "last_edited_time"):
        try:
            return dt.datetime.fromisoformat(str(page.get(key) or "").replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            pass
    return dt.datetime.now(dt.timezone.utc).date()


_TIMESTAMP = re.compile(
    r"^\s*(?:\[(?P<bracket>\d{1,2}:\d{2}(?::\d{2})?)\]\s*|(?P<plain>\d{1,2}:\d{2}(?::\d{2})?)\s*)"
    r"(?:[-–—|]\s*)?(?P<speaker>[^:\n]{1,80}):\s*(?P<text>.+?)\s*$"
)


def _segments(text: str) -> tuple[list[dict], float]:
    lines = [x.strip() for x in (text or "").strip()[:MAX_TEXT].splitlines() if x.strip()]
    result = []
    for line in lines:
        match = _TIMESTAMP.match(line)
        if not match:
            continue
        parts = [int(x) for x in (match.group("bracket") or match.group("plain")).split(":")]
        seconds = float(parts[-1] + parts[-2] * 60 + (parts[-3] * 3600 if len(parts) == 3 else 0))
        result.append({"speaker": match.group("speaker").strip()[:120], "start": seconds,
                       "end": seconds, "text": match.group("text").strip()})
    if len(result) >= 2 and len(result) >= max(2, int(len(lines) * 0.3)):
        result.sort(key=lambda item: item["start"])
        for index in range(len(result) - 1):
            if result[index + 1]["start"] > result[index]["start"]:
                result[index]["end"] = result[index + 1]["start"]
        return result, max((item["end"] for item in result), default=0.0)
    whole = "\n".join(lines)
    if not whole:
        raise NotionError("Страница Notion пока не содержит текста; импорт будет повторён.")
    return [{"speaker": "Говорящий не указан", "start": 0.0, "end": 0.0, "text": whole}], 0.0


def _mark_sync_time():
    with SessionLocal() as s:
        row = s.get(NotionConnection, CONNECTION_ID)
        if row:
            row.last_sync_at = row.updated_at = now()
            s.commit()


def import_page(page_id: str) -> dict:
    page_id = normalise_parent_setting(page_id)
    root = normalise_parent_setting(settings_store.get("notion_parent_id") or "")
    if not page_id:
        raise NotionError("Некорректный ID страницы Notion.")
    if not root:
        raise NotionError("Сохраните ID страницы/базы Notion, в которую TicNote экспортирует записи.")
    with SessionLocal() as s:
        old = s.get(NotionImportedPage, page_id)
        if old:
            return {"status": "duplicate", "page_id": page_id, "meeting_id": old.meeting_id, "title": old.title or ""}
    page = api_request("GET", "/pages/" + page_id)
    if not _belongs_to_parent(page, root):
        return {"status": "ignored", "page_id": page_id, "title": _title(page)}
    segments, duration = _segments(_collect_text(page_id))
    title = _title(page)
    with SessionLocal() as s:
        old = s.get(NotionImportedPage, page_id)
        if old:
            return {"status": "duplicate", "page_id": page_id, "meeting_id": old.meeting_id, "title": old.title or ""}
        meeting = Meeting(title=title, source="notion", filename="notion-" + page_id[:8] + ".txt",
            audio_path="", status="queued", progress="Текст импортирован из Notion", error="",
            duration_sec=duration, meeting_date=_page_date(page), transcript=segments, options={})
        s.add(meeting)
        s.flush()
        s.add(NotionImportedPage(page_id=page_id, meeting_id=meeting.id, title=title[:300],
            page_url=str(page.get("url") or "")[:1000], last_edited_time=str(page.get("last_edited_time") or "")[:50],
            status="queued", imported_at=now(), error=""))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            with SessionLocal() as check:
                old = check.get(NotionImportedPage, page_id)
                return {"status": "duplicate", "page_id": page_id, "meeting_id": old.meeting_id if old else None,
                        "title": old.title if old else title}
        meeting_id = meeting.id
    from . import pipeline
    pipeline.submit(meeting_id)
    _mark_sync_time()
    log.info("Imported Notion page %s as SYSAI meeting %s", page_id[:8], meeting_id)
    return {"status": "queued", "page_id": page_id, "meeting_id": meeting_id, "title": title}


def sync_existing_pages(max_pages: int = 500) -> dict:
    if not connection_status()["connected"]:
        raise NotionError("Сначала подключите Notion.")
    if not normalise_parent_setting(settings_store.get("notion_parent_id") or ""):
        raise NotionError("Сохраните ID страницы/базы Notion перед синхронизацией.")
    found = queued = duplicate = ignored = failed = 0
    cursor = None
    while found < max_pages:
        body = {"page_size": min(100, max_pages - found),
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
        if cursor:
            body["start_cursor"] = cursor
        payload = api_request("POST", "/search", json_body=body)
        pages = payload.get("results") or []
        if not pages:
            break
        for page in pages:
            page_id = str(page.get("id") or "")
            if not page_id:
                continue
            found += 1
            try:
                result = import_page(page_id)
                if result["status"] == "queued":
                    queued += 1
                elif result["status"] == "duplicate":
                    duplicate += 1
                else:
                    ignored += 1
            except NotionError as exc:
                failed += 1
                log.warning("Notion backfill failed for %s: %s", page_id[:8], str(exc)[:150])
            if found >= max_pages:
                break
        if not payload.get("has_more") or not payload.get("next_cursor"):
            break
        cursor = payload["next_cursor"]
    _mark_sync_time()
    return {"found": found, "queued": queued, "duplicate": duplicate, "ignored": ignored, "failed": failed}


def _store_event(payload: dict, raw: bytes) -> bool:
    event_id = str(payload.get("id") or hashlib.sha256(raw).hexdigest())[:200]
    event_type = str(payload.get("type") or "")[:80]
    entity, data = payload.get("entity") or {}, payload.get("data") or {}
    page_id = str((entity.get("id") if isinstance(entity, dict) else "") or
                  (data.get("id") if isinstance(data, dict) else "") or "")
    if event_type not in ("page.created", "page.content_updated") or not page_id:
        return False
    page_id = normalise_parent_setting(page_id)
    if not page_id:
        return False
    with SessionLocal() as s:
        if s.get(NotionWebhookEvent, event_id):
            return True
        s.add(NotionWebhookEvent(event_id=event_id, page_id=page_id, event_type=event_type,
            status="pending", attempts=0, error="", received_at=now()))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
    return True


def receive_webhook(raw_body: bytes, signature: str) -> dict:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WebhookError("Некорректный JSON webhook Notion.") from None
    if not isinstance(payload, dict):
        raise WebhookError("Webhook Notion должен содержать JSON-объект.")
    challenge = payload.get("verification_token")
    if challenge and not payload.get("type"):
        token = str(challenge)
        if len(token) < 16 or len(token) > 500:
            raise WebhookError("Некорректный verification token.")
        current = webhook_verification_token()
        if current and not hmac.compare_digest(current, token):
            raise WebhookError("Сохранён другой verification token. Сбросьте его перед повторной проверкой.", 409)
        if not current:
            with SessionLocal() as s:
                row = s.get(NotionConnection, CONNECTION_ID)
                if row is None:
                    row = NotionConnection(id=CONNECTION_ID)
                    s.add(row)
                row.webhook_verification_token_encrypted = _seal(token)
                row.updated_at = now()
                s.commit()
        return {"ok": True, "verification_token_received": True}
    token = webhook_verification_token()
    if not token:
        raise WebhookError("Сначала запустите проверку webhook в Notion и сохраните verification token.", 401)
    expected = "sha256=" + hmac.new(token.encode(), raw_body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature.strip()):
        raise WebhookError("Недействительная подпись Notion webhook.", 401)
    queued = _store_event(payload, raw_body)
    return {"ok": True, "queued": queued, "ignored": not queued}


def _utc(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def process_pending_events() -> int:
    if not connection_status()["connected"] or not normalise_parent_setting(settings_store.get("notion_parent_id") or ""):
        return 0
    cutoff = time.time() - SETTLE_SECONDS
    with SessionLocal() as s:
        for row in s.query(NotionWebhookEvent).filter_by(status="processing").all():
            if time.time() - _utc(row.received_at).timestamp() > SETTLE_SECONDS * 4:
                row.status, row.error = "pending", "Recovered after worker restart"
        s.commit()
        rows = s.query(NotionWebhookEvent).filter_by(status="pending").order_by(NotionWebhookEvent.received_at.asc()).all()
        groups, newest = {}, {}
        for row in rows:
            groups.setdefault(row.page_id, []).append(row.event_id)
            newest[row.page_id] = max(newest.get(row.page_id, 0.0), _utc(row.received_at).timestamp())
    handled = 0
    for page_id, event_ids in groups.items():
        if newest[page_id] > cutoff:
            continue
        with SessionLocal() as s:
            count = s.query(NotionWebhookEvent).filter(
                NotionWebhookEvent.event_id.in_(event_ids), NotionWebhookEvent.status == "pending"
            ).update({NotionWebhookEvent.status: "processing"}, synchronize_session=False)
            s.commit()
        if not count:
            continue
        try:
            result = import_page(page_id)
            final_status = "ignored" if result["status"] == "ignored" else "done"
            with SessionLocal() as s:
                s.query(NotionWebhookEvent).filter(NotionWebhookEvent.event_id.in_(event_ids)).update(
                    {NotionWebhookEvent.status: final_status, NotionWebhookEvent.processed_at: now(),
                     NotionWebhookEvent.error: ""}, synchronize_session=False)
                s.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Notion import failed for %s: %s", page_id[:8], str(exc)[:180])
            with SessionLocal() as s:
                for event_id in event_ids:
                    row = s.get(NotionWebhookEvent, event_id)
                    if row:
                        row.attempts = int(row.attempts or 0) + 1
                        row.error = str(exc)[:1000]
                        row.status = "pending" if row.attempts < 5 else "failed"
                        row.processed_at = now() if row.status == "failed" else None
                s.commit()
        handled += 1
    return handled


def _worker_loop():
    while not _STOP.wait(WORKER_SECONDS):
        try:
            process_pending_events()
        except Exception:  # noqa: BLE001
            log.exception("Notion webhook worker iteration failed")


def start_worker():
    global _WORKER
    if config.TESTING or (_WORKER is not None and _WORKER.is_alive()):
        return
    _STOP.clear()
    _WORKER = threading.Thread(target=_worker_loop, name="sysai-notion-webhooks", daemon=True)
    _WORKER.start()


def stop_worker():
    _STOP.set()
