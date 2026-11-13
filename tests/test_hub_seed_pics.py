"""hub/seed/pic-index — the index of HMI equipment pictures stays valid for SYSAI_Hub and keeps working.

No picture library is needed: the test writes tiny pictures itself (a real PNG, a BMP, a GIF header, a JPEG with a
SOF0 marker, an SVG) named the way such libraries are named — Russian and Latin, camelCase, numbers, subfolders."""
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from app.net import hub

SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "pic-index"
sys.path.append(str(SEED))          # at the end: other seed tools have a `server` module of their own
import picindex  # noqa: E402

TOOLS = ["pics_index_update", "pics_find", "pics_list", "pics_label_update", "pics_check"]


def _png(width, height, color=(200, 30, 30)) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _bmp(width, height) -> bytes:
    pixels = b"\x00" * ((width * 3 + 3) // 4 * 4 * height)
    return (b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0) + pixels)


def _gif(width, height) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def _jpeg(width, height) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


SVG = b'<?xml version="1.0"?>\n<svg xmlns="x" viewBox="0 0 64 32"><rect width="5" height="5"/></svg>'

LIBRARY = {
    "noria/Нория_большая_выкл.png": _png(48, 96, (120, 120, 120)),
    "noria/Нория_большая_вкл.png": _png(48, 96, (0, 200, 0)),
    "noria/Нория_большая_авария.png": _png(48, 96, (200, 0, 0)),
    "noria/Нория_большая_ручн.png": _png(48, 96, (0, 0, 200)),
    "conveyors/belt_0.png": _png(100, 20, (1, 1, 1)),
    "conveyors/belt_1.png": _png(100, 20, (2, 2, 2)),
    "conveyors/belt_2.png": _png(100, 20, (3, 3, 3)),
    "ShnekLeftOn.png": _png(60, 20, (4, 4, 4)),
    "ShnekLeftOff.png": _png(60, 20, (5, 5, 5)),
    "ShnekRightOn.png": _png(60, 20, (6, 6, 6)),
    "zadvizhka-open.bmp": _bmp(30, 40),
    "zadvizhka-closed.bmp": _bmp(30, 44),
    "lamps/lamp_green.gif": _gif(16, 16),
    "lamps/lamp_red.gif": _gif(16, 16),
    "fan_run.jpg": _jpeg(33, 21),
    "pipes/pipe_hor.svg": SVG,
    "img0042.png": _png(10, 10, (7, 7, 7)),
    "copy_of_img.png": _png(10, 10, (7, 7, 7)),
    ".hidden/noria_on.png": _png(5, 5),
    "readme.txt": b"not a picture",
}


def _library(tmp_path) -> str:
    root = tmp_path / "pictures"
    for name, data in LIBRARY.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return str(root)


def _by_file(index) -> dict:
    return {r["file"]: r for r in index["pictures"]}


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


def test_the_tool_passes_the_hub_check():
    meta, report = hub.check(_files())
    assert report == {"errors": [], "warnings": []} and meta["name"] == "pic-index" and meta["kind"] == "mcp"
    assert meta["platform"] == "any" and meta["version"] == "1.0.3" and meta["run"] == "python server.py"
    assert [t.strip() for t in meta["tools"].split(",")] == TOOLS
    assert not any(w in src for src in (f.read_text(encoding="utf-8") for f in SEED.glob("*.py"))
                   for w in ("import socket", "urllib", "http.client", "requests", "subprocess"))


def test_picture_headers():
    assert picindex.picture_info(_png(48, 96), "a.png") == ("png", 48, 96)
    assert picindex.picture_info(_bmp(30, 40), "a.bmp") == ("bmp", 30, 40)                  # a top-down BMP: height < 0
    assert picindex.picture_info(_gif(16, 17), "a.gif") == ("gif", 16, 17)
    assert picindex.picture_info(_jpeg(33, 21), "a.jpg") == ("jpeg", 33, 21)
    assert picindex.picture_info(SVG, "a.svg") == ("svg", 64, 32)
    assert picindex.picture_info(b'<svg width="120px" height="40.4">', "a.svg") == ("svg", 120, 40)
    assert picindex.picture_info(b'<svg width="100%" height="100%">', "a.svg") == ("svg", None, None)
    assert picindex.picture_info(_png(3, 4), "really_a_png.jpg")[0] == "png"                # the content, not the extension
    assert picindex.picture_info(b"garbage", "a.png") == ("png", None, None) and picindex.picture_info(b"\x89PNG", "a.png")[1] is None


def test_words_spelling_and_names():
    assert picindex.tokenize("Нория_большая-ON2.v1 NoriaLeftOn") == ["Нория", "большая", "ON", "2", "v", "1", "Noria", "Left", "On"]
    assert len({picindex.key(w) for w in ("нория", "Нории", "норию", "noria", "Noriya")}) == 1
    assert picindex.key("задвижку") == picindex.key("zadvizhka") and picindex.key("pumps") == picindex.key("pump")
    assert picindex.key("насосы") == picindex.key("насос") == picindex.key("nasosa")
    assert picindex.key("сервер") != picindex.key("серый") == picindex.key("сер")
    assert [picindex.equipment_of(w) for w in ("Elevators", "транспортёр", "шибер", "мотора", "hopper", "звонок", "что-то")] == \
        ["нория", "конвейер", "задвижка", "двигатель", "силос", "сирена", ""]
    d = picindex.derive("noria/Нория_большая_вкл.png")
    assert (d["equipment"], d["state"], d["orientation"], d["base"]) == ("нория", "on", "", "нория_большая")
    d = picindex.derive("NoriaLeftON.png")
    assert (d["equipment"], d["state"], d["orientation"], d["base"]) == ("нория", "on", "left", "noria")
    states = {"m_on": "on", "m_run": "on", "m_вкл": "on", "m_работа": "on", "m_off": "off", "m_stop": "off", "m_выкл": "off",
              "m_стоп": "off", "m_alarm": "alarm", "m_err": "alarm", "m_fault": "alarm", "m_avar": "alarm", "m_авария": "alarm",
              "m_ручн": "manual", "m_man": "manual", "m_open": "open", "m_откр": "open", "m_closed": "closed", "m_закр": "closed",
              "m_green": "on", "m_зел": "on", "m_red": "alarm", "m_красный": "alarm", "m_gray": "off", "m_grey": "off",
              "m_серый": "off", "m_yellow": "warning", "m_желтая": "warning", "m": "unknown", "m_7": "unknown"}
    assert {name: picindex.derive(name + ".png")["state"] for name in states} == states
    assert all(picindex.derive(name + ".png")["base"] == "m" for name in states)
    # an explicit state wins over a color, and the color then stays in the name of the set
    d = picindex.derive("red_lamp_on.png")
    assert (d["state"], d["base"], d["equipment"]) == ("on", "red_lamp", "лампа")
    # the equipment and the name of the set come from the folder when the file name has neither
    d = picindex.derive("silo/big/on.png")
    assert (d["equipment"], d["state"], d["base"]) == ("силос", "on", "big")
    assert picindex.derive("шнек_вправо_авария.png")["orientation"] == "right" and picindex.derive("pipe_верт.png")["orientation"] == "vertical"


def test_the_index_sizes_equipment_states_and_sets(tmp_path):
    root = _library(tmp_path)
    index, stats = picindex.refresh(root)
    assert (stats["total"], stats["added"], stats["changed"], stats["removed"], stats["moved"]) == (18, 18, 0, 0, 0)
    assert stats["sets"] == 9 and stats["unrecognized"] == 2 and stats["unrecognized_files"] == ["copy_of_img.png", "img0042.png"]
    doc = json.loads((Path(root) / "pic-index.json").read_text(encoding="utf-8"))
    assert doc["version"] == 1 and doc["updated"] and doc["labels"] == {} and doc["pictures"] == index["pictures"]
    assert not (Path(root) / "pic-index.json.tmp").exists()
    recs = _by_file(index)
    assert ".hidden/noria_on.png" not in recs and "readme.txt" not in recs and "pic-index.json" not in recs
    r = recs["noria/Нория_большая_авария.png"]
    assert (r["format"], r["width"], r["height"], r["equipment"], r["state"], r["base"]) == ("png", 48, 96, "нория", "alarm", "нория_большая")
    assert r["bytes"] == len(LIBRARY["noria/Нория_большая_авария.png"]) and len(r["sha1"]) == 40 and "nor" in r["words"]
    assert [(recs[f]["format"], recs[f]["width"], recs[f]["height"]) for f in
            ("zadvizhka-open.bmp", "lamps/lamp_red.gif", "fan_run.jpg", "pipes/pipe_hor.svg")] == \
        [("bmp", 30, 40), ("gif", 16, 16), ("jpeg", 33, 21), ("svg", 64, 32)]
    assert (recs["pipes/pipe_hor.svg"]["equipment"], recs["pipes/pipe_hor.svg"]["orientation"]) == ("труба", "horizontal")
    assert (recs["lamps/lamp_green.gif"]["state"], recs["lamps/lamp_red.gif"]["state"]) == ("on", "alarm")
    # name_0 / name_1 / name_2 next to each other: the number is the state, and it is marked as a guess
    assert [(recs[f"conveyors/belt_{n}.png"]["state"], recs[f"conveyors/belt_{n}.png"].get("state_guess")) for n in "012"] == \
        [("off", True), ("on", True), ("alarm", True)]
    assert recs["img0042.png"]["state"] == "unknown" and "state_guess" not in recs["img0042.png"]      # a lone number is no state
    sets = {(s["folder"], s["base"]): s for s in picindex.sets_of(index["pictures"])}
    noria = sets[("noria", "нория_большая")]
    assert noria["equipment"] == "нория" and noria["size"] == "48x96" and len(noria["files"]) == 4
    assert noria["states"] == {"off": "noria/Нория_большая_выкл.png", "on": "noria/Нория_большая_вкл.png",
                               "alarm": "noria/Нория_большая_авария.png", "manual": "noria/Нория_большая_ручн.png"}
    shnek = sets[("", "shnek")]
    assert shnek["states"] == {"off": "ShnekLeftOff.png", "on": "ShnekLeftOn.png"}
    assert shnek["by_orientation"] == {"left": {"on": "ShnekLeftOn.png", "off": "ShnekLeftOff.png"}, "right": {"on": "ShnekRightOn.png"}}
    assert sets[("", "zadvizhka")]["size"] == "" and sets[("conveyors", "belt")]["state_guess"] is True
    with pytest.raises(picindex.PicError, match="нет папки"):
        picindex.refresh(str(tmp_path / "nowhere"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(picindex.PicError, match="в папке нет картинок"):
        picindex.find(str(tmp_path / "empty"), "нория")


def test_numbered_variants_are_separate_sets(tmp_path):
    for name in ("noria1_on", "noria1_off", "noria2_on", "noria2_off", "fan_on_1", "fan_on_2", "fan_off"):
        (tmp_path / f"{name}.png").write_bytes(_png(8, 8, (len(name), sum(map(ord, name)) % 256, 0)))
    index, stats = picindex.refresh(str(tmp_path))
    sets = {s["base"]: s["states"] for s in picindex.sets_of(index["pictures"])}
    assert sets == {"noria_1": {"off": "noria1_off.png", "on": "noria1_on.png"}, "noria_2": {"off": "noria2_off.png", "on": "noria2_on.png"},
                    "fan": {"off": "fan_off.png", "on": "fan_on_1.png"}}       # fan_on_1 / fan_on_2 are frames of one set
    assert stats["sets"] == 3


def test_find_by_meaning(tmp_path):
    root = _library(tmp_path)

    def top(query, **kw):
        got = picindex.find(root, query, **kw)["sets"]
        return (got[0]["folder"], got[0]["base"]) if got else None

    assert not (Path(root) / "pic-index.json").exists()
    assert top("нория") == ("noria", "нория_большая") and (Path(root) / "pic-index.json").exists()      # built silently
    assert top("noria") == top("Elevator") == top("элеваторы") == top("норию большую") == ("noria", "нория_большая")
    assert top("conveyor") == top("конвейер") == top("транспортёр") == top("лента") == ("conveyors", "belt")
    assert top("шнек") == top("screw") == top("auger left") == ("", "shnek")
    assert top("задвижка") == top("задвижку открыть") == top("gate") == top("шибер") == ("", "zadvizhka")
    assert top("вентилятор") == top("fan") == ("", "fan") and top("лампа") == top("индикатор") == ("lamps", "lamp")
    assert top("труба") == top("pipe") == ("pipes", "pipe") and top("экскаватор") is None
    got = picindex.find(root, "нория")
    assert got["found"] == 1 and got["dir"] == root and got["sets"][0]["states"]["alarm"] == "noria/Нория_большая_авария.png"
    assert set(got["sets"][0]) == {"equipment", "base", "folder", "size", "states", "files", "note"}
    assert "hint" in picindex.find(root, "экскаватор")
    # a word of the name that is no equipment, and a folder name
    assert top("большая") == ("noria", "нория_большая") and top("lamps") == ("lamps", "lamp")
    assert [s["base"] for s in picindex.find(root, "img")["sets"]] == ["copy_of_img", "img"]
    # the equipment is worth more than a word of the name: a picture merely called «noria…» in the lamps folder comes second
    (Path(root) / "lamps" / "lamp_noria_room.gif").write_bytes(_gif(8, 8))
    assert [s["base"] for s in picindex.find(root, "нория")["sets"]] == ["нория_большая", "lamp_noria_room"]
    (Path(root) / "lamps" / "lamp_noria_room.gif").unlink()
    assert [s["base"] for s in picindex.find(root, "conveyor belt")["sets"]] == ["belt"]
    # the state filter and a state named in the query
    assert top("шнек", state="alarm") is None and top("шнек", state="on, off") == ("", "shnek")
    assert [s["base"] for s in picindex.find(root, "open")["sets"]] == ["zadvizhka"]
    assert {s["base"] for s in picindex.find(root, "авария")["sets"]} == {"нория_большая", "belt", "lamp"}
    assert len(picindex.find(root, "on", limit=2)["sets"]) == 2 and picindex.find(root, "on")["found"] == 5
    with pytest.raises(picindex.PicError, match="пустой запрос"):
        picindex.find(root, " 12 ")
    with pytest.raises(picindex.PicError, match="нет состояния"):
        picindex.find(root, "нория", state="burning")


def test_incremental_update(tmp_path, monkeypatch):
    root = Path(_library(tmp_path))
    index, _ = picindex.refresh(str(root))
    before = _by_file(index)
    stamp = (root / "pic-index.json").stat().st_mtime_ns
    reads = []
    real = picindex.picture_info
    monkeypatch.setattr(picindex, "picture_info", lambda data, name="": reads.append(name) or real(data, name))
    # nothing changed: no file is read again, the index file is not rewritten
    index, stats = picindex.refresh(str(root))
    assert reads == [] and (stats["added"], stats["changed"], stats["removed"], stats["moved"]) == (0, 0, 0, 0)
    assert (root / "pic-index.json").stat().st_mtime_ns == stamp and stats["saved"] is True
    # touched (same content), redrawn, renamed, deleted, added
    os.utime(root / "fan_run.jpg", (1_700_000_000, 1_700_000_000))
    (root / "lamps/lamp_red.gif").write_bytes(_gif(20, 20))
    (root / "ShnekRightOn.png").rename(root / "noria" / "screw_right_run.png")
    (root / "img0042.png").unlink()
    (root / "насос_вкл.png").write_bytes(_png(12, 12, (9, 9, 9)))
    index, stats = picindex.refresh(str(root))
    assert sorted(reads) == ["fan_run.jpg", "lamps/lamp_red.gif", "noria/screw_right_run.png", "насос_вкл.png"]
    assert (stats["total"], stats["added"], stats["changed"], stats["removed"], stats["moved"]) == (18, 1, 2, 1, 1)
    after = _by_file(index)
    assert after["fan_run.jpg"]["sha1"] == before["fan_run.jpg"]["sha1"] and after["fan_run.jpg"]["mtime"] == 1_700_000_000
    assert after["lamps/lamp_red.gif"]["width"] == 20 and after["lamps/lamp_red.gif"]["sha1"] != before["lamps/lamp_red.gif"]["sha1"]
    moved = after["noria/screw_right_run.png"]
    assert moved["sha1"] == before["ShnekRightOn.png"]["sha1"] and (moved["equipment"], moved["state"], moved["orientation"]) == ("шнек", "on", "right")
    assert "img0042.png" not in after and after["насос_вкл.png"]["equipment"] == "насос"
    assert json.loads((root / "pic-index.json").read_text(encoding="utf-8"))["pictures"] == index["pictures"]
    # the reading tools see a change of the folder without an explicit update
    (root / "noria" / "screw_right_run.png").unlink()
    assert picindex.find(str(root), "screw")["sets"][0]["files"] == ["ShnekLeftOff.png", "ShnekLeftOn.png"]
    # full: every file is read again, nothing counts as changed
    reads.clear()
    index, stats = picindex.refresh(str(root), full=True)
    assert len(reads) == 17 and (stats["added"], stats["changed"], stats["removed"]) == (0, 0, 0)
    # a broken index file is rebuilt
    (root / "pic-index.json").write_text("{not json", encoding="utf-8")
    assert picindex.refresh(str(root))[1]["added"] == 17


def test_labels_win_and_survive(tmp_path):
    root = Path(_library(tmp_path))
    (root / "copy_of_img.png").unlink()          # the same content as img0042.png: a label goes by content
    rec = picindex.label(str(root), "img0042.png", equipment="Сирены", state="alarm", note="Сирена на крыше, красная")
    assert (rec["equipment"], rec["state"], rec["note"], rec["labeled"]) == ("сирена", "alarm", "Сирена на крыше, красная", True)
    doc = json.loads((root / "pic-index.json").read_text(encoding="utf-8"))
    assert doc["labels"] == {rec["sha1"]: {"equipment": "сирена", "state": "alarm", "note": "Сирена на крыше, красная"}}
    found = picindex.find(str(root), "siren")["sets"]
    assert [(s["equipment"], s["states"], s["note"]) for s in found] == [("сирена", {"alarm": "img0042.png"}, "Сирена на крыше, красная")]
    assert picindex.find(str(root), "крыша")["sets"][0]["base"] == "img" and picindex.overview(str(root))["unlabeled"] == 0
    # a label wins over the name: the belt pictures are really open / closed, and one of them joins another set
    rec = picindex.label(str(root), str(root / "conveyors" / "belt_1.png"), state="open", base="Belt Wide")
    assert (rec["state"], rec["base"]) == ("open", "belt_wide") and "state_guess" not in rec
    # ... it survives a rename, a move to another folder and a full re-index
    (root / "img0042.png").rename(root / "lamps" / "x.png")
    (root / "conveyors" / "belt_1.png").rename(root / "conveyors" / "thing.png")
    index, stats = picindex.refresh(str(root), full=True)
    recs = _by_file(index)
    assert stats["moved"] == 2 and (recs["lamps/x.png"]["equipment"], recs["lamps/x.png"]["state"]) == ("сирена", "alarm")
    assert (recs["conveyors/thing.png"]["state"], recs["conveyors/thing.png"]["base"], recs["conveyors/thing.png"]["equipment"]) == \
        ("open", "belt_wide", "конвейер")
    # a kind that is not in the dictionary is kept as written and found
    rec = picindex.label(str(root), "fan_run.jpg", equipment="Аспиратор", orientation="left")
    assert (rec["equipment"], rec["orientation"], rec["state"]) == ("аспиратор", "left", "on")
    assert picindex.find(str(root), "аспираторы")["sets"][0]["files"] == ["fan_run.jpg"]
    # an empty string clears a field; the last field gone — the label is gone
    rec = picindex.label(str(root), "fan_run.jpg", equipment="", orientation="")
    assert (rec["equipment"], rec["orientation"]) == ("вентилятор", "") and "labeled" not in rec
    assert len(json.loads((root / "pic-index.json").read_text(encoding="utf-8"))["labels"]) == 2
    with pytest.raises(picindex.PicError, match="нет картинки"):
        picindex.label(str(root), "nothing.png", state="on")
    with pytest.raises(picindex.PicError, match="нет состояния"):
        picindex.label(str(root), "fan_run.jpg", state="burning")
    with pytest.raises(picindex.PicError, match="нет положения"):
        picindex.label(str(root), "fan_run.jpg", orientation="sideways")


def test_overview_and_check(tmp_path):
    root = Path(_library(tmp_path))
    (root / "noria" / "huge_noria_on.png").write_bytes(_png(1500, 2, (1, 2, 3)))
    over = picindex.overview(str(root))
    assert (over["pictures"], over["sets"], over["unlabeled"]) == (19, 10, 2)
    kinds = {k["equipment"]: (k["sets"], k["pictures"]) for k in over["equipment"]}
    assert kinds == {"нория": (2, 5), "конвейер": (1, 3), "шнек": (1, 3), "задвижка": (1, 2), "лампа": (1, 2), "вентилятор": (1, 1),
                     "труба": (1, 1)}
    row = next(r for r in over["list"] if r["name"] == "нория_большая")
    assert row == {"equipment": "нория", "name": "нория_большая", "folder": "noria", "states": ["off", "on", "alarm", "manual"],
                   "size": "48x96", "pictures": 4}
    only = picindex.overview(str(root), "Elevator")
    assert only["sets"] == 2 and {r["equipment"] for r in only["list"]} == {"нория"} and only["pictures"] == 19
    report = picindex.check(str(root))
    issues = [(i["level"], i.get("set") or i.get("file"), i["text"]) for i in report["issues"]]

    def has(level, where, part):
        return any(lv == level and w == where and part in text for lv, w, text in issues)

    assert has("warning", "fan", "есть состояние on, но нет off") and has("warning", "noria/huge_noria", "но нет off")
    assert has("info", "shnek", "есть состояние on, но нет alarm") and not has("info", "noria/нория_большая", "но нет")
    assert has("warning", "lamps/lamp", "есть состояние alarm, но нет off")
    assert has("warning", "zadvizhka", "разного размера: 30x40, 30x44")
    assert has("info", "copy_of_img.png", "одинаковые картинки: copy_of_img.png, img0042.png")
    assert has("info", "img0042.png", "не распознано") and has("info", "conveyors/belt", "угаданы по номерам")
    assert has("warning", "noria/huge_noria_on.png", "тяжёлая картинка для панели: 1500x2")
    assert report["summary"]["error"] == 0 and report["summary"]["warning"] + report["summary"]["info"] == len(issues)
    assert [i["level"] for i in report["issues"]] == sorted((i["level"] for i in report["issues"]), key=["warning", "info"].index)
    (root / "noria" / "big.png").write_bytes(b"\x00" * (picindex.HEAVY_BYTES + 1))
    report = picindex.check(str(root))
    texts = [i["text"] for i in report["issues"] if i.get("file") == "noria/big.png"]
    assert any("1.0 МБ" in t for t in texts) and any("размер не прочитан" in t for t in texts)


def test_the_mcp_server_end_to_end(tmp_path):
    root = _library(tmp_path)
    out = _server([("pics_index_update", {"dir": root}), ("pics_find", {"dir": root, "query": "elevator", "state": "alarm"}),
                   ("pics_list", {"dir": root, "equipment": "шнек"}),
                   ("pics_label_update", {"dir": root, "file": "img0042.png", "equipment": "сирена", "state": "off"}),
                   ("pics_check", {"dir": root}), ("pics_find", {"dir": str(tmp_path / "nowhere"), "query": "x"}),
                   ("pics_find", {"dir": root}), ("pics_nothing", {})])
    assert out[0]["result"]["serverInfo"]["name"] == "pic-index" and "pics_find" in out[0]["result"]["instructions"]
    tools = out[1]["result"]["tools"]
    assert [t["name"] for t in tools] == TOOLS
    assert [t["annotations"]["readOnlyHint"] for t in tools] == [False, True, True, False, True]
    assert all(t["inputSchema"]["required"][0] == "dir" and t["inputSchema"]["additionalProperties"] is False for t in tools)
    results = [json.loads(x["result"]["content"][0]["text"]) for x in out[2:7]]
    assert (results[0]["total"], results[0]["added"], results[0]["sets"]) == (18, 18, 9)
    assert results[1]["sets"][0]["states"]["on"] == "noria/Нория_большая_вкл.png" and "Нория" in out[3]["result"]["content"][0]["text"]
    assert results[2]["sets"] == 1 and results[2]["list"][0]["states"] == ["off", "on"]
    assert (results[3]["file"], results[3]["equipment"], results[3]["state"]) == ("img0042.png", "сирена", "off")
    assert results[4]["summary"]["warning"] >= 3
    assert out[7]["result"]["isError"] and "нет папки" in out[7]["result"]["content"][0]["text"]
    assert out[8]["result"]["isError"] and "не хватает поля 'query'" in out[8]["result"]["content"][0]["text"]
    assert out[9]["result"]["isError"] and "нет инструмента pics_nothing" in out[9]["result"]["content"][0]["text"]
