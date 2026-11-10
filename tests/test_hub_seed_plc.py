"""hub/seed/plc-kit — the PLC tool (Machine Expert / CODESYS, ST) stays valid for SYSAI_Hub and keeps working.

No customer project is needed: the test writes a small Machine Expert `.export` in the same shape as the real ones
(entries with MetaObject / Object, TextBlobForSerialisation for the declaration and the ST body, a cyclic task)."""
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from app.net import hub

SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "plc-kit"
sys.path.insert(0, str(SEED))
import plcbuild  # noqa: E402
import plcme  # noqa: E402
import plcst  # noqa: E402

TOOLS = ["plc_info", "plc_pous", "plc_pou", "plc_variables", "plc_addresses", "plc_hmi_tags", "plc_check", "plc_style",
         "plc_export_st", "plc_build"]

GVL = """VAR_GLOBAL
	PUSK AT %MX0.0:BOOL; //ПУСК
	Alarms_reset AT %MX0.2:BOOL; //сброс аварий
	//Нория 2 Входы
	noria2_hand_mode AT %MX1.2: BOOL; //ручной режим
	noria2_button AT %MX4.5:BOOL;
	noria2_button_output AT %MX4.5:BOOL;
	noria2_status AT %MW624:INT; //статус
	noria2_speed AT %MD400:REAL;
	Noria_2:Noria_class;
	Noria_6:Noria_class;
END_VAR"""
IO = """VAR_GLOBAL
	//Входы
	x0_0: BOOL; //Кнопка ПУСК
	y0_0: BOOL; //Нория 2
END_VAR"""
FB_DECL = """FUNCTION_BLOCK Noria_class
VAR_INPUT
	button: BOOL; //кнопка
	usl: BOOL; //условие
END_VAR
VAR_OUTPUT
	on: BOOL; //включена
END_VAR
VAR
	t_on: TON;
END_VAR
VAR RETAIN
	hours: INT;
END_VAR"""
FB_BODY = """t_on(IN:=(button AND usl), PT:=T#5S);
IF t_on.Q THEN
	on:=TRUE;
END_IF
IF Alarms_reset THEN
	on:=FALSE;
END_IF"""
MAIN_DECL = "PROGRAM Main\nVAR\nEND_VAR"
MAIN_BODY = """//Нория 2
Noria_2(button:=noria2_button, usl:=PUSK);
noria2_button_output:=noria2_button;
IF Noria_2.on THEN
	noria2_status:=1;
ELSE
	noria2_status:=0;
END_IF"""
BIND_DECL = "PROGRAM BIND\nVAR\nEND_VAR"
BIND_BODY = "PUSK:=x0_0;\ny0_0:=Noria_2.on;"

ENTRY = "{6198ad31-4b98-445c-927f-3258a0e82fe3}"
ST = "{3b83b776-fb25-43b8-99f2-3c507c9143fc}"
LD = "{32d3375e-c010-41e2-9e43-b2fbf4f2b374}"


def _entry(guid, name, type_guid, obj, parent="00000000-0000-0000-0000-000000000000"):
    return (f'<Single Type="{ENTRY}" Method="IArchivable"><Single Name="IsRoot" Type="bool">False</Single>'
            f'<Single Name="MetaObject" Type="{{81297157-7ec9-45ce-845e-84cab2b88ade}}" Method="IArchivable">'
            f'<Single Name="Guid" Type="System.Guid">{guid}</Single><Single Name="ParentGuid" Type="System.Guid">{parent}</Single>'
            f'<Single Name="Name" Type="string">{name}</Single><Single Name="TypeGuid" Type="System.Guid">{type_guid}</Single>'
            f'</Single>{obj}<Array Name="Path" Type="string"><Single Type="string">MyController</Single>'
            f'<Single Type="string">Application</Single></Array></Single>')


def _doc(part, text, impl=ST):
    typ = "{a9ed5b7e-75c5-4651-af16-d2c27e98cb94}" if part == "Interface" else impl
    return (f'<Single Name="{part}" Type="{typ}" Method="IArchivable"><Single Name="TextDocument" Method="IArchivable">'
            f'<Single Name="TextBlobForSerialisation" Type="string">{escape(text)}</Single></Single></Single>')


def _pou(guid, name, decl, body):
    return _entry(guid, name, plcme.T_POU, f'<Single Name="Object" Type="{{{plcme.T_POU}}}" Method="IArchivable">'
                  + _doc("Implementation", body) + _doc("Interface", decl) + "</Single>")


def _export(tmp_path, task_pous=("BIND", "Main"), gvl=GVL, main_body=MAIN_BODY, drawn=False) -> str:
    entries = [
        _entry("d1", "MyController", plcme.T_DEVICE, '<Single Name="Object" Method="IArchivable" />'),
        _entry("d2", "DI", plcme.T_DEVICE, '<Single Name="Object" Method="IArchivable" />', parent="d1"),
        _entry("a1", "Application", plcme.T_APPLICATION, '<Single Name="Object" Method="IArchivable" />', parent="d1"),
        _entry("g1", "GVL", plcme.T_GVL, f'<Single Name="Object" Type="{{{plcme.T_GVL}}}" Method="IArchivable">'
               + _doc("Interface", gvl) + "</Single>"),
        _entry("g2", "In_Out", plcme.T_GVL, f'<Single Name="Object" Type="{{{plcme.T_GVL}}}" Method="IArchivable">'
               + _doc("Interface", IO) + "</Single>"),
        _pou("p1", "Noria_class", FB_DECL, FB_BODY), _pou("p2", "Main", MAIN_DECL, main_body), _pou("p3", "BIND", BIND_DECL, BIND_BODY),
        _entry("t1", "MAST", plcme.T_TASK, f'<Single Name="Object" Type="{{{plcme.T_TASK}}}" Method="IArchivable">'
               '<Single Name="Kindoftask">Cyclic</Single><Single Name="Priority" Type="string">15</Single>'
               '<Single Name="Interval" Method="IArchivable"><Single Name="Time" Type="string">20</Single>'
               '<Single Name="Unit" Type="string">ms</Single></Single><List Name="PouList">'
               + "".join(f'<Single Method="IArchivable"><Single Name="Name" Type="string">{n}</Single></Single>' for n in task_pous)
               + "</List></Single>"),
    ]
    if drawn:
        entries.append(_entry("p4", "Instances", plcme.T_POU, f'<Single Name="Object" Type="{{{plcme.T_POU}}}" Method="IArchivable">'
                              f'<Single Name="Implementation" Type="{LD}" Method="IArchivable"><Single Name="Items">'
                              '<Single Name="Text" Type="string">Noria_6</Single><Single Name="Text" Type="string">noria2_button</Single>'
                              '</Single></Single>' + _doc("Interface", "PROGRAM Instances\nVAR\nEND_VAR") + "</Single>"))
    path = tmp_path / "line.export"
    path.write_text('<ExportFile><StructuredView Guid="{x}"><Single><List2 Name="EntryList">' + "".join(entries)
                    + "</List2></Single></StructuredView></ExportFile>", encoding="utf-8")
    return str(path)


def _files():
    return {str(p.relative_to(SEED)).replace(os.sep, "/"): p.read_bytes() for p in SEED.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def _server(calls):
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    lines += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
              for i, (n, a) in enumerate(calls)]
    out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(json.dumps(x) for x in lines) + "\n",
                         capture_output=True, text=True, timeout=60, encoding="utf-8").stdout.splitlines()
    return [json.loads(x) for x in out]


def test_the_tool_and_the_skill_pass_the_hub_check():
    meta, report = hub.check(_files())
    assert report["errors"] == [] and meta["name"] == "plc-kit" and meta["kind"] == "mcp"
    # the only remarks: the XML namespace names in the code look like addresses (the tool opens no connection)
    assert sorted(w["what"] for w in report["warnings"]) == ["внешний адрес www.3s-software.com", "внешний адрес www.plcopen.org",
                                                             "внешний адрес www.w3.org"]
    assert [t.strip() for t in meta["tools"].split(",")] == TOOLS
    assert not any(w in src for src in (f.read_text(encoding="utf-8") for f in SEED.glob("*.py"))
                   for w in ("import socket", "urllib", "http.client", "requests", "subprocess"))
    root = SEED.parent / "plc-project"
    files = {str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    meta, report = hub.check(files)
    assert report == {"errors": [], "warnings": []} and meta["name"] == "plc-project" and hub.doc_of(files) == "SKILL.md"
    body = files["SKILL.md"].decode("utf-8")
    assert all(x in body for x in ("plc-kit", "ebpro-cmtp", "plc_check", "plc_hmi_tags", "hub_propose"))


def test_declarations_addresses_and_modbus():
    d = plcst.parse_declaration(GVL)
    v = {x["name"]: x for x in d["vars"]}
    assert d["kind"] == "gvl" and len(d["vars"]) == 9
    assert v["PUSK"]["address"] == "%MX0.0" and v["PUSK"]["comment"] == "ПУСК" and v["PUSK"]["type"] == "BOOL"
    assert v["noria2_hand_mode"]["group"] == "Нория 2 Входы" and v["Noria_2"]["type"] == "Noria_class"
    fb = plcst.parse_declaration(FB_DECL)
    assert (fb["kind"], fb["name"]) == ("function_block", "Noria_class")
    assert [(x["name"], x["section"], x["qualifiers"]) for x in fb["vars"]][-1] == ("hours", "VAR", ["RETAIN"])
    two = plcst.parse_declaration("VAR\n a, b : ARRAY [0..9] OF INT := [10(0)]; (* two *)\nEND_VAR")["vars"]
    assert [x["name"] for x in two] == ["a", "b"] and two[0]["init"] == "[10(0)]" and plcst.base_type(two[0]["type"]) == "INT"
    # bits numbered by byte (Schneider): %MX a.b is bit (a mod 2) * 8 + b of register a div 2
    assert plcst.modbus("%MX1.2") == {"register": 0, "one_based": 1, "registers": 1, "bit": 10}
    assert plcst.modbus("%MX33.5")["register"] == 16 and plcst.modbus("%MX33.5")["bit"] == 13
    assert plcst.modbus("%MW624") == {"register": 624, "one_based": 625, "registers": 1}
    assert plcst.modbus("%MD400")["register"] == 800 and plcst.modbus("%MD400")["registers"] == 2
    assert plcst.modbus("%IX0.1") is None and plcst.parse_address("%MX0.8") is None
    # ... and by word (bit 0..15 of the word)
    assert plcst.modbus("%MX454.9", word_bits=True) == {"register": 454, "one_based": 455, "registers": 1, "bit": 9}


def test_the_st_check_names_what_is_wrong():
    assert plcst.check_body(FB_BODY) == [] and plcst.check_body(MAIN_BODY) == []
    bad = plcst.check_body("IF a THEN\n b:=1;\nFOR i:=0 TO 3 DO\n x:=(1\nEND_WHILE\ny = 3;")
    text = " | ".join(f"{i['line']}: {i['text']}" for i in bad)
    assert "END_WHILE закрывает FOR" in text and "IF не закрыт" in text and "скобка ( не закрыта" in text and "вместо «:=»" in text
    assert plcst.check_body("// IF without end in a comment\nx := 'END_IF';") == []
    names = [n for n, _, dot in plcst.identifiers("t(IN:=(a AND b), PT:=T#5S); c.Q := 16#FF; // zzz") if not dot]
    assert names == ["t", "a", "AND", "b", "c"]


def test_reads_a_machine_expert_export(tmp_path):
    p = plcme.Project.load(_export(tmp_path, drawn=True))
    info = p.info()
    assert (info["controller"], info["application"], info["pous"], info["st"], info["graphic"]) == ("MyController", "Application", 4, 3, 1)
    assert info["programs"] == 3 and info["function_blocks"] == 1 and info["gvls"] == ["GVL", "In_Out"]
    assert info["tasks"] == [{"name": "MAST", "kind": "Cyclic", "priority": "15", "interval": "20ms", "pous": ["BIND", "Main"]}]
    assert info["variables"] == 16 and info["addressed"] == 7 and info["modbus"] == 7 and info["bit_numbering"] == "byte"
    assert p.pou("main")["body"] == MAIN_BODY and p.pou("Noria_class")["declaration"] == FB_DECL
    drawn = p.pou("Instances")
    assert drawn["language"] == "graphic" and drawn["uses"] == ["Noria_6", "noria2_button"] and drawn["body"] == ""
    with pytest.raises(plcme.PlcError, match="закрытый формат"):
        plcme.Project.load(str(tmp_path / "x.project"))
    (tmp_path / "bad.export").write_text("<html/>", encoding="utf-8")
    with pytest.raises(plcme.PlcError, match="не экспорт"):
        plcme.Project.load(str(tmp_path / "bad.export"))


def test_addresses_conflicts_and_hmi_tags(tmp_path):
    p = plcme.Project.load(_export(tmp_path))
    a = p.addresses()
    assert a["ranges"]["M"]["count"] == 7 and a["ranges"]["M"]["last"] == "%MD400" and a["free"]["word"] == "%MW802"
    assert [(c["level"], c["a_address"], c["b_address"]) for c in a["conflicts"]] == [("warning", "%MX4.5", "%MX4.5")]
    tags = {t["name"]: t for t in p.hmi_tags()}
    assert tags["PUSK"]["weintek"] == {"type": "4x_Bit", "address": "100"} and tags["PUSK"]["kind"] == "bit"
    assert tags["noria2_hand_mode"]["weintek"] == {"type": "4x_Bit", "address": "110"}       # register 0, bit 10
    assert tags["noria2_status"]["weintek"] == {"type": "4x", "address": "625"} and tags["noria2_status"]["kind"] == "word"
    assert tags["noria2_speed"]["kind"] == "real" and tags["noria2_speed"]["register"] == 800
    # a word over the bits of its register is an error; bits 8..15 switch the whole project to numbering by word
    over = plcme.Project.load(_export(tmp_path, gvl=GVL.replace("END_VAR", "\tw0 AT %MW0:INT;\nEND_VAR")))
    assert any(c["level"] == "error" and "одну память" in c["text"] for c in over.addresses()["conflicts"])
    word = plcme.Project.load(_export(tmp_path, gvl="VAR_GLOBAL\n\ta AT %MX3.9:BOOL;\n\tb AT %MX3.1:BOOL;\nEND_VAR", main_body=""))
    assert word.info()["bit_numbering"] == "word" and [t["register"] for t in word.hmi_tags()] == [3, 3]
    assert [t["bit"] for t in word.hmi_tags()] == [1, 9]


def test_check_and_style(tmp_path):
    p = plcme.Project.load(_export(tmp_path))
    issues = p.check()
    assert [i for i in issues if i["level"] == "error"] == []
    assert [i["text"] for i in issues if i["level"] == "warning"] == ["один адрес %MX4.5: noria2_button и noria2_button_output"]
    broken = plcme.Project.load(_export(tmp_path, task_pous=("Main", "Trier1"),
                                        main_body=MAIN_BODY.replace("END_IF", "").replace("PUSK);", "PUSK);\nghost := 1;")))
    text = " | ".join(f"{i['where']}: {i['text']}" for i in broken.check() if i["level"] != "info")
    assert "задача MAST: вызывает Trier1" in text and "Main: IF не закрыт" in text and "Main: имя ghost нигде не объявлено" in text
    st = p.style()
    cls = st["classes"][0]
    assert (cls["name"], cls["inputs"], cls["outputs"], cls["timers"], cls["retain"]) == ("Noria_class", ["button", "usl"], ["on"], ["t_on"], ["hours"])
    assert cls["instances"] == ["Noria_2", "Noria_6"]
    roles = {x["name"]: x["role"] for x in st["programs"]}
    assert roles == {"Main": "вызовы блоков механизмов", "BIND": "привязка входов и выходов к именам"}
    assert st["naming"]["case"] == "snake_case" and st["naming"]["class_suffix"] == "_class" and st["naming"]["io_names"] == ["xN_N", "yN_N"]
    assert st["naming"]["comments"]["russian"] and st["address_conflicts"] == 1
    md = plcme.style_markdown(st)
    assert "# Стиль проекта ПЛК «line»" in md and "| `Noria_class` | 2 | 1 | 1 | 1 |" in md and "Нория 2 Входы" in md


def test_the_st_folder_and_plcopen_keep_every_character(tmp_path):
    p = plcme.Project.load(_export(tmp_path, drawn=True))
    files = plcbuild.write_dir(p, str(tmp_path / "st"))
    assert sorted(os.path.basename(f) for f in files) == ["BIND.st", "GVL.st", "In_Out.st", "Instances.st", "Main.st", "Noria_class.st", "project.json"]
    (tmp_path / "st" / "Instances.st").unlink()                 # a drawn POU is only a note in the folder
    back = plcbuild.load_dir(str(tmp_path / "st"))

    def sig(x):
        return (sorted((u["name"], u["kind"], u["declaration"], u["body"]) for u in x.pous if u["language"] == "st"),
                sorted((g["name"], g["declaration"]) for g in x.gvls), [(t["name"], t["interval"], t["pous"]) for t in x.tasks])
    assert sig(back) == sig(p) and back.source == "st-folder" and back.controller == "MyController"
    blob, skipped = plcbuild.to_plcopen(p)
    assert skipped == ["Instances"] and blob.startswith(b'<?xml version="1.0" encoding="utf-8"?>')
    (tmp_path / "out.xml").write_bytes(blob)
    again = plcme.Project.load(str(tmp_path / "out.xml"))
    assert sig(again) == sig(p) and again.source == "plcopen"
    assert [(v["name"], v["address"], v["type"]) for v in again.variables()] == [(v["name"], v["address"], v["type"]) for v in back.variables()]
    text = blob.decode("utf-8")
    assert '<variable name="PUSK" address="%MX0.0">' in text and '<task name="MAST" interval="PT0.02S" priority="15">' in text
    assert 'pouType="functionBlock"' in text and "plcopenxml/globalvars" in text and "interfaceasplaintext" in text
    # PLCopen written by another tool (no plain-text declarations): the declaration is rebuilt from the elements
    plain = text.replace("interfaceasplaintext", "other")
    (tmp_path / "plain.xml").write_text(plain, encoding="utf-8")
    rebuilt = plcme.Project.load(str(tmp_path / "plain.xml"))
    assert {v["name"]: (v["address"], v["type"], v["comment"]) for v in rebuilt.variables()}["noria2_status"] == ("%MW624", "INT", "статус")
    assert rebuilt.pou("Noria_class")["declaration"].startswith("FUNCTION_BLOCK Noria_class\nVAR_INPUT")


def test_a_project_written_from_nothing_builds(tmp_path):
    src = tmp_path / "plc"
    src.mkdir()
    (src / "project.json").write_text(json.dumps({"name": "New", "controller": "TM241", "tasks": [
        {"name": "MAST", "interval": "10ms", "pous": ["Main"]}]}), encoding="utf-8")
    (src / "GVL.st").write_text("VAR_GLOBAL\n\tstart AT %MX0.0 : BOOL; // пуск\n\tspeed AT %MW10 : INT := 5;\nEND_VAR\n", encoding="utf-8")
    (src / "Motor.st").write_text("TYPE Motor :\nSTRUCT\n\ton : BOOL;\n\thours : DINT;\nEND_STRUCT\nEND_TYPE\n", encoding="utf-8")
    (src / "Scale.st").write_text("FUNCTION Scale : REAL\nVAR_INPUT\n\traw : INT;\nEND_VAR\nScale := INT_TO_REAL(raw) / 10.0;\n", encoding="utf-8")
    (src / "Main.st").write_text("PROGRAM Main\nVAR\n\tm : Motor;\n\tv : REAL;\nEND_VAR\nm.on := start;\nv := Scale(raw := speed);\n", encoding="utf-8")
    p = plcbuild.load_dir(str(src))
    assert [(x["name"], x["kind"]) for x in p.pous] == [("Main", "program"), ("Scale", "function")] and [t["name"] for t in p.types] == ["Motor"]
    assert p.pou("Scale")["body"] == "Scale := INT_TO_REAL(raw) / 10.0;" and p.pou("Scale")["returns"] == "REAL"
    issues = [i for i in p.check() if i["level"] != "info"]
    assert [i["text"] for i in issues] == ["имя INT_TO_REAL нигде не объявлено (ни в POU, ни в GVL)"]       # a library function
    blob, skipped = plcbuild.to_plcopen(p)
    text = blob.decode("utf-8")
    assert skipped == [] and '<dataType name="Motor">' in text and "<struct>" in text and "<returnType>" in text
    assert '<task name="MAST" interval="PT0.01S"' in text and '<simpleValue value="5" />' in text
    (src / "Bad.st").write_text("x := 1;\n", encoding="utf-8")
    with pytest.raises(plcme.PlcError, match="Bad.st: файл ST должен начинаться"):
        plcbuild.load_dir(str(src))


def test_server_reads_checks_and_builds(tmp_path):
    exp = _export(tmp_path)
    out = tmp_path / "out"
    replies = _server([
        ("plc_info", {"path": exp}), ("plc_pous", {"path": exp}), ("plc_pou", {"path": exp, "name": "Main"}),
        ("plc_variables", {"path": exp, "filter": "noria2", "addressed": True}), ("plc_addresses", {"path": exp}),
        ("plc_hmi_tags", {"path": exp, "filter": "нория 2", "out_file": str(out / "tags.csv")}), ("plc_check", {"path": exp}),
        ("plc_style", {"path": exp, "out_file": str(out / "style.md")}), ("plc_export_st", {"path": exp, "out_dir": str(out / "st")}),
        ("plc_build", {"src_dir": str(out / "st"), "out_file": str(out / "plc.xml")}), ("plc_info", {"path": str(out / "st")}),
        ("plc_info", {"path": str(tmp_path / "x.project")}), ("plc_pou", {"path": exp, "name": "Nope"}),
    ])
    assert replies[0]["result"]["serverInfo"] == {"name": "plc-kit", "title": "ПЛК: Machine Expert, ST", "version": "1.0.0"}
    assert [t["name"] for t in replies[1]["result"]["tools"]] == TOOLS
    r = [x["result"] for x in replies[2:]]
    assert json.loads(r[0]["content"][0]["text"])["pous"] == 3
    assert [x["name"] for x in json.loads(r[1]["content"][0]["text"])["pous"]] == ["Noria_class", "Main", "BIND"]
    assert r[2]["content"][0]["text"] == MAIN_DECL + "\n\n" + MAIN_BODY
    assert json.loads(r[3]["content"][0]["text"])["count"] == 5
    assert json.loads(r[4]["content"][0]["text"])["free"]["word"] == "%MW802"
    tags = json.loads(r[5]["content"][0]["text"])
    assert [t["name"] for t in tags["tags"]] == ["noria2_hand_mode", "noria2_button", "noria2_button_output", "noria2_status", "noria2_speed"]
    assert (out / "tags.csv").read_text(encoding="utf-8-sig").splitlines()[1] == "noria2_hand_mode;BOOL;bit;%MX1.2;0;10;4x_Bit;110;Нория 2 Входы;ручной режим"
    assert json.loads(r[6]["content"][0]["text"])["summary"] == {"error": 0, "warning": 1, "info": 0}
    assert (out / "style.md").read_text(encoding="utf-8").startswith("# Стиль проекта ПЛК «line»") and r[7]["structuredContent"]["classes"]
    assert len(r[8]["structuredContent"]["files"]) == 6
    assert r[9]["structuredContent"]["written"] and "Импорт PLCopenXML" in r[9]["content"][0]["text"]
    assert plcme.Project.load(str(out / "plc.xml")).pou("Main")["body"] == MAIN_BODY
    assert json.loads(r[10]["content"][0]["text"])["source"] == "st-folder"
    assert r[11]["isError"] and "закрытый формат" in r[11]["content"][0]["text"]
    assert r[12]["isError"] and "нет POU «Nope»" in r[12]["content"][0]["text"]
    # a broken project is not written unless forced
    (out / "st" / "Main.st").write_text(MAIN_DECL + "\n\nIF PUSK THEN\n\tnoria2_status := 1;\n", encoding="utf-8")
    again = _server([("plc_build", {"src_dir": str(out / "st"), "out_file": str(out / "nope.xml")}),
                     ("plc_build", {"src_dir": str(out / "st"), "out_file": str(out / "bad.xml"), "force": True})])
    assert again[2]["result"]["isError"] and "IF не закрыт" in again[2]["result"]["content"][0]["text"] and not (out / "nope.xml").exists()
    assert again[3]["result"]["structuredContent"]["written"] and (out / "bad.xml").exists()
