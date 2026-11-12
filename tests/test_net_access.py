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
    # one model for the whole conversation (a model that changes between requests reads nothing from the cache)
    assert hermes["model"]["base_url"] == "https://sysai.example/v1" and hermes["model"]["default"] == "sysai/mid"
    assert "auto" in doc["rules"]["models"]["tiers"]  # sysai/auto stays allowed: chosen in the profile or the window
    # the size of a conversation is its cost: squeezed at a count of tokens said outright, big results go to a file
    assert hermes["compression"] == {"enabled": True, "threshold_tokens": access.COMPRESS_AT} and access.COMPRESS_AT < 272_000
    assert hermes["tool_budget"] == {"mcp_result_size_chars": 20000} and hermes["tool_output"] == {"max_bytes": 20000}
    # the schemas of MCP tools on demand on every Hermes alike; «on» is a string in YAML, not a boolean
    assert hermes["tools"] == {"tool_search": {"enabled": "on"}} and 'enabled: "on"' in access.to_yaml(hermes)
    assert access.hermes_note({"hermes": "0.22.1"}) == "" and access.hermes_note({}) == "" and access.hermes_note(None) == ""
    assert "hermes update" in access.hermes_note({"hermes": "0.19.4"}) and "новее" in access.hermes_note({"hermes": "0.31.0"})
    # the helpers carry the token themselves: a helper refused with 401 is a conversation never squeezed
    assert {hermes["auxiliary"][k]["api_key"] for k in ("compression", "vision")} == {"${SYSAI_AGENT_TOKEN}"}
    assert hermes["delegation"]["max_iterations"] == 60 and hermes["delegation"]["max_summary_chars"] == 8000
    # who asks is told to the gateway (the trace); the helpers of Hermes get tiers with models of their own, not Jev
    assert hermes["delegation"]["request_overrides"]["extra_body"] == {"sysai": {"role": "workers"}}
    assert hermes["auxiliary"]["compression"]["model"] == "sysai/cheap"
    assert hermes["auxiliary"]["vision"]["model"] == "sysai/vision"
    assert hermes["auxiliary"]["vision"]["extra_body"] == {"sysai": {"role": "vision"}}
    assert hermes["mcp_servers"]["sysai"]["url"] == "https://sysai.example/mcp"
    assert hermes["delegation"]["model"] == "sysai/cheap" and hermes["delegation"]["max_spawn_depth"] == 1
    # allowed servers that are not in the hub would never start: only SYSAI and the delivered hub servers are there
    assert set(hermes["mcp_servers"]) == {"sysai"} and {"plc-kit", "machine-expert", "cad"} <= set(doc["rules"]["mcp"])
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
    folders = {"read": ["D:\\ОПО", "D:\\ТО"], "write": ["D:\\ОПО\\Проекты"], "deny": ["C:\\Windows", "C:\\Program Files"]}
    rules = access.cap_by_person(access.normalize({**access.BUILTIN[0][3], "folders": folders}), "user")
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
    # «..» is collapsed as on the computer: no way out of an allowed folder into a forbidden one
    assert not access.path_allowed(rules, "D:\\ОПО\\Проекты\\..\\..\\Windows\\win.ini")
    assert not access.path_allowed(rules, "d:/ОПО/Проекты/../../ТО/Пакет 3/схема.pdf", write=True)
    assert access.path_allowed(rules, "D:\\ОПО\\Проекты\\Линия 1\\..\\Линия 2\\main.st", write=True)
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
    assert gateway.model_for("vision") == gateway.VISION_DEFAULT  # the vision model of SYSAI
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


def test_the_agent_sees_the_whole_computer_and_hermes_asks_before_acting(client, net_agent):
    engineer = access.cap_by_person(access.normalize(access.BUILTIN[0][3]), "user")
    viewer = access.cap_by_person(access.normalize(access.BUILTIN[3][3]), "user")
    for path in ("C:\\Users\\ivanov\\Desktop\\план.xlsx", "D:\\ТО\\Пакет 3\\схема.pdf", "\\\\nas\\share\\a.dwg"):
        assert access.path_allowed(engineer, path) and access.path_allowed(engineer, path, write=True), path
        assert access.path_allowed(viewer, path) and not access.path_allowed(viewer, path, write=True), path
    assert engineer["internet"]["mode"] == "all" and viewer["internet"]["mode"] == "off"
    narrow = access.normalize({"folders": {"read": ["*"], "write": ["*"], "deny": ["D:\\Бухгалтерия"]}})
    assert not access.path_allowed(narrow, "d:/бухгалтерия/2025.xlsx") and access.path_allowed(narrow, "D:\\Бух.txt")
    assert not access.path_allowed(access.normalize({"folders": {"read": ["*"], "deny": ["*"]}}), "C:\\a.txt")
    # Hermes asks the synchronizer before its own tools: what is open whole is not asked about
    hook = access.document(net_agent["id"], "https://sysai.example")["doc"]["harness_config"]["hooks"]["pre_tool_call"]
    assert hook == [{"command": "sysai-agent hook", "matcher": "terminal|execute_code|write_file|patch",
                     "timeout": access.HOOK_TIMEOUT, "fail_closed": True}]
    access.set_agent_access(net_agent["id"], overrides={"folders": {"read": ["D:\\ОПО"], "write": [], "deny": []},
                                                        "internet": {"mode": "list", "sites": ["se.com"]},
                                                        "commands": {"run": False}})
    hook = access.document(net_agent["id"])["doc"]["harness_config"]["hooks"]["pre_tool_call"][0]
    asked = hook["matcher"].split("|")
    assert {"read_file", "search_files", "web_search", "web_extract", "browser_.*", "vision_analyze",
            "process_manage", "computer_use", "terminal"} <= set(asked)
    assert access.to_yaml({"hooks": {"pre_tool_call": [hook]}}).count("fail_closed: true") == 1


def test_the_first_placeholder_folders_and_sites_give_way_once(client, net_agent):
    """Profiles made before the whole computer: a section nobody changed gets the new default; a changed one stays."""
    access.ensure_builtin()
    with SessionLocal() as s:
        for row in s.query(NetAccessProfile).filter(NetAccessProfile.builtin.is_(True)).all():
            first = access.FIRST_DEFAULTS.get(row.code) or {}
            rules = copy.deepcopy(row.rules)
            rules.update(copy.deepcopy(first))
            if row.code == "to":
                rules["internet"] = {"mode": "list", "sites": ["sysat.by", "мой-сайт.by"]}  # changed by a person
            row.rules, row.version = rules, 3
        s.commit()
    access.ensure_builtin()
    access.ensure_builtin()  # once: the second call changes nothing
    engineer, to, viewer = _profile("opo-engineer"), _profile("to"), _profile("viewer")
    assert engineer.rules["folders"] == {"read": ["*"], "write": ["*"], "deny": []} and engineer.version == 4
    assert engineer.rules["internet"]["mode"] == "all" and engineer.updated_by == "SYSAI"
    assert to.rules["folders"]["read"] == ["*"] and to.rules["internet"]["sites"] == ["sysat.by", "мой-сайт.by"]
    assert viewer.rules["folders"] == {"read": ["*"], "write": [], "deny": []} and viewer.rules["internet"]["mode"] == "off"


def test_the_signed_document_offers_the_synchronizer_to_update_to(client, net_agent):
    from app import settings_store
    from app.net import installer
    doc = access.document(net_agent["id"])
    offer = doc["doc"]["synchronizer"]
    assert offer == {"version": installer.version(), "sha256": installer.wheel()[2], "auto_update": True}
    settings_store.set_many({"agents_auto_update": False})
    later = access.document(net_agent["id"])
    assert later["doc"]["synchronizer"]["auto_update"] is False and later["rev"] != doc["rev"]  # the computers learn


def test_a_tool_added_to_the_seed_is_given_to_the_department_profiles_once(client, net_agent, monkeypatch):
    """Nobody writes «ebpro-gui: *» into every profile: a shipped MCP tool the profile was never given is added by the
    server, the old default main model «auto» becomes «mid» — once; what a person changes afterwards stays."""
    shipped = access.seed_tools()
    assert {"plc-kit", "machine-expert", "ebpro-cmtp", "ebpro-gui", "pic-index", "doc-kit"} <= set(shipped)
    assert "plc-project" not in shipped                      # a skill is not an MCP server
    access.ensure_builtin()
    fresh = _profile("opo-engineer")
    assert set(shipped) | {"sysai", "cad"} == set(fresh.rules["mcp"]) and fresh.rules["models"]["main"] == "mid"
    assert not set(shipped) & set(_profile("to").rules["mcp"]) and not set(shipped) & set(_profile("viewer").rules["mcp"])

    # a profile of a database made before: fewer servers, the main model «auto», nothing remembered
    with SessionLocal() as s:
        for code in ("opo-engineer", "opo-lead"):
            row = s.query(NetAccessProfile).filter(NetAccessProfile.code == code).one()
            rules = copy.deepcopy(row.rules)
            rules["mcp"] = {"sysai": ["*"], "plc-kit": ["plc_info"], "machine-expert": ["*"], "cad": ["*"], "ebpro-cmtp": ["*"]}
            rules["models"]["main"] = "auto"
            row.rules, row.given, row.version = rules, None, 5
        s.commit()
    access.ensure_builtin()
    access.ensure_builtin()
    old = _profile("opo-engineer")
    assert set(old.rules["mcp"]) == set(shipped) | {"sysai", "cad"} and old.version == 6 and old.updated_by == "SYSAI"
    assert old.rules["mcp"]["plc-kit"] == ["plc_info"]       # what the profile says about a server it has is kept
    assert old.rules["models"]["main"] == "mid" and old.rules["models"]["budget_day_usd"] == 3.0
    assert _profile("opo-lead").rules["mcp"]["ebpro-gui"] == ["*"]
    doc = access.document(net_agent["id"])["doc"]
    assert "ebpro-gui" in doc["rules"]["mcp"]                # the computer gets it with the next sync

    # a person takes a server away and chooses «auto» again: it stays
    rules = access.normalize(old.rules)
    del rules["mcp"]["ebpro-gui"]
    rules["models"]["main"] = "auto"
    access.save_profile(old.id, old.name, old.description, rules, by="admin")
    access.ensure_builtin()
    kept = _profile("opo-engineer")
    assert "ebpro-gui" not in kept.rules["mcp"] and kept.rules["models"]["main"] == "auto" and kept.updated_by == "admin"

    # a tool added to hub/seed later reaches the profile by itself
    monkeypatch.setattr(access, "seed_tools", lambda: shipped + ["plc-live"])
    access.ensure_builtin()
    after = _profile("opo-engineer")
    assert after.rules["mcp"]["plc-live"] == ["*"] and "ebpro-gui" not in after.rules["mcp"]
    assert "plc-live" not in _profile("to").rules["mcp"]
