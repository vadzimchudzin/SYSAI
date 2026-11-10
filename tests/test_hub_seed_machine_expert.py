"""hub/seed/machine-expert — the tool that drives EcoStruxure Machine Expert stays valid for SYSAI_Hub and its chain
works: the folder of ST files → a job → the script inside the program → the result with compiler messages put on the
files and lines of the folder.

There is no Machine Expert here (it is a Windows program): `tests/fake_machine_expert.py` stands in for it and runs
the very script the program would run, giving it scripting objects shaped by the documented interface. So these tests
prove the tool's own logic — what it asks of Machine Expert and how it reads the answers — not Machine Expert."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from app.net import hub

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "hub" / "seed" / "machine-expert"
FAKE = Path(__file__).resolve().parent / "fake_machine_expert.py"
sys.path.append(str(SEED))          # at the end: other seed tools have a `server` module of their own
import mehost  # noqa: E402
import mesrc  # noqa: E402

TOOLS = ["me_find", "me_devices", "me_create", "me_update", "me_build", "me_export", "me_scan", "me_online", "me_open"]
GVL = "{attribute 'symbol' := 'readwrite'}\nVAR_GLOBAL\n\t//Нория 1\n\tStart_1: BOOL; //пуск\n\tSP_1: INT := 40;\n\tNoria_1: Noria;\nEND_VAR"
FB = "FUNCTION_BLOCK Noria\nVAR_INPUT\n\trun: BOOL;\nEND_VAR\nVAR_OUTPUT\n\ton: BOOL;\nEND_VAR\n\non := run;"
MAIN = "PROGRAM Main\nVAR\n\tstep: INT;\nEND_VAR\n\nNoria_1(run := Start_1);\nstep := step + 1;"
BAD_MAIN = "PROGRAM Main\nVAR\n\tstep: INT;\nEND_VAR\n\nNoria_1(run := Start_1);\nstep := @@nope;\n// TODO"
MOTOR = "TYPE Motor :\nSTRUCT\n\tspeed: REAL;\nEND_STRUCT\nEND_TYPE"
SCALE = "FUNCTION Scale : REAL\nVAR_INPUT\n\traw: INT;\nEND_VAR\n\nScale := INT_TO_REAL(raw) / 10.0;"


def _files() -> dict:
    return {p.relative_to(SEED).as_posix(): p.read_bytes() for p in SEED.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def _src(tmp_path, main=MAIN, link=True) -> Path:
    src = tmp_path / "plc"
    src.mkdir(exist_ok=True)
    for name, text in (("GVL", GVL), ("Noria", FB), ("Main", main), ("Motor", MOTOR), ("Scale", SCALE)):
        (src / f"{name}.st").write_text(text + "\n", encoding="utf-8")
    meta = {"name": "Line", "controller": "TM241CE40R", "tasks": [{"name": "MAST", "interval": "20ms", "priority": 15, "pous": ["Main"]}]}
    if link:
        meta["link"] = {"kind": "opcua", "lists": ["GVL"]}
    (src / "project.json").write_text(json.dumps(meta), encoding="utf-8")
    return src


def _env(tmp_path, **more) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", SYSAI_ME_WORK=str(tmp_path / "work"),
               SYSAI_ME_CMD=f'"{sys.executable}" "{FAKE}" "{{script}}"')
    env.pop("SYSAI_ME_EXE", None)
    env.update(more)
    return env


def _server(tmp_path, calls, **env) -> list:
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
    lines += [json.dumps({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}},
                         ensure_ascii=False) for i, (n, a) in enumerate(calls)]
    out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(lines) + "\n", env=_env(tmp_path, **env),
                         capture_output=True, text=True, timeout=180, encoding="utf-8").stdout.splitlines()
    return [json.loads(x)["result"] for x in out][2:]


def test_the_tool_passes_the_hub_check():
    meta, report = hub.check(_files())
    assert report["errors"] == [] and meta["name"] == "machine-expert" and meta["kind"] == "mcp"
    assert [t.strip() for t in meta["tools"].split(",")] == TOOLS
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
    out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(lines) + "\n", capture_output=True,
                         text=True, timeout=60, encoding="utf-8").stdout.splitlines()
    assert json.loads(out[0])["result"]["serverInfo"]["name"] == "machine-expert"
    assert [t["name"] for t in json.loads(out[1])["result"]["tools"]] == TOOLS


def test_the_job_script_stays_valid_for_the_python_of_machine_expert():
    """Machine Expert runs scripts on IronPython 2.7: nothing of Python 3 only may get into me_job.py."""
    text = (SEED / "me_job.py").read_text(encoding="utf-8")
    code = re.sub(r"'''.*?'''|\"\"\".*?\"\"\"|#[^\n]*", "", text, flags=re.S)
    assert not re.search(r"\b[fF][rR]?['\"]", code), "f-strings"
    for word in ("nonlocal ", ":=", "async ", "await ", "yield from", "print >>", " -> "):
        assert word not in code, word
    assert not re.search(r"^\s*def \w+\([^)]*\*,", code, re.M), "keyword-only arguments"
    assert "from __future__ import print_function" in text and "PromptHandling.None" not in code


def test_source_folder_is_read_and_messages_are_placed(tmp_path):
    src = mesrc.load_dir(str(_src(tmp_path)))
    assert [(o["name"], o["kind"]) for o in src["objects"]] == [("Motor", "type"), ("GVL", "gvl"), ("Scale", "function"),
                                                                ("Noria", "function_block"), ("Main", "program")]
    main = src["objects"][-1]
    assert main["declaration"] == "PROGRAM Main\nVAR\n\tstep: INT;\nEND_VAR" and main["body_line"] == 6
    assert main["body"].splitlines()[0] == "Noria_1(run := Start_1);" and src["objects"][2]["returns"] == "REAL"
    assert src["link"] == {"kind": "opcua", "lists": ["GVL"]} and src["tasks"][0]["pous"] == ["Main"]
    placed = mesrc.place(src, {"object": "main", "line": 2, "part": "body", "text": "x"})
    assert (placed["file"], placed["file_line"]) == ("Main.st", 7)
    assert mesrc.place(src, {"object": "GVL", "line": 4, "part": "declaration", "text": "x"})["file_line"] == 4
    assert "file" not in mesrc.place(src, {"object": "Elsewhere", "line": 1, "part": "body", "text": "x"})


def test_create_fix_update_export(tmp_path):
    src = _src(tmp_path, BAD_MAIN)
    project = tmp_path / "out" / "Line.project"
    r = _server(tmp_path, [("me_find", {}), ("me_devices", {"filter": "TM241CE40"}),
                           ("me_create", {"src_dir": str(src), "out_file": str(project)}),
                           ("me_create", {"src_dir": str(src), "out_file": str(project)}),
                           ("me_create", {"src_dir": str(src), "out_file": str(tmp_path / "x.txt")}),
                           ("me_build", {"file": str(project), "src_dir": str(src)})])
    assert "не найден" in r[0]["content"][0]["text"]          # nothing is installed here; the command line is given
    assert [d["version"] for d in r[1]["structuredContent"]["devices"]] == ["1.2.3.4", "5.0.8.2"]
    made = r[2]["structuredContent"]
    assert made["from"] == "контроллер TM241CE40R, версия 5.0.8.2" and made["controller"] == "MyController"
    assert made["objects"]["created"] == ["Motor", "GVL", "Scale", "Noria", "Main"] and made["tasks"] == ["MAST"]
    assert made["symbols"] == "создана" and not made["build"]["ok"]
    err = made["build"]["errors"][0]
    assert (err["file"], err["file_line"], err["code"], err["part"]) == ("Main.st", 7, "C0046", "body")
    assert made["build"]["warnings"][0]["file_line"] == 8
    text = r[2]["content"][0]["text"]
    assert "ОШИБКА Main.st:7 — C0046: Identifier 'step := @@nope' not defined" in text and "me_update" in text
    assert r[3]["isError"] and "уже есть" in r[3]["content"][0]["text"]
    assert r[4]["isError"] and ".project" in r[4]["content"][0]["text"]
    assert r[5]["structuredContent"]["build"]["counts"] == {"error": 1, "warning": 1}
    # the agent fixes the file and updates the project: only Main changes, the build is clean
    (src / "Main.st").write_text(MAIN + "\n", encoding="utf-8")
    out_dir = tmp_path / "back"
    r = _server(tmp_path, [("me_update", {"file": str(project), "src_dir": str(src)}),
                           ("me_export", {"file": str(project), "out_dir": str(out_dir)}),
                           ("me_export", {"file": str(project), "out_file": str(tmp_path / "plc.xml")}),
                           ("me_export", {"file": str(project)}),
                           ("me_update", {"file": str(tmp_path / "none.project"), "src_dir": str(src)})])
    upd = r[0]["structuredContent"]
    assert upd["objects"] == {"created": [], "updated": ["Main"], "same": ["Motor", "GVL", "Scale", "Noria"], "extra": []}
    assert upd["build"]["ok"] and upd["symbols"] == "есть" and "Ошибок нет" in r[0]["content"][0]["text"]
    assert sorted(r[1]["structuredContent"]["files"]) == ["GVL.st", "Main.st", "Motor.st", "Noria.st", "Scale.st"]
    for name, text in (("GVL", GVL), ("Noria", FB), ("Main", MAIN), ("Motor", MOTOR), ("Scale", SCALE)):
        assert (out_dir / f"{name}.st").read_text(encoding="utf-8") == text + "\n"         # the round trip keeps the text
    back = json.loads((out_dir / "project.json").read_text(encoding="utf-8"))
    assert back["tasks"] == [{"name": "MAST", "interval": "20ms", "priority": "15", "pous": ["Main"]}]
    assert (tmp_path / "plc.xml").read_text(encoding="utf-8").startswith("<?xml") and "plc-kit" in r[2]["content"][0]["text"]
    assert r[3]["isError"] and "out_dir" in r[3]["content"][0]["text"]
    assert r[4]["isError"] and "нет файла проекта" in r[4]["content"][0]["text"]


def test_template_keeps_what_the_engineer_configured(tmp_path):
    src = _src(tmp_path, link=False)
    template = tmp_path / "template.project"
    r = _server(tmp_path, [("me_create", {"src_dir": str(src), "out_file": str(template)})])
    assert r[0]["structuredContent"]["build"]["ok"] and "symbols" not in r[0]["structuredContent"]
    (src / "Extra.st").write_text("PROGRAM Extra\nVAR\nEND_VAR\n\n;\n", encoding="utf-8")
    (src / "Noria.st").unlink()
    (src / "GVL.st").write_text("VAR_GLOBAL\n\tStart_1: BOOL;\nEND_VAR\n", encoding="utf-8")
    (src / "Main.st").write_text("PROGRAM Main\nVAR\nEND_VAR\n\n;\n", encoding="utf-8")
    out = tmp_path / "new.project"
    r = _server(tmp_path, [("me_create", {"src_dir": str(src), "out_file": str(out), "template_file": str(template)}),
                           ("me_create", {"src_dir": str(src), "out_file": str(out), "controller": "ПЛК-которого-нет", "overwrite": True})])
    made = r[0]["structuredContent"]
    assert made["from"].startswith("шаблон") and made["objects"]["created"] == ["Extra"]
    assert made["objects"]["updated"] == ["GVL", "Main"] and made["objects"]["extra"] == ["Noria"]      # kept, and told
    assert "которых нет в папке ST: Noria" in r[0]["content"][0]["text"]
    assert r[1]["isError"] and "нет в репозитории устройств" in r[1]["content"][0]["text"]


def test_online_simulation_and_the_real_controller(tmp_path):
    src = _src(tmp_path)
    project = tmp_path / "Line.project"
    r = _server(tmp_path, [
        ("me_create", {"src_dir": str(src), "out_file": str(project)}),
        ("me_online", {"file": str(project), "simulation": True, "start": True, "write": {"GVL.SP_1": 55, "GVL.Nope": 1},
                       "read": ["GVL.Start_1", "GVL.SP_1", "GVL.Missing"]}),
        ("me_scan", {}),
        ("me_online", {"file": str(project), "download": True, "ip": "192.168.1.10"}),
        ("me_online", {"file": str(project), "write": {"GVL.SP_1": 1}}),
        ("me_online", {"file": str(project), "ip": "192.168.1.10", "read": ["GVL.SP_1"], "user": "Administrator", "password": "secret"}),
        ("me_online", {"file": str(project), "ip": "10.0.0.99", "read": ["GVL.SP_1"]}),
        ("me_online", {"file": str(project), "download": True, "start": True, "node": "M241-Line1", "confirm": True}),
    ])
    sim = r[1]["structuredContent"]
    assert sim["target"] == "симуляция" and sim["logged_in"] and sim["downloaded"] and sim["state"] == "run"
    assert sim["values"]["GVL.SP_1"] == {"ok": True, "value": "55"} and sim["values"]["GVL.Start_1"]["value"] == "FALSE"
    assert not sim["values"]["GVL.Missing"]["ok"] and not sim["written"]["GVL.Nope"]["ok"]
    assert "НЕТ GVL.Missing" in r[1]["content"][0]["text"] and "Прочитано: 2 из 3." in r[1]["content"][0]["text"]
    assert r[2]["structuredContent"]["controllers"] == [{"name": "M241-Line1", "type": "TM241CE40R", "vendor": "Schneider Electric",
                                                         "address": "0301.A0C3"}]
    for refused in (r[3], r[4]):        # the real controller is not changed without the engineer's word
        assert refused["isError"] and "confirm" in refused["content"][0]["text"]
    read = r[5]["structuredContent"]
    assert read["logged_in"] and not read["downloaded"] and read["values"] == {"GVL.SP_1": {"ok": True, "value": "40"}}
    assert r[6]["isError"] and "вход в контроллер не удался" in r[6]["content"][0]["text"]
    real = r[7]["structuredContent"]
    assert real["downloaded"] and real["state"] == "run" and "M241-Line1" in real["target"]
    # the password given for the controller is not left in the job folder
    jobs = list((tmp_path / "work" / "jobs").rglob("job.json"))
    assert jobs and not any("secret" in p.read_text(encoding="utf-8") for p in jobs)


def test_no_machine_expert_and_a_program_that_does_not_answer(tmp_path, monkeypatch):
    src = _src(tmp_path)
    args = {"src_dir": str(src), "out_file": str(tmp_path / "Line.project")}
    r = _server(tmp_path, [("me_create", args)], SYSAI_ME_CMD="", ProgramFiles=str(tmp_path / "pf"),
                **{"ProgramFiles(x86)": str(tmp_path / "pf"), "ProgramW6432": str(tmp_path / "pf")})
    assert r[0]["isError"] and "не найден" in r[0]["content"][0]["text"] and "SYSAI_ME_EXE" in r[0]["content"][0]["text"]
    r = _server(tmp_path, [("me_create", args)], FAKE_ME_REFUSE="1")
    text = r[0]["content"][0]["text"]
    assert r[0]["isError"] and "не выполнил скрипт" in text and "закрылся с кодом 3" in text and "SYSAI_ME_CMD" in text
    # installations are found by their folders, the console for scripts first
    pf = tmp_path / "Program Files"
    home = pf / "Schneider Electric" / "EcoStruxure Machine Expert" / "V2.2"
    (home / "Profiles").mkdir(parents=True)
    for name in ("LogicBuilder.exe", "LogicBuilderShell.exe"):
        (home / name).write_bytes(b"MZ")
    (home / "Profiles" / "EcoStruxure Machine Expert V2.2.profile").write_text("", encoding="utf-8")
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.setenv(key, str(pf))
    monkeypatch.delenv("SYSAI_ME_EXE", raising=False)
    monkeypatch.delenv("SYSAI_ME_CMD", raising=False)
    real_glob = mehost.glob.glob
    monkeypatch.setattr(mehost.glob, "glob", lambda pattern: real_glob(pattern.replace("\\", "/")))      # not Windows here
    found = mehost.installs()
    assert [(i["kind"], i["version"], i["profile"]) for i in found] == [
        ("shell", "V2.2", "EcoStruxure Machine Expert V2.2"), ("logicbuilder", "V2.2", "EcoStruxure Machine Expert V2.2")]
    cmds = mehost.commands("job.py")
    assert cmds[0][1][1:] == ["job.py"]
    assert cmds[1][1][1:] == ["--profile=EcoStruxure Machine Expert V2.2", "--noUI", "--runscript=job.py"]
