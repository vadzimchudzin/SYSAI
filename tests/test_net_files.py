"""The shared storage of people and agents (docs/opo/TASKS.md, stage 14): sections, versions, the trash, the rights."""
import base64
import hashlib
import json
from urllib.parse import unquote_plus

import pytest

from app.db import Department, SessionLocal, User
from app.net import access, files
from app.net.models import NetAccessProfile, NetFile, NetFileVersion

BOSS = {"role": "superuser", "username": "sysai"}


def _up(client, folder, name, data: bytes, **form):
    return client.post("/files/upload", data={"folder": folder, **form}, files={"files": (name, data, "text/plain")},
                       follow_redirects=False)


def _said(response) -> str:
    return unquote_plus(response.headers["location"])


def _mcp(client, agent, name, args=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
    r = client.post("/mcp", json=body, headers={**agent["headers"], "Accept": "application/json, text/event-stream"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _open_storage(client, agent, read, write, profile="opo-engineer"):
    with SessionLocal() as s:
        profile_id = s.query(NetAccessProfile).filter(NetAccessProfile.code == profile).one().id
    r = client.post(f"/agents/{agent['id']}/access", data={
        "profile_id": profile_id, "department_id": agent["department_id"],
        "overrides": json.dumps({"files": {"read": read, "write": write}})}, follow_redirects=False)
    assert "msg=" in r.headers["location"], _said(r)


def test_sections_versions_and_downloads(client, net_agent):
    page = client.get("/files").text
    assert "Общее" in page and "ОПО" in page and "Хранилище" in page
    assert "msg=" in _up(client, "Общее", "план.txt", "версия 1".encode()).headers["location"]
    assert "без изменений: 1" in _said(_up(client, "Общее", "план.txt", "версия 1".encode()))  # the same bytes: no version
    _up(client, "общее", "ПЛАН.txt", "версия 2".encode(), note="поправил сроки")  # Windows does not tell the case either
    with SessionLocal() as s:
        row = s.query(NetFile).filter(NetFile.path == "Общее/план.txt").one()
        assert row.version == 2 and s.query(NetFileVersion).filter(NetFileVersion.file_id == row.id).count() == 2
        assert s.query(NetFile).count() == 1
    raw = client.get("/files/raw", params={"path": "Общее/план.txt"})
    assert raw.content == "версия 2".encode() and "attachment" in raw.headers["content-disposition"]
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert client.get("/files/raw", params={"path": "Общее/план.txt", "v": 1}).content == "версия 1".encode()
    card = client.get("/files", params={"path": "Общее", "f": "Общее/план.txt"}).text
    assert "поправил сроки" in card and 'data-open="1"' in card
    # folders appear by themselves; a new one by hand
    _up(client, "ОПО/24-017/ТО", "io.csv", b"tag;addr")
    boss = files.for_person(BOSS)
    assert [i["name"] for i in files.listing(boss, "ОПО")["items"]] == ["24-017"]
    r = client.post("/files/mkdir", data={"folder": "ОПО", "name": "Шаблоны"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert [i["name"] for i in files.listing(boss, "ОПО")["items"]] == ["24-017", "Шаблоны"]
    assert "io.csv" in client.get("/files", params={"q": "IO"}).text
    with pytest.raises(files.FilesError):
        files.put(boss, "Общее/../секрет.txt", data=b"x")
    with pytest.raises(files.FilesError, match="Нет раздела"):
        files.put(boss, "Чужое/a.txt", data=b"x")
    assert "error=" in _up(client, "Общее/24-017", "a:b.txt", b"x").headers["location"]


def test_the_trash_gives_back_and_only_the_boss_deletes_for_good(client, net_agent):
    _up(client, "Общее", "a.txt", b"one")
    _up(client, "Общее/Папка", "b.txt", b"two")
    boss = files.for_person(BOSS)
    stored = files.blob(boss, "Общее/a.txt")["stored"]
    assert files._disk(stored.partition(":")[2]).exists()
    client.post("/files/delete", data={"path": "Общее/Папка"})
    assert [i["name"] for i in files.listing(boss, "Общее")["items"]] == ["a.txt"]
    assert {i["path"] for i in files.listing(boss, trash=True)["items"]} == {"Общее/Папка", "Общее/Папка/b.txt"}
    assert "b.txt" in client.get("/files", params={"trash": "1"}).text
    client.post("/files/restore", data={"path": "Общее/Папка"})
    assert files.read_text(boss, "Общее/Папка/b.txt")["text"] == "two"
    client.post("/files/delete", data={"path": "Общее/a.txt"})
    with SessionLocal() as s:
        s.add(User(username="ivan", password_hash="x", role="user", status="active"))
        s.commit()
    with pytest.raises(files.FilesError, match="руководитель"):
        files.purge(files.for_person({"role": "user", "username": "ivan", "id": 99}), "Общее/a.txt")
    r = client.post("/files/purge", data={"path": "Общее/a.txt"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert not files._disk(stored.partition(":")[2]).exists()  # nobody else had these bytes
    with SessionLocal() as s:
        assert s.query(NetFile).filter(NetFile.path == "Общее/a.txt").first() is None


def test_people_see_the_common_section_and_their_departments(client, net_agent):
    with SessionLocal() as s:
        s.add(Department(name="ПТО"))
        s.commit()
    boss = files.for_person(BOSS)
    files.put(boss, "ПТО/смета.xlsx", data=b"pto")
    with SessionLocal() as s:
        s.get(User, net_agent["user_id"]).role = "user"  # the owner of the ОПО agent is now a usual employee
        s.commit()
    me = files.for_person({"role": "user", "username": "sysai", "id": net_agent["user_id"]})
    assert me.can_write("Общее") and me.can_write("ОПО") and not me.can_read("ПТО")
    assert [i["name"] for i in files.listing(me)["items"]] == ["Общее", "ОПО"]
    with pytest.raises(files.FilesError, match="Нет доступа"):
        files.read_text(me, "ПТО/смета.xlsx")
    assert files.listing(me, q="смета")["items"] == []


def test_agents_get_the_storage_only_when_it_is_opened_in_the_access(client, net_agent):
    assert access.rules_for(net_agent["id"])["files"] == {"read": [], "write": []}  # closed by default
    closed = _mcp(client, net_agent, "files_list")
    assert closed["isError"] and "не открыто" in closed["content"][0]["text"]
    assert client.get("/agents/api/files", headers=net_agent["headers"]).status_code == 403
    files.put(files.for_person(BOSS), "Общее/Регламент.md", data="# Регламент\nПроверять теги.".encode())
    _open_storage(client, net_agent, read=["Общее"], write=["свой отдел"])
    sections = _mcp(client, net_agent, "files_list")["structuredContent"]["items"]
    assert [(i["name"], i["can_write"]) for i in sections] == [("Общее", False), ("ОПО", True)]
    text = _mcp(client, net_agent, "files_read", {"path": "Общее/Регламент.md"})["structuredContent"]
    assert "Проверять теги" in text["text"] and text["download"].startswith("/agents/api/files/raw?path=")
    assert _mcp(client, net_agent, "files_write", {"path": "Общее/x.txt", "text": "нет"})["isError"]  # read only
    wrote = _mcp(client, net_agent, "files_write", {"path": "ОПО/24-017/теги.csv", "text": "tag;addr", "note": "из ТО"})
    assert not wrote["isError"] and wrote["structuredContent"]["version"] == 1
    binary = _mcp(client, net_agent, "files_write", {"path": "ОПО/24-017/logo.png",
                                                      "base64": base64.b64encode(b"\x89PNG..").decode()})
    assert not binary["isError"]
    found = _mcp(client, net_agent, "files_find", {"query": "теги"})["structuredContent"]["items"]
    assert [i["path"] for i in found] == ["ОПО/24-017/теги.csv"]
    # the synchronizer: big files through the API, with the same rights
    body = b"\x00" * 1000
    put = client.put("/agents/api/files/raw", params={"path": "ОПО/24-017/проект.project", "note": "сборка"},
                     headers={**net_agent["headers"], "Content-Type": "application/octet-stream"}, content=body)
    assert put.status_code == 200 and put.json()["version"] == 1 and put.json()["sha256"] == hashlib.sha256(body).hexdigest()
    again = client.put("/agents/api/files/raw", params={"path": "ОПО/24-017/проект.project"},
                       headers=net_agent["headers"], content=body)
    assert again.json()["changed"] is False
    got = client.get("/agents/api/files/raw", params={"path": "ОПО/24-017/проект.project"}, headers=net_agent["headers"])
    assert got.content == body and got.headers["x-version"] == "1"
    denied = client.put("/agents/api/files/raw", params={"path": "Общее/y.bin"}, headers=net_agent["headers"], content=b"1")
    assert denied.status_code == 400
    listing = client.get("/agents/api/files", params={"path": "ОПО/24-017"}, headers=net_agent["headers"]).json()
    assert {i["name"] for i in listing["items"]} == {"теги.csv", "logo.png", "проект.project"} and listing["can_write"]
    # who did what is in the card of the file
    card = files.info(files.for_person(BOSS), "ОПО/24-017/теги.csv")
    assert card["by_agent"] and card["updated_by"] == "opo-sysai" and card["versions"][0]["note"] == "из ТО"


def test_the_agent_of_an_employee_never_opens_more_than_the_employee(client, net_agent):
    with SessionLocal() as s:
        s.add(Department(name="ПТО"))
        s.commit()
    _open_storage(client, net_agent, read=["*"], write=["*", "ПТО", "Общее", "свой отдел"])
    assert access.rules_for(net_agent["id"])["files"]["write"] == ["*", "ПТО", "Общее", "свой отдел"]  # the boss: as is
    with SessionLocal() as s:
        s.get(User, net_agent["user_id"]).role = "user"
        s.commit()
    rules = access.rules_for(net_agent["id"])
    assert rules["files"] == {"read": [], "write": ["Общее", "свой отдел"]}
    hermes = access.document(net_agent["id"], "https://sysai.example")["doc"]["harness_config"]
    assert hermes["sysai"]["files"] == rules["files"]
