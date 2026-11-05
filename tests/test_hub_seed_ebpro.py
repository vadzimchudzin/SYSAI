"""hub/seed/ebpro-cmtp — the EasyBuilder Pro .cmtp/.emtp MCP tool stays valid for SYSAI_Hub and keeps working.

No EasyBuilder samples are needed here: a project is built in the test from the documented container format
(u32 revision, MFC wide CString version, u32 revision_min, [u32 length + AES-128-ECB body] for revision >= 240)."""
import io
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from app.net import hub

SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "ebpro-cmtp"
sys.path.insert(0, str(SEED))
import ebpcmtp  # noqa: E402

TOOLS = ["cmtp_info", "cmtp_labels", "cmtp_extract_images", "cmtp_unpack", "cmtp_pack", "cmtp_relabel",
         "cmtp_windows", "cmtp_objects", "cmtp_tags", "cmtp_edit"]


def _png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (3, 2), (255, 0, 0, 255)).save(out, "PNG")
    return out.getvalue()


def _payload() -> bytes:
    """An inner blob shaped like the real one: a window name (UTF-16LE) and a length-prefixed PNG."""
    png = _png()
    body = b"\x10\x00\xd0\x07" + "Window_0".encode("utf-16le") + b"\x00" * 6 + struct.pack("<I", len(png)) + png
    return body + b"\x00" * ((-len(body)) % 16)


def _project(revision: int = 378, version: str = "6.07.02.420", revision_min=None) -> bytes:
    return ebpcmtp.Project(revision, version, revision >= 240, _payload(), revision_min=revision_min).dump()


def _files() -> dict:
    return {str(p.relative_to(SEED)).replace(os.sep, "/"): p.read_bytes()
            for p in SEED.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def test_seed_passes_the_hub_check():
    meta, report = hub.check(_files())
    assert report == {"errors": [], "warnings": []}
    spec = hub.tool_spec(meta)
    assert meta["name"] == "ebpro-cmtp" and spec["kind"] == "mcp" and spec["run"] == "python server.py"
    assert spec["tools"] == TOOLS
    assert hub.read_zip(hub.build_zip(_files())).keys() == _files().keys()


@pytest.mark.parametrize("revision,revision_min", [(378, None), (433, 437), (191, None)])
def test_container_roundtrip(revision, revision_min):
    data = _project(revision, "6.09.01.578", revision_min)
    p = ebpcmtp.Project.load(data)
    assert p.encrypted == (revision >= 240) and p.revision_min == revision_min
    assert p.dump() == data
    if revision >= 240:  # the body really is AES-128-ECB with the key, not plain text
        assert "Window_0".encode("utf-16le") not in data and "Window_0".encode("utf-16le") in p.payload


def test_labels_images_relabel():
    p = ebpcmtp.Project.load(_project())
    off = next(o for o, s in p.labels() if s == "Window_0")
    (im,) = list(p.images())
    assert p.payload[im["offset"]:im["offset"] + im["length"]] == _png()
    with pytest.raises(ValueError):
        p.replace_label(off, "Window_0", "W")
    p.replace_label(off, "Window_0", "Window_9")
    q = ebpcmtp.Project.load(p.dump())
    assert [s for _, s in q.labels()][0] == "Window_9"


# -- a small project blob shaped like EasyBuilder Pro 6.10 (windows, widgets, shapes, OPC UA tag table) --------------
PART_DEF = b"\xff\xff\x01\x00\x0b\x00CPartObject"
DRAW_DEF = b"\xff\xff\x01\x00\x0b\x00CDrawObject"
_cs = ebpcmtp.cstring


def _part(typ, bbox, first=False, devices=("Local HMI",) * 3, atypes=b"\x08\x08\x00", addrs=("0", "0", "0"), labels=()):
    rec = (PART_DEF if first else b"\x01\x80") + bytes([typ]) + _cs("") + b"\x00\x04\x00\x00\x01" + bytes(7)
    rec += b"".join(_cs(d) for d in devices) + bytes(20) + atypes + b"".join(_cs(a) for a in addrs)
    rec += b"\x02\x02\x02\x01" * 6 + bytes(4) + struct.pack("<IIII", *bbox) + b"\x00\xf0\xf0\xf0\x00\xf0\xf0\xf0\x00"
    for text, font, size, x, y in labels:
        rec += _cs(text) + _cs(font) + bytes([size]) + struct.pack("<II", x, y) + b"\xff\xff\xff\x00" + bytes(8)
    return rec + b"\x02" * 8


def _draw(tag, shape, bbox):
    return tag + bytes([shape]) + b"\x00\x00\x00\x00\x01" + bytes(21) + b"\x2f\xc8\x37\xf0" + struct.pack("<IIII", *bbox) + b"\x00\xff\xff\xff\x00" + bytes(8)


def _window(name, w, h, layer, objs):
    lay = layer.encode()
    return (b"\x01\x00\x00\x00" + _cs(name) + struct.pack("<II", w, h) + bytes(10) + b"\x01\x00\x00\x00\x00\x00" + bytes(range(16))
            + struct.pack("<H", len(lay)) + lay + struct.pack("<I", len(objs)) + b"".join(objs))


def _tag(name, typ):
    full = f"{name} (ns:2;s:{name})({name})".encode()
    return b"\x00\x03TAG" + len(full).to_bytes(2, "big") + full + len(typ).to_bytes(2, "big") + typ.encode() + bytes(4) + b"\xf4\x01" + bytes(12)


def _opc(name):
    return f"{name} (ns:2;s:{name})"


def _ui_payload() -> bytes:
    main = _window("Главное", 1024, 768, "Layer 1", [
        _part(0x2D, (0, 0, 1023, 767), first=True),
        _part(0x15, (100, 20, 220, 60), labels=[("Нория 1", "Arial", 24, 100, 20)]),
        _part(0x1B, (300, 100, 380, 140), devices=("OPC UA Client", "OPC UA Client", "Local HMI"), atypes=b"\xc1\xc1\x00",
              addrs=(_opc("GVL.A"), _opc("GVL.A"), "0"), labels=[("", "Arial", 16, 300, 100)]),
        _draw(DRAW_DEF, 4, (10, 10, 500, 90)),
    ])
    popup = _window("Нория 1", 600, 400, "Слой 1", [
        _part(0x2D, (0, 0, 599, 399)),
        _draw(b"\x05\x80", 2, (0, 100, 599, 102)),   # CDrawObject is map index 5: class, 3 widgets, class
        _part(0x15, (40, 300, 140, 340), labels=[("Пуск", "Arial", 20, 40, 300)]),
    ])
    tags = _tag("GVL.A", "BOOL") + _tag("GVL.B", "BOOL") + _tag("GVL.N", "INT")
    # settings after the windows: one MFC class (map index 10 = 1 + 2 classes + 7 objects) and its uses
    settings = b"\xff\xff\x01\x00\x0a\x00CTagObject" + bytes(12) + (b"\x0a\x80" + bytes(14)) * 3
    body = b"\x00" * 8 + main + popup + bytes(2000) + tags + settings
    return body + b"\x00" * ((-len(body)) % 16)


def _ui_project() -> ebpcmtp.Project:
    return ebpcmtp.Project(378, "6.10.02.300", True, _ui_payload())


def test_windows_and_objects():
    p = ebpcmtp.Project.load(_ui_project().dump())
    ws = p.windows()
    assert [(w["number"], w["name"], w["width"], w["height"], w["layer"]) for w in ws] == [
        (0, "Главное", 1024, 768, "Layer 1"), (1, "Нория 1", 600, 400, "Слой 1")]
    assert [o["kind"] for o in ws[0]["objects"]] == ["Window", "Text", "ToggleSwitch", "Rect"]
    assert [o["kind"] for o in ws[1]["objects"]] == ["Window", "Line", "Text"]
    title, sw, rect = ws[0]["objects"][1:]
    assert title["bbox"] == [100, 20, 220, 60] and title["labels"] == [
        {"text": "Нория 1", "font": "Arial", "size": 24, "x": 100, "y": 20}]
    assert [(a["slot"], a["tag"]) for a in sw["addresses"]] == [(1, "GVL.A"), (2, "GVL.A")] and sw["bbox"] == [300, 100, 380, 140]
    assert rect["bbox"] == [10, 10, 500, 90] and ws[1]["objects"][1]["bbox"] == [0, 100, 599, 102]
    assert p.window("Нория 1")["number"] == 1 and p.window(0)["name"] == p.window("0")["name"] == "Главное"
    assert p.opcua_tags() == [{"name": "GVL.A", "type": "BOOL", "node": "ns:2;s:GVL.A"},
                              {"name": "GVL.B", "type": "BOOL", "node": "ns:2;s:GVL.B"},
                              {"name": "GVL.N", "type": "INT", "node": "ns:2;s:GVL.N"}]
    assert "_texts" not in ebpcmtp.public(title) and "objects" not in ebpcmtp.public(ws[0])
    with pytest.raises(ValueError):
        p.window("Нет такого")


def test_edits_text_move_tag_window_name():
    p = _ui_project()
    assert p.edit_text("Главное", "Нория 1", "Нория 1 — сушилка, линия загрузки") == 1   # longer: everything after shifts
    r = p.window("Главное")["objects"][1]["bbox"][2]
    assert 500 < r < 800                                 # and the Text frame widens with it
    assert p.move_object("Главное", 1, 50, 5) == [150, 25, r + 50, 65]
    assert p.retag("Главное", "GVL.A", "GVL.B") == 2
    p.rename_window("Нория 1", "Нория 1 SYSAI")
    q = ebpcmtp.Project.load(p.dump())
    w0, w1 = q.windows()
    title, sw = w0["objects"][1:3]
    assert title["labels"] == [{"text": "Нория 1 — сушилка, линия загрузки", "font": "Arial", "size": 24, "x": 150, "y": 25}]
    assert title["bbox"] == [150, 25, r + 50, 65]
    assert [a["tag"] for a in sw["addresses"]] == ["GVL.B", "GVL.B"] and sw["addresses"][0]["node"] == "ns:2;s:GVL.B"
    assert w1["name"] == "Нория 1 SYSAI" and [o["kind"] for o in w1["objects"]] == ["Window", "Line", "Text"]
    assert w1["objects"][2]["labels"][0]["text"] == "Пуск" and len(q.opcua_tags()) == 3
    with pytest.raises(ValueError):
        q.retag("Главное", "GVL.B", "GVL.NOPE")       # not in the tag table
    with pytest.raises(ValueError):
        q.retag("Главное", "GVL.B", "GVL.N")          # BOOL -> INT
    with pytest.raises(ValueError):
        q.edit_text("Нория 1 SYSAI", "Стоп", "Пуск")  # no such text
    with pytest.raises(ValueError):
        q.move_object("Главное", 1, -500, 0)          # would leave the window
    assert ebpcmtp.cstring("x" * 300)[:6] == b"\xff\xfe\xff\xff\x2c\x01"   # long CString: FF + u16 length


def test_clone_window_and_objects():
    p = _ui_project()
    assert p.clone_window("Нория 1", "Нория 2", number=5) == 5
    q = ebpcmtp.Project.load(p.dump())
    ws = q.windows()
    assert [(w["number"], w["name"]) for w in ws] == [(0, "Главное"), (1, "Нория 1"), (5, "Нория 2")]
    assert [o["kind"] for o in ws[2]["objects"]] == ["Window", "Line", "Text"] and ws[2]["guid"] != ws[1]["guid"]
    assert q.payload.count(b"\x0d\x80" + bytes(14)) == 3            # settings class 10 -> 13: three objects more
    assert q.clone_object("Главное", 1, dy=100) == 4
    assert q.clone_object("Главное", 3, to="Нория 2", dx=5) == 3     # the CDrawObject definition -> a reference
    r = ebpcmtp.Project.load(q.dump())
    w0, w5 = r.window("Главное"), r.window(5)
    copy = w0["objects"][4]
    assert w0["declared"] == 5 and copy["kind"] == "Text" and copy["bbox"] == [100, 120, 220, 160]
    assert copy["labels"] == [{"text": "Нория 1", "font": "Arial", "size": 24, "x": 100, "y": 120}]
    assert w5["declared"] == 4 and w5["objects"][3]["kind"] == "Rect" and w5["objects"][3]["bbox"] == [15, 10, 505, 90]
    assert r.payload.count(b"\x0f\x80" + bytes(14)) == 3 and r.payload.count(ebpcmtp._DRAW_DEF) == 1
    with pytest.raises(ValueError):
        r.clone_window("Нория 1", number=5)          # taken
    with pytest.raises(ValueError):
        r.clone_object("Главное", 0)                 # the window object itself
    with pytest.raises(ValueError):
        r.clone_object("Главное", 1, dy=-500)        # would leave the window


def test_mcp_server_over_stdio(tmp_path):
    src = tmp_path / "topol.emtp"
    src.write_bytes(_project())
    proc = subprocess.Popen([sys.executable, str(SEED / "server.py")], cwd=str(SEED), text=True, encoding="utf-8",
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def rpc(i, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def call(i, name, **args):
        result = rpc(i, "tools/call", {"name": name, "arguments": args})["result"]
        return result.get("isError", False), result["content"][0]["text"]

    try:
        assert rpc(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"] == "ebpro-cmtp"
        assert [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]] == TOOLS
        err, text = call(3, "cmtp_info", path=str(src))
        info = json.loads(text)
        assert not err and info["revision"] == 378 and info["encrypted"] and info["images"] == 1
        err, text = call(4, "cmtp_labels", path=str(src), filter="window")
        assert not err and json.loads(text)["labels"][0]["text"] == "Window_0"
        err, _ = call(5, "cmtp_extract_images", path=str(src), out_dir=str(tmp_path / "img"))
        assert not err and [f.read_bytes() for f in (tmp_path / "img").iterdir()] == [_png()]
        assert not call(6, "cmtp_unpack", path=str(src), out_file=str(tmp_path / "p.bin"))[0]
        assert not call(7, "cmtp_pack", in_file=str(tmp_path / "p.bin"), out_file=str(tmp_path / "re.emtp"),
                        revision=378, version="6.07.02.420")[0]
        assert (tmp_path / "re.emtp").read_bytes() == src.read_bytes()
        assert not call(8, "cmtp_relabel", path=str(src), out_file=str(tmp_path / "rl.emtp"), old="Window_0", new="Window_9")[0]
        assert call(9, "cmtp_relabel", path=str(src), out_file=str(tmp_path / "x.emtp"), old="Window_0", new="W")[0]
        assert call(10, "cmtp_info", path=str(tmp_path / "missing.emtp"))[0]
        assert rpc(11, "nope")["error"]["code"] == -32601
        ui = tmp_path / "ui.cmtp"
        ui.write_bytes(_ui_project().dump())
        err, text = call(12, "cmtp_windows", path=str(ui))
        assert not err and [w["name"] for w in json.loads(text)["windows"]] == ["Главное", "Нория 1"]
        err, text = call(13, "cmtp_objects", path=str(ui), window="Главное", kind="ToggleSwitch")
        objs = json.loads(text)["objects"]
        assert not err and [o["index"] for o in objs] == [2] and objs[0]["addresses"][0]["tag"] == "GVL.A"
        err, text = call(14, "cmtp_tags", path=str(ui), filter="gvl.n")
        assert not err and json.loads(text)["tags"] == [{"name": "GVL.N", "type": "INT", "node": "ns:2;s:GVL.N"}]
        out = tmp_path / "ui2.cmtp"
        err, text = call(15, "cmtp_edit", path=str(ui), out_file=str(out), edits=[
            {"op": "text", "window": "Главное", "old": "Нория 1", "new": "Нория 2"},
            {"op": "move", "window": "0", "object": 1, "dx": 10, "dy": 0},
            {"op": "tag", "window": "Главное", "old": "GVL.A", "new": "GVL.B", "object": 2},
            {"op": "rename_window", "window": "Нория 1", "new": "Нория 2"},
            {"op": "clone_window", "window": "Нория 2", "new": "Нория 3", "number": 7},
            {"op": "clone_object", "window": "Главное", "object": 1, "to": "7", "dy": 50}])
        assert not err, text
        q = ebpcmtp.Project.load(out.read_bytes())
        assert [(w["number"], w["name"]) for w in q.windows()] == [(0, "Главное"), (1, "Нория 2"), (7, "Нория 3")]
        assert q.window(7)["objects"][3]["labels"][0]["text"] == "Нория 2"
        assert q.windows()[0]["objects"][1]["labels"][0]["text"] == "Нория 2" and q.windows()[0]["objects"][1]["bbox"][0] == 110
        err, text = call(16, "cmtp_edit", path=str(ui), out_file=str(tmp_path / "bad.cmtp"), edits=[
            {"op": "text", "window": "Главное", "old": "Нория 1", "new": "x"},
            {"op": "tag", "window": "Главное", "old": "GVL.A", "new": "GVL.N"}])
        assert err and "правка 2" in text and not (tmp_path / "bad.cmtp").exists()
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert proc.returncode == 0
