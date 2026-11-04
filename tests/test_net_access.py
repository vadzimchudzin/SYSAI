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
    assert hermes["model"]["base_url"] == "https://sysai.example/v1" and hermes["model"]["default"] == "sysai/auto"
    # who asks is told to the gateway (the trace); the helpers of Hermes get tiers with models of their own, not Jev
    assert hermes["delegation"]["request_overrides"]["extra_body"] == {"sysai": {"role": "workers"}}
    assert hermes["auxiliary"]["compression"]["model"] == "sysai/cheap"
    assert hermes["auxiliary"]["vision"]["model"] == "sysai/vision"
    assert hermes["auxiliary"]["vision"]["extra_body"] == {"sysai": {"role": "vision"}}
    assert hermes["mcp_servers"]["sysai"]["url"] == "https://sysai.example/mcp"
    assert hermes["delegation"]["model"] == "sysai/cheap" and hermes["delegation"]["max_spawn_depth"] == 1
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
    assert rules["models"] == {"tiers": ["cheap", "mid"], "budget_day_usd": 2.0, "budget_project_usd": 0.0,
                              "main": "mid", "workers": "cheap", "auto_cost": "low"}
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


def test_sysai_auto_in_a_profile(client, net_agent):
    profile = _profile("opo-engineer")
    form = {"name": profile.name, "description": "", "commands_deny": "", "folders_read": "", "folders_write": "",
            "folders_deny": "", "programs": "", "mcp": "sysai: whoami", "data_projects": "own", "data_to_base": "none",
            "data_tags": "none", "internet_mode": "off", "internet_sites": "", "tier_mid": "on", "tier_auto": "on",
            "model_main": "auto", "model_workers": "mid", "auto_cost": "medium", "budget_day_usd": "1",
            "budget_project_usd": "0", "schedule": "", "chat_owner": "on", "chat_others": "", "skills_departments": "*"}
    assert client.post(f"/agents/access/{profile.id}", data=form, follow_redirects=False).status_code == 303
    rules = access.rules_for(net_agent["id"])["models"]
    assert (rules["tiers"], rules["main"], rules["workers"], rules["auto_cost"]) == (["mid", "auto"], "auto", "mid", "medium")
    hermes = access.document(net_agent["id"])["doc"]["harness_config"]
    assert hermes["model"]["default"] == "sysai/auto"
    assert hermes["auxiliary"]["compression"]["model"] == "sysai/mid" and hermes["auxiliary"]["vision"]["model"] == "sysai/mid"
    # Jev picks among the models of the other tiers: without a text tier sysai/auto goes away
    alone = access.normalize({"models": {"tiers": ["vision", "auto"], "main": "auto", "auto_cost": "lots"}})["models"]
    assert alone["tiers"] == ["vision"] and alone["auto_cost"] == "low"
    page = client.get("/agents/access").text
    assert "sysai/auto" in page and 'name="auto_cost"' in page and 'name="model_auto"' not in page


def test_the_character_of_the_agents_soul(client, net_agent):
    from app.net import access, soul
    doc = access.document(net_agent["id"], "https://sysai.example")
    text = doc["doc"]["soul"]
    assert text.startswith("# opo-sysai") and "Ты — opo-sysai, агент сети SYSAI" in text and "## Отдел ОПО" in text
    page = client.get("/agents/access").text
    assert 'id="soul"' in page and 'name="common"' in page and "ОПО" in page
    r = client.post("/agents/access/soul", data={"common": "Отвечай коротко.", "dept_name_0": "ОПО",
                                                 "dept_0": "Сначала сверяй адреса."}, follow_redirects=False)
    assert r.headers["location"].startswith("/agents/access?msg=") and r.headers["location"].endswith("#soul")
    after = access.document(net_agent["id"], "https://sysai.example")
    assert "Отвечай коротко." in after["doc"]["soul"] and "Сначала сверяй адреса." in after["doc"]["soul"]
    assert after["rev"] != doc["rev"] and soul.texts()["common"] == "Отвечай коротко."
    page = client.get(f"/agents/{net_agent['id']}").text
    assert "SOUL.md" in page and "Отвечай коротко." in page
    # without a department addition the section is not there
    client.post("/agents/access/soul", data={"common": "Отвечай коротко.", "dept_name_0": "ОПО", "dept_0": ""})
    assert "## Отдел" not in access.document(net_agent["id"])["doc"]["soul"]
