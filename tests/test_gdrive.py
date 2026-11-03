"""№28 «Google Drive»: a folder of the company shared with a service account. Files put into «Входящие» come to
«Документы» (read and analyzed like files from Telegram); the documents of SYSAI go to «Документы» by month (not the
confidential ones); «Шаблоны» and «Документы по шаблону» are kept the same both ways. In an ordinary «Мой диск» Google
gives a service account no space: reading works, putting is shown as impossible. The Drive API is a fake behind
httpx.MockTransport (the JWT of the key is checked by its public key)."""
import base64
import datetime as dt
import hashlib
import json
import re
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import doc_templates, gdrive, settings_store
from app.db import DocTemplate, Document, DriveFile, SessionLocal
from test_doc_templates import make_docx

FOLDER = gdrive.FOLDER
ROOT = "1RootFolderOfTheCompany"
_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY = json.dumps({"type": "service_account", "project_id": "sysai", "client_email": "sysai@sysai.iam.gserviceaccount.com",
                  "private_key": _PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()).decode(),
                  "token_uri": "https://oauth2.googleapis.com/token"})


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeDrive:
    def __init__(self, shared: bool = True):
        self.shared, self.n, self.calls = shared, 0, []
        self.files = {ROOT: {"id": ROOT, "name": "Компания", "mimeType": FOLDER, "parents": [],
                             "driveId": "0AShared" if shared else None}}

    def new_id(self) -> str:
        self.n += 1
        return f"f{self.n:04d}"

    def folder(self, name: str) -> str:
        return next(f["id"] for f in self.files.values() if f["name"] == name and f["mimeType"] == FOLDER)

    def put(self, folder: str, name: str, data: bytes, mime: str = "application/pdf") -> str:
        fid = self.new_id()
        self.files[fid] = {"id": fid, "name": name, "mimeType": mime, "parents": [self.folder(folder)], "data": data,
                           "version": "1", "modifiedTime": "2026-03-02T10:00:00.000Z"}
        self._sign(fid)
        return fid

    def change(self, fid: str, data: bytes) -> None:
        f = self.files[fid]
        f["data"], f["version"] = data, str(int(f["version"]) + 1)
        self._sign(fid)

    def _sign(self, fid: str) -> None:
        f = self.files[fid]
        if not f["mimeType"].startswith("application/vnd.google-apps."):
            f["md5Checksum"], f["size"] = hashlib.md5(f["data"]).hexdigest(), str(len(f["data"]))

    def in_folder(self, name: str) -> list:
        parent = self.folder(name)
        return [f for f in self.files.values() if parent in f["parents"]]

    @staticmethod
    def meta(f: dict) -> dict:
        return {k: v for k, v in f.items() if k not in ("data", "parents") and v is not None}

    def error(self, code: int, reason: str, message: str) -> httpx.Response:
        return httpx.Response(code, json={"error": {"code": code, "message": message,
                                                    "errors": [{"reason": reason, "message": message}]}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        self.calls.append((method, url.path))
        if url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            head, claims, signature = form["assertion"][0].split(".")
            _PRIVATE.public_key().verify(_unb64(signature), f"{head}.{claims}".encode(), padding.PKCS1v15(),
                                         hashes.SHA256())
            assert json.loads(_unb64(claims))["scope"] == gdrive.SCOPE
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600, "token_type": "Bearer"})
        if request.headers.get("Authorization") != "Bearer token-1":
            return self.error(401, "authError", "Invalid Credentials")
        path = url.path
        if path == "/drive/v3/files" and method == "GET":
            q = url.params["q"]
            parent = re.search(r"'([^']+)' in parents", q).group(1)
            folders = "mimeType = '" in q
            name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            rows = [f for f in self.files.values() if parent in f["parents"] and (f["mimeType"] == FOLDER) == folders
                    and (name is None or f["name"] == name.group(1).replace("\\'", "'"))]
            return httpx.Response(200, json={"files": [self.meta(f) for f in rows]})
        if path == "/drive/v3/files" and method == "POST":
            body = json.loads(request.content)
            fid = self.new_id()
            self.files[fid] = {"id": fid, "name": body["name"], "mimeType": body["mimeType"], "parents": body["parents"]}
            return httpx.Response(200, json={"id": fid})
        if path == "/upload/drive/v3/files" and method == "POST":
            if not self.shared:
                return self.error(403, "storageQuotaExceeded", "Service Accounts do not have storage quota.")
            boundary = re.search(r"boundary=(\S+)", request.headers["content-type"]).group(1).encode()
            parts = request.content.split(b"--" + boundary)
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
            head, data = parts[2].split(b"\r\n\r\n", 1)
            data = data[:-2] if data.endswith(b"\r\n") else data
            mime = re.search(rb"Content-Type: (\S+)", head).group(1).decode()
            fid = self.new_id()
            self.files[fid] = {"id": fid, "name": meta["name"], "mimeType": mime, "parents": meta["parents"],
                               "data": data, "version": "1", "modifiedTime": "2026-03-02T11:00:00.000Z"}
            self._sign(fid)
            return httpx.Response(200, json=self.meta(self.files[fid]))
        m = re.fullmatch(r"/upload/drive/v3/files/([\w-]+)", path)
        if m and method == "PATCH":
            self.change(m.group(1), request.content)
            return httpx.Response(200, json=self.meta(self.files[m.group(1)]))
        m = re.fullmatch(r"/drive/v3/files/([\w-]+)(/export)?", path)
        if m:
            f = self.files.get(m.group(1))
            if f is None:
                return self.error(404, "notFound", "File not found")
            if method == "DELETE":
                del self.files[f["id"]]
                return httpx.Response(204)
            if m.group(2):
                return httpx.Response(200, content=f["data"])
            if url.params.get("alt") == "media":
                return httpx.Response(200, content=f["data"])
            meta = self.meta(f)
            meta["capabilities"] = {"canAddChildren": True}
            return httpx.Response(200, json=meta)
        raise AssertionError(f"unexpected {method} {url}")


def _connect(monkeypatch, shared=True) -> FakeDrive:
    fake = FakeDrive(shared)
    monkeypatch.setattr(gdrive, "_transport", httpx.MockTransport(fake))
    gdrive._TOKEN.update(value="", until=0.0, email="")
    gdrive.save(KEY, f"https://drive.google.com/drive/folders/{ROOT}?usp=sharing")
    gdrive.switched({"gdrive_inbox": True, "gdrive_upload": True, "gdrive_templates": True})
    return fake


@pytest.fixture
def drive(monkeypatch):
    return _connect(monkeypatch)


@pytest.fixture
def my_drive(monkeypatch):
    return _connect(monkeypatch, shared=False)


def test_the_check_finds_or_makes_the_three_folders_and_tries_putting(drive):
    state = gdrive.check()
    assert state["root_name"] == "Компания" and state["shared"] is True and state["upload_blocked"] == ""
    assert state["email"] == "sysai@sysai.iam.gserviceaccount.com"
    assert {f["name"] for f in drive.files.values() if f["mimeType"] == FOLDER} == {"Компания", "Входящие", "Документы",
                                                                                    "Шаблоны"}
    assert not [f for f in drive.files.values() if f["name"].startswith("sysai-")]  # the probe is removed
    again = gdrive.check()
    assert again["folders"] == state["folders"]  # found, not made again


def test_files_put_into_inbox_come_to_documents_once_and_again_when_changed(drive):
    gdrive.check()
    pdf = drive.put("Входящие", "Счёт 15.pdf", b"%PDF-1.4 invoice")
    drive.files["gdoc1"] = {"id": "gdoc1", "name": "Протокол", "mimeType": "application/vnd.google-apps.document",
                                   "parents": [drive.folder("Входящие")], "data": make_docx(), "version": "3",
                                   "modifiedTime": "2026-03-02T10:00:00.000Z"}
    drive.files["form1"] = {"id": "form1", "name": "Анкета", "mimeType": "application/vnd.google-apps.form",
                            "parents": [drive.folder("Входящие")], "version": "1"}
    state = gdrive.sync()
    assert state["ok"] is True and "файлов из «Входящих»: 2" in state["summary"]
    gdrive.sync()
    with SessionLocal() as s:
        docs = s.query(Document).order_by(Document.id).all()
        assert [(d.chat_id, d.filename, d.kind) for d in docs] == [("drive", "Счёт 15.pdf", "pdf"),
                                                                   ("drive", "Протокол.docx", "docx")]
        assert bytes(docs[0].data) == b"%PDF-1.4 invoice" and docs[0].text_status == "pending"
        assert "Google Drive" in docs[0].caption
        assert s.query(DriveFile).filter_by(file_id="form1").one().status == "skipped"
    drive.change(pdf, b"%PDF-1.4 invoice v2")
    gdrive.sync()
    with SessionLocal() as s:
        assert s.query(Document).count() == 3
        assert bytes(s.query(Document).order_by(Document.id.desc()).first().data) == b"%PDF-1.4 invoice v2"


def test_documents_of_sysai_go_to_drive_by_month_but_not_confidential_or_from_drive(drive):
    gdrive.check()
    with SessionLocal() as s:
        for i, (chat, name, sensitive) in enumerate((("mail", "Договор.pdf", False), ("123", "Паспорт.jpg", True),
                                                      ("drive", "Из Drive.pdf", False)), 1):
            s.add(Document(chat_id=chat, message_id=i, filename=name, mime="application/pdf", size=3, sha256="x" * 64,
                           data=b"abc", sensitive=sensitive))
        s.commit()
    state = gdrive.sync()
    gdrive.sync()
    assert "положено документов в Drive: 1" in state["summary"]
    month = next(f for f in drive.files.values() if re.fullmatch(r"\d{4}-\d{2}", f["name"]))
    assert month["parents"] == [drive.folder("Документы")]
    put = [f for f in drive.files.values() if month["id"] in f["parents"]]
    assert [f["name"] for f in put] == ["Договор.pdf"] and put[0]["data"] == b"abc"


def test_documents_from_before_the_connection_are_not_put(drive):
    with SessionLocal() as s:
        s.add(Document(chat_id="mail", message_id=1, filename="Старый.pdf", size=3, sha256="x" * 64, data=b"abc"))
        s.commit()
    gdrive.switched({"gdrive_upload": False})
    settings_store.set_many({"gdrive_upload": False})
    gdrive.switched({"gdrive_upload": True})
    gdrive.sync()
    assert not [f for f in drive.files.values() if f["name"] == "Старый.pdf"]


def test_in_my_drive_reading_works_and_putting_is_shown_as_impossible(my_drive):
    state = gdrive.check()
    assert "общий диск" in state["upload_blocked"]
    my_drive.put("Входящие", "Акт.pdf", b"%PDF act")
    with SessionLocal() as s:
        s.add(Document(chat_id="mail", message_id=1, filename="Новый.pdf", size=3, sha256="x" * 64, data=b"abc"))
        s.commit()
    uploads = sum(1 for m, p in my_drive.calls if p == "/upload/drive/v3/files")
    state = gdrive.sync()
    assert state["ok"] is True
    assert sum(1 for m, p in my_drive.calls if p == "/upload/drive/v3/files") == uploads  # not tried again
    with SessionLocal() as s:
        assert [d.filename for d in s.query(Document).filter_by(chat_id="drive")] == ["Акт.pdf"]


def test_templates_are_kept_the_same_both_ways(drive):
    gdrive.check()
    first = make_docx(body='<w:p><w:r><w:t xml:space="preserve">Кому: {{Получатель}}</w:t></w:r></w:p>', header=None)
    fid = drive.put("Шаблоны", "Гарантийное письмо.docx", first, gdrive.DOCX)
    drive.put("Шаблоны", "Прайс.pdf", b"%PDF price")
    state = gdrive.sync()
    assert "новых шаблонов: 1" in state["summary"], state
    with SessionLocal() as s:
        t = s.query(DocTemplate).one()
        assert t.title == "Гарантийное письмо" and [f["name"] for f in t.fields] == ["Получатель"]
        assert t.updated_by == "Google Drive"
        tid = t.id
        assert s.query(DriveFile).filter_by(name="Прайс.pdf").one().status == "skipped"
    drive.change(fid, make_docx(body='<w:p><w:r><w:t xml:space="preserve">{{Получатель}}, {{Сумма}}</w:t></w:r></w:p>',
                                header=None))
    gdrive.sync()
    with SessionLocal() as s:
        assert [f["name"] for f in s.get(DocTemplate, tid).fields] == ["Получатель", "Сумма"]
    # made in the panel → «Шаблоны»; its new file replaces the one in Drive
    own = doc_templates.save_template(title="Акт сверки", filename="akt.docx", data=make_docx(header=None))
    gdrive.sync()
    put = [f for f in drive.in_folder("Шаблоны") if f["name"] == "akt.docx"]
    assert len(put) == 1
    newer = make_docx(body='<w:p><w:r><w:t xml:space="preserve">Период: {{Период}}</w:t></w:r></w:p>', header=None)
    doc_templates.save_template(template_id=own, title="Акт сверки", filename="akt.docx", data=newer)
    gdrive.sync()
    assert drive.files[put[0]["id"]]["data"] == newer
    calls = len(drive.calls)
    gdrive.sync()
    assert not [c for c in drive.calls[calls:] if c[0] in ("PATCH", "POST")]  # nothing changed: nothing sent
    # removed from «Шаблоны» → switched off here
    del drive.files[fid]
    gdrive.sync()
    with SessionLocal() as s:
        assert s.get(DocTemplate, tid).active is False and s.get(DocTemplate, own).active is True


def test_the_key_and_the_folder_are_checked_before_saving(monkeypatch):
    with pytest.raises(gdrive.DriveError, match="JSON"):
        gdrive.save("не ключ", ROOT)
    with pytest.raises(gdrive.DriveError, match="service_account"):
        gdrive.save(json.dumps({"type": "authorized_user"}), ROOT)
    with pytest.raises(gdrive.DriveError, match="ссылку на папку"):
        gdrive.save(KEY, "https://example.com/")
    assert gdrive.folder_id(f"https://drive.google.com/drive/u/0/folders/{ROOT}") == ROOT
    assert gdrive.folder_id(f"https://drive.google.com/open?id={ROOT}") == ROOT
    assert gdrive.folder_id(ROOT) == ROOT

    def refused(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
    monkeypatch.setattr(gdrive, "_transport", httpx.MockTransport(refused))
    gdrive._TOKEN.update(value="", until=0.0, email="")
    gdrive.save(KEY, ROOT)
    with pytest.raises(gdrive.DriveError, match="Google не принял ключ: Invalid JWT Signature"):
        gdrive.check()


def test_the_page_connects_the_folder_and_never_shows_the_key(client, monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(gdrive, "_transport", httpx.MockTransport(fake))
    gdrive._TOKEN.update(value="", until=0.0, email="")
    r = client.post("/integrations/drive", data={"key": KEY, "folder": f"https://drive.google.com/drive/folders/{ROOT}",
                                                 "present": "1", "gdrive_inbox": "on", "gdrive_templates": "on"},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"], r.headers["location"]
    assert settings_store.get("gdrive_upload") is False and settings_store.get("gdrive_inbox") is True
    page = client.get("/integrations").text
    assert "sysai@sysai.iam.gserviceaccount.com" in page and "папка «Компания»" in page
    assert "PRIVATE KEY" not in page
    fake.put("Входящие", "Письмо.pdf", b"%PDF")
    assert "msg=" in client.post("/integrations/drive/sync", follow_redirects=False).headers["location"]
    with SessionLocal() as s:
        doc_id = s.query(Document).filter_by(chat_id="drive").one().id
    assert "Google Drive" in client.get(f"/documents/{doc_id}/view").text
    client.post("/integrations/drive/forget")
    assert not settings_store.get("gdrive_key") and not gdrive.configured()
