"""Private S3-compatible object storage: Cloudflare R2, Yandex Object Storage, Selectel, MinIO…

Only a handful of requests are needed (put, head, delete and a presigned GET for the browser's player), so they are signed
with AWS Signature V4 right here over httpx: boto3 would bring ~80 MB of dependencies to a 512 MB server for the same
four calls. Addressing is path-style, https://<endpoint>/<bucket>/<key>, which every S3-compatible service accepts.
Error messages are safe to show: they never contain the keys, a signature or a presigned link.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import pathlib
import re
import secrets
import urllib.parse
from dataclasses import dataclass

import httpx

from . import config

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CHUNK = 256 * 1024
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
HINTS = {
    "SignatureDoesNotMatch": "неверный S3_SECRET_ACCESS_KEY",
    "InvalidAccessKeyId": "неверный S3_ACCESS_KEY_ID",
    "NoSuchBucket": "такого бакета нет, проверьте S3_BUCKET",
    "AccessDenied": "у ключа нет доступа к бакету: нужны права Object Read & Write на него",
    "Unauthorized": "ключ не подходит к этому бакету",
}


class StorageError(RuntimeError):
    """A storage request failed or the storage is misconfigured; the text is safe to show to the user."""


@dataclass(frozen=True)
class Target:
    endpoint: str  # scheme://host[:port], no path
    host: str      # the Host header exactly as signed
    bucket: str
    access_key: str
    secret_key: str
    region: str


def configured() -> bool:
    """All four variables are set (they may still be wrong: see problem() and check())."""
    return all((config.S3_ENDPOINT, config.S3_BUCKET, config.S3_ACCESS_KEY_ID, config.S3_SECRET_ACCESS_KEY))


def endpoint_of(raw: str, bucket: str) -> tuple[str, str]:
    """(endpoint, host) from S3_ENDPOINT. The address copied from a bucket page ends with /<bucket>: that is accepted."""
    value = (raw or "").strip().rstrip("/")
    if value and "://" not in value:
        value = "https://" + value
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise StorageError("S3_ENDPOINT: неверный адрес") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password or parts.query or parts.fragment:
        raise StorageError("S3_ENDPOINT: нужен адрес вида https://<ID аккаунта>.r2.cloudflarestorage.com")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise StorageError("S3_ENDPOINT: нужен адрес с https://")
    if parts.path.strip("/") not in ("", bucket):
        raise StorageError("S3_ENDPOINT: укажите адрес без пути, только https://<хост>")
    if ":" in host:
        host = f"[{host}]"
    if port and port != {"https": 443, "http": 80}[parts.scheme]:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}", host


def target() -> Target:
    if not configured():
        raise StorageError("хранилище не подключено: задайте S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY_ID и S3_SECRET_ACCESS_KEY")
    bucket = config.S3_BUCKET
    if not BUCKET_RE.match(bucket):
        raise StorageError("S3_BUCKET: имя бакета — 3–63 строчные латинские буквы, цифры и дефисы")
    endpoint, host = endpoint_of(config.S3_ENDPOINT, bucket)
    return Target(endpoint, host, bucket, config.S3_ACCESS_KEY_ID, config.S3_SECRET_ACCESS_KEY, config.S3_REGION or "auto")


def problem() -> str:
    """Why the configured storage cannot be used, without a request ("" when the settings look right)."""
    try:
        target()
    except StorageError as exc:
        return str(exc)
    return ""


def describe() -> dict:
    """Safe summary for the settings page: never the keys."""
    if not configured():
        values = (("S3_ENDPOINT", config.S3_ENDPOINT), ("S3_BUCKET", config.S3_BUCKET),
                  ("S3_ACCESS_KEY_ID", config.S3_ACCESS_KEY_ID), ("S3_SECRET_ACCESS_KEY", config.S3_SECRET_ACCESS_KEY))
        missing = [name for name, value in values if not value]
        return {"configured": False, "problem": "", "bucket": config.S3_BUCKET, "host": "",
                "missing": missing if len(missing) < len(values) else []}
    try:
        t = target()
    except StorageError as exc:
        return {"configured": True, "problem": str(exc), "bucket": config.S3_BUCKET, "host": ""}
    return {"configured": True, "problem": "", "bucket": t.bucket, "host": t.host}


# ---- AWS Signature V4 --------------------------------------------------------------------------------------------
def _quote(value: str, safe: str = "") -> str:
    """RFC 3986 encoding as SigV4 wants it: only letters, digits and -_.~ stay as they are."""
    return urllib.parse.quote(str(value), safe=safe)


def _query(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted((_quote(k), _quote(v)) for k, v in params.items()))


def object_path(t: Target, key: str) -> str:
    return f"/{_quote(t.bucket)}/{_quote(key, safe='/')}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _signature(t: Target, stamp: str, canonical: str) -> tuple[str, str]:
    day = stamp[:8]
    scope = f"{day}/{t.region}/s3/aws4_request"
    key = ("AWS4" + t.secret_key).encode()
    for part in (day, t.region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    text = "\n".join((ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    return scope, hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def signed_headers(t: Target, method: str, path: str, query: dict | None = None, headers: dict | None = None,
                   payload: str = EMPTY_SHA256, now: dt.datetime | None = None) -> dict:
    """Headers of a request signed in the Authorization header. `path` is already encoded."""
    stamp = (now or _now()).strftime("%Y%m%dT%H%M%SZ")
    out = {k.lower(): " ".join(str(v).split()) for k, v in (headers or {}).items()}
    out.update({"host": t.host, "x-amz-content-sha256": payload, "x-amz-date": stamp})
    names = sorted(out)
    canonical = "\n".join((method, path, _query(query or {}), "".join(f"{n}:{out[n]}\n" for n in names), ";".join(names), payload))
    scope, signature = _signature(t, stamp, canonical)
    out["authorization"] = f"{ALGORITHM} Credential={t.access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    return out


def presigned_url(t: Target, method: str, path: str, expires: int, params: dict | None = None,
                  now: dt.datetime | None = None) -> str:
    """A link that works without credentials for `expires` seconds (at most 7 days, as S3 allows)."""
    stamp = (now or _now()).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{stamp[:8]}/{t.region}/s3/aws4_request"
    query = {"X-Amz-Algorithm": ALGORITHM, "X-Amz-Credential": f"{t.access_key}/{scope}", "X-Amz-Date": stamp,
             "X-Amz-Expires": str(max(1, min(int(expires), 604800))), "X-Amz-SignedHeaders": "host", **(params or {})}
    canonical = "\n".join((method, path, _query(query), f"host:{t.host}\n", "host", UNSIGNED))
    _, signature = _signature(t, stamp, canonical)
    return f"{t.endpoint}{path}?{_query(query)}&X-Amz-Signature={signature}"


# ---- requests ----------------------------------------------------------------------------------------------------
def _failure(response: httpx.Response, action: str) -> StorageError:
    body = response.text[:2000] if response.content else ""
    code = re.search(r"<Code>([^<]{1,80})</Code>", body)
    message = re.search(r"<Message>([^<]{1,200})</Message>", body)
    reason = code.group(1) if code else f"HTTP {response.status_code}"
    if not code and response.status_code in (401, 403):
        reason = f"HTTP {response.status_code}, доступ запрещён"
    text = f"{action}: {reason}"
    if code and code.group(1) in HINTS:
        text += f" — {HINTS[code.group(1)]}"
    elif message:
        text += f" ({message.group(1)})"
    return StorageError(text)


def _send(method: str, url: str, headers: dict, content=None, timeout: float = 30.0, action: str = "") -> httpx.Response:
    try:
        return httpx.request(method, url, headers=headers, content=content,
                             timeout=httpx.Timeout(timeout, connect=15.0), follow_redirects=False)
    except httpx.HTTPError as exc:
        raise StorageError(f"{action}: хранилище недоступно ({type(exc).__name__})") from None


def put_file(key: str, path: str | pathlib.Path, content_type: str = "application/octet-stream") -> int:
    """Uploads a file in one streamed PUT (up to 5 GB; a recording is tens of megabytes). Returns its size."""
    t = target()
    source = pathlib.Path(path)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as stream:  # the hash is signed: a body damaged on the way is refused by the storage
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
            size += len(block)
    route = object_path(t, key)
    headers = signed_headers(t, "PUT", route, headers={"content-type": content_type}, payload=digest.hexdigest())
    headers["content-length"] = str(size)  # explicit length: httpx then streams without chunked encoding
    with source.open("rb") as stream:
        response = _send("PUT", t.endpoint + route, headers, content=iter(lambda: stream.read(CHUNK), b""),
                         timeout=max(60.0, size / 50_000), action="загрузка")
    if response.status_code != 200:
        raise _failure(response, "загрузка")
    return size


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    t = target()
    route = object_path(t, key)
    headers = signed_headers(t, "PUT", route, headers={"content-type": content_type}, payload=hashlib.sha256(data).hexdigest())
    response = _send("PUT", t.endpoint + route, headers, content=data, action="запись")
    if response.status_code != 200:
        raise _failure(response, "запись")


def head(key: str) -> dict | None:
    """{"size", "type"} of an object, None when there is no such object."""
    t = target()
    route = object_path(t, key)
    response = _send("HEAD", t.endpoint + route, signed_headers(t, "HEAD", route), action="чтение")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _failure(response, "чтение")
    return {"size": int(response.headers.get("content-length") or 0), "type": response.headers.get("content-type", "")}


def delete(key: str) -> None:
    """Deleting a missing object is not an error (S3 answers 204 either way)."""
    t = target()
    route = object_path(t, key)
    response = _send("DELETE", t.endpoint + route, signed_headers(t, "DELETE", route), action="удаление")
    if response.status_code not in (200, 204, 404):
        raise _failure(response, "удаление")


def _disposition(filename: str) -> str:
    """Download name: UTF-8 for browsers (RFC 6266), a Latin fallback for the rest."""
    stem, dot, ext = filename.rpartition(".")
    stem, ext = (stem, ext) if dot else (filename, "")
    plain = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "recording"
    if ext:
        plain += "." + re.sub(r"[^A-Za-z0-9]+", "", ext)[:8]
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{_quote(filename)}"


def download_url(key: str, expires: int = 3600, filename: str = "") -> str:
    """Presigned GET: the browser plays (or downloads, with a filename) the private object without our keys."""
    t = target()
    params = {"response-content-disposition": _disposition(filename)} if filename else None
    return presigned_url(t, "GET", object_path(t, key), expires, params)


def check() -> str:
    """Writes, reads back through a presigned link (as the player will) and deletes a small object."""
    t = target()
    key = f"sysai-check/{secrets.token_hex(8)}.txt"
    body = b"SYSAI storage check\n"
    put_bytes(key, body, "text/plain")
    failure = None
    try:
        info = head(key)
        if not info or info["size"] != len(body):
            failure = StorageError("проверка: записанный файл не нашёлся")
        else:
            try:
                answer = httpx.get(download_url(key, expires=120), timeout=httpx.Timeout(30.0, connect=15.0))
            except httpx.HTTPError as exc:
                failure = StorageError(f"ссылка для плеера: хранилище недоступно ({type(exc).__name__})")
            else:
                if answer.status_code != 200 or answer.content != body:
                    failure = _failure(answer, "ссылка для плеера")
    except StorageError as exc:
        failure = exc
    try:
        delete(key)
    except StorageError as exc:
        failure = failure or exc
    if failure:
        raise failure
    return f"запись, чтение и удаление работают (бакет {t.bucket} на {t.host})"
