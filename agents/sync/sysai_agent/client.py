"""Requests to SYSAI — JSON, archives, multipart — with the standard library only. The transport is swappable: the
tests give the server's TestClient."""
import json
import urllib.error
import urllib.request
import uuid

from . import __version__

USER_AGENT = f"sysai-agent/{__version__}"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise ApiError(f"Нет связи с SYSAI ({url.split('/agents/')[0]}): {reason}", 0, "offline") from exc


def _problem(status: int, data: bytes) -> tuple[str, str]:
    try:
        body = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message") or f"HTTP {status}"), str(body["error"].get("code") or "")
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"]), ""
    text = data.decode("utf-8", "replace").strip()[:300]
    return f"HTTP {status}" + (f": {text}" if text else ""), ""


class Client:
    def __init__(self, server: str, token: str = "", transport=None, timeout: float = 60):
        self.server = server.rstrip("/")
        self.token, self.transport, self.timeout = token, transport or urllib_transport, timeout

    def call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        url = path if path.startswith(("http://", "https://")) else self.server + path
        sent = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        if self.token:
            sent["Authorization"] = f"Bearer {self.token}"
        status, got, data = self.transport(method, url, sent, body, self.timeout)
        if status >= 400:
            message, code = _problem(status, data)
            raise ApiError(message, status, code)
        return got, data

    def json(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        _, data = self.call(method, path, body, {"Content-Type": "application/json"} if body is not None else None)
        try:
            return json.loads(data.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("SYSAI ответил не JSON: проверьте адрес сервера", 0, "bad_answer") from exc

    def get_bytes(self, path: str) -> tuple[bytes, dict]:
        got, data = self.call("GET", path)
        return data, got

    def multipart(self, path: str, fields: dict, files: dict):
        """files: {field: (file name, bytes, content type)}."""
        boundary = "sysai" + uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8") +
                         str(value).encode("utf-8") + b"\r\n")
        for key, (name, blob, ctype) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\n'
                         f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        _, data = self.call("POST", path, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            return json.loads(data.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("SYSAI ответил не JSON", 0, "bad_answer") from exc
