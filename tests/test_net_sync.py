"""The synchronizer on the employee's computer (agents/sync, docs/opo/TASKS.md ОПО-6.3) against the real server, and
what the computer downloads from SYSAI: the installer and the wheel (ОПО-6.2)."""
import base64
import hashlib
import io
import json
import os
import subprocess
import stat
import sys
import zipfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "sync"))

from sysai_agent import __version__ as SYNC_VERSION, cli, core, desktop, hermes, hook, hubsync, mcpwrap, paths, rules as R, signing, update  # noqa: E402,E501

mcpwrap.WAIT_STEP = 0  # the tests do not wait for a synchronizer in mid-work

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
name: report-kit
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


LAUNCHER = "C:\\Users\\a\\.local\\bin\\sysai-agent.exe"


def test_sync_applies_only_its_keys_and_keeps_the_persons(pc, monkeypatch):
    monkeypatch.setattr(core, "launcher", lambda: LAUNCHER)
    profile = pc["profile"]
    profile.mkdir(parents=True)
    own = {"display": {"skin": "mono"}, "model": {"default": "openai/gpt-4o", "provider": "openrouter"},
           "mcp_servers": {"mine": {"command": "my-mcp"}}, "skills": {"external_dirs": ["C:/my-skills"], "own": 1},
           "auxiliary": {"web_extract": {"model": "x"}}, "sysai": {"stale": True},
           "compression": {"threshold_tokens": 900000, "tail_mode": "lean"}, "tool_output": {"max_bytes": 50000, "max_lines": 500},
           "tools": {"tool_search": {"enabled": "off", "threshold_pct": 5}, "mine": 1},
           "hooks": {"post_tool_call": [{"command": "my-hook"}], "pre_tool_call": [{"command": "rm-guard"}]}}
    (profile / "config.yaml").write_text(yaml.safe_dump(own, allow_unicode=True), encoding="utf-8")
    (profile / "SOUL.md").write_text("Мой характер", encoding="utf-8")
    (profile / ".env").write_text("OPENAI_API_KEY=mine\nSYSAI_AGENT_TOKEN=old\n", encoding="utf-8")
    mine = {"event": "post_tool_call", "command": "my-hook", "approved_at": "2026-01-01T00:00:00+00:00"}
    stale = {"event": "pre_tool_call", "command": '"D:\\old\\sysai-agent.exe" hook', "approved_by": "SYSAI"}
    (profile / hermes.ALLOWLIST).write_text(json.dumps({"approvals": [mine, stale]}), encoding="utf-8")
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["errors"] == [] and set(report["changed"]) == {"config.yaml", ".env", "SOUL.md", hermes.ALLOWLIST}
    cfg = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    home = str(pc["home"])
    assert cfg["display"] == {"skin": "mono"} and cfg["skills"]["own"] == 1  # the person's own stays
    assert cfg["auxiliary"]["web_extract"] == {"model": "x"} and set(cfg["auxiliary"]) == {"web_extract", "vision", "compression"}
    assert cfg["model"]["base_url"] == "http://testserver/v1" and cfg["model"]["default"].startswith("sysai/")
    # when a conversation is squeezed and how big a result may be is SYSAI's; the person's other keys there stay
    assert cfg["compression"] == {"threshold_tokens": access.COMPRESS_AT, "tail_mode": "lean", "enabled": True}
    assert cfg["tool_output"] == {"max_bytes": access.RESULT_CHARS, "max_lines": 500}
    assert cfg["tools"] == {"tool_search": {"enabled": "on"}, "mine": 1}   # the part is SYSAI's, the rest stays
    assert cfg["tool_budget"] == {"mcp_result_size_chars": access.RESULT_CHARS}
    assert cfg["auxiliary"]["compression"]["api_key"] == "${SYSAI_AGENT_TOKEN}"
    assert cfg["model"]["api_key"] == "${SYSAI_AGENT_TOKEN}" and "provider" in cfg["model"]
    assert "mine" not in cfg["mcp_servers"] and cfg["fallback_providers"] == []
    assert cfg["skills"]["external_dirs"] == [home + "/hub"] and "sysai" not in cfg
    assert cfg["memory"]["memory_enabled"] is True and cfg["security"]["redact_secrets"] is True
    # every action of Hermes's own tools is asked of the synchronizer, by its full path; the consent is given for it
    guard = f'"{LAUNCHER}" hook'
    [entry] = cfg["hooks"]["pre_tool_call"]
    assert entry["command"] == guard and entry["fail_closed"] is True and entry["timeout"] == access.HOOK_TIMEOUT
    assert {"terminal", "execute_code", "write_file", "patch"} <= set(entry["matcher"].split("|"))
    assert cfg["hooks"]["post_tool_call"] == [{"command": "my-hook"}]  # the person's other hooks stay
    consent = json.loads((profile / hermes.ALLOWLIST).read_text(encoding="utf-8"))["approvals"]
    assert consent[0] == mine and [e["command"] for e in consent] == ["my-hook", guard]  # the stale one of SYSAI went
    assert consent[1]["event"] == "pre_tool_call" and consent[1]["approved_by"] == "SYSAI"
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
        assert row.applied_rev == rev and agent.versions["sysai-agent"] == SYNC_VERSION and agent.versions["hermes"] == "0.9.1"
        assert agent.last_error == "" and agent.last_seen_at is not None
    again = core.sync_once(pc["home"], transport=pc["send"])
    assert again["changed"] == [] and (profile / hermes.SOUL_BACKUP).read_text(encoding="utf-8") == "Мой характер"
    status = core.status(pc["home"])
    assert status["connected"] and status["access_rev"] == rev and status["hermes"]["soul"] is True
    assert status["hermes"]["hook"] is True


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
    # not taken whole: the revision of the hub is not kept, so the next pulse sees it new and tries again
    assert "hub_rev" not in json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(hubsync, "_download", real)
    assert core.sync_once(pc["home"], transport=pc["send"])["hub"]["changed"] == ["opo-intake 1.0.0"]
    assert json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))["hub_rev"]
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
    _publish(client, {"TOOL.md": TOOL, "server.py": SERVER}, "report-kit")
    access.set_agent_access(pc["agent_id"], overrides={
        "mcp": {"sysai": ["*"], "report-kit": ["read_doc", "write_report"]},
        "folders": {"read": ["D:\\ОПО\\ТО"], "write": ["D:\\ОПО\\Проекты"], "deny": ["D:\\ОПО\\Проекты\\Архив"]},
        "internet": {"mode": "list", "sites": ["se.com"]}, "commands": {"run": False}})
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["hub"]["changed"] == ["report-kit 1.0.0"], report
    cfg = yaml.safe_load((pc["profile"] / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["mcp_servers"]["report-kit"]["args"] == ["mcp", "report-kit"]
    assert cfg["mcp_servers"]["report-kit"]["env"]["SYSAI_TOOL_DIR"] == str(pc["home"]) + "/tools/report-kit"
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
        _rpc(12, "tools/call", {"name": "write_report", "arguments": {"edits": [{"states": [{"image_file": "D:\\ОПО\\Проекты\\ok.png"}]}]}}),
        _rpc(13, "tools/call", {"name": "write_report", "arguments": {"edits": [{"states": [{"image_file": "C:\\Windows\\secret.png"}]}]}}),
    ]
    out = io.BytesIO()
    code = mcpwrap.serve("report-kit", pc["home"], stdin=io.BytesIO(("\n".join(calls) + "\n").encode("utf-8")), stdout=out)
    assert code == 0
    answers = {m["id"]: m for m in (json.loads(line) for line in out.getvalue().decode("utf-8").splitlines() if line.strip())}
    assert [t["name"] for t in answers[1]["result"]["tools"]] == ["read_doc", "write_report"]
    ok = {i for i, m in answers.items() if "result" in m}
    assert ok == {1, 2, 7, 10, 12}, answers
    assert "delete_all" in answers[3]["error"]["message"] and "не разрешён" in answers[3]["error"]["message"]
    assert "C:\\Windows" in answers[4]["error"]["message"] and "secret" in answers[5]["error"]["message"]
    assert "на запись" in answers[6]["error"]["message"] and "Архив" in answers[8]["error"]["message"]
    assert "evil.example" in answers[9]["error"]["message"] and "команды агенту запрещены" in answers[11]["error"]["message"]
    assert "C:\\Windows" in answers[13]["error"]["message"]
    # the server is not allowed any more: the guard does not start it at all
    access.set_agent_access(pc["agent_id"], overrides={"mcp": {"sysai": ["*"]}})
    core.sync_once(pc["home"], transport=pc["send"])
    assert mcpwrap.serve("report-kit", pc["home"], stdin=io.BytesIO(b""), stdout=io.BytesIO()) == 2


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
    whole = access.normalize({"folders": {"read": ["*"], "write": ["D:\\ОПО"], "deny": ["C:\\Windows"]}})
    for path, write, allowed in (("C:\\Users\\a\\x.txt", False, True), ("C:\\Users\\a\\x.txt", True, False),
                                 ("C:\\Windows\\win.ini", False, False), ("D:\\ОПО\\1", True, True), ("", False, False)):
        assert R.path_allowed(whole, path, write) == access.path_allowed(whole, path, write) == allowed, path


def test_a_restart_waits_only_for_a_running_gateway_and_the_pulse_is_kept(pc, monkeypatch):
    restarts, running = [], [False]
    monkeypatch.setattr(hermes, "restart", lambda *a: restarts.append(a) or (True, "Hermes перезапущен"))
    monkeypatch.setattr(hermes, "gateway_running", lambda profile_dir: running[0])
    report = core.sync_once(pc["home"], transport=pc["send"], restart=False)
    assert report["changed"] and restarts == [] and "не запущен" in report["restart"]
    state = json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))
    assert "restart_pending" not in state and state["pulse_at"]  # Hermes reads the settings when it starts
    assert report["access"] == state["access_rev"] and core.status(pc["home"])["pulse_at"] == state["pulse_at"]
    running[0] = True
    access.set_agent_access(pc["agent_id"], overrides={"commands": {"run": False}})
    report = core.sync_once(pc["home"], transport=pc["send"], restart=False)  # «--no-restart»: it waits
    assert report["changed"] == ["config.yaml"] and restarts == []
    assert json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))["restart_pending"] is True
    report = core.sync_once(pc["home"], transport=pc["send"])
    assert report["changed"] == [] and len(restarts) == 1 and report["restart"] == "Hermes перезапущен"
    assert "restart_pending" not in json.loads((pc["home"] / "state.json").read_text(encoding="utf-8"))
    # only the rules for the MCP servers changed (not the files of the profile): the log still tells it
    assert "доступы: abc" in core.describe({"changed": [], "hub": {}, "access": "abc"})


def test_the_hermes_root_and_its_gateway_are_found_as_hermes_finds_them(tmp_path, monkeypatch):
    from sysai_agent import paths
    native = tmp_path / ".hermes"
    monkeypatch.setattr(paths, "_native_hermes_home", lambda: native)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert paths.hermes_root() == native
    monkeypatch.setenv("HERMES_HOME", str(native / "profiles" / "opo-pc"))
    assert paths.hermes_root() == native  # a profile in the usual place: the usual root
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "opt" / "data"))
    assert paths.hermes_root() == tmp_path / "opt" / "data"  # Docker, a root of its own
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "opt" / "data" / "profiles" / "x"))
    assert paths.hermes_root() == tmp_path / "opt" / "data"
    profile = native / "profiles" / "opo-pc"
    profile.mkdir(parents=True)
    alive = {4242}
    monkeypatch.setattr(hermes, "_alive", lambda pid: pid in alive)
    assert not hermes.gateway_running(profile)
    (profile / "gateway.pid").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    assert hermes.gateway_running(profile)  # a gateway of the profile itself
    (profile / "gateway.pid").unlink()
    # Hermes 0.22: one gateway of the computer serves the profiles; its pid and its state are in the root
    (native / "gateway.pid").write_text("4242", encoding="utf-8")
    state = {"pid": 4242, "gateway_state": "running", "served_profiles": ["default"]}
    (native / "gateway_state.json").write_text(json.dumps(state), encoding="utf-8")
    assert not hermes.gateway_running(profile)  # this profile is not served
    state["served_profiles"].append("opo-pc")
    (native / "gateway_state.json").write_text(json.dumps(state), encoding="utf-8")
    assert hermes.gateway_running(profile)
    alive.clear()
    assert not hermes.gateway_running(profile)  # its process is gone
    alive.add(4242)
    (native / "gateway_state.json").write_text(json.dumps({**state, "gateway_state": "stopped"}), encoding="utf-8")
    assert not hermes.gateway_running(profile)


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
    assert installer.version() == SYNC_VERSION and name == f"sysai_agent-{SYNC_VERSION}-py3-none-any.whl"
    assert f"http://testserver/agents/api/sync/{name}" in script
    assert "__SYSAI_" not in script and "hermes profile create $Agent --no-skills" in script
    assert "sysai-agent connect --server $Server --code $Code" in script and "CurrentVersion\\Run" in script
    w = client.get(f"/agents/api/sync/{name}")
    assert w.status_code == 200 and hashlib.sha256(w.content).hexdigest() == w.headers["x-sha256"]
    archive = zipfile.ZipFile(io.BytesIO(w.content))
    names = set(archive.namelist())
    info = f"sysai_agent-{SYNC_VERSION}.dist-info"
    assert {"sysai_agent/cli.py", "sysai_agent/mcpwrap.py", "sysai_agent/hook.py", "sysai_agent/update.py",
            f"{info}/METADATA", f"{info}/RECORD"} <= names
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


def _ask(home, tool, args, cwd=""):
    """One question of Hermes to the hook, as Hermes asks it (agent/shell_hooks.py): the refusal or ""."""
    payload = {"hook_event_name": "pre_tool_call", "tool_name": tool, "tool_input": args, "session_id": "s1",
               "cwd": cwd, "extra": {}}
    out = io.BytesIO()
    assert hook.main(home, stdin=io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), stdout=out) == 0
    text = out.getvalue().decode("utf-8")
    if not text:
        return ""
    answer = json.loads(text)
    assert answer["action"] == "block" and answer["message"].startswith("SYSAI: ")
    return answer["message"]


def _matcher(profile):
    cfg = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    return set(cfg["hooks"]["pre_tool_call"][0]["matcher"].split("|"))


def test_the_hook_checks_every_action_of_hermes_by_the_signed_rules(pc, tmp_path, monkeypatch):
    home, profile, opo = pc["home"], pc["profile"], tmp_path / "ОПО"
    projects = opo / "Проекты"
    access.set_agent_access(pc["agent_id"], overrides={
        "folders": {"read": [str(opo)], "write": [str(projects)], "deny": [str(opo / "Секрет")]},
        "internet": {"mode": "list", "sites": ["se.com"]}, "commands": {"run": True, "deny": ["format *"]}})
    core.sync_once(home, transport=pc["send"])
    assert {"read_file", "search_files", "web_search", "web_extract", "browser_.*", "vision_analyze"} <= _matcher(profile)
    assert "process_manage" not in _matcher(profile)  # commands are open: processes and the screen are not asked about
    assert _ask(home, "read_file", {"path": str(opo / "ТЗ.pdf")}) == ""
    assert _ask(home, "read_file", {"path": "ТЗ.pdf"}, cwd=str(opo)) == ""  # relative: from the folder Hermes works in
    assert "/etc/passwd" in _ask(home, "read_file", {"path": "/etc/passwd"})
    assert "не разрешена" in _ask(home, "read_file", {"path": "../Секрет/a.txt"}, cwd=str(projects))  # «..» leads nowhere
    assert "не разрешена" in _ask(home, "search_files", {"pattern": "x"}, cwd="/")  # no path: where Hermes stands
    assert "запись в" in _ask(home, "write_file", {"path": str(opo / "ТЗ.pdf"), "content": "x"})
    assert _ask(home, "patch", {"path": str(projects / "24-017" / "отчёт.md")}) == ""
    for place in (home / "access.json", home / "hub" / "x" / "SKILL.md", profile / "config.yaml", profile / "SOUL.md"):
        assert "меняет только SYSAI" in _ask(home, "write_file", {"path": str(place)}), place
    assert _ask(home, "terminal", {"command": "dir", "workdir": str(opo)}) == ""
    assert "«format *»" in _ask(home, "terminal", {"command": "FORMAT   c:"})
    assert "«format *»" in _ask(home, "terminal", {"items": [{"command": "dir"}, {"command": "format d:"}]})
    for command in (f"cat {home}/access.json", "sysai-agent stop", "taskkill /im sysai-agentw.exe",
                    "hermes -p opo-pc config set model x", "hermes hooks revoke"):
        assert "меняет только SYSAI" in _ask(home, "terminal", {"command": command}), command
    assert _ask(home, "terminal", {"command": "sysai-agent status"}) == "" and _ask(home, "terminal", {"command": "hermes --version"}) == ""
    assert "меняет только SYSAI" in _ask(home, "execute_code", {"code": f"import os; os.remove(r'{profile / 'SOUL.md'}')"})
    assert "только сайты из списка" in _ask(home, "web_search", {"query": "Modicon M580"})
    assert "evil.example" in _ask(home, "web_extract", {"urls": ["https://www.se.com/ru", "https://evil.example/x"]})
    assert _ask(home, "web_extract", {"urls": ["https://se.com/x"]}) == ""
    assert "браузер" in _ask(home, "browser_navigate", {"url": "https://se.com"})
    assert "evil.example" in _ask(home, "vision_analyze", {"image_url": "https://evil.example/a.png"})
    assert _ask(home, "vision_analyze", {"image_url": str(opo / "схема.png")}) == ""
    assert _ask(home, "process_manage", {"action": "list"}) == ""
    log = (home / "logs" / "hook.log").read_text(encoding="utf-8")
    assert "запрещено read_file" in log and "/etc/passwd" in log
    # the whole computer, no commands: reading and the internet are not asked about, the screen and processes are
    access.set_agent_access(pc["agent_id"], overrides={
        "folders": {"read": ["*"], "write": ["*"], "deny": []}, "internet": {"mode": "all", "sites": []},
        "commands": {"run": False}})
    core.sync_once(home, transport=pc["send"])
    assert not {"read_file", "search_files", "web_search", "browser_.*"} & _matcher(profile)
    assert {"process_manage", "computer_use", "terminal", "write_file"} <= _matcher(profile)
    assert _ask(home, "write_file", {"path": str(tmp_path / "где угодно.txt")}) == ""
    assert _ask(home, "write_file", {"path": str(home / "proposals" / "opo" / "x" / "SKILL.md")}) == ""  # the agent's own
    assert "меняет только SYSAI" in _ask(home, "write_file", {"path": str(home / "state.json")})
    assert "не разрешены" in _ask(home, "terminal", {"command": "dir"})
    assert "Команд на ПК" in _ask(home, "computer_use", {"action": "screenshot"})
    assert _ask(home, "web_search", {"query": "x"}) == "" and _ask(home, "read_file", {"path": "/etc/hosts"}) == ""
    # what the hook trusts: the file the synchronizer verified, or the signature; anything else refuses everything
    raw = (home / "access.json").read_bytes()
    forged = json.loads(raw)
    forged["doc"]["rules"]["commands"] = {"run": True, "deny": []}
    (home / "access.json").write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
    assert "нет подписанных доступов" in _ask(home, "terminal", {"command": "dir"})
    assert "нет подписанных доступов" in _ask(home, "write_file", {"path": str(tmp_path / "x.txt")})
    (home / "access.json").write_bytes(raw)
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    state.pop("access_sha")
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert _ask(home, "write_file", {"path": str(tmp_path / "x.txt")}) == ""  # signed by the pinned key: trusted
    monkeypatch.setattr(hook, "check", lambda *a, **k: 1 / 0)
    assert "проверка SYSAI не удалась" in _ask(home, "write_file", {"path": str(tmp_path / "x.txt")})  # fail closed


def test_the_hook_takes_windows_paths_as_windows_does(monkeypatch):
    monkeypatch.setattr(paths, "WINDOWS", True)
    rules = access.normalize({"folders": {"read": ["*"], "write": ["D:\\ОПО\\Проекты"], "deny": ["C:\\Users\\a\\Documents\\Личное"]},
                              "commands": {"run": True, "deny": []}, "internet": {"mode": "off"}})
    home = Path("C:/Users/a/AppData/Local/SYSAI")
    profile = Path("C:/Users/a/AppData/Local/hermes/profiles/opo-pc")

    def ask(tool, args, cwd=""):
        return hook.check(rules, tool, args, cwd, home, profile)
    assert ask("read_file", {"path": "C:\\Windows\\win.ini"}) == "" and ask("read_file", {"path": "file:///C:/Users/a/x.txt"}) == ""
    assert "Личное" in ask("read_file", {"path": "Личное\\паспорт.pdf"}, cwd="C:\\Users\\a\\Documents")
    assert ask("write_file", {"path": "отчёт.docx"}, cwd="d:\\опо\\проекты\\24-017") == ""
    assert "запись в C:\\Users" in ask("write_file", {"path": "C:\\Users\\a\\x.txt"})
    assert "меняет только SYSAI" in ask("write_file", {"path": "c:/users/a/appdata/local/sysai/access.json"})
    for command in ("Remove-Item $env:LOCALAPPDATA\\SYSAI\\access.json", "del %LOCALAPPDATA%\\SYSAI\\state.json",
                    "notepad C:\\Users\\a\\AppData\\Local\\hermes\\profiles\\opo-pc\\config.yaml", "sysai-agent.exe stop",
                    "Stop-Process -Name sysai-agentw"):
        assert ask("terminal", {"command": command}) == hook.SETTINGS_NOTE, command
    assert ask("terminal", {"command": "Get-ChildItem C:\\Users\\a\\Documents"}) == ""
    assert "интернет агенту закрыт" in ask("web_search", {"query": "x"})


def test_hermes_asks_the_hook_through_the_command_line(pc, monkeypatch):
    core.sync_once(pc["home"], transport=pc["send"])
    access.set_agent_access(pc["agent_id"], overrides={"commands": {"run": False}})
    core.sync_once(pc["home"], transport=pc["send"])
    monkeypatch.setenv("SYSAI_HOME", str(pc["home"]))
    payload = {"hook_event_name": "pre_tool_call", "tool_name": "terminal", "tool_input": {"command": "dir"}}
    stdin = type("In", (), {"buffer": io.BytesIO(json.dumps(payload).encode("utf-8"))})()
    stdout = type("Out", (), {"buffer": io.BytesIO()})()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert cli.main(["hook"]) == 0
    assert json.loads(stdout.buffer.getvalue().decode("utf-8"))["action"] == "block"


def test_the_synchronizer_updates_itself_only_to_the_wheel_sysai_signed(pc, monkeypatch):
    home = pc["home"]
    monkeypatch.setattr(installer, "version", lambda: "9.9.9")  # SYSAI gives out a newer synchronizer
    monkeypatch.setattr(installer, "_cache", {})
    monkeypatch.setattr(paths, "WINDOWS", True)
    monkeypatch.setattr(hermes, "gateway_running", lambda profile_dir: False)
    monkeypatch.setattr(desktop, "due", lambda home, state, now=None: {})  # the window: tests/test_desktop.py
    started = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda args, **kw: started.append((args, kw)))
    assert update.newer("9.9.9") and update.newer("0.10.0", "0.9.9") and not update.newer(SYNC_VERSION) and not update.newer("")

    def run(transport=pc["send"]):
        naps = []
        assert core.run(home, every=1, transport=transport, cycles=2, sleep=naps.append) == 0
        return naps
    settings_store.set_many({"agents_auto_update": False})  # «обновлять сами» off: the computer only learns of it
    assert run() == [1] and started == []
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert state["offer"]["version"] == "9.9.9" and state["offer"]["auto_update"] is False
    settings_store.set_many({"agents_auto_update": True})

    def broken(method, url, headers, body, timeout):
        status, got, data = pc["send"](method, url, headers, body, timeout)
        return status, got, (data + b"x" if url.endswith(".whl") else data)
    assert run(broken) == [1] and started == []  # not the wheel SYSAI signed: never installed
    assert not list((home / "updates").glob("*.whl"))
    assert run() == [1] and started == []  # and not tried again before update.RETRY
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    state["update_at"] = 0
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert run() == []  # the loop leaves at once: the helper waits for it
    [(args, kw)] = started
    wheel = home / "updates" / "sysai_agent-9.9.9-py3-none-any.whl"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == installer.wheel()[2]
    assert args[0] == "powershell" and args[-2:] == ["-File", str(home / update.SCRIPT)] and kw["creationflags"] == update.DETACHED
    helper = (home / update.SCRIPT).read_text(encoding="utf-8")
    assert helper.startswith("\ufeff") and f"Wait-Process -Id {os.getpid()}" in helper
    assert f"uv tool install --force --python 3.12 '{wheel}'" in helper and "-ArgumentList 'run'" in helper
    assert "taskkill" not in helper and "Stop-Process" not in helper  # nothing is stopped by force
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert state["update_to"] == "9.9.9" and not (home / "run.lock").exists()
    assert run() == [1] and len(started) == 1  # once: the helper is at work
    monkeypatch.setattr(paths, "WINDOWS", False)  # elsewhere the synchronizer is updated by whoever installed it
    assert core.self_update(home, None, {"version": "9.9.9"}, {}) is False


def test_windows_installer_is_downloadable_from_agent_page(client, net_agent):
    for path in ('/agents', f"/agents/{net_agent['id']}"):
        page = client.get(path)
        assert page.status_code == 200
        if path == '/agents':
            assert '/agents/download/install.ps1' in page.text
    page = client.post(f"/agents/{net_agent['id']}/connect").text
    assert '/agents/download/install.ps1' in page
    r = client.get('/agents/download/install.ps1')
    assert r.status_code == 200 and 'SYSAI-install.ps1' in r.headers['content-disposition']
    assert r.content.startswith(b'\xef\xbb\xbf')  # Windows PowerShell 5.1 decodes Cyrillic correctly
    script = r.content.decode('utf-8-sig')
    assert "$Server = 'http://testserver'" in script and '__SYSAI_' not in script
    assert 'sysai-agent desktop --install-only' in script and "Read-Host 'Код подключения" in script
    assert 'окно и ярлык ещё не установлены' in script


def test_a_tool_that_is_running_is_updated_file_by_file(tmp_path, monkeypatch):
    """Windows does not rename a folder a program works in (the MCP server of the tool itself): the new version is put
    over the old one, and nothing of the old one is left behind."""
    def blob(files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, text in files.items():
                z.writestr(name, text)
        return buf.getvalue()
    target = tmp_path / "tools" / "plc-kit"
    hubsync.install(target, blob({"server.py": "v1", "old_module.py": "gone later", "sub/a.py": "a1"}), True)
    assert (target / "server.py").read_text() == "v1"
    real = os.replace

    def busy(src, dst):
        if Path(src) == target:   # the folder itself cannot be moved aside
            raise PermissionError(32, "Процесс не может получить доступ к файлу", str(src))
        return real(src, dst)
    monkeypatch.setattr(hubsync.os, "replace", busy)
    hubsync.install(target, blob({"server.py": "v2", "new_module.py": "n", "sub/a.py": "a2"}), True)
    assert (target / "server.py").read_text() == "v2" and (target / "sub" / "a.py").read_text() == "a2"
    assert (target / "new_module.py").is_file() and not (target / "old_module.py").exists()
    assert not target.with_name("plc-kit.new").exists() and not target.with_name("plc-kit.old").exists()
    # the server is started outside the folder of the tool, its script by the full path: the folder is not held
    args = mcpwrap.command({"run": "python server.py"}, target, "C:/venv/python.exe")
    assert args == ["C:/venv/python.exe", str(target / "server.py")]
    assert mcpwrap.command({"run": "python -u server.py --x"}, target)[1:] == ["-u", str(target / "server.py"), "--x"]


def test_the_environment_of_a_tool_is_kept_while_its_requirements_are_the_same(tmp_path, monkeypatch):
    """A tool with requirements.txt has an environment named by them: a new version with the same requirements builds
    nothing, other requirements get a new folder, and an environment that is held (a running tool has loaded a library
    of it — «[WinError 5] Отказано в доступе … _rust.pyd») is left where it is instead of failing the update."""
    home, folder = tmp_path / "home", tmp_path / "home" / "tools" / "plc-live"
    folder.mkdir(parents=True)
    (folder / "requirements.txt").write_text("asyncua>=1.1\n", encoding="utf-8")
    runs = []
    exe = "Scripts/python.exe" if paths.WINDOWS else "bin/python"

    def run(args, **kw):
        runs.append(args[1:3])
        if args[1] == "venv":
            (Path(args[-1]) / exe).parent.mkdir(parents=True)
            (Path(args[-1]) / exe).write_text("python")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(hubsync.shutil, "which", lambda name: "uv")
    monkeypatch.setattr(hubsync.subprocess, "run", run)
    old = home / "venvs" / "plc-live"                         # what a synchronizer before this one made
    (old / "Lib").mkdir(parents=True)
    first = hubsync._tool_env(home, "plc-live", folder)
    assert Path(first).parent.parent.name.startswith("plc-live-") and (Path(first).parent.parent / hubsync.READY).is_file()
    assert not old.exists() and runs == [["venv", "--quiet"], ["pip", "install"]]
    assert hubsync._tool_env(home, "plc-live", folder) == first and len(runs) == 2          # the same requirements: nothing is built

    # other requirements while the old environment is held by the running tool: a new one next to it, no error
    (folder / "requirements.txt").write_text("asyncua>=2\n", encoding="utf-8")
    held = Path(first).parent.parent
    real = hubsync.fsutil.rmtree

    def rmtree(path):
        if Path(path) == held:
            raise PermissionError(5, "Отказано в доступе", str(held / "Lib" / "_rust.pyd"))
        return real(path)
    monkeypatch.setattr(hubsync.fsutil, "rmtree", rmtree)
    second = hubsync._tool_env(home, "plc-live", folder)
    assert second != first and Path(second).exists() and held.exists()
    monkeypatch.setattr(hubsync.fsutil, "rmtree", real)
    assert hubsync._tool_env(home, "plc-live", folder) == second and not held.exists()      # freed later: now it goes

    # a build cut short leaves no mark and is not used: it is built again
    (Path(second).parent.parent / hubsync.READY).unlink()
    before = len(runs)
    assert hubsync._tool_env(home, "plc-live", folder) == second and len(runs) == before + 2
    # the tool no longer has requirements: its environments go, it runs on the synchronizer's Python
    (folder / "requirements.txt").unlink()
    assert hubsync._tool_env(home, "plc-live", folder) == "" and not list((home / "venvs").iterdir())


def test_a_tool_started_while_the_synchronizer_is_in_mid_work_waits_instead_of_giving_up(pc, monkeypatch):
    """Hermes starts a server once; one that exits is «not connected» for the whole session. A state file being written
    anew or a folder being swapped at that very moment is waited for."""
    reads = {"n": 0}
    real = mcpwrap.fsutil.read_json

    def read_json(path, default=None):
        if Path(path).name == "state.json":
            reads["n"] += 1
            if reads["n"] <= 3:
                return default       # caught between the old file and the new one
        return real(path, default)
    monkeypatch.setattr(mcpwrap.fsutil, "read_json", read_json)
    seen = []
    monkeypatch.setattr(mcpwrap.Guard, "server_allowed", lambda self: True)
    monkeypatch.setattr(mcpwrap, "command", lambda spec, folder, python="": seen.append(folder) or [sys.executable, "-c", "pass"])
    state = real(pc["home"] / "state.json", {}) or {}
    state.setdefault("tools", {})["wait-kit"] = {"version": "1.0.0", "sha256": "x", "spec": {"run": "python server.py"}}
    mcpwrap.fsutil.write_json(pc["home"] / "state.json", state)
    (pc["home"] / "tools" / "wait-kit").mkdir(parents=True, exist_ok=True)
    assert mcpwrap.serve("wait-kit", pc["home"], stdin=io.BytesIO(b""), stdout=io.BytesIO()) != 2
    assert reads["n"] >= 4 and seen                                   # it was started after the file came back
    assert mcpwrap.serve("нет-такого", pc["home"], stdin=io.BytesIO(b""), stdout=io.BytesIO()) == 2   # really absent: still refused
