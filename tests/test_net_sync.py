"""The synchronizer on the employee's computer (agents/sync, docs/opo/TASKS.md ОПО-6.3) against the real server, and
what the computer downloads from SYSAI: the installer and the wheel (ОПО-6.2)."""
import base64
import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "sync"))

from sysai_agent import core, hermes, hubsync, mcpwrap, rules as R, signing  # noqa: E402

from app import settings_store  # noqa: E402
from app.db import Department, SessionLocal, User  # noqa: E402
from app.net import access, agents, hub, installer  # noqa: E402
from app.net.models import NetAgent, NetAgentAccess, NetSkill, NetSkillVersion  # noqa: E402

SKILL = """---
name: opo-intake
description: Как принять пакет от ТО: комплектность, что спросить, что записать в проект.
version: 1.0.0
---
# Приём пакета ТО

## Когда применять
Пришёл пакет от технолога.

## Как делать
Сверить состав с перечнем.

## Ошибки
Нет листа сигналов.

## Проверка
Все листы на месте.
"""

TOOL = """---
name: doc-kit
kind: mcp
description: Читает документы ТО и пишет отчёты в папку проекта.
version: 1.0.0
run: python server.py
platform: any
tools: read_doc, write_report, delete_all
---
# Документы ТО

## Когда применять
Нужно прочитать документ ТО.
"""

# a tiny MCP server: answers tools/list with three tools and echoes the arguments of tools/call
SERVER = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "tools/list":
        out = {"tools": [{"name": n} for n in ("read_doc", "write_report", "delete_all")]}
    else:
        out = {"content": [{"type": "text", "text": json.dumps(msg.get("params"), ensure_ascii=False)}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": out}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
'''


def _transport(tc):
    def send(method, url, headers, body, timeout):
        path = url.split("://", 1)[-1]
        path = path[path.index("/"):] if "/" in path else "/"
        r = tc.request(method, path, headers=headers, content=body)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content
    return send


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for path, text in files.items():
            z.writestr(path, text)
    return out.getvalue()


def _publish(client, files: dict, name: str):
    r = client.post("/hub/upload", files={"file": (f"{name}.zip", _zip(files), "application/zip")}, follow_redirects=False)
    assert r.status_code == 303, r.text
    client.post(f"/hub/{name}/1.0.0/publish", data={"note": "ок"})


@pytest.fixture
def pc(client, net_on, tmp_path, monkeypatch):
    """A computer connected by the code: {"home", "profile", "agent_id", "send", "token"}."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("SYSAI_KEYRING", "0")
    monkeypatch.delenv("SYSAI_AGENT_TOKEN", raising=False)
    monkeypatch.setattr(hermes, "version", lambda: "0.9.1")
    monkeypatch.setitem(core._hermes_version, "at", 0.0)
    with SessionLocal() as s:
        admin = s.query(User).filter(User.role == "superuser").first()
        dept = s.query(Department).filter(Department.name == "ОПО").first() or Department(name="ОПО")
        s.add(dept)
        s.commit()
        admin_id, dept_id = admin.id, dept.id
    agent = agents.create(admin_id, "opo-pc", dept_id, None, "hermes", "", by="test")
    code = agents.issue_code(agent.id)
    home, profile = tmp_path / "SYSAI", tmp_path / "hermes" / "profiles" / "opo-pc"
    send = _transport(TestClient(app))
    info = core.connect("http://testserver", code, home, transport=send, hermes_home=str(profile))
    assert info["agent"] == "opo-pc" and info["token_store"] == "file"
    return {"home": home, "profile": profile, "agent_id": agent.id, "dept_id": dept_id, "send": send,
            "token": (home / "token").read_text(encoding="utf-8")}


def test_connect_pins_the_key_and_keeps_the_token_private(pc):
    cfg = json.loads((pc["home"] / "agent.json").read_text(encoding="utf-8"))
    assert cfg["public_key"] == access.public_key()["key"] and cfg["key_id"] == access.public_key()["key_id"]
    assert cfg["server"] == "http://testserver" and cfg["hermes_home"] == str(pc["profile"])
    assert "token" not in cfg and pc["token"].startswith(agents.TOKEN_PREFIX)
    if os.name != "nt":
        assert stat.S_IMODE((pc["home"] / "token").stat().st_mode) == 0o600
    with pytest.raises(core.SyncError, match="Код не подходит"):  # the code works once
        core.connect("http://testserver", "AAAA-BBBB", pc["home"], transport=pc["send"])


def test_sync_applies_only_its_keys_and_keeps_the_persons(pc):
    profile = pc["profile"]
    profile.mkdir(parents=True)
    own = {"display": {"skin": "mono"}, "model": {"default": "openai/gpt-4o", "provider": "openrouter"},
           "mcp_servers": {"mine": {"command": "my-mcp"}}, "skills": {"external_dirs": ["C:/my-skills"], "own": 1},
           "auxiliary": {"web_extract": {"model": "x"}}, "sysai": {"stale": True}}
    (profile / "config.yaml").write_text(yaml.safe_dump(own, allow_unicode=True), encoding="utf-8")
    (profile / "SOUL.md").write_text("Мой характер", encoding="utf-8")
    (profile / ".env").write_text("OPENAI_API_KEY=mine\nSYSAI_AGENT_TOKEN=old\n", encoding="utf-8")
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["errors"] == [] and set(report["changed"]) == {"config.yaml", ".env", "SOUL.md"}
    cfg = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    home = str(pc["home"])
    assert cfg["display"] == {"skin": "mono"} and cfg["skills"]["own"] == 1  # the person's own stays
    assert cfg["auxiliary"]["web_extract"] == {"model": "x"} and set(cfg["auxiliary"]) == {"web_extract", "vision", "compression"}
    assert cfg["model"]["base_url"] == "http://testserver/v1" and cfg["model"]["default"].startswith("sysai/")
    assert cfg["model"]["api_key"] == "${SYSAI_AGENT_TOKEN}" and "provider" in cfg["model"]
    assert "mine" not in cfg["mcp_servers"] and cfg["fallback_providers"] == []
    assert cfg["skills"]["external_dirs"] == [home + "/hub"] and "sysai" not in cfg
    assert cfg["memory"]["memory_enabled"] is True and cfg["security"]["redact_secrets"] is True
    assert yaml.safe_load((profile / hermes.CONFIG_BACKUP).read_text(encoding="utf-8")) == own
    env = (profile / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=mine" in env and f"SYSAI_AGENT_TOKEN={pc['token']}" in env and "=old" not in env
    assert env.count("SYSAI_AGENT_TOKEN=") == 1 and f"SYSAI_URL=http://testserver" in env
    soul = (profile / "SOUL.md").read_text(encoding="utf-8")
    assert soul.startswith("# opo-pc") and "Ты — opo-pc, агент сети SYSAI" in soul and "## Отдел ОПО" in soul
    assert (profile / hermes.SOUL_BACKUP).read_text(encoding="utf-8") == "Мой характер"
    # what the MCP servers on the computer check: the same signed document
    signed = json.loads((pc["home"] / "access.json").read_text(encoding="utf-8"))
    assert signing.verify(signed, access.public_key()["key"], access.public_key()["key_id"])
    sysai = json.loads((pc["home"] / "sysai.json").read_text(encoding="utf-8"))
    assert sysai["agent"] == "opo-pc" and "folders" in sysai
    # the pulse told SYSAI what is applied, with the versions
    rev = access.document(pc["agent_id"])["rev"]
    with SessionLocal() as s:
        row = s.query(NetAgentAccess).filter(NetAgentAccess.agent_id == pc["agent_id"]).one()
        agent = s.get(NetAgent, pc["agent_id"])
        assert row.applied_rev == rev and agent.versions["sysai-agent"] == "0.1.0" and agent.versions["hermes"] == "0.9.1"
        assert agent.last_error == "" and agent.last_seen_at is not None
    again = core.sync_once(pc["home"], transport=pc["send"])
    assert again["changed"] == [] and (profile / hermes.SOUL_BACKUP).read_text(encoding="utf-8") == "Мой характер"
    status = core.status(pc["home"])
    assert status["connected"] and status["access_rev"] == rev and status["hermes"]["soul"] is True


def test_nothing_is_applied_when_the_signature_does_not_match(pc):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    cfg_path = pc["home"] / "agent.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["public_key"], cfg["key_id"] = base64.b64encode(other).decode(), ""
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(core.SyncError, match="Подпись настроек не сходится"):
        core.sync_once(pc["home"], transport=pc["send"])
    assert not (pc["profile"] / "config.yaml").exists() and not (pc["home"] / "access.json").exists()


def test_a_refused_token_says_what_to_do(pc):
    agents.revoke(pc["agent_id"], by="test")
    with pytest.raises(core.SyncError, match="не принял токен агента"):
        core.sync_once(pc["home"], transport=pc["send"])


def test_hub_skills_come_read_only_and_leave_with_the_hub(client, pc):
    _publish(client, {"SKILL.md": SKILL, "references/list.md": "Перечень"}, "opo-intake")
    (pc["profile"] / "skills" / "opo" / "opo-intake").mkdir(parents=True)
    (pc["profile"] / "skills" / "opo" / "opo-intake" / "SKILL.md").write_text("свой", encoding="utf-8")
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["hub"]["changed"] == ["opo-intake 1.0.0"] and report["errors"] == []
    folder = pc["home"] / "hub" / "opo-intake"
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == SKILL and (folder / "references" / "list.md").exists()
    if os.name != "nt":
        assert not os.access(folder / "SKILL.md", os.W_OK)
    with SessionLocal() as s:
        state = s.get(NetAgent, pc["agent_id"]).hub_state
        assert state["skills"] == {"opo-intake": "1.0.0"} and state["conflicts"] == ["opo-intake"]
    assert core.sync_once(pc["home"], transport=pc["send"])["hub"]["changed"] == []  # same sha256: not downloaded again
    client.post("/hub/opo-intake/retire")
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["hub"]["removed"] == ["opo-intake"] and not folder.exists()


def test_a_broken_download_is_never_installed(client, pc, monkeypatch):
    _publish(client, {"SKILL.md": SKILL}, "opo-intake")
    real = hubsync._download

    def broken(c, item):
        return real(c, {**item, "sha256": "0" * 64})
    monkeypatch.setattr(hubsync, "_download", broken)
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert "sha256 не сходится" in report["errors"][0] and not (pc["home"] / "hub" / "opo-intake").exists()
    with SessionLocal() as s:
        assert "sha256" in s.get(NetAgent, pc["agent_id"]).last_error
    with pytest.raises(hubsync.HubError, match="недопустимый путь"):
        hubsync.unpack(_zip({"../evil.txt": "x"}), pc["home"] / "x")


def test_skills_the_agent_wrote_are_proposed_once_when_allowed(client, pc):
    folder = pc["home"] / "proposals" / "opo" / "opo-intake"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.pyc").write_bytes(b"junk")
    access.set_agent_access(pc["agent_id"], overrides={"skills": {"propose": False}})
    assert core.sync_once(pc["home"], transport=pc["send"])["proposals"] == []  # not allowed by the rules
    access.set_agent_access(pc["agent_id"], overrides={"skills": {"propose": True}})
    assert core.sync_once(pc["home"], transport=pc["send"])["proposals"] == ["opo-intake 1.0.0"]
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == "opo-intake").one()
        version = s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == skill.id).one()
        assert version.status == "review" and set(hub.unpack(version.blob)) == {"SKILL.md"}
    assert core.sync_once(pc["home"], transport=pc["send"])["proposals"] == []  # unchanged: not sent again
    state = json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))
    assert state["proposals"]["opo/opo-intake"]["status"] == "review"


def _rpc(id_, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, ensure_ascii=False)


def test_the_mcp_guard_checks_the_signed_rules_itself(client, pc):
    _publish(client, {"TOOL.md": TOOL, "server.py": SERVER}, "doc-kit")
    access.set_agent_access(pc["agent_id"], overrides={
        "mcp": {"sysai": ["*"], "doc-kit": ["read_doc", "write_report"]},
        "folders": {"read": ["D:\\ОПО\\ТО"], "write": ["D:\\ОПО\\Проекты"], "deny": ["D:\\ОПО\\Проекты\\Архив"]},
        "internet": {"mode": "list", "sites": ["se.com"]}, "commands": {"run": False}})
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["hub"]["changed"] == ["doc-kit 1.0.0"], report
    cfg = yaml.safe_load((pc["profile"] / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["mcp_servers"]["doc-kit"]["args"] == ["mcp", "doc-kit"]
    assert cfg["mcp_servers"]["doc-kit"]["env"]["SYSAI_TOOL_DIR"] == str(pc["home"]) + "/tools/doc-kit"
    calls = [
        _rpc(1, "tools/list"),
        _rpc(2, "tools/call", {"name": "read_doc", "arguments": {"path": "D:\\ОПО\\ТО\\24-017\\ТЗ.pdf"}}),
        _rpc(3, "tools/call", {"name": "delete_all", "arguments": {}}),
        _rpc(4, "tools/call", {"name": "read_doc", "arguments": {"path": "C:\\Windows\\win.ini"}}),
        _rpc(5, "tools/call", {"name": "read_doc", "arguments": {"path": "D:\\ОПО\\ТО\\..\\..\\secret.txt"}}),
        _rpc(6, "tools/call", {"name": "write_report", "arguments": {"out_dir": "D:\\ОПО\\ТО\\24-017"}}),
        _rpc(7, "tools/call", {"name": "write_report", "arguments": {"out_dir": "D:\\ОПО\\Проекты\\24-017", "project": "24-017"}}),
        _rpc(8, "tools/call", {"name": "write_report", "arguments": {"out_dir": "D:\\ОПО\\Проекты\\Архив\\old"}}),
        _rpc(9, "tools/call", {"name": "read_doc", "arguments": {"url": "https://evil.example/x"}}),
        _rpc(10, "tools/call", {"name": "read_doc", "arguments": {"url": "https://www.se.com/ru/doc"}}),
        _rpc(11, "tools/call", {"name": "read_doc", "arguments": {"command": "del /s C:\\"}}),
    ]
    out = io.BytesIO()
    code = mcpwrap.serve("doc-kit", pc["home"], stdin=io.BytesIO(("\n".join(calls) + "\n").encode("utf-8")), stdout=out)
    assert code == 0
    answers = {m["id"]: m for m in (json.loads(line) for line in out.getvalue().decode("utf-8").splitlines() if line.strip())}
    assert [t["name"] for t in answers[1]["result"]["tools"]] == ["read_doc", "write_report"]
    ok = {i for i, m in answers.items() if "result" in m}
    assert ok == {1, 2, 7, 10}, answers
    assert "delete_all" in answers[3]["error"]["message"] and "не разрешён" in answers[3]["error"]["message"]
    assert "C:\\Windows" in answers[4]["error"]["message"] and "secret" in answers[5]["error"]["message"]
    assert "на запись" in answers[6]["error"]["message"] and "Архив" in answers[8]["error"]["message"]
    assert "evil.example" in answers[9]["error"]["message"] and "команды агенту запрещены" in answers[11]["error"]["message"]
    # the server is not allowed any more: the guard does not start it at all
    access.set_agent_access(pc["agent_id"], overrides={"mcp": {"sysai": ["*"]}})
    core.sync_once(pc["home"], transport=pc["send"])
    assert mcpwrap.serve("doc-kit", pc["home"], stdin=io.BytesIO(b""), stdout=io.BytesIO()) == 2


def test_the_rules_on_the_computer_are_the_servers(pc):
    rules = access.normalize({"commands": {"run": True, "deny": ["rm -rf *", "format *"]},
                              "folders": {"read": ["D:\\ОПО"], "write": ["D:/ОПО/Проекты/"], "deny": ["D:\\ОПО\\Секрет"]}})
    for path, write in (("d:\\опо\\ТЗ.pdf", False), ("D:\\ОПО\\Проекты\\1", True), ("D:\\ОПО\\Секрет\\a", False),
                        ("D:\\ОПОx", False), ("D:\\ОПО\\ТЗ.pdf", True), ("", False)):
        assert R.path_allowed(rules, path, write) == access.path_allowed(rules, path, write), path
    assert not R.path_allowed(rules, "D:\\ОПО\\..\\Windows")  # «..» never leads out of a folder
    for command in ("rm -rf /", "RM   -RF  /tmp", "dir", "format c:"):
        assert R.command_denied(rules, command) == access.command_denied(rules, command), command
    assert R.tool_allowed({"mcp": {"x": ["*"]}}, "x", "any") and not R.tool_allowed({"mcp": {"x": ["a"]}}, "x", "b")


def test_the_loop_pulses_and_resyncs_when_the_rules_change(pc):
    core.sync_once(pc["home"], transport=pc["send"])
    first = json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))["access_rev"]
    naps = []

    def nap(_):
        if not naps:
            access.set_agent_access(pc["agent_id"], overrides={"models": {"budget_day_usd": 7.25}})
        naps.append(1)
    assert core.run(pc["home"], every=2, transport=pc["send"], cycles=2, sleep=nap) == 0
    state = json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))
    assert state["access_rev"] != first and state["access_rev"] == access.document(pc["agent_id"])["rev"]
    assert not (pc["home"] / "run.lock").exists()
    # asked to stop (the installer does so before updating): the loop ends at the next nap
    assert core.run(pc["home"], every=5, transport=pc["send"], sleep=lambda _: (pc["home"] / "stop").write_text("x")) == 0
    assert not (pc["home"] / "stop").exists() and not (pc["home"] / "run.lock").exists()


def test_the_installer_and_the_wheel(client, net_on):
    r = client.get("/agents/api/install.ps1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and "utf-8" in r.headers["content-type"]
    script = r.text
    name = installer.wheel_name()
    assert name == "sysai_agent-0.1.0-py3-none-any.whl" and f"http://testserver/agents/api/sync/{name}" in script
    assert "__SYSAI_" not in script and "hermes profile create $Agent --no-skills" in script
    assert "sysai-agent connect --server $Server --code $Code" in script and "CurrentVersion\\Run" in script
    w = client.get(f"/agents/api/sync/{name}")
    assert w.status_code == 200 and hashlib.sha256(w.content).hexdigest() == w.headers["x-sha256"]
    archive = zipfile.ZipFile(io.BytesIO(w.content))
    names = set(archive.namelist())
    info = "sysai_agent-0.1.0.dist-info"
    assert {"sysai_agent/cli.py", "sysai_agent/mcpwrap.py", f"{info}/METADATA", f"{info}/RECORD"} <= names
    assert not any("__pycache__" in n for n in names)
    eps = archive.read(f"{info}/entry_points.txt").decode()
    assert "sysai-agent = sysai_agent.cli:main" in eps and "sysai-agentw = sysai_agent.cli:main_windowless" in eps
    meta = archive.read(f"{info}/METADATA").decode()
    assert "Requires-Dist: PyYAML>=6" in meta and 'keyring>=24; sys_platform == "win32"' in meta
    for line in archive.read(f"{info}/RECORD").decode().splitlines():
        path, digest, size = line.split(",")
        if digest:
            data = archive.read(path)
            want = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert digest == want and int(size) == len(data), path
    assert client.get("/agents/api/sync/sysai_agent-0.0.1-py3-none-any.whl").status_code == 404
    settings_store.set_many({"agents_on": False})
    assert client.get("/agents/api/install.ps1").status_code == 404 and client.get(f"/agents/api/sync/{name}").status_code == 404


def test_the_agent_page_gives_the_one_line_installer(client, net_agent):
    page = client.post(f"/agents/{net_agent['id']}/connect").text
    assert "irm http://testserver/agents/api/install.ps1 | iex" in page and "$env:SYSAI_CODE=" in page


def test_an_old_synchronizer_is_seen_on_the_agent_page(client, net_agent):
    with SessionLocal() as s:
        s.get(NetAgent, net_agent["id"]).versions = {"hermes": "0.12.0", "sync": "0.0.9"}
        s.commit()
    page = client.get(f"/agents/{net_agent['id']}").text
    assert f"на сервере синхронизатор {installer.version()}" in page
    with SessionLocal() as s:
        s.get(NetAgent, net_agent["id"]).versions = {"hermes": "0.12.0", "sync": installer.version()}
        s.commit()
    assert "на сервере синхронизатор" not in client.get(f"/agents/{net_agent['id']}").text
