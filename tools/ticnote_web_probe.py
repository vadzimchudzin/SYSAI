#!/usr/bin/env python3
"""Inspect TicNote web-app network shapes using a user-controlled browser session.

This is a local, read-only reconnaissance tool. It does not call private endpoints
itself, submit forms, export files, or save response bodies. It records HTTP method,
redacted URL patterns, status/content type, and JSON field names/types only.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import async_playwright

OUT_DIR = Path.home() / ".sysai-ticnote-probe"
PROFILE_DIR = OUT_DIR / "browser-profile"
REPORT_PATH = OUT_DIR / "network-map.txt"
START_URL = "https://ticnote.com/en/studio"
MAX_EVENTS = 400

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}$")
_LONG_HEX = re.compile(r"^[0-9a-fA-F]{24,}$")
_LONG_DIGITS = re.compile(r"^\d{6,}$")
_SECRET_KEY = re.compile(r"(token|secret|password|authorization|cookie|api.?key|credential|signature|session)", re.I)
_URL_KEY = re.compile(r"(url|uri|audio|download|media|file|asset|blob|stream|source)", re.I)


def safe_url(raw: str) -> str:
    """Keep route structure while removing IDs, query values, and fragments."""
    try:
        parsed = urlsplit(raw)
        segments = []
        for segment in parsed.path.split("/"):
            if (_UUID.match(segment) or _LONG_HEX.match(segment) or _LONG_DIGITS.match(segment) or
                    (len(segment) >= 16 and re.match(r"(?=.*[A-Za-z])(?=.*\\d)[A-Za-z0-9_-]+$", segment))):
                segments.append("{id}")
            elif len(segment) > 80:
                segments.append("{opaque}")
            else:
                segments.append(segment)
        path = "/".join(segments)
        query_keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        suffix = "?keys=" + ",".join(query_keys) if query_keys else ""
        return f"{parsed.scheme}://{parsed.netloc}{path}{suffix}"
    except Exception:
        return "<url-redacted>"


def shape(value, *, key: str = "", depth: int = 0):
    """Describe JSON structure without printing transcript, personal data, or secrets."""
    if _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        if depth >= 4:
            return {"type": "object", "keys": sorted(str(k) for k in value.keys())[:60]}
        return {
            str(k): shape(v, key=str(k), depth=depth + 1)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))[:60]
        }
    if isinstance(value, list):
        first = value[0] if value else None
        return {
            "type": "array",
            "length": len(value),
            "sample": shape(first, key=key, depth=depth + 1) if first is not None else None,
        }
    if isinstance(value, str):
        if _URL_KEY.search(key) and value.startswith(("http://", "https://")):
            return safe_url(value)
        return "string"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def body_shape(raw: str | None) -> str:
    if not raw:
        return "-"
    try:
        parsed = json.loads(raw)
        return json.dumps(shape(parsed), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (ValueError, TypeError):
        # Never print arbitrary form data or unstructured request bodies.
        return f"<non-JSON body, {len(raw)} chars; content hidden>"


def interested(request) -> bool:
    # Fetch/XHR requests often use a separate API hostname, so don't filter by
    # the visible TicNote domain alone. No response content is saved.
    return request.resource_type in {"xhr", "fetch"} or "/api/" in request.url.lower()


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "TicNote web network map (redacted)",
        "This file contains URL patterns and JSON shapes only; response bodies and credentials are not recorded.",
        f"Start page: {safe_url(START_URL)}",
        "",
    ]
    requests_seen: dict[int, dict] = {}
    event_count = 0

    def write(line: str) -> None:
        lines.append(line)
        # Keep terminal output readable while still writing the complete map to disk.
        print(line, flush=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 950},
        )
        page = context.pages[0] if context.pages else await context.new_page()

        def on_request(request) -> None:
            nonlocal event_count
            if event_count >= MAX_EVENTS or not interested(request):
                return
            event_count += 1
            item = {
                "method": request.method,
                "url": safe_url(request.url),
                "resource_type": request.resource_type,
                "body": body_shape(request.post_data),
            }
            requests_seen[id(request)] = item
            write(f"REQ  {event_count:03d} {item['method']} {item['url']} "
                  f"type={item['resource_type']} body={item['body']}")

        def on_response(response) -> None:
            item = requests_seen.get(id(response.request))
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            content_length = response.headers.get("content-length", "?")
            if item is None and not (content_type.startswith("audio/") or "octet-stream" in content_type):
                return
            nonlocal event_count
            if item is None:
                if event_count >= MAX_EVENTS:
                    return
                event_count += 1
                item = {
                    "method": response.request.method,
                    "url": safe_url(response.request.url),
                    "resource_type": response.request.resource_type,
                    "body": body_shape(response.request.post_data),
                }
                write(f"REQ  {event_count:03d} {item['method']} {item['url']} "
                      f"type={item['resource_type']} body={item['body']}")
            response_shape = "-"
            if "json" in content_type:
                try:
                    payload = response.json()
                    response_shape = json.dumps(shape(payload), ensure_ascii=False,
                                                 separators=(",", ":"), sort_keys=True)
                except Exception:
                    response_shape = "<JSON shape unavailable>"
            write(f"RESP {response.status} {item['method']} {item['url']} "
                  f"type={content_type or 'unknown'} bytes={content_length} shape={response_shape}")

        def on_request_failed(request) -> None:
            item = requests_seen.get(id(request))
            if item:
                write(f"FAIL {item['method']} {item['url']} (failure details hidden)")

        def on_download(download) -> None:
            # Do not copy the downloaded audio or print its original filename.
            name = Path(download.suggested_filename).suffix.lower() or "<no-extension>"
            write(f"DOWNLOAD browser-triggered file; extension={name}")

        context.on("request", on_request)
        context.on("response", on_response)
        context.on("requestfailed", on_request_failed)
        page.on("download", on_download)

        print("\nОткрылся Chromium. Войди в TicNote вручную — скрипт не читает и не печатает пароль.")
        print("Открой список записей, зайди в одну новую запись и выполни доступные действия:")
        print("  1) открыть/посмотреть детали записи;")
        print("  2) если нужно — открыть transcript;")
        print("  3) нажать экспорт/скачивание оригинального аудио.")
        print("Ничего не отправляй в чат из браузера. Когда закончишь, вернись в терминал и нажми Enter.\n")
        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            write("NOTE Could not open the suggested page automatically; use the browser address bar to open ticnote.com.")
        await asyncio.to_thread(input, "Нажми Enter после действий в браузере, чтобы сохранить карту запросов... ")
        await context.close()

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nГотово. Секреты, значения cookies и тела ответов не сохранялись.")
    print(f"Файл отчёта: {REPORT_PATH}")
    print("Не отправляй папку browser-profile: в ней хранится авторизованная сессия TicNote.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
