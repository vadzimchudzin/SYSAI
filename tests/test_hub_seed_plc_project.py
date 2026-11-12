"""hub/seed/plc-kit — the `.project` file of Machine Expert is read and written without the program.

The fixture `tests/fixtures/me_engine_samples.project` was written by the CODESYS engine of Machine Expert 2.2
(3.5.19.40) itself through its scripting interface: data types, a function block with a method, an action and a
property, a function, a folder, a variable list. It holds no customer data. The tests prove that

* the file is read whole and written back byte for byte (container, zip, tables, every object);
* objects made here are laid out exactly as the engine lays out its own (the decoded trees are equal);
* a folder of ST files becomes a project and reads back as the same program; tasks, channels and the OPC UA list
  are written the way the three customer projects hold them (their structure is rebuilt here by hand — the
  customer files are not in the repository).

What these tests cannot prove — that Machine Expert opens the result — is checked outside CI with the engine itself
(`scripts/me_engine_wine.sh`, docs/opo/MACHINE_EXPERT.md)."""
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "plc-kit"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "me_engine_samples.project"
sys.path.append(str(SEED))
import mearchive as M  # noqa: E402
import meaes  # noqa: E402
import meproject as P  # noqa: E402
import plcbuild  # noqa: E402
import plcme  # noqa: E402
import plcproj  # noqa: E402

GVL = "{attribute 'symbol' := 'readwrite'}\nVAR_GLOBAL\n\t//Нория 1\n\tStart_1: BOOL; //пуск\n\tSP_1: INT := 40;\n\tNoria_1: Noria;\n\tx0_0: BOOL;\nEND_VAR"
FB = "FUNCTION_BLOCK Noria\nVAR_INPUT\n\trun: BOOL;\nEND_VAR\nVAR_OUTPUT\n\ton: BOOL;\nEND_VAR\n\non := run;"
MAIN = "PROGRAM Main\nVAR\n\tstep: INT;\nEND_VAR\n\nNoria_1(run := Start_1);\nstep := step + 1;"
MOTOR = "TYPE Motor :\nSTRUCT\n\tspeed: REAL;\nEND_STRUCT\nEND_TYPE"
SCALE = "FUNCTION Scale : REAL\nVAR_INPUT\n\traw: INT;\nEND_VAR\n\nScale := INT_TO_REAL(raw) / 10.0;"


def _src(tmp_path, main=MAIN, link=True, tasks=True) -> Path:
    src = tmp_path / "plc"
    src.mkdir(exist_ok=True)
    for name, text in (("GVL", GVL), ("Noria", FB), ("Main", main), ("Motor", MOTOR), ("Scale", SCALE)):
        (src / f"{name}.st").write_text(text + "\n", encoding="utf-8")
    meta = {"name": "Line", "controller": "TM241CE40R"}
    if tasks:
        meta["tasks"] = [{"name": "MAST", "interval": "20ms", "priority": 15, "pous": ["Main"]}]
    if link:
        meta["link"] = {"kind": "opcua", "lists": ["GVL"]}
    (src / "project.json").write_text(json.dumps(meta), encoding="utf-8")
    return src


def _tree(node) -> str:
    """The decoded object as text, without the numbers of schema entries (they differ from table to table)."""
    return re.sub(r"schema=\d+", "schema", "\n".join(M.format_node(node)))


def _template(tmp_path) -> Path:
    """The engine's project plus a controller the way the customer projects hold one: device → Plc Logic →
    Application → Task Configuration → MAST, one input module with two channels, the OPC UA list, and a program of
    its own (to be taken out)."""
    p = P.MEProject.open(str(FIXTURE))
    guid = lambda: str(uuid.uuid4())  # noqa: E731
    dev, logic, app, cfg = guid(), guid(), guid(), guid()

    def channel(label, ident, variable):
        maps = M.Coll(M.F_HOM_LIST_TI, None, (M.F_ARCHIVABLE, None),
                      [M.Obj("47edf8ea-3f84-452c-b998-e18f878578d3", [("Variable", M.F_STRING, None, variable)])])
        inner = M.Obj("8a62c05c-4fee-4b4a-8600-a677aedc92c7", [("Mappings", maps.fmt, maps.ctype, maps)])
        return M.Obj("1944b378-8867-41f4-b1f4-6e5b1e6547ba", [
            ("IoMapping", M.F_ARCHIVABLE, None, M.Obj("d6d934cf-5ec0-42c3-b628-2a7aea7d364c", [("Mappings", M.F_ARCHIVABLE, None, inner)])),
            ("Description", M.F_ARCHIVABLE, None, M.Obj("35cf7b1a-a4cd-490b-8ebe-385e61fcc6d9", [("Default", M.F_STRING, None, "Fast input")])),
            ("VisibleName", M.F_ARCHIVABLE, None, M.Obj("35cf7b1a-a4cd-490b-8ebe-385e61fcc6d9", [("Default", M.F_STRING, None, label)])),
            ("Identifier", M.F_STRING, None, ident), ("Type", M.F_STRING, None, "BOOL")])
    elements = M.Coll(M.F_HOM_LIST_TI, None, (M.F_ARCHIVABLE, None), [channel("I0", "Bit0", "Application.old_0"), channel("I1", "Bit1", "Application.old_1")])
    param = M.Obj("8c994079-f3c6-4484-9643-9258628feecd", [
        ("Id", M.F_UINT, None, 1000), ("ChannelType", M.F_TYPECONVERTER, "{abf9b454-3972-4b82-88a2-feef15a02f10}", "Input"),
        ("elements", elements.fmt, elements.ctype, elements)])
    params = M.Coll(M.F_HET_LIST_TI, None, None, [param])
    device = M.Obj(P.T_DEVICE, [("Params", params.fmt, params.ctype, params)])
    p._put(p._meta(dev, P.NULL, "MyController", P.T_DEVICE, None), device)
    p._put(p._meta(guid(), dev, "DI", P.T_DEVICE, None), device)
    p._put(p._meta(logic, dev, "Plc Logic", P.T_PLCLOGIC, None), M.Obj(P.T_PLCLOGIC, []))
    p._put(p._meta(app, logic, "Application", P.T_APP, None), M.Obj(P.T_APP, [("DownloadContent", M.F_BOOL, None, True)]))
    p._put(p._meta(cfg, app, "Task Configuration", P.T_TASKCFG, None), M.Obj(P.T_TASKCFG, []))
    p.set_task("MAST", "10ms", 15, ["OldMain"])
    p.set_task("SLOW", "100ms", 20, [])
    sel = M.Coll(M.F_HOM_LIST_TI, None, (M.F_STRING, None), ["Old.a"])
    rights = M.Dict(M.F_HH_DICT_TI, None, (M.F_STRING, None), (M.F_INT, None), [["Old.a", 2], ["Old.gone", 8]])
    p._put(p._meta(guid(), app, "OPCUASymbolConfiguration", P.MEProject.T_OPCUA, None),
           M.Obj(P.MEProject.T_OPCUA, [("SelectedVariables", sel.fmt, sel.ctype, sel),
                                       ("SelectedVariablesWithAccessRights", rights.fmt, rights.ctype, rights)]))
    p.add_pou("OldMain", "PROGRAM OldMain\nVAR\nEND_VAR", "old_0 := old_1;", app, p.add_folder("Старое", app))
    p.add_gvl("Old", "VAR_GLOBAL\n\ta: INT;\n\told_0, old_1: BOOL;\nEND_VAR", app)
    out = tmp_path / "template.project"
    p.save(str(out))
    return out


def _server(calls) -> list:
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})]
    lines += [json.dumps({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}})
              for i, (n, a) in enumerate(calls)]
    out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(lines) + "\n", capture_output=True,
                         text=True, timeout=120, encoding="utf-8").stdout.splitlines()
    return [json.loads(x)["result"] for x in out][1:]


# ------------------------------------------------------------------------------------------------ the file itself
def test_the_engine_file_is_read_whole_and_written_back_byte_for_byte():
    data = FIXTURE.read_bytes()
    assert M.is_container(data)
    iv, salt = M.container_params(data)
    plain = M.open_container(data)
    assert plain[:2] == b"PK" and M.seal_container(plain, iv, salt) == data
    a = M.Project.open(data)
    kinds = {}
    for name, blob in a.entries.items():
        kind = M.classify(name, blob)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "archive":                       # every .meta and .object: decoded and encoded to the same bytes
            assert M.dump(M.load(blob, a.storage), a.storage) == blob, name
        elif kind == "archive-embedded":
            assert M.Archive.parse(blob).to_bytes() == blob, name
        elif kind == "legacy":
            assert M.LegacyArchive.parse(blob).to_bytes() == blob, name
    assert kinds["archive"] == 2 * len(a.object_guids()) == 28 and kinds["string-table"] == kinds["schema-table"] == 1
    assert a.storage.serialize() == (a.entries[M.STRING_TABLE_ENTRY], a.entries[M.SCHEMA_TABLE_ENTRY])
    assert M.write_zip((n, d, a.date_times[n]) for n, d in a.entries.items()) == plain
    assert a.to_bytes(keep_iv=True) == data


def test_plain_python_aes_is_the_same_cipher():
    data = FIXTURE.read_bytes()
    iv, salt, digest, body = data[16:32], data[32:48], data[48:80], data[80:]
    key = M._derive_key(digest, salt)
    plain = meaes.decrypt_cbc(key, iv, body)
    assert plain[:2] == b"PK" and meaes.encrypt_cbc(key, iv, plain) == body
    # FIPS-197, appendix C.3
    key = bytes(range(32))
    block = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert meaes.encrypt_cbc(key, bytes(16), block).hex() == "8ea2b7ca516745bfeafc49904b496089"
    assert meaes.decrypt_cbc(key, bytes(16), bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")) == block


def test_the_tree_and_the_texts_of_the_engine_file():
    p = P.MEProject.open(str(FIXTURE))
    by = {n.name: n for n in p.objects()}
    assert {n.name: n.kind for n in p.objects() if n.kind in ("pou", "type", "gvl", "folder", "method", "action", "property")} == {
        "T_Mode": "type", "T_Rec": "type", "T_U": "type", "T_A": "type", "FB_Motor": "pou", "Reset": "method",
        "ActStop": "action", "Speed": "property", "F_Add": "pou", "Folder1": "folder", "GVL_In": "gvl"}
    assert p.text(by["FB_Motor"]) == ("FUNCTION_BLOCK FB_Motor\nVAR_INPUT\n\tStart : BOOL;\nEND_VAR\nVAR_OUTPUT\n\tRun : BOOL;\nEND_VAR\n",
                                      "Run := Start;\n", "st")
    assert p.text(by["Reset"])[1] == "Reset := TRUE;\n" and p.path(by["Reset"]) == "FB_Motor/Reset"
    assert p.text(by["T_Rec"])[0].startswith("TYPE T_Rec :\nSTRUCT")
    m = plcproj.load(str(FIXTURE))
    assert [x["name"] for x in m.pous] == ["FB_Motor", "F_Add"] and m.pou("F_Add")["kind"] == "function"
    assert [c["name"] for c in m.pou("FB_Motor")["children"]] == ["Reset", "ActStop", "Speed"]
    assert [g["name"] for g in m.gvls] == ["GVL_In"] and len(m.types) == 4
    d = plcproj.probe(str(FIXTURE))
    assert d["container"] == "sealed" and d["counts"]["pou"] == 2 and d["channels"] == 0 and d["opcua_variables"] is None


def test_objects_made_here_are_laid_out_as_the_engine_lays_out_its_own(monkeypatch):
    """Take every program object out of the engine's file, make it again from its texts and compare the decoded
    objects: equal but for the counter of line numbers (the engine counts every edit, a new object has none)."""
    ref, mine = P.MEProject.open(str(FIXTURE)), P.MEProject.open(str(FIXTURE))
    code = [n for n in ref.objects() if n.kind in ("pou", "method", "action", "gvl", "type", "folder")]
    for n in reversed(code):
        if n.guid in mine.nodes:
            mine.remove(n)
    assert not [n for n in mine.objects() if n.kind in P.MEProject.PROGRAM_KINDS and n.kind not in ("property", "accessor")]
    compared = 0
    for n in code:
        decl, impl, _ = ref.text(n)
        parent = None if n.parent == P.NULL else n.parent
        monkeypatch.setattr(P.uuid, "uuid4", lambda g=n.guid: uuid.UUID(g))
        new = {"pou": lambda: mine.add_pou(n.name, decl, impl, parent), "method": lambda: mine.add_method(parent, n.name, decl, impl),
               "action": lambda: mine.add_action(parent, n.name, impl), "gvl": lambda: mine.add_gvl(n.name, decl, parent),
               "type": lambda: mine.add_type(n.name, decl, parent), "folder": lambda: mine.add_folder(n.name, parent)}[n.kind]()
        new.meta["Timestamp"] = n.meta["Timestamp"]
        mine.a.store(n.guid + ".meta", new.meta)
        assert _tree(mine.a.load(n.guid + ".meta")) == _tree(ref.a.load(n.guid + ".meta")), n.name
        theirs, ours = ref.a.load(n.guid + ".object"), mine.a.load(n.guid + ".object")
        for o in (theirs, ours):
            if "UniqueIdGenerator" in o:
                o["UniqueIdGenerator"] = "0"
        assert _tree(ours) == _tree(theirs), n.name
        for part, text in (("decl", decl), ("impl", impl)):
            entry = f"{n.guid}_{part}_lineids.auxiliary"
            assert (entry in mine.a.entries) == (entry in ref.a.entries), entry
            if entry in ref.a.entries:
                a, b = json.loads(ref.a.entries[entry]), json.loads(mine.a.entries[entry])
                ends = lambda j: [j["EndOffsets"][i] + (j["GapWidth"] if j["GapLine"] != -1 and i >= j["GapLine"] else 0)  # noqa: E731
                                  for i in range(j["LineCount"])]
                assert ends(a) == ends(b) and a["Hash"] == b["Hash"], entry
        compared += 1
    assert compared == 10
    back = P.MEProject.open(mine.to_bytes())          # and the whole file reads back
    assert {n.name for n in back.objects()} == {n.name for n in ref.objects()} - {"Speed", "Get", "Set"}   # the property was not made again


# ------------------------------------------------------------------------------------------------ a new project
def test_a_folder_of_st_becomes_a_project_and_reads_back_as_the_same_program(tmp_path):
    template = _template(tmp_path)
    src = plcbuild.load_dir(str(_src(tmp_path)))
    out = tmp_path / "out" / "Line.project"
    rep = plcproj.create(src, str(template), str(out))
    assert rep["verify"] == [] and rep["controller"] == "MyController" and rep["tasks"] == ["MAST"] and rep["opcua"] == 3
    assert sorted(rep["created"]) == ["GVL", "Main", "Motor", "Noria", "Scale"] and not rep["updated"]
    assert {"OldMain", "Old", "Старое", "FB_Motor", "Reset", "GVL_In", "SLOW"} <= set(rep["removed"])
    assert out.read_bytes()[:16] == M.CONTAINER_MAGIC                      # sealed, as Machine Expert saves
    back = plcme.Project.load(str(out))
    assert [x["name"] for x in back.pous] == ["Scale", "Noria", "Main"] and [g["name"] for g in back.gvls] == ["GVL"]
    main = back.pou("Main")
    assert main["declaration"] == "PROGRAM Main\nVAR\n\tstep: INT;\nEND_VAR" and main["body"] == "Noria_1(run := Start_1);\nstep := step + 1;"
    assert back.tasks == [{"name": "MAST", "kind": "Cyclic", "priority": "15", "interval": "20ms", "pous": ["Main"]}]
    assert back.link == {"kind": "opcua", "lists": ["GVL"]} and back.controller == "MyController"
    p = P.MEProject.open(str(out))
    assert p.opcua() == ["GVL.Start_1", "GVL.SP_1", "GVL.x0_0"]
    assert all(p.path(n).startswith("MyController/Plc Logic/Application/") for n in p.objects() if n.kind in ("pou", "gvl", "type"))
    assert not [n for n in p.a.entries if n.endswith(".precompileinfo.auxiliary")]      # stale caches are dropped
    # every object decodes and encodes to the same bytes with the grown tables
    for name, blob in p.a.entries.items():
        if M.classify(name, blob) == "archive":
            assert M.dump(M.load(blob, p.a.storage), p.a.storage) == blob
    with pytest.raises(plcproj.ProjectFileError, match="уже есть"):
        plcproj.create(src, str(template), str(out))


def test_update_changes_only_what_changed_and_keeps_the_rest(tmp_path):
    template = _template(tmp_path)
    out = tmp_path / "Line.project"
    plcproj.create(plcbuild.load_dir(str(_src(tmp_path))), str(template), str(out))
    before = P.MEProject.open(str(out))
    src = _src(tmp_path, main=MAIN + "\nSP_1 := step;")
    (src / "Extra.st").write_text("PROGRAM Extra\nVAR\nEND_VAR\n\nStart_1 := FALSE;\n", encoding="utf-8")
    rep = plcproj.update(str(out), plcbuild.load_dir(str(src)))
    assert rep["updated"] == ["Main"] and rep["created"] == ["Extra"] and rep["verify"] == [] and not rep["removed"]
    assert sorted(rep["kept"]) == ["GVL", "Motor", "Noria", "Scale"] and rep["left"] == []
    after = P.MEProject.open(str(out))
    same = [n for n in before.a.entries if n.endswith(".object") and before.a.entries[n] == after.a.entries.get(n)]
    main = before.find("Main", "pou")[0].guid
    assert main + ".object" not in same and len(same) == len(before.a.object_guids()) - 1      # Main alone
    assert after.find("Main", "pou")[0].guid == main                                        # the object is the same one
    assert after.text(after.find("Main")[0])[1].endswith("SP_1 := step;")
    # keep_program: the template's own objects stay and are named
    kept = tmp_path / "kept.project"
    rep = plcproj.create(plcbuild.load_dir(str(_src(tmp_path, tasks=False, link=False))), str(template), str(kept), keep_program=True)
    assert "OldMain" in rep["left"] and "Old" in rep["left"] and not rep["removed"]
    assert [t["pous"] for t in P.MEProject.open(str(kept)).tasks()] == [["OldMain"], []]


def test_a_template_is_a_project_without_its_program(tmp_path):
    rep = plcproj.template(str(_template(tmp_path)), str(tmp_path / "t.project"))
    assert rep["controller"] == "MyController" and rep["devices"] == ["MyController", "DI"] and rep["tasks"] == ["MAST", "SLOW"]
    p = P.MEProject.open(str(tmp_path / "t.project"))
    assert not [n for n in p.objects() if n.kind in P.MEProject.PROGRAM_KINDS]
    assert [t["pous"] for t in p.tasks()] == [[], []] and p.opcua() == []
    assert len(p.io_channels()) == 4                           # the channels keep their variables: plc_io shows them


def test_channels_carry_variables(tmp_path):
    out = tmp_path / "Line.project"
    plcproj.create(plcbuild.load_dir(str(_src(tmp_path))), str(_template(tmp_path)), str(out))
    rows = [r for r in plcproj.io(str(out)) if r["device"] == "DI"]
    assert [(r["channel"], r["direction"], r["type"], r["variable"]) for r in rows] == [
        ("I0", "input", "BOOL", "Application.old_0"), ("I1", "input", "BOOL", "Application.old_1")]
    rep = plcproj.set_io(str(out), [{"device": "DI", "channel": "I0", "variable": "x0_0"}, {"device": "DI", "channel": "Bit1", "variable": ""}])
    assert rep["changed"] == [{"device": "DI", "channel": "I0", "was": "Application.old_0", "now": "x0_0"},
                              {"device": "DI", "channel": "I1", "was": "Application.old_1", "now": None}]
    rows = [r for r in plcproj.io(str(out)) if r["device"] == "DI"]
    assert [r["variable"] for r in rows] == ["Application.x0_0", None]
    with pytest.raises(plcproj.ProjectFileError, match="нет канала"):
        plcproj.set_io(str(out), [{"device": "DI", "channel": "I9", "variable": "x0_0"}])


def test_what_cannot_be_written_is_said(tmp_path):
    p = P.MEProject.open(str(FIXTURE))
    with pytest.raises(P.ProjectError, match="уже есть"):
        p.add_pou("fb_motor", "PROGRAM FB_Motor", "")
    with pytest.raises(P.ProjectError, match="не годится"):
        p.add_gvl("1 список", "VAR_GLOBAL\nEND_VAR")
    with pytest.raises(P.ProjectError, match="Конфигурации задач"):
        p.set_task("MAST", "20ms")
    with pytest.raises(P.ProjectError, match="период"):
        P.MEProject._interval("скоро")
    assert P.MEProject._interval("T#1s") == ("1000", "ms") and P.MEProject._interval("500us") == ("500", "µs")
    # no controller in the template: the program is written at the project level, the tasks are named as not written
    rep = plcproj.create(plcbuild.load_dir(str(_src(tmp_path))), str(FIXTURE), str(tmp_path / "flat.project"))
    assert any("Конфигурации задач" in w for w in rep["warnings"]) and any("OPC UA" in w for w in rep["warnings"]) and rep["verify"] == []


def test_the_server_builds_templates_and_maps(tmp_path):
    template, src, out = _template(tmp_path), _src(tmp_path), tmp_path / "srv" / "Line.project"
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "Main.st").write_text("PROGRAM Main\nVAR\nEND_VAR\n\nIF TRUE THEN\n", encoding="utf-8")
    r = _server([
        ("plc_project_template", {"path": str(template), "out_file": str(tmp_path / "srv" / "t.project")}),
        ("plc_project_build", {"src_dir": str(src), "template_file": str(tmp_path / "srv" / "t.project"), "out_file": str(out)}),
        ("plc_info", {"path": str(out)}), ("plc_pou", {"path": str(out), "name": "Noria"}),
        ("plc_hmi_tags", {"path": str(out)}), ("plc_io", {"path": str(out), "device": "DI"}),
        ("plc_io_map", {"path": str(out), "out_file": str(out), "changes": [{"device": "DI", "channel": "I0", "variable": "x0_0"}]}),
        ("plc_io", {"path": str(out), "free": True}),
        ("plc_project_build", {"src_dir": str(src), "out_file": str(out)}),
        ("plc_project_build", {"src_dir": str(src), "out_file": str(tmp_path / "srv" / "none.project")}),
        ("plc_project_build", {"src_dir": str(tmp_path / "bad"), "template_file": str(template), "out_file": str(tmp_path / "srv" / "bad.project")}),
        ("plc_export_st", {"path": str(out), "out_dir": str(tmp_path / "srv" / "st")}),
    ])
    assert "вынуто объектов программы" in r[0]["content"][0]["text"] and "контроллер MyController" in r[0]["content"][0]["text"]
    assert not r[1].get("isError") and "Новый проект" in r[1]["content"][0]["text"] and "сходится" in r[1]["content"][0]["text"]
    assert r[1]["structuredContent"]["opcua"] == 3
    info = r[2]["structuredContent"]
    assert info["container"] == "sealed" and info["program"]["pous"] == 3 and info["program"]["link"] == {"kind": "opcua", "lists": ["GVL"]}
    assert info["controller"] == "MyController" and info["channels"] == 4 and info["opcua_variables"] == 3
    assert r[3]["content"][0]["text"] == FB.split("\n\n")[0] + "\n\n" + FB.split("\n\n")[1]
    assert [t["name"] for t in json.loads(r[4]["content"][0]["text"])["tags"]] == ["GVL.Start_1", "GVL.SP_1", "GVL.x0_0"]
    assert json.loads(r[5]["content"][0]["text"])["count"] == 2
    assert "Каналов изменено: 1" in r[6]["content"][0]["text"]
    assert json.loads(r[7]["content"][0]["text"])["count"] == 0
    assert "Проект обновлён" in r[8]["content"][0]["text"] and r[8]["structuredContent"]["kept"] and not r[8]["structuredContent"]["updated"]
    assert r[9]["isError"] and "template_file" in r[9]["content"][0]["text"]
    assert r[10]["isError"] and "IF не закрыт" in r[10]["content"][0]["text"] and not (tmp_path / "srv" / "bad.project").exists()
    assert len(r[11]["structuredContent"]["files"]) == 6
    assert plcbuild.load_dir(str(tmp_path / "srv" / "st")).pou("Main")["body"] == "Noria_1(run := Start_1);\nstep := step + 1;"


def test_other_shapes_of_the_file_and_the_variables_of_the_mapping(tmp_path):
    import zipfile
    p = P.MEProject.open(str(_template(tmp_path)))
    plain = tmp_path / "plain.project"
    p.save(str(plain), sealed=False)                                   # no integrity wrapper: a ZIP, also a project
    assert plain.read_bytes()[:2] == b"PK" and plcproj.probe(str(plain))["container"] == "zip"
    assert plcme.Project.load(str(plain)).pou("OldMain")["body"] == "old_0 := old_1;"
    archive = tmp_path / "line.projectarchive"                         # «Файл → Архив проекта»: the project inside a ZIP
    with zipfile.ZipFile(archive, "w") as z:
        z.write(plain, "line.project")
        z.writestr("Standard.compiled-library", b"x")
    assert plcproj.probe(str(archive))["container"] == "archive" and len(plcme.Project.load(str(archive)).pous) == 3
    # a variable declared by the mapping itself is a tag the panel may use: IoConfig_Globals_Mapping.<name>
    dev = p.find("DI", "device")[0]
    o = p.object(dev)
    el, _ = p._channels(o)[0]
    el["IoMapping"]["Mappings"]["Mappings"][0].set("CreateVariable", True, M.F_BOOL)
    el["IoMapping"]["Mappings"]["Mappings"][0]["Variable"] = "ixDI_I0"
    p.a.store(dev.guid + ".object", o)
    made = tmp_path / "made.project"
    p.save(str(made))
    m = plcme.Project.load(str(made))
    assert m.io_variables == {"ixdi_i0"}
    r = m.tag_check(["IoConfig_Globals_Mapping.ixDI_I0", "IoConfig_Globals_Mapping.nope", "Old.a"])
    assert r["ok"] == 2 and r["missing"] == ["IoConfig_Globals_Mapping.nope"]
    assert [x for x in plcproj.io(str(made)) if x["new_variable"]][0]["variable"] == "ixDI_I0"
