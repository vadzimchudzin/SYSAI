"""Recordings storage: SigV4 requests to an S3-compatible bucket, keeping a compressed copy of each new recording,
listening from the meeting page and removing copies (storage.py, audio_archive.py)."""
import array
import datetime as dt
import hashlib
import json
import math
import pathlib
import re
import shutil
import subprocess
import urllib.parse
import wave

import httpx
import pytest

from app import audio_archive, config, db, pipeline, settings_store, storage
from app.db import Meeting, Recording, SessionLocal, StorageCleanup, TicnoteImport, now

ENDPOINT = "https://acc123.r2.cloudflarestorage.com"
BUCKET = "sysai-recordings"


class FakeS3:
    """An in-memory bucket behind httpx: answers like S3 and checks what it cheaply can (signed, body hash, length)."""

    def __init__(self):
        self.objects, self.calls, self.fail, self.during_put = {}, [], {}, None

    def request(self, method, url, headers=None, content=None, timeout=None, follow_redirects=False, **_):
        parts = urllib.parse.urlsplit(url)
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        request = httpx.Request(method, url)
        body = content if isinstance(content, (bytes, type(None))) else b"".join(content)
        body = body or b""
        self.calls.append((method, urllib.parse.unquote(parts.path), parts.query))
        if method in self.fail:
            failure = self.fail[method]
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure[0], text=failure[1], request=request)
        prefix = f"/{BUCKET}/"
        if parts.netloc != "acc123.r2.cloudflarestorage.com" or not parts.path.startswith(prefix):
            return httpx.Response(404, text="<Error><Code>NoSuchBucket</Code></Error>", request=request)
        key = urllib.parse.unquote(parts.path[len(prefix):])
        signed = "X-Amz-Signature=" in parts.query or headers.get("authorization", "").startswith("AWS4-HMAC-SHA256 Credential=AKID123/")
        if not signed:
            return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>", request=request)
        if method == "PUT":
            assert headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
            assert int(headers.get("content-length", len(body))) == len(body)
            if self.during_put:
                self.during_put()
            self.objects[key] = (body, headers.get("content-type", ""))
            return httpx.Response(200, request=request)
        if method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204, request=request)
        if key not in self.objects:
            return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>", request=request)
        data, kind = self.objects[key]
        if method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data)), "content-type": kind}, request=request)
        return httpx.Response(200, content=data, request=request)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def methods(self):
        return [call[0] for call in self.calls]


def connect(monkeypatch, endpoint=ENDPOINT):
    for name, value in (("S3_ENDPOINT", endpoint), ("S3_BUCKET", BUCKET), ("S3_ACCESS_KEY_ID", "AKID123"),
                        ("S3_SECRET_ACCESS_KEY", "SECRET/abc+xyz"), ("S3_REGION", "auto")):
        monkeypatch.setattr(config, name, value)


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    connect(monkeypatch)
    monkeypatch.setattr(httpx, "request", fake.request)
    monkeypatch.setattr(httpx, "get", fake.get)
    shutil.rmtree(audio_archive.archive_dir(), ignore_errors=True)
    return fake


def make_wav(path, seconds=20, rate=16000):
    values = array.array("h", (int(8000 * math.sin(2 * math.pi * 220 * i / rate)) if (i // rate) % 4 < 2 else 0
                               for i in range(seconds * rate)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(values.tobytes())
    return path


def seg(speaker, start, end, text="Обсуждаем план работ на неделю", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


TRANSCRIPT = [
    seg("Анна Петрова", 0, 30, "Коллеги, начинаем планёрку.", speaker_source="voiceprint", speaker_employee_id=1),
    seg("Спикер 2", 31, 50, "По складу отгрузку переносим на пятницу."),
    seg("Анна Петрова", 51, 70, "Тогда предупредим клиента сегодня.", speaker_source="voiceprint", speaker_employee_id=1),
    seg("Спикер 2", 71, 90, "Я напишу им до обеда."),
]


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_meeting(**fields):
    fields.setdefault("status", "awaiting_approval")
    fields.setdefault("source", "web")
    with SessionLocal() as s:
        meeting = Meeting(**fields)
        s.add(meeting)
        s.commit()
        return meeting.id


def kept(key="recordings/2026/11/m1-0123456789ab.mp3", size=1_234_567, days=150, **extra):
    """A meeting whose copy is in the storage."""
    record = {"key": key, "size": size, "duration": 90.0, "mime": "audio/mpeg", "stored_at": iso(now()),
              "expires_at": iso(now() + dt.timedelta(days=days))}
    fields = dict(title="Планёрка", transcript=TRANSCRIPT, duration_sec=90, meeting_date=dt.date(2026, 11, 2),
                  report={"title": "Планёрка", "summary": "", "tasks": []}, recording_status="stored", recording=record)
    fields.update(extra)
    return add_meeting(**fields)


def meeting(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid)


def cleanups():
    with SessionLocal() as s:
        return [(r.key, r.reason) for r in s.query(StorageCleanup).order_by(StorageCleanup.id)]


def leftovers(mid):
    return sorted(p.name for p in audio_archive.archive_dir().glob(f"m{mid}-*"))


def payload_of(html):
    found = re.search(r'<script type="application/json" id="voice-diagram-data">(.*?)</script>', html, re.S)
    return json.loads(found.group(1)) if found else None


# ------------------------------------------------------------------------------------------- SigV4 (storage.py)
AWS = storage.Target("https://examplebucket.s3.amazonaws.com", "examplebucket.s3.amazonaws.com", "examplebucket",
                     "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1")
AWS_DAY = dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc)


def signature(headers):
    return headers["authorization"].split("Signature=")[1]


def test_requests_are_signed_exactly_as_in_the_aws_examples():
    get = storage.signed_headers(AWS, "GET", "/test.txt", headers={"Range": "bytes=0-9"}, now=AWS_DAY)
    assert signature(get) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date," in get["authorization"]
    body = b"Welcome to Amazon S3."
    put = storage.signed_headers(AWS, "PUT", "/" + storage._quote("test$file.text", safe="/"), now=AWS_DAY,
                                 headers={"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
                                 payload=hashlib.sha256(body).hexdigest())
    assert signature(put) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    lifecycle = storage.signed_headers(AWS, "GET", "/", query={"lifecycle": ""}, now=AWS_DAY)
    assert signature(lifecycle) == "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"
    listing = storage.signed_headers(AWS, "GET", "/", query={"max-keys": "2", "prefix": "J"}, now=AWS_DAY)
    assert signature(listing) == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"


def test_presigned_links_match_the_aws_example_and_botocore():
    url = storage.presigned_url(AWS, "GET", "/test.txt", 86400, now=AWS_DAY)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query["X-Amz-Signature"] == ["aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"]
    assert url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256&")
    # Values computed by botocore 1.x (S3SigV4QueryAuth / S3SigV4Auth) for the same requests to R2.
    r2 = storage.Target(ENDPOINT, "acc123.r2.cloudflarestorage.com", BUCKET, "AKID123", "SECRET/abc+xyz", "auto")
    moment = dt.datetime(2026, 11, 2, 12, 34, 56, tzinfo=dt.timezone.utc)
    params = {"response-content-disposition": storage._disposition('Планёрка 02.11 "итоги"; v2.mp3')}
    link = storage.presigned_url(r2, "GET", storage.object_path(r2, "recordings/2026/11/m42-ab12cd.mp3"), 3600, params, now=moment)
    assert link.endswith("&X-Amz-Signature=714692b711544b6e712f08b15d308a6ea3289c4d25fbbcb6e19737e36d06f207")
    body = b"SYSAI storage check\n"
    put = storage.signed_headers(r2, "PUT", storage.object_path(r2, "sysai-check/abc.txt"), now=moment,
                                 headers={"Content-Type": "text/plain", "Content-Length": str(len(body))},
                                 payload=hashlib.sha256(body).hexdigest())
    assert signature(put) == "b7e45e5e372d2b2affecbdec5d9de66f87f737bd8f27c2f8c362962be7034be8"
    assert storage.presigned_url(r2, "GET", "/x", 10 ** 9, now=moment).count("X-Amz-Expires=604800&") == 1  # S3 allows 7 days


def test_download_names_are_safe_in_both_header_forms():
    value = storage._disposition('Планёрка 02.11 "итоги"; v2.mp3')
    assert value == ("attachment; filename=\"02-11-v2.mp3\"; filename*=UTF-8''%D0%9F%D0%BB%D0%B0%D0%BD%D1%91%D1%80%D0%BA%D0%B0"
                     "%2002.11%20%22%D0%B8%D1%82%D0%BE%D0%B3%D0%B8%22%3B%20v2.mp3")
    assert storage._disposition("Совещание.mp3").startswith('attachment; filename="recording.mp3"; ')


@pytest.mark.parametrize("raw, expected", [
    ("https://acc123.r2.cloudflarestorage.com", (ENDPOINT, "acc123.r2.cloudflarestorage.com")),
    ("https://acc123.r2.cloudflarestorage.com/", (ENDPOINT, "acc123.r2.cloudflarestorage.com")),
    ("https://ACC123.r2.cloudflarestorage.com/sysai-recordings", (ENDPOINT, "acc123.r2.cloudflarestorage.com")),
    ("acc123.r2.cloudflarestorage.com", (ENDPOINT, "acc123.r2.cloudflarestorage.com")),
    ("https://storage.yandexcloud.net:443", ("https://storage.yandexcloud.net", "storage.yandexcloud.net")),
    ("https://s3.example.org:9000", ("https://s3.example.org:9000", "s3.example.org:9000")),
    ("http://127.0.0.1:7070", ("http://127.0.0.1:7070", "127.0.0.1:7070")),
])
def test_the_endpoint_copied_from_the_bucket_page_is_accepted(raw, expected):
    assert storage.endpoint_of(raw, BUCKET) == expected


@pytest.mark.parametrize("raw", ["http://acc123.r2.cloudflarestorage.com", "https://host/other-bucket", "ftp://host",
                                 "https://user:pw@host", "https://host?x=1", "https://[::1", ""])
def test_wrong_endpoints_are_explained(raw):
    with pytest.raises(storage.StorageError, match="S3_ENDPOINT"):
        storage.endpoint_of(raw, BUCKET)


def test_settings_problems_are_reported_without_the_keys(monkeypatch):
    assert not storage.configured() and storage.describe() == {"configured": False, "problem": "", "bucket": "", "host": "",
                                                               "missing": []}
    monkeypatch.setattr(config, "S3_BUCKET", BUCKET)
    assert storage.describe()["missing"] == ["S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"]
    connect(monkeypatch)
    assert storage.describe() == {"configured": True, "problem": "", "bucket": BUCKET, "host": "acc123.r2.cloudflarestorage.com"}
    monkeypatch.setattr(config, "S3_BUCKET", "Recordings_2026")
    assert "S3_BUCKET" in storage.problem() and "SECRET" not in json.dumps(storage.describe())
    with pytest.raises(storage.StorageError, match="S3_BUCKET"):
        storage.download_url("recordings/x.mp3")


# ----------------------------------------------------------------------------------------------- requests
def test_files_are_uploaded_read_and_deleted(s3, tmp_path):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"\xff\xf3" * 300_000)  # bigger than one streamed block
    assert storage.put_file("recordings/a b/ü.mp3", source, "audio/mpeg") == 600_000
    assert s3.objects["recordings/a b/ü.mp3"] == (source.read_bytes(), "audio/mpeg")
    assert storage.head("recordings/a b/ü.mp3") == {"size": 600_000, "type": "audio/mpeg"}
    assert storage.head("recordings/missing.mp3") is None
    link = storage.download_url("recordings/a b/ü.mp3", 60, filename="Запись.mp3")
    assert link.startswith(f"{ENDPOINT}/{BUCKET}/recordings/a%20b/%C3%BC.mp3?X-Amz-Algorithm=")
    assert "response-content-disposition=attachment" in link and "X-Amz-Expires=60&" in link
    assert httpx.get(link).content == source.read_bytes()
    storage.delete("recordings/a b/ü.mp3")
    storage.delete("recordings/a b/ü.mp3")  # already gone: not an error
    assert s3.objects == {}


@pytest.mark.parametrize("answer, text", [
    ((403, "<Error><Code>SignatureDoesNotMatch</Code><Message>The request signature we calculated…</Message></Error>"),
     "загрузка: SignatureDoesNotMatch — неверный S3_SECRET_ACCESS_KEY"),
    ((403, "<Error><Code>InvalidAccessKeyId</Code></Error>"), "загрузка: InvalidAccessKeyId — неверный S3_ACCESS_KEY_ID"),
    ((404, "<Error><Code>NoSuchBucket</Code></Error>"), "загрузка: NoSuchBucket — такого бакета нет, проверьте S3_BUCKET"),
    ((403, ""), "загрузка: HTTP 403, доступ запрещён"),
    ((500, "<Error><Code>InternalError</Code><Message>We encountered an internal error</Message></Error>"),
     "загрузка: InternalError (We encountered an internal error)"),
    (httpx.ConnectError("no route"), "загрузка: хранилище недоступно (ConnectError)"),
])
def test_storage_failures_are_explained(s3, tmp_path, answer, text):
    s3.fail["PUT"] = answer
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    with pytest.raises(storage.StorageError) as failure:
        storage.put_file("recordings/a.mp3", source)
    assert str(failure.value) == text


def test_the_check_writes_reads_through_a_link_and_cleans_up(s3):
    assert storage.check() == "запись, чтение и удаление работают (бакет sysai-recordings на acc123.r2.cloudflarestorage.com)"
    assert s3.methods() == ["PUT", "HEAD", "GET", "DELETE"] and s3.objects == {}
    assert "X-Amz-Signature=" in s3.calls[2][2]  # read the way the player reads: through a presigned link
    s3.calls.clear()
    s3.fail["GET"] = (403, "<Error><Code>AccessDenied</Code></Error>")
    with pytest.raises(storage.StorageError, match="ссылка для плеера: AccessDenied — у ключа нет доступа"):
        storage.check()
    assert s3.methods()[-1] == "DELETE" and s3.objects == {}


# ----------------------------------------------------------------------------- keeping a new recording
@pytest.fixture
def processing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    settings_store.set_many({"approver_chat_ids": []})
    segments = [seg("Спикер 1", 0.5, 7.5), seg("Спикер 2", 8.0, 15.5)]
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **kw: (20.0, [dict(s) for s in segments]))
    monkeypatch.setattr(pipeline.analyze, "analyze", lambda *a, **kw: {"title": "Отчёт", "summary": "Итог", "tasks": []})
    path = make_wav(tmp_path / "upload.wav")
    return pipeline.create_meeting("upload.wav", str(path), title="Планёрка"), path


def test_a_processed_recording_is_compressed_kept_and_the_local_copies_removed(s3, processing, tmp_path):
    mid, path = processing
    pipeline.process(mid)
    m = meeting(mid)
    assert m.status == "awaiting_approval" and not path.exists()
    assert m.recording_status == "stored" and leftovers(mid) == []
    rec = m.recording
    assert re.fullmatch(rf"recordings/\d{{4}}/\d{{2}}/m{mid}-[0-9a-f]{{12}}\.mp3", rec["key"])
    body, kind = s3.objects[rec["key"]]
    assert kind == "audio/mpeg" and rec["size"] == len(body) and rec["mime"] == "audio/mpeg"
    assert 60_000 < len(body) < 100_000  # 20 s at 32 kbit/s
    assert abs(rec["duration"] - 20.0) < 0.3
    stored, until = (dt.datetime.fromisoformat(rec[k].replace("Z", "+00:00")) for k in ("stored_at", "expires_at"))
    assert until - stored == dt.timedelta(days=180)
    copy = tmp_path / "copy.mp3"
    copy.write_bytes(body)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels,sample_rate",
                            "-of", "json", str(copy)], capture_output=True, text=True, check=True)
    assert json.loads(probe.stdout)["streams"] == [{"codec_name": "mp3", "sample_rate": "16000", "channels": 1}]


def test_in_production_compression_runs_next_to_the_processing(s3, processing, monkeypatch):
    mid, path = processing
    with SessionLocal() as s:
        s.get(Meeting, mid).status = "analyzing"
        s.commit()
    monkeypatch.setattr(config, "TESTING", False)
    job = audio_archive.start(mid, str(path), "web")
    assert job is not None and job.name == f"archive-{mid}"
    assert meeting(mid).recording_status in ("pending", "stored")
    path.unlink()  # the pipeline may delete the original at once: the archive holds its own link
    job.join(60)
    assert meeting(mid).recording_status == "stored" and leftovers(mid) == []


def test_without_storage_nothing_is_kept_and_the_original_still_goes(processing, monkeypatch):
    mid, path = processing
    pipeline.process(mid)
    m = meeting(mid)
    assert m.status == "awaiting_approval" and not path.exists()
    assert (m.recording_status or "") == "" and m.recording is None and leftovers(mid) == []


def test_demo_meetings_have_nothing_to_keep(s3):
    settings_store.set_many({"approver_chat_ids": []})
    mid = pipeline.create_meeting("demo", source="demo")
    pipeline.process(mid)
    assert meeting(mid).status == "awaiting_approval" and (meeting(mid).recording_status or "") == "" and s3.calls == []


def test_a_kept_recording_is_not_uploaded_again(s3, tmp_path):
    mid = kept()
    assert audio_archive.start(mid, str(make_wav(tmp_path / "again.wav", seconds=2)), "web") is None
    assert s3.calls == [] and meeting(mid).recording_status == "stored" and leftovers(mid) == []


def test_a_failed_upload_is_retried_by_the_background_loop(s3, processing):
    mid, path = processing
    s3.fail["PUT"] = (503, "<Error><Code>ServiceUnavailable</Code><Message>Please try again</Message></Error>")
    pipeline.process(mid)
    m = meeting(mid)
    assert m.status == "awaiting_approval" and not path.exists()
    assert m.recording_status == "pending" and m.recording["attempts"] == 1
    assert "ServiceUnavailable" in m.recording["error"] and len(leftovers(mid)) == 1
    due = dt.datetime.fromisoformat(m.recording["next_at"].replace("Z", "+00:00"))
    assert dt.timedelta(seconds=50) < due - now() <= dt.timedelta(seconds=60)
    assert audio_archive.view(m)["text"] == "Запись пока не загрузилась в хранилище, попробуем ещё раз автоматически."
    del s3.fail["PUT"]
    audio_archive.run_due()  # not yet due
    assert meeting(mid).recording_status == "pending" and s3.methods().count("PUT") == 1
    with SessionLocal() as s:
        row = s.get(Meeting, mid)
        row.recording = {**row.recording, "next_at": iso(now() - dt.timedelta(seconds=1))}
        s.commit()
    audio_archive.run_due()
    m = meeting(mid)
    assert m.recording_status == "stored" and m.recording["key"] in s3.objects and leftovers(mid) == []


def test_after_the_last_attempt_the_recording_is_reported_lost(s3, processing):
    mid, _ = processing
    s3.fail["PUT"] = (500, "")
    pipeline.process(mid)
    key = meeting(mid).recording["key"]
    for _ in audio_archive.RETRIES:
        with SessionLocal() as s:
            row = s.get(Meeting, mid)
            row.recording = {**row.recording, "next_at": iso(now() - dt.timedelta(seconds=1))}
            s.commit()
        audio_archive.run_due()
    m = meeting(mid)
    assert m.recording_status == "failed" and leftovers(mid) == []
    assert s3.methods().count("PUT") == len(audio_archive.RETRIES) + 1
    assert audio_archive.view(m)["text"] == "Запись не сохранилась: не удалось загрузить в хранилище: загрузка: HTTP 500."
    # a PUT that the storage finished while its answer was lost must not stay forever: deleted in the same pass
    assert ("DELETE", f"/{BUCKET}/{key}", "") in s3.calls and cleanups() == []


def test_a_compression_failure_never_stops_the_meeting(s3, processing, monkeypatch):
    mid, path = processing

    def broken(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"Invalid data found when processing input")
    monkeypatch.setattr(audio_archive, "compress", broken)
    pipeline.process(mid)
    m = meeting(mid)
    assert m.status == "awaiting_approval" and not path.exists() and leftovers(mid) == []
    assert m.recording_status == "failed" and m.recording["error"] == "не удалось сжать запись для хранения"
    assert s3.calls == []


def test_a_restart_before_the_upload_is_reported_after_a_grace_period(s3):
    since = now() - dt.timedelta(minutes=5)
    lost = add_meeting(title="Перезапуск", recording_status="pending", recording={"attempts": 0, "since": iso(since)})
    audio_archive.run_due()
    assert meeting(lost).recording_status == "pending"  # a deploy: the previous instance may still be compressing it
    with SessionLocal() as s:
        row = s.get(Meeting, lost)
        row.recording = {**row.recording, "since": iso(now() - audio_archive.STALE - dt.timedelta(seconds=5)),
                         "local": str(audio_archive.archive_dir() / "m999-gone.mp3")}
        s.commit()
    audio_archive.run_due()
    m = meeting(lost)
    assert m.recording_status == "failed" and "сервер перезапустился" in m.recording["error"] and s3.calls == []
    assert audio_archive.view(m)["text"] == "Запись не сохранилась: сервер перезапустился раньше, чем она загрузилась в хранилище."
    assert "restorable" not in audio_archive.view(m)  # an upload from the browser: its original is gone with the disk


def test_the_recording_is_kept_while_the_transcription_runs(s3, processing, monkeypatch):
    mid, path = processing
    seen = []

    def transcribing(*_args, **_kwargs):
        seen.append(meeting(mid).recording_status)  # synchronous under tests: already in the storage
        return 20.0, [seg("Спикер 1", 0.5, 7.5)]
    monkeypatch.setattr(pipeline.transcribe, "transcribe", transcribing)
    pipeline.process(mid)
    assert seen == ["stored"] and meeting(mid).recording_status == "stored" and s3.methods().count("PUT") == 1


def test_a_resumed_processing_keeps_the_recording_again(s3, tmp_path):
    mid = add_meeting(title="Возобновили", recording_status="pending",
                      recording={"attempts": 0, "since": iso(now()), "local": str(audio_archive.archive_dir() / "m1-lost.mp3")})
    original = make_wav(tmp_path / "resumed.wav", seconds=3)
    audio_archive.start(mid, str(original), "web")  # the copy of the interrupted run is gone: keep it again
    assert meeting(mid).recording_status == "stored" and s3.methods() == ["PUT"] and original.exists()
    with SessionLocal() as s:
        row = s.get(Meeting, mid)
        audio_archive.archive_dir().mkdir(parents=True, exist_ok=True)
        local = audio_archive.archive_dir() / "m1-waiting.mp3"
        local.write_bytes(b"mp3")
        row.recording_status, row.recording = "pending", {"attempts": 1, "since": iso(now()), "local": str(local), "key": "recordings/k.mp3"}
        s.commit()
    assert audio_archive.start(mid, str(original), "web") is None  # its compressed copy waits for a retry: not twice
    assert s3.methods() == ["PUT"] and meeting(mid).recording["local"] == str(local)


def test_forgotten_local_copies_are_swept(s3, monkeypatch):
    folder = audio_archive.archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    names = ("m5-aaaa.src.wav", "m6-bbbb.mp3", "m7-cccc.src.m4a")
    for name in names:
        (folder / name).write_bytes(b"x")
    add_meeting(title="Ждёт", recording_status="pending", recording={"attempts": 1, "local": str(folder / "m6-bbbb.mp3")})
    assert audio_archive._sweep() == 0  # all of them are fresh
    monkeypatch.setattr(audio_archive, "STALE", dt.timedelta(seconds=-60))
    audio_archive._ACTIVE.add(7)  # being compressed by this process
    try:
        assert audio_archive._sweep() == 1
    finally:
        audio_archive._ACTIVE.discard(7)
    assert sorted(p.name for p in folder.iterdir()) == ["m6-bbbb.mp3", "m7-cccc.src.m4a"]


def test_a_meeting_deleted_during_the_upload_leaves_nothing_behind(s3):
    folder = audio_archive.archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    local = folder / "m1-abcd.mp3"
    local.write_bytes(b"\xff\xf3" * 100)
    key = "recordings/2026/11/m1-0123456789ab.mp3"
    mid = add_meeting(title="Удаляют", recording_status="pending",
                      recording={"attempts": 0, "since": iso(now()), "local": str(local), "key": key})

    def deleted():
        with SessionLocal() as s:
            s.delete(s.get(Meeting, mid))
            s.commit()
    s3.during_put = deleted
    assert audio_archive._upload(mid) is True
    assert not local.exists() and cleanups() == [(key, "orphan")]
    audio_archive.run_due()
    assert s3.objects == {} and cleanups() == []


def test_local_paths_outside_the_archive_are_never_touched(s3, tmp_path):
    outside = tmp_path / "precious.wav"
    outside.write_bytes(b"data")
    mid = add_meeting(title="Чужой путь", recording_status="pending",
                      recording={"attempts": 1, "since": iso(now()), "local": str(outside), "key": "recordings/x.mp3"})
    assert audio_archive.remove(mid) is True
    assert outside.exists() and meeting(mid).recording_status == "deleted"
    assert cleanups() == [("recordings/x.mp3", "removed")]


# --------------------------------------------------------------------------------------- removing copies
def test_removing_a_recording_deletes_it_from_the_storage(s3):
    mid = kept()
    s3.objects["recordings/2026/11/m1-0123456789ab.mp3"] = (b"mp3", "audio/mpeg")
    assert audio_archive.remove(mid) is True and audio_archive.remove(mid) is False
    m = meeting(mid)
    assert m.recording_status == "deleted" and set(m.recording) == {"deleted_at"}
    assert audio_archive.view(m) == {"state": "deleted", "text": "Запись этого совещания удалена."}
    assert cleanups() == [("recordings/2026/11/m1-0123456789ab.mp3", "removed")]
    audio_archive.run_due()
    assert s3.objects == {} and cleanups() == []


def test_deletions_are_retried_with_a_growing_pause_and_then_given_up(s3):
    with SessionLocal() as s:
        s.add(StorageCleanup(key="recordings/2026/11/m7-aa.mp3", reason="meeting"))
        s.commit()
    s3.fail["DELETE"] = (500, "<Error><Code>InternalError</Code></Error>")
    audio_archive._clean_up()
    audio_archive._clean_up()  # not due yet: one request
    with SessionLocal() as s:
        row = s.query(StorageCleanup).one()
        assert row.attempts == 1 and "InternalError" in row.error
        wait = row.next_at.replace(tzinfo=row.next_at.tzinfo or dt.timezone.utc) - now()
        assert dt.timedelta(seconds=50) < wait <= dt.timedelta(seconds=60)
        row.attempts, row.next_at = audio_archive.CLEANUP_GIVE_UP - 1, now() - dt.timedelta(seconds=1)
        s.commit()
    assert s3.methods() == ["DELETE"]
    audio_archive._clean_up()
    assert cleanups() == [] and s3.methods() == ["DELETE", "DELETE"]


def test_only_recordings_are_ever_deleted_from_the_bucket(s3):
    with SessionLocal() as s:
        s.add_all([StorageCleanup(key="backups/db.sql", reason="meeting"),
                   StorageCleanup(key="recordings/../backups/db.sql", reason="meeting")])
        s.commit()
    audio_archive._clean_up()
    assert s3.calls == [] and cleanups() == []
    mid = kept(key="backups/db.sql")
    with SessionLocal() as s:
        audio_archive.forget(s, s.get(Meeting, mid), "meeting")
        s.commit()
    assert cleanups() == []


def test_copies_past_the_retention_period_are_removed(s3):
    old = kept(days=-1)
    fresh = kept(key="recordings/2026/11/m2-0123456789ab.mp3")
    assert audio_archive.view(meeting(old)) == {"state": "expired", "text": "Запись удалена: прошло 180 дней хранения."}
    assert audio_archive.purge_expired() == 1
    assert meeting(old).recording_status == "expired" and meeting(fresh).recording_status == "stored"
    assert cleanups() == [("recordings/2026/11/m1-0123456789ab.mp3", "expired")]


def test_deleting_a_meeting_deletes_its_recording(client, s3):
    mid = kept(status="done")
    response = client.post(f"/meetings/{mid}/delete", follow_redirects=False)
    assert response.status_code == 303 and meeting(mid) is None
    assert cleanups() == [("recordings/2026/11/m1-0123456789ab.mp3", "meeting")]


def test_the_recording_columns_are_added_to_an_existing_database():
    from sqlalchemy import inspect, text
    with db.engine.begin() as con:
        con.execute(text("ALTER TABLE meetings DROP COLUMN recording"))
        con.execute(text("ALTER TABLE meetings DROP COLUMN recording_status"))
    db.engine.dispose()
    db.init_db()
    columns = {c["name"] for c in inspect(db.engine).get_columns("meetings")}
    assert {"recording", "recording_status"} <= columns and "storage_cleanup" in inspect(db.engine).get_table_names()


# --------------------------------------------------------------------------------------- what the page says
def test_the_page_explains_every_state_of_the_recording(monkeypatch):
    def text(**fields):
        return audio_archive.view(Meeting(id=3, **fields))["text"]
    assert text(source="demo") == "У демо нет записи, слушать нечего."
    assert text(source="notion") == "Совещание импортировано текстом, записи нет."
    assert text(source="web", status="done").startswith("Чтобы слушать записи, подключите хранилище")
    connect(monkeypatch)
    assert text(source="web", status="transcribing") == "Запись сохранится в хранилище во время обработки."
    assert text(source="web", status="done") == "Запись этого совещания не сохранялась: оно обработано до подключения хранилища."
    assert text(recording_status="pending", recording={"attempts": 0}) == "Запись сохраняется в хранилище, обычно это занимает пару минут."
    assert text(recording_status="failed", recording={"error": "нет места"}) == "Запись не сохранилась: нет места."
    # earlier versions stored the reason with the same words in front: the page said them twice
    old = "запись не сохранилась: сервер перезапустился, не успев загрузить её в хранилище"
    assert text(recording_status="failed", recording={"error": old}) == (
        "Запись не сохранилась: сервер перезапустился, не успев загрузить её в хранилище.")
    assert text(recording_status="expired", recording={}) == "Запись удалена: прошло 180 дней хранения."
    until = dt.datetime(2027, 5, 1, 21, 30, tzinfo=dt.timezone.utc)  # already 2 May in Minsk
    view = audio_archive.view(Meeting(id=3, recording_status="stored", recording={"size": 14_400_000, "expires_at": iso(until)}))
    assert view == {"state": "stored", "playable": True, "src": "/meetings/3/recording", "download": "/meetings/3/recording?download=1",
                    "size": "14,4 МБ", "text": "Запись хранится до 02.05.2027"}


def test_the_player_is_offered_for_a_kept_recording(client, s3):
    mid = kept()
    html = client.get(f"/meetings/{mid}").text
    assert "data-vd-play " in html and "data-vd-rate" in html and "Запись хранится до" in html
    assert payload_of(html)["audio"] == {"src": f"/meetings/{mid}/recording"}
    assert f'href="/meetings/{mid}/recording?download=1"' in html and "Скачать MP3 · 1,2 МБ" in html
    assert f'action="/meetings/{mid}/recording/delete"' in html
    assert 'data-vd-play-seq="1"' in html and 'data-vd-play-seq="3"' in html  # «Кто это?»: «Спикер 2» can be listened to
    assert "Прослушайте фрагменты кнопкой ▶" in html and "после подключения хранилища" not in html
    assert f'<audio class="vd-rec-audio" controls preload="none" src="/meetings/{mid}/recording">' in html  # without JavaScript


def test_without_a_kept_recording_the_button_says_why(client, monkeypatch):
    mid = add_meeting(title="Старое", transcript=TRANSCRIPT, duration_sec=90, status="done",
                      report={"title": "Старое", "summary": "", "tasks": []})
    html = client.get(f"/meetings/{mid}").text
    assert "data-vd-play " not in html and "data-vd-play-seq" not in html and "audio" not in payload_of(html)
    assert 'disabled title="Чтобы слушать записи, подключите хранилище: Настройки → Хранилище записей."' in html
    connect(monkeypatch)
    html = client.get(f"/meetings/{mid}").text
    assert "Запись этого совещания не сохранялась: оно обработано до подключения хранилища." in html
    assert client.get(f"/meetings/{mid}/recording", follow_redirects=False).status_code == 404


def test_listening_goes_through_a_short_presigned_link(client, s3):
    mid = kept()
    response = client.get(f"/meetings/{mid}/recording", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{ENDPOINT}/{BUCKET}/recordings/2026/11/m1-0123456789ab.mp3?X-Amz-Algorithm=AWS4-HMAC-SHA256&")
    assert "X-Amz-Expires=10800&" in location and "response-content-disposition" not in location
    assert response.headers["cache-control"] == "private, no-store" and response.headers["referrer-policy"] == "no-referrer"
    download = client.get(f"/meetings/{mid}/recording?download=1", follow_redirects=False).headers["location"]
    disposition = urllib.parse.parse_qs(urllib.parse.urlsplit(download).query)["response-content-disposition"][0]
    assert disposition == "attachment; filename=\"02-11-2026.mp3\"; filename*=UTF-8''" + urllib.parse.quote("Планёрка 02.11.2026.mp3")
    assert "X-Amz-Credential=AKID123%2F" in location and "SECRET" not in location  # the key id is public by design


def test_listening_needs_a_signed_in_user_with_access(client, s3):
    from fastapi.testclient import TestClient
    from app.main import app
    mid = kept()
    with TestClient(app) as anonymous:
        response = anonymous.get(f"/meetings/{mid}/recording", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    with SessionLocal() as s:  # recorded in someone else's browser
        s.add(Recording(id="r-other", owner="user:999", mime_type="audio/webm", meeting_id=mid, status="done"))
        s.commit()
    assert client.get(f"/meetings/{mid}/recording", follow_redirects=False).status_code == 404
    html = client.get(f"/meetings/{mid}").text
    assert "data-vd-play " not in html and "audio" not in payload_of(html)
    assert "Запись этого совещания доступна только тому, кто его записал." in html


def test_expired_or_broken_storage_links_are_refused(client, s3, monkeypatch):
    old = kept(days=-1)
    assert client.get(f"/meetings/{old}/recording", follow_redirects=False).status_code == 404
    mid = kept(key="recordings/2026/11/m2-0123456789ab.mp3")
    monkeypatch.setattr(config, "S3_ENDPOINT", "http://insecure.example.com")
    response = client.get(f"/meetings/{mid}/recording", follow_redirects=False)
    assert response.status_code == 503 and "S3_ENDPOINT: нужен адрес с https://" in response.text


def test_only_a_superuser_removes_a_recording(client, s3):
    from fastapi.testclient import TestClient
    from app.main import app
    mid = kept()
    client.post("/admin/users/create", data={"username": "worker", "password": "correct-horse", "role": "user"})
    with TestClient(app) as other:
        other.post("/login", data={"username": "worker", "password": "correct-horse"})
        html = other.get(f"/meetings/{mid}").text
        assert "data-vd-play " in html and "/recording/delete" not in html
        assert other.get(f"/meetings/{mid}/recording", follow_redirects=False).status_code == 302
        assert other.post(f"/meetings/{mid}/recording/delete", follow_redirects=False).status_code == 403
    assert meeting(mid).recording_status == "stored"
    response = client.post(f"/meetings/{mid}/recording/delete", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?recording=deleted#voice-diagram"
    assert meeting(mid).recording_status == "deleted" and cleanups() == [("recordings/2026/11/m1-0123456789ab.mp3", "removed")]
    html = client.get(f"/meetings/{mid}?recording=deleted").text
    assert "Запись удалена из хранилища. Расшифровка, протокол и диаграмма остались." in html
    assert "Запись этого совещания удалена." in html and "data-vd-play " not in html


# --------------------------------------------------------------------------------------------------- settings
def test_settings_show_the_storage_and_check_it(client, s3):
    kept()
    page = client.get("/settings").text
    assert 'id="recording-storage"' in page and "Подключено" in page and "acc123.r2.cloudflarestorage.com" in page
    assert "Хранится записей: 1 · 1,2 МБ" in page and 'formaction="/settings/test-storage"' in page
    assert "SECRET/abc+xyz" not in page
    response = client.post("/settings/test-storage", follow_redirects=False)
    assert response.status_code == 303
    assert urllib.parse.unquote(response.headers["location"]) == (
        "/settings?msg=Хранилище записей: запись, чтение и удаление работают (бакет sysai-recordings на "
        "acc123.r2.cloudflarestorage.com)#recording-storage")
    s3.fail["PUT"] = (403, "<Error><Code>InvalidAccessKeyId</Code></Error>")
    location = urllib.parse.unquote(client.post("/settings/test-storage", follow_redirects=False).headers["location"])
    assert location == "/settings?msg=Хранилище записей: запись: InvalidAccessKeyId — неверный S3_ACCESS_KEY_ID#recording-storage"


def test_settings_explain_how_to_connect_the_storage(client, monkeypatch):
    page = client.get("/settings").text
    assert "Пока не подключено" in page and "S3_SECRET_ACCESS_KEY" in page and "Проверить хранилище" not in page
    monkeypatch.setattr(config, "S3_ENDPOINT", ENDPOINT)
    page = client.get("/settings").text
    assert "Не хватает настроек" in page and "не заданы: S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY" in page
    location = urllib.parse.unquote(client.post("/settings/test-storage", follow_redirects=False).headers["location"])
    assert "хранилище не подключено: задайте S3_ENDPOINT, S3_BUCKET" in location


# ------------------------------------------------------------------------------ a TicNote recording taken again
@pytest.fixture
def cloud(monkeypatch, tmp_path):
    """TicNote's cloud: every recording is still there as a WAV."""
    from app import ticnote
    settings_store.set_many({"ticnote_appkey": "tnovs_sk_x"})
    wav = make_wav(tmp_path / "cloud.wav", seconds=4)
    state = {"asked": [], "fail": None}

    def detail(appkey, record_id):
        state["asked"].append(record_id)
        return {"formatUrl": f"https://cdn.ticnote.example/{record_id}.wav"}

    def download(url, dest, timeout=180):
        if state["fail"]:
            raise ticnote.TicNoteError(state["fail"])
        shutil.copyfile(wav, dest)
        return pathlib.Path(dest).stat().st_size
    monkeypatch.setattr(ticnote, "file_detail", detail)
    monkeypatch.setattr(ticnote, "download_wav", download)
    return state


def from_ticnote(record_id="rec-1", **fields):
    fields = {"title": "Планёрка", "transcript": TRANSCRIPT, "duration_sec": 90, "source": "ticnote",
              "report": {"title": "Планёрка", "summary": "", "tasks": []}, **fields}
    mid = add_meeting(**fields)
    with SessionLocal() as s:
        s.add(TicnoteImport(record_id=record_id, title="Планёрка", status="queued", attempts=1, meeting_id=mid))
        s.commit()
    return mid


def failed_at(mid, moment):
    with SessionLocal() as s:
        row = s.get(Meeting, mid)
        row.recording = {**row.recording, "failed_at": iso(moment)}
        s.commit()


def test_a_ticnote_recording_lost_in_a_restart_is_downloaded_again(s3, cloud):
    since = now() - audio_archive.STALE - dt.timedelta(seconds=5)
    mid = from_ticnote(recording_status="pending", recording={"attempts": 0, "since": iso(since),
                                                             "local": str(audio_archive.archive_dir() / "m1-gone.mp3")})
    audio_archive.run_due()
    m = meeting(mid)
    assert cloud["asked"] == ["rec-1"] and m.recording_status == "stored" and leftovers(mid) == []
    assert m.recording["key"] in s3.objects and abs(m.recording["duration"] - 4.0) < 0.3
    assert audio_archive.view(m)["playable"] and s3.methods() == ["PUT"]


def test_old_lost_ticnote_recordings_come_back_one_at_a_time(s3, cloud):
    old = "запись не сохранилась: сервер перезапустился, не успев загрузить её в хранилище"
    first = from_ticnote("rec-1", recording_status="failed", recording={"error": old, "failed_at": iso(now())})
    second = from_ticnote("rec-2", recording_status="failed", recording={"error": old, "failed_at": iso(now())})
    view = audio_archive.view(meeting(first))
    assert view["restorable"] and view["text"] == ("Запись не сохранилась: сервер перезапустился, не успев загрузить её в "
                                                   "хранилище. Скачаем её из TicNote заново автоматически.")
    audio_archive.run_due()
    assert cloud["asked"] == ["rec-2"] and meeting(second).recording_status == "stored"  # the newest first
    assert meeting(first).recording_status == "failed"
    audio_archive.run_due()
    assert cloud["asked"] == ["rec-2", "rec-1"] and meeting(first).recording_status == "stored"


def test_taking_it_again_is_retried_with_growing_pauses_and_then_left_to_the_button(s3, cloud):
    mid = from_ticnote(recording_status="failed", recording={"error": audio_archive.LOST, "failed_at": iso(now())})
    cloud["fail"] = "Не удалось скачать WAV: timed out"
    audio_archive.run_due()
    m = meeting(mid)
    assert m.recording_status == "failed" and m.recording["restores"] == 1 and leftovers(mid) == []
    assert m.recording["error"] == "не удалось скачать её из TicNote заново (Не удалось скачать WAV: timed out)"
    audio_archive.run_due()
    assert len(cloud["asked"]) == 1  # the next attempt waits
    for pause in audio_archive.RESTORE_PAUSE[1:]:
        failed_at(mid, now() - dt.timedelta(seconds=pause + 1))
        audio_archive.run_due()
    m = meeting(mid)
    assert len(cloud["asked"]) == audio_archive.MAX_RESTORES and m.recording["restores"] == audio_archive.MAX_RESTORES
    failed_at(mid, now() - dt.timedelta(days=2))
    audio_archive.run_due()
    assert len(cloud["asked"]) == audio_archive.MAX_RESTORES  # given up: no more by itself
    view = audio_archive.view(meeting(mid))
    assert view["restorable"] and "автоматически" not in view["text"] and s3.calls == []
    cloud["fail"] = None
    assert audio_archive.restore(mid) and meeting(mid).recording_status == "stored"  # the button still works


def test_only_ticnote_meetings_within_the_retention_period_are_taken_again(s3, cloud, monkeypatch):
    web = add_meeting(title="Загрузка", recording_status="failed", recording={"error": audio_archive.LOST, "failed_at": iso(now())})
    ancient = from_ticnote("rec-9", recording_status="failed", created_at=now() - dt.timedelta(days=200),
                           recording={"error": audio_archive.LOST, "failed_at": iso(now())})
    busy = from_ticnote("rec-8", status="analyzing")
    audio_archive.run_due()
    assert cloud["asked"] == [] and not audio_archive.restore(web) and not audio_archive.restore(ancient)
    assert not audio_archive.restore(busy) and "restorable" not in audio_archive.view(meeting(ancient))
    fresh = from_ticnote("rec-7", recording_status="failed", recording={"error": audio_archive.LOST})
    settings_store.set_many({"ticnote_appkey": ""})
    monkeypatch.setattr(config, "TICNOTE_APPKEY", "")
    audio_archive.run_due()
    assert cloud["asked"] == [] and "restorable" not in audio_archive.view(meeting(fresh))


def test_the_page_offers_to_take_a_ticnote_recording_again(client, s3, cloud):
    mid = from_ticnote(recording_status="failed", recording={"error": "не удалось сжать запись для хранения", "restores": 3})
    html = client.get(f"/meetings/{mid}").text
    assert f'action="/meetings/{mid}/recording/restore"' in html and "Скачать из TicNote заново" in html
    assert "Запись не сохранилась: не удалось сжать запись для хранения." in html
    response = client.post(f"/meetings/{mid}/recording/restore", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?recording=restoring#voice-diagram"
    assert meeting(mid).recording_status == "stored" and cloud["asked"] == ["rec-1"]
    html = client.get(f"/meetings/{mid}?recording=restoring").text
    assert "Скачиваем запись из TicNote заново" in html and "data-vd-play " in html and "/recording/restore" not in html
    response = client.post(f"/meetings/{mid}/recording/restore", follow_redirects=False)  # nothing to take any more
    assert response.headers["location"] == f"/meetings/{mid}?recording=unavailable#voice-diagram"
    web = kept(recording_status="", recording=None)
    assert "/recording/restore" not in client.get(f"/meetings/{web}").text


def test_a_ticnote_recording_being_downloaded_says_so():
    view = audio_archive.view(Meeting(id=4, source="ticnote", recording_status="pending",
                                      recording={"attempts": 0, "from": "ticnote"}))
    assert view == {"state": "pending", "text": "Скачиваем запись из TicNote заново и сохраняем в хранилище, это займёт несколько минут."}
