"""TicNote cloud API: AppKey login, recordings list, WAV download.

We take only audio (formatUrl / WAV). Transcript and summary from TicNote are ignored.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("sysai.ticnote")

APPKEY_PREFIX_MAP = [
    ("tnovs_sit_sk_", "https://ainote-sit-api.mobvoi.com"),
    ("tnovs_sk_", "https://ainote-api.mobvoi.com"),
    ("tncn_sit_sk_", "https://voice-api-sit.ticnote.cn"),
    ("tncn_sk_", "https://voice-api.ticnote.cn"),
]

VOICE_TYPES = {"recording_file", "upload_recording"}
READY_STATUS = {2, 4, 5}  # COMPLETED / TRANSSUC / SUMMARYSUC
TOKEN_TTL_SEC = 23 * 3600

_token_cache: dict = {}  # appkey -> (token, expires_at)


class TicNoteError(Exception):
    """Login or API failure. Message is safe to show in the panel."""


def resolve_base_url(appkey: str) -> str:
    key = (appkey or "").strip()
    for prefix, url in APPKEY_PREFIX_MAP:
        if key.startswith(prefix):
            return url
    known = ", ".join(p for p, _ in APPKEY_PREFIX_MAP)
    raise TicNoteError(f"Неизвестный префикс AppKey. Ожидается один из: {known}")


def _request(method: str, url: str, *, token: str | None = None, body: dict | None = None,
             timeout: int = 30) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        raise TicNoteError(f"TicNote HTTP {e.code}: {err}") from e
    except urllib.error.URLError as e:
        raise TicNoteError(f"TicNote сеть: {e.reason}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise TicNoteError("TicNote вернул не JSON") from e


def login(appkey: str, *, use_cache: bool = True) -> str:
    """Exchange AppKey for a JWT. Cached ~23 hours per process."""
    key = (appkey or "").strip()
    if not key:
        raise TicNoteError("AppKey TicNote пуст")
    now = time.time()
    if use_cache:
        cached = _token_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    base = resolve_base_url(key)
    payload = _request("POST", f"{base}/api/p1/appkey/login", body={"appkey": key})
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = (inner or {}).get("token") or (inner or {}).get("accessToken")
    if payload.get("code") not in (None, 200) and not token:
        raise TicNoteError(f"TicNote login code={payload.get('code')}")
    if not token:
        raise TicNoteError("TicNote не вернул token")
    _token_cache[key] = (token, now + TOKEN_TTL_SEC)
    return token


def clear_token_cache():
    _token_cache.clear()


def _authed(appkey: str, method: str, path: str) -> dict:
    """GET/POST with the cached token; a 401 (token revoked or key re-issued) logs in once more and repeats."""
    base = resolve_base_url(appkey)
    try:
        return _request(method, base + path, token=login(appkey))
    except TicNoteError as e:
        if "HTTP 401" not in str(e):
            raise
    return _request(method, base + path, token=login(appkey, use_cache=False))


def list_projects(appkey: str) -> list[dict]:
    payload = _authed(appkey, "GET", "/api/v2/file-index/chats")
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("chats") or data.get("data") or []
    return []


def recordings_project_id(projects: list[dict]) -> str | None:
    """Prefer the Recordings folder (device uploads)."""
    for chat in projects:
        name = (chat.get("name") or "").strip().lower()
        pid = chat.get("project_id") or chat.get("projectId")
        info = chat.get("projectInfo") or {}
        pid = pid or info.get("id") or info.get("projectId")
        if name == "recordings" and pid:
            return str(pid)
    for chat in projects:
        pid = chat.get("project_id") or chat.get("projectId")
        info = chat.get("projectInfo") or {}
        pid = pid or info.get("id")
        if pid and chat.get("name") != "System Notifications":
            return str(pid)
    return None


def _flatten(nodes: list) -> list[dict]:
    out = []
    for node in nodes or []:
        out.append(node)
        kids = node.get("children") or []
        if kids:
            out.extend(_flatten(kids))
    return out


def list_files(appkey: str, root_id: str) -> list[dict]:
    q = urllib.parse.quote(str(root_id))
    payload = _authed(appkey, "GET", f"/api/v1/file-index/file-tree?rootId={q}")
    tree = payload.get("fileTree") or (payload.get("data") or {}).get("fileTree") or []
    return _flatten(tree)


def file_detail(appkey: str, record_id: str) -> dict:
    payload = _authed(appkey, "GET", f"/api/v2/file-index/file-detail/{record_id}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise TicNoteError("TicNote file-detail пуст")
    return data


def is_ready_recording(node: dict) -> bool:
    ftype = str(node.get("fileType") or "")
    if ftype not in VOICE_TYPES:
        return False
    status = node.get("status")
    try:
        status = int(status)
    except (TypeError, ValueError):
        return False
    return status in READY_STATUS


def ready_recordings(appkey: str) -> list[dict]:
    """Device recordings that finished transcoding (audio is downloadable)."""
    projects = list_projects(appkey)
    root = recordings_project_id(projects)
    if not root:
        return []
    files = list_files(appkey, root)
    return [n for n in files if is_ready_recording(n)]


def audio_url(detail: dict) -> str:
    """WAV/transcoded URL only — not the raw .avo from the device."""
    url = (detail.get("formatUrl") or "").strip()
    if url:
        return url
    raise TicNoteError("У записи нет WAV (formatUrl). Подождите окончания обработки на TicNote.")


def download_wav(url: str, dest, *, timeout: int = 180) -> int:
    """Write WAV bytes to dest (path or file-like). Returns size."""
    import shutil
    from pathlib import Path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if hasattr(dest, "write"):
                start = dest.tell() if hasattr(dest, "tell") else 0
                shutil.copyfileobj(resp, dest, 1 << 20)
                size = (dest.tell() - start) if hasattr(dest, "tell") else 0
            else:
                with open(dest, "wb") as f:
                    shutil.copyfileobj(resp, f, 1 << 20)
                size = Path(dest).stat().st_size
    except urllib.error.URLError as e:
        raise TicNoteError(f"Не удалось скачать WAV: {e.reason}") from e
    if not size:
        raise TicNoteError("Скачанный WAV пуст")
    return size


def check_key(appkey: str) -> str:
    """Panel probe: login + count ready recordings. Does not download audio."""
    login(appkey, use_cache=False)
    recs = ready_recordings(appkey)
    return f"ключ принят, готовых записей: {len(recs)}"
