"""hub/seed/plc-kit — the program of a controller made from the specification by code (plan П3.5): the same
specification gives the same text, the text passes the check, becomes a `.project` and reads back; files of one's
own are never overwritten. Whether Machine Expert compiles it is checked on a PC with Machine Expert, not here."""
import json
import sys
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "plc-kit"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "me_engine_samples.project"
sys.path.append(str(SEED))
import plcbuild  # noqa: E402
import plcgen  # noqa: E402
import plcproj  # noqa: E402
import plcspec  # noqa: E402


def _spec(link="opcua"):
    return {
        "project": {"name": "Line", "controller": "TM241CE40T", "panel": "cMT3072X", "link": link},
        "expected": {"mechanisms": 3, "signals": 11},
        "types": {"noria": {"title": "Нория", "in": ["speed", "belt", "local_stop"], "out": ["run"], "optional": ["ready"],
                            "faults": ["belt", "local_stop", "!ready"], "pulse": ["speed"]},
                  "conveyor": {"title": "Конвейер", "in": ["speed"], "out": ["run", "lamp"], "pulse": ["speed"]},
                  "fan": {"title": "Вентилятор", "in": ["ok"], "out": ["run"], "faults": ["!ok"]}},
        "mechanisms": [
            {"id": "N1", "type": "noria", "title": "Нория 1", "src": "Э3 лист 7", "params": {"start_delay_s": 5, "speed_timeout_s": 8},
             "in": {"speed": "DI.I0", "belt": "!DI.I1", "local_stop": "!DI.I2", "ready": "Module_5.I12"}, "out": {"run": "DQ.Q0"},
             "interlocks": {"requires_running": ["F1"]}},
            {"id": "C1", "type": "conveyor", "title": "Конвейер 1", "src": "Э3 лист 8", "in": {"speed": "DI.I3"},
             "out": {"run": "DQ.Q1", "lamp": "DQ.Q2"}},
            {"id": "F1", "type": "fan", "title": "Вентилятор аспирации", "src": "Э3 лист 9", "in": {"ok": "DI.I4"}, "out": {"run": "DQ.Q3"}}],
        "signals": [{"id": "EStop", "title": "Аварийный стоп", "dir": "in", "channel": "!DI.I5", "role": "estop", "src": "Э3 лист 2"}],
        "routes": [{"id": "R1", "title": "Приём", "mechanisms": ["C1", "N1"]}],
    }


def _write(tmp_path, spec):
    file = tmp_path / "_work" / "spec" / "project.spec.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return file, tmp_path / "_work" / "st"


def test_the_same_specification_gives_the_same_program_and_it_passes_the_check(tmp_path):
    assert plcspec.check(_spec())["summary"]["complete"]
    file, out = _write(tmp_path, _spec())
    rep = plcgen.generate(str(file), str(out))
    assert rep["written"] == ["BIND.st", "BIND_OUT.st", "FB_Mech.st", "FB_Pulse.st", "HMI.st", "IO.st", "Mech.st", "Mechs.st", "Routes.st",
                              "Signals.st", "project.json"]
    assert (rep["mechanisms"], rep["routes"], rep["channels"]) == (3, 1, 11) and rep["outputs_nothing_drives"] == ["C1_lamp"]
    assert rep["tasks"] == ["BIND", "Routes", "Mech", "BIND_OUT"] and rep["signals_without_channel"] == []
    assert any("F1 в этом маршруте нет" in n for n in rep["notes"])       # N1 waits for a fan the route does not start
    texts = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}
    again = plcgen.render(_spec())
    assert all(texts[name] == text for name, text in again["files"].items())  # byte for byte
    assert all(t.startswith(plcgen.MARK) for n, t in texts.items() if n.endswith(".st"))
    issues = plcbuild.load_dir(str(out)).check()
    assert [i for i in issues if i["level"] != "info"] == []                    # no error and no warning of the check

    bind, mech, routes, hmi = texts["BIND.st"], texts["Mech.st"], texts["Routes.st"], texts["HMI.st"]
    assert "N1_belt := NOT IO.x_DI_I1;" in bind and "N1_speed := IO.x_DI_I0;" in bind and "EStop := NOT IO.x_DI_I5;" in bind
    assert "IO.y_DQ_Q0 := N1_run;" in texts["BIND_OUT.st"]
    assert "STOP_ALL := EStop;" in mech
    assert "N1_fault := N1_belt OR N1_local_stop OR NOT N1_ready OR N1_speed_mon.fault;" in mech
    assert "timeout := T#8000ms" in mech and "C1_speed_mon(run := C1_on, pulse := C1_speed, timeout := T#5000ms);" in mech
    assert "N1_permit := NOT STOP_ALL AND F1_on;" in mech and "N1_run := N1_on;" in mech
    # the route C1 → N1: the last of the flow starts first, the first stops first
    assert "R1_N1_w := R1_active OR (NOT R1_active AND N1_on AND R1_N1_down.Q);" in routes
    assert "R1_C1_up(IN := R1_active AND N1_on, PT := T#3000ms);" in routes and "R1_C1_w := R1_C1_up.Q;" in routes
    assert "F1_want := FALSE;" in routes
    assert hmi.splitlines()[1] == "{attribute 'symbol' := 'readwrite'}" and "N1_state: INT;" in hmi and " AT %" not in hmi
    assert json.loads(texts["project.json"])["link"] == {"kind": "opcua", "lists": ["HMI"]}
    assert rep["io_map"][0] == {"device": "DI", "channel": "I0", "variable": "IO.x_DI_I0"} and len(rep["io_map"]) == 11


def test_modbus_gets_addresses_bits_first_and_words_after_them(tmp_path):
    file, out = _write(tmp_path, _spec("modbus_tcp"))
    plcgen.generate(str(file), str(out))
    hmi = (out / "HMI.st").read_text(encoding="utf-8")
    assert "Alarms_reset AT %MX0.0: BOOL;" in hmi and "R1_active AT %MX2.2: BOOL;" in hmi and "attribute 'symbol'" not in hmi
    assert "N1_state AT %MW2: INT;" in hmi and "F1_state AT %MW4: INT;" in hmi   # 19 bits fill registers 0–1; words from 2
    p = plcbuild.load_dir(str(out))
    assert [i for i in p.check() if i["level"] != "info"] == [] and p.addresses()["conflicts"] == []
    assert json.loads((out / "project.json").read_text(encoding="utf-8"))["link"] == {"kind": "modbus_tcp"}


def test_files_of_ones_own_stay_and_a_block_of_ones_own_is_used(tmp_path):
    spec = _spec()
    spec["types"]["fan"].update(fb="Fan_class", pins={"AZ": "ok", "usl": "@permit", "vkl": "@on", "avar": "@alarm", "sbros": "@reset",
                                                      "pusk": "=F1_cmd_start OR {id}_want"})
    spec["project"]["programs"] = ["Custom"]
    file, out = _write(tmp_path, spec)
    out.mkdir(parents=True)
    with pytest.raises(plcgen.GenError, match="блок Fan_class .* не найден"):
        plcgen.generate(str(file), str(out))
    (out / "Fan_class.st").write_text("FUNCTION_BLOCK Fan_class\nVAR_INPUT\n\tAZ: BOOL;\n\tusl: BOOL;\n\tpusk: BOOL;\n\tsbros: BOOL;\nEND_VAR\n"
                                      "VAR_OUTPUT\n\tvkl: BOOL;\n\tavar: BOOL;\nEND_VAR\n\nIF sbros THEN\n\tavar := FALSE;\nEND_IF\n"
                                      "IF NOT AZ THEN\n\tavar := TRUE;\nEND_IF\nvkl := pusk AND usl AND NOT avar;\n", encoding="utf-8")
    (out / "Custom.st").write_text("PROGRAM Custom\n\nC1_lamp := C1_on;\n", encoding="utf-8")
    rep = plcgen.generate(str(file), str(out))
    mech = (out / "Mech.st").read_text(encoding="utf-8")
    assert "F1(AZ := F1_ok,\n\tusl := F1_permit,\n\tvkl => F1_on,\n\tavar => F1_alarm,\n\tsbros := Alarms_reset,\n\tpusk := F1_cmd_start OR F1_want);" in mech
    assert "F1: Fan_class;" in (out / "Mechs.st").read_text(encoding="utf-8")
    assert rep["tasks"] == ["BIND", "Routes", "Mech", "Custom", "BIND_OUT"]
    assert (out / "Custom.st").read_text(encoding="utf-8").startswith("PROGRAM Custom")          # not touched
    assert [i for i in plcbuild.load_dir(str(out)).check() if i["level"] == "error"] == []
    spec["types"]["fan"]["pins"]["нет_такого"] = "ok"
    file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(plcgen.GenError, match="нет входа или выхода «нет_такого»"):
        plcgen.generate(str(file), str(out))
    # a file with a name of the generator written by hand stops the run; nothing of it is lost
    (out / "Routes.st").write_text("PROGRAM Routes\n\n; // мой\n", encoding="utf-8")
    del spec["types"]["fan"]["pins"]["нет_такого"]
    file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(plcgen.GenError, match="Routes.st .* написан не генератором"):
        plcgen.generate(str(file), str(out))
    assert "мой" in (out / "Routes.st").read_text(encoding="utf-8")
    (out / "Routes.st").unlink()
    # no pulse sensor any more: the block the generator made for it goes, the files of one's own stay
    for t in spec["types"].values():
        t.pop("pulse", None)
    file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    rep = plcgen.generate(str(file), str(out))
    assert rep["removed"] == ["FB_Pulse.st"] and (out / "Fan_class.st").is_file() and (out / "Custom.st").is_file()


def test_an_incomplete_specification_is_not_generated_from(tmp_path):
    spec = _spec()
    spec["mechanisms"][0]["in"].pop("belt")
    file, out = _write(tmp_path, spec)
    with pytest.raises(plcgen.GenError, match="plc_spec_check"):
        plcgen.generate(str(file), str(out))
    assert not out.exists() or not list(out.glob("*.st"))
    spec = _spec()
    spec["routes"] = [{"id": "R1", "mechanisms": ["C1", "X9"]}]
    assert any("X9" in i["text"] for i in plcspec.check(spec)["issues"])
    spec["types"]["fan"]["faults"] = ["!нет"]
    assert any("сигнала «нет» у вида нет" in i["text"] for i in plcspec.check(spec)["issues"])


def test_the_generated_program_becomes_a_project_file_and_its_channels_are_mapped(tmp_path):
    """The whole way without a model: specification → ST → .project on a template → channels; read back and checked."""
    import test_hub_seed_plc_project as base
    file, out = _write(tmp_path, _spec())
    rep = plcgen.generate(str(file), str(out))
    template = base._template(tmp_path)
    target = tmp_path / "plc" / "Line.project"
    target.parent.mkdir()
    made = plcproj.create(plcbuild.load_dir(str(out)), str(template), str(target), True, False, "clear")
    assert made["verify"] == [] and made["opcua"] == 22 and made["tasks"] == ["MAST"]
    back = plcproj.load(str(target))
    assert sorted(g["name"] for g in back.gvls) == ["HMI", "IO", "Mechs", "Signals"]
    assert sorted(x["name"] for x in back.pous) == ["BIND", "BIND_OUT", "FB_Mech", "FB_Pulse", "Mech", "Routes"]
    assert [i for i in back.check() if i["level"] == "error"] == [] and back.link == {"kind": "opcua", "lists": ["HMI"]}
    # the channels of the template of the tests: those of the specification that it has are mapped by the names generated
    have = {(r["device"], r["channel"]) for r in plcproj.io(str(target))}
    changes = [c for c in rep["io_map"] if (c["device"], c["channel"]) in have]
    if changes:
        plcproj.set_io(str(target), changes, str(target))
        report = plcproj.io_report(plcproj.open_project(str(target)), plcproj.load(str(target)))
        assert report["bound"] == len(changes) and report["undeclared"] == []


def test_the_server_generates_and_says_what_is_left_to_a_person(tmp_path):
    import test_hub_seed_plc as base
    proj = tmp_path / "Линия 1"
    _write(proj, _spec())
    r = base._server([("plc_generate", {"project_dir": str(proj)}), ("plc_check", {"path": str(proj / "_work" / "st")})])
    said = r[2]["result"]
    assert not said.get("isError") and "механизмов 3, маршрутов 1, каналов 11" in said["content"][0]["text"]
    assert "C1_lamp" in said["content"][0]["text"] and said["structuredContent"]["check"]["errors"] == 0
    assert said["structuredContent"]["io_map"]["count"] == 11
    assert json.loads(r[3]["result"]["content"][0]["text"])["summary"]["error"] == 0
    bad = base._server([("plc_generate", {"project_dir": str(tmp_path / "нет")})])[2]["result"]
    assert bad["isError"] and "не читается" in bad["content"][0]["text"]
