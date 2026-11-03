"""Access of agents only in the settings of SYSAI (docs/opo/TASKS.md, stage 2): profiles, exceptions, the signed
document and the Hermes configuration made of it; every section forbids what it should."""
import copy

from app import config
from app.db import SessionLocal, User
from app.net import access, agents
from app.net.models import NetAccessProfile


def _profile(code):
    with SessionLocal() as s:
        return s.query(NetAccessProfile).filter(NetAccessProfile.code == code).one()


def test_builtin_profiles_and_the_hermes_configuration(client, net_agent):
    codes = {p["code"] for p in access.profiles()}
    assert {"opo-engineer", "opo-lead", "to", "viewer"} <= codes
    doc = access.document(net_agent["id"], "https://sysai.example")
    assert doc["profile"]["code"] == "opo-engineer"
    hermes = doc["doc"]["harness_config"]
    assert hermes["approvals"]["mode"] == "off"  # no confirmations: what is not allowed is forbidden
    assert set(access.ALWAYS_DENY) <= set(hermes["approvals"]["deny"])
    assert hermes["model"]["base_url"] == "https://sysai.example/v1" and hermes["model"]["default"] == "sysai/mid"
    assert hermes["mcp_servers"]["sysai"]["url"] == "https://sysai.example/mcp"
    assert set(hermes["mcp_servers"]) == {"sysai", "plc-kit", "machine-expert", "cad"}
    assert hermes["skills"]["external_dirs"] and hermes["skills"]["create_dir"]
    assert "sya_" not in str(hermes)  # the token never sits in the configuration
    yaml = access.to_yaml(hermes)
    assert 'mode: "off"' in yaml and "mcp_servers:" in yaml


def test_the_document_is_signed_and_damage_is_noticed(client, net_agent):
    signed = client.get("/agents/api/config", headers=net_agent["headers"]).json()
    key = client.get("/agents/api/public-key").json()
    assert key["alg"] == "Ed25519" and signed["key_id"] == key["key_id"]
    assert access.verify(signed, key["key"])
    forged = copy.deepcopy(signed)
    forged["doc"]["rules"]["commands"]["deny"] = []
    assert not access.verify(forged, key["key"])


def test_a_profile_change_reaches_the_agent_as_a_new_revision(client, net_agent):
    before = access.document(net_agent["id"])
    profile = _profile("opo-engineer")
    form = {"name": profile.name, "description": "", "commands_run": "on", "commands_deny": "git push --force*",
            "folders_read": "D:\\ОПО", "folders_write": "D:\\ОПО\\Проекты", "folders_deny": "", "programs": "Excel",
            "mcp": "sysai: whoami, hub_search\nplc-kit: *", "data_projects": "own", "data_to_base": "read",
            "data_tags": "write", "internet_mode": "off", "internet_sites": "", "tier_cheap": "on", "tier_mid": "on",
            "budget_day_usd": "2", "budget_project_usd": "0", "schedule": "каждые 15 минут | Новые пакеты ТО",
            "chat_owner": "on", "chat_others": "", "skills_departments": "*", "skills_propose": "on"}
    r = client.post(f"/agents/access/{profile.id}", data=form, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    after = access.document(net_agent["id"])
    assert after["rev"] != before["rev"] and after["profile"]["version"] == before["profile"]["version"] + 1
    rules = after["rules"]
    assert rules["mcp"] == {"sysai": ["whoami", "hub_search"], "plc-kit": ["*"]}
    assert rules["models"] == {"tiers": ["cheap", "mid"], "budget_day_usd": 2.0, "budget_project_usd": 0.0}
    assert rules["schedule"] == [{"when": "каждые 15 минут", "task": "Новые пакеты ТО"}]
    assert rules["internet"]["mode"] == "off"
    assert client.post("/agents/api/heartbeat", headers=net_agent["headers"], json={}).json()["access_rev"] == after["rev"]
    page = client.get("/agents/access").text
    assert "Инженер ОПО" in page and "Наблюдатель" in page


def test_personal_exceptions_and_never_more_than_the_person(client, net_agent):
    r = client.post(f"/agents/{net_agent['id']}/access", data={"profile_id": _profile("opo-lead").id,
                    "overrides": '{"models": {"budget_day_usd": 7}, "data": {"projects": "all"}}',
                    "department_id": net_agent["department_id"]}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    rules = access.rules_for(net_agent["id"])
    assert rules["models"]["budget_day_usd"] == 7.0 and rules["models"]["tiers"]  # the rest of the section stays
    assert rules["data"]["projects"] == "all"  # the person is a superuser
    bad = client.post(f"/agents/{net_agent['id']}/access", data={"overrides": "{not json"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    unknown = client.post(f"/agents/{net_agent['id']}/access", data={"overrides": '{"root": true}'}, follow_redirects=False)
    assert "error=" in unknown.headers["location"]
    with SessionLocal() as s:
        s.get(User, net_agent["user_id"]).role = "user"
        s.commit()
    assert access.rules_for(net_agent["id"])["data"]["projects"] == "department"


def test_command_folder_and_tool_checks():
    rules = access.cap_by_person(access.normalize(access.BUILTIN[0][3]), "user")
    assert access.command_denied(rules, "git status") == ""
    assert access.command_denied(rules, "git push --force origin main") == "git push --force*"
    assert access.command_denied(rules, "FORMAT  D: /q")  # always forbidden, any case and spacing
    viewer = access.cap_by_person(access.normalize(access.BUILTIN[3][3]), "user")
    assert access.command_denied(viewer, "dir") == "*"
    assert access.path_allowed(rules, "d:/ОПО/Проекты/Линия 1/main.st", write=True)
    assert access.path_allowed(rules, "D:\\ТО\\Пакет 3\\схема.pdf")
    assert not access.path_allowed(rules, "D:\\ТО\\Пакет 3\\схема.pdf", write=True)
    assert not access.path_allowed(rules, "C:\\Windows\\System32\\drivers\\etc\\hosts")
    assert not access.path_allowed(rules, "D:\\ОПО-чужое\\x.txt")
    assert access.tool_allowed(rules, "plc-kit", "anything") and not access.tool_allowed(viewer, "plc-kit", "parse")
    assert access.tool_allowed(viewer, "sysai", "whoami") and not access.tool_allowed(viewer, "sysai", "people")


def test_the_signing_key_follows_the_secret(monkeypatch):
    first = access.public_key()["key"]
    monkeypatch.setattr(config, "SECRET_KEY", "another-secret")
    assert access.public_key()["key"] != first


def test_profiles_can_be_added_and_only_unused_custom_ones_deleted(client, net_agent):
    r = client.post("/agents/access/new", data={"name": "Пусконаладка", "code": "opo-commissioning", "copy": "opo-engineer"},
                    follow_redirects=False)
    assert r.status_code == 303
    custom = _profile("opo-commissioning")
    assert custom.rules["mcp"].get("plc-kit") == ["*"] and not custom.builtin
    builtin = _profile("viewer")
    r = client.post(f"/agents/access/{builtin.id}", data={"delete": "1"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    access.set_agent_access(net_agent["id"], custom.id, {})
    r = client.post(f"/agents/access/{custom.id}", data={"delete": "1"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    access.set_agent_access(net_agent["id"], _profile("opo-engineer").id, {})
    client.post(f"/agents/access/{custom.id}", data={"delete": "1"})
    with SessionLocal() as s:
        assert s.query(NetAccessProfile).filter(NetAccessProfile.code == "opo-commissioning").count() == 0


def test_tier_models_are_set_for_the_whole_network(client, net_agent):
    r = client.post("/agents/access/settings", data={"model_cheap": "openai/gpt-4o-mini", "model_mid": "", "model_strong": "",
                                                     "model_vision": "", "mcp_per_minute": "60", "hub_hosts": "sysat.by"},
                    follow_redirects=False)
    assert r.status_code == 303
    from app.net import gateway
    assert gateway.model_for("cheap") == "openai/gpt-4o-mini"
    assert gateway.model_for("mid") == gateway.DEFAULT_MODELS["mid"]
    assert gateway.model_for("vision") == "google/gemini-2.5-flash"  # the vision model of SYSAI
    bad = client.post("/agents/access/settings", data={"model_cheap": "bad model; rm"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    assert agents  # the module is used by the fixture
