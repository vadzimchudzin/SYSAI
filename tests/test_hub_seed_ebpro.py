"""hub/seed/ebpro-cmtp — the EasyBuilder Pro .cmtp/.emtp MCP tool stays valid for SYSAI_Hub and keeps working.

No EasyBuilder samples are needed here: a project is built in the test from the documented container format
(u32 revision, MFC wide CString version, u32 revision_min, [u32 length + AES-128-ECB body] for revision >= 240)."""
import io
import json
import os
import shutil
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
         "cmtp_windows", "cmtp_objects", "cmtp_tags", "cmtp_scene", "cmtp_links", "cmtp_check", "cmtp_render", "cmtp_edit"]


def _png(rgba=(255, 0, 0, 255)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (3, 2), rgba).save(out, "PNG")
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


def _draw(tag, shape, bbox, attrs=b"\x00\xff\xff\xff\x00"):
    return tag + bytes([shape]) + b"\x00\x00\x00\x00\x01" + bytes(21) + b"\x2f\xc8\x37\xf0" + struct.pack("<IIII", *bbox) + attrs + bytes(7) + b"\x02"


def _window(name, w, h, layer, objs):
    lay = layer.encode()
    return (b"\x01\x00\x00\x00" + _cs(name) + struct.pack("<II", w, h) + bytes(10) + b"\x01\x00\x00\x00\x00\x00" + bytes(range(16))
            + struct.pack("<H", len(lay)) + lay + struct.pack("<I", len(objs)) + b"".join(objs))


MID = bytes(4) + b"\xf4\x01" + bytes(9) + b"\x01"     # the 16 bytes between a tag's type and its node id


def _tag(name, typ, ns=2):
    """One entry of the OPC UA tag table: «name (ns:2;s:id)(id)», description, type, 16 bytes, ns, id, 4 bytes."""
    full, ident = f"{name} (ns:{ns};s:{name})({name})".encode(), name.encode()
    return (b"\x00\x03TAG" + len(full).to_bytes(2, "big") + full + b"\x00" + bytes([len(typ)]) + typ.encode() + MID
            + ns.to_bytes(2, "big") + len(ident).to_bytes(2, "big") + ident + bytes(4))


def _tags(*entries):
    """The table: u32 count, the server's settings, the entries."""
    return struct.pack("<I", len(entries)) + b"\x01\x00\x08\x00" + bytes(8) + b"".join(entries)


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
    tags = _tags(_tag("GVL.A", "BOOL"), _tag("GVL.B", "BOOL"), _tag("GVL.N", "INT"))
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


# -- how a window looks: the picture library, the picture objects of the widgets, colours -----------------------------
IMG, IMG2, PIC = bytes(range(16, 32)), bytes(range(32, 48)), bytes(range(48, 64))
SHAPE = bytes.fromhex("51546f194dad57e29f1ab6efb338d5ee")   # a shape of EasyBuilder's own library (a UUID v5)


def _bmp(rgb=(0, 210, 255)):
    """2x2, 24 bits, bottom-up: the bottom left pixel white, the rest of the colour."""
    c = bytes(rgb[::-1])
    pix = b"\xff\xff\xff" + c + bytes(2) + c + c + bytes(2)
    return (b"BM" + struct.pack("<IHHI", 54 + len(pix), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, len(pix), 2835, 2835, 0, 0) + pix)


def _attrs(fill_on, fill, frame, width):
    return bytes([fill_on]) + bytes(fill) + b"\x00" + bytes(frame) + b"\x00" + bytes(35) + bytes([width])


def _wid(typ, bbox, attrs, labels=(), pic=b"", first=False, devices=("Local HMI",) * 3, atypes=b"\x08\x08\x00",
         addrs=("0",) * 3):
    rec = (PART_DEF if first else b"\x01\x80") + bytes([typ]) + _cs("") + b"\x00\x04\x00\x00\x01" + bytes(7)
    rec += b"".join(_cs(d) for d in devices) + bytes(20) + atypes + b"".join(_cs(a) for a in addrs)
    rec += b"\x02\x02\x02\x01" * 6 + bytes(4) + struct.pack("<IIII", *bbox) + attrs
    for text, font, size, x, y, rgb, align in labels:
        rec += _cs(text) + _cs(font) + bytes([size]) + struct.pack("<II", x, y) + bytes(rgb) + b"\x00" + bytes([align, 0, 0])
    return rec + pic + b"\x02" * 8


GROUP_CLASS, STATE_CLASS = b"class PicGroup<class BitmapPicture>", b"class BitmapPicture"


def _groups(groups) -> bytes:
    """The picture objects right after the library: 04 00 00 00 00 00, u32 count; the first object (and its first
    state) start with their MFC class; per state the file GUID, u32 «transparent on», u32 0, colour; then the key."""
    out = b"\x04\x00\x00\x00\x00\x00" + struct.pack("<I", len(groups))
    for i, (name, states, key) in enumerate(groups):
        out += (b"\x01\x00\x01\x00" + struct.pack("<H", len(GROUP_CLASS)) + GROUP_CLASS + b"\x01\x04") if i == 0 \
            else b"\x01\x04\x01\x00"
        out += struct.pack("<H", len(name)) + name.encode() + struct.pack("<I", len(states))
        for j, (guid, rgb) in enumerate(states):
            out += (b"\x01\x00\x01\x00" + struct.pack("<H", len(STATE_CLASS)) + STATE_CLASS + b"\x02\x04\x01\x00"
                    if i == j == 0 else b"\x02\x04\x01\x00\x01\x00")
            out += guid + struct.pack("<II", rgb is not None, 0) + bytes(rgb or (255, 255, 255)) + b"\x00"
        out += key
    return out


def _look_payload() -> bytes:
    png, bmp = _png(), _bmp()
    lib = (struct.pack("<I", 2) + b"\xff" + IMG + struct.pack("<I", len(png)) + png
           + b"\xff" + IMG2 + struct.pack("<I", len(bmp)) + bmp)
    groups = _groups([("lamp", [(IMG, None), (IMG2, (0, 210, 255))], PIC)])
    plain = _attrs(0, (240, 240, 240), (240, 240, 240), 1)
    win = _window("Экран", 800, 480, "Слой 1", [
        _wid(0x2D, (0, 0, 799, 479), _attrs(0, (0, 128, 192), (192, 192, 192), 1), first=True),
        _draw(DRAW_DEF, 4, (10, 10, 300, 60), _attrs(1, (0, 84, 128), (255, 255, 255), 2)),
        _wid(0x15, (20, 15, 120, 50), plain, [("Нория 3", "Droid Sans Fallback", 20, 20, 15, (255, 255, 255), 0)]),
        _wid(0x17, (400, 100, 460, 160), plain, [("", "Arial", 12, 400, 100, (0, 0, 0), 1)] * 2, pic=PIC),
        _wid(0x1B, (500, 100, 580, 140), plain, [("Пуск", "Arial", 12, 510, 110, (0, 0, 0), 1)], pic=SHAPE),
    ])
    keypad = _window("Клавиатура", 300, 200, "Слой 1", [
        _wid(0x2D, (0, 0, 299, 199), plain),
        _wid(0x15, (10, 10, 100, 40), plain, [("1", "Arial", 12, 10, 10, (0, 0, 0), 0)])])
    # the settings after the windows: class 10 = 1 + 2 classes + 7 objects
    settings = b"\xff\xff\x01\x00\x0a\x00CTagObject" + bytes(12) + (b"\x0a\x80" + bytes(14)) * 3
    body = (b"\x00" * 8 + lib + groups + bytes(16) + win + keypad + bytes(2000) + _tags(_tag("GVL.A", "BOOL"))
            + settings)
    return body + b"\x00" * ((-len(body)) % 16)


def _look_project() -> bytes:
    return ebpcmtp.Project(378, "6.10.02.300", True, _look_payload()).dump()


def test_scene_pictures_and_colours(tmp_path):
    from PIL import Image
    p = ebpcmtp.Project.load(_look_project())
    assert {g: v["format"] for g, v in p.pictures().items()} == {IMG.hex(): "png", IMG2.hex(): "bmp"}
    sc = p.scene("Экран", tmp_path / "img")
    assert sc["window"] == {"number": 0, "name": "Экран", "width": 800, "height": 480, "background": "#0080c0"}
    win, rect, text, lamp, sw = sc["objects"]
    assert [o["kind"] for o in sc["objects"]] == ["Window", "Rect", "Text", "BitLamp", "ToggleSwitch"]
    assert (rect["fill"], rect["frame"], rect["frame_width"], rect["bbox"]) == ("#005480", "#ffffff", 2, [10, 10, 300, 60])
    assert text["fill"] is None and text["labels"] == [{"text": "Нория 3", "font": "Droid Sans Fallback", "size": 20,
                                                        "x": 20, "y": 15, "color": "#ffffff", "align": "left"}]
    assert [(s["image"], s["transparent"]) for s in lamp["picture"]] == [(IMG.hex(), None), (IMG2.hex(), "#00d2ff")]
    assert len(lamp["labels"]) == 2 and lamp["labels"][0]["align"] == "center"
    assert sw["shape"] == SHAPE.hex() and "picture" not in sw and sw["labels"][0]["text"] == "Пуск"
    files = sc["images"]
    assert set(files) == {IMG.hex(), IMG2.hex() + "-00d2ff"} == {s["key"] for s in lamp["picture"]}
    assert open(files[IMG.hex()], "rb").read() == _png()
    im = Image.open(files[IMG2.hex() + "-00d2ff"])          # the bitmap with its transparent colour -> PNG with alpha
    assert im.mode == "RGBA" and im.getpixel((0, 1)) == (255, 255, 255, 255)
    assert im.getpixel((1, 1))[3] == im.getpixel((0, 0))[3] == 0
    data, kind = p.picture(IMG2.hex())
    assert kind == "png" and Image.open(io.BytesIO(data)).getpixel((1, 1)) == (0, 210, 255, 255)
    with pytest.raises(ValueError):
        p.picture("00" * 16)
    assert p.scene("Экран")["images"] == {}                # nothing written without images_dir


def test_style_changes_only_known_appearance_fields():
    import ebpedit
    p = ebpcmtp.Project.load(_look_project())
    before = p.scene("Экран")
    assert ebpedit.style(p, "Экран", 1, {"fill": "#0aB123", "frame": "#123456", "frame_width": 3}) == {
        "fill": "#0ab123", "frame": "#123456", "frame_width": 3}
    assert ebpedit.style(p, "Экран", 2, {"text_color": "#102030", "font_size": 28}) == {
        "text_color": "#102030", "font_size": 28}
    assert ebpedit.style(p, "Экран", 0, {"fill": "#223344"}) == {"fill": "#223344"}
    q = ebpcmtp.Project.load(p.dump())
    scene = q.scene("Экран")
    assert scene["window"]["background"] == "#223344"
    assert (scene["objects"][1]["fill"], scene["objects"][1]["frame"], scene["objects"][1]["frame_width"]) == (
        "#0ab123", "#123456", 3)
    assert scene["objects"][2]["labels"][0]["size"] == 28
    assert scene["objects"][2]["labels"][0]["color"] == "#102030"
    assert scene["objects"][3:] == before["objects"][3:]
    ebpedit.style(q, "Экран", 1, {"fill": None})
    assert q.scene("Экран")["objects"][1]["fill"] is None
    original = q.payload
    for index, bad in [(1, {"fill": "red"}), (1, {"frame_width": -1}), (2, {"fill": "#ffffff"}),
                       (2, {"font_size": 0}), (2, {"label": 8, "font_size": 15}), (0, {"fill": None})]:
        with pytest.raises(ValueError):
            ebpedit.style(q, "Экран", index, bad)
        assert q.payload == original


def test_bind_tag_one_existing_slot_only():
    import ebpedit
    p = _ui_project()
    before = p.window("Главное")["objects"][2]
    out = ebpedit.bind_tag(p, "Главное", 2, 1, "GVL.B")
    assert out == {"slot": 1, "old": "GVL.A", "tag": "GVL.B", "type": "BOOL"}
    q = ebpcmtp.Project.load(p.dump())
    obj = q.window("Главное")["objects"][2]
    assert [(a["slot"], a["tag"]) for a in obj["addresses"]] == [(1, "GVL.B"), (2, "GVL.A")]
    assert [d[2] for d in obj["_dev"]] == [d[2] for d in before["_dev"]]
    original = q.payload
    for slot, tag in [(3, "GVL.A"), (1, "GVL.N"), (1, "missing"), (0, "GVL.A"), (True, "GVL.A")]:
        with pytest.raises(ValueError):
            ebpedit.bind_tag(q, "Главное", 2, slot, tag)
        assert q.payload == original


def test_mcp_style_and_tag_binding_are_atomic(tmp_path):
    import server
    src, dst = tmp_path / "in.cmtp", tmp_path / "out.cmtp"
    src.write_bytes(_ui_project().dump())
    args = {"path": str(src), "out_file": str(dst), "edits": [
        {"op": "move", "window": "Главное", "object": 3, "dx": 5},
        {"op": "bind_tag", "window": "Главное", "object": 2, "slot": 1, "new": "GVL.B"}]}
    result = server._call("cmtp_edit", args)
    assert not result.get("isError"), result
    assert [r["op"] for r in result["structuredContent"]["results"]] == ["move", "bind_tag"]
    q = ebpcmtp.Project.load(dst)
    assert q.scene("Главное")["objects"][3]["bbox"] == [15, 10, 505, 90]
    assert q.window("Главное")["objects"][2]["addresses"][0]["tag"] == "GVL.B"
    saved = dst.read_bytes()
    args["edits"].append({"op": "bind_tag", "window": "Главное", "object": 2, "slot": 2, "new": "GVL.N"})
    result = server._call("cmtp_edit", args)
    assert result.get("isError") and dst.read_bytes() == saved
    assert src.read_bytes() == _ui_project().dump()
    look = tmp_path / "look.cmtp"
    look.write_bytes(_look_project())
    result = server._call("cmtp_edit", {"path": str(look), "out_file": str(dst), "edits": [
        {"op": "style", "window": "Экран", "object": 1, "fill": "#123456"}]})
    assert not result.get("isError"), result
    assert ebpcmtp.Project.load(dst).scene("Экран")["objects"][1]["fill"] == "#123456"


@pytest.mark.skipif(not shutil.which("node"), reason="нужен Node.js")
def test_real_canvas_style_and_binding_with_undo(tmp_path):
    desk = SEED.parent.parent.parent / "agents" / "desktop"
    look, ui = tmp_path / "look.cmtp", tmp_path / "ui.cmtp"
    look.write_bytes(_look_project())
    ui.write_bytes(_ui_project().dump())
    script = r"""
const { Canvas } = require('./lib/canvas');
const a = JSON.parse(process.env.CANVAS_TEST);
(async () => {
  const c = new Canvas({home:a.home, cache:a.cache, tool:{command:a.python,args:[a.server],cwd:a.cwd},watch:60000});
  try {
    await c.open(a.look, '0');
    await c.edit('Агент', [{op:'style',object:1,fill:'#123456'}, {op:'style',object:2,font_size:29,text_color:'#abcdef'}]);
    const style = c.state().scene.objects;
    await c.undo();
    const undone = c.state().scene.objects;
    await c.open(a.ui, '0');
    await c.edit('Агент', [{op:'bind_tag',object:2,slot:1,new:'GVL.B'}]);
    const tags = c.state().scene.objects[2].tags;
    await c.undo();
    const old = c.state().scene.objects[2].tags;
    process.stdout.write(JSON.stringify({fill:style[1].fill, label:style[2].labels[0],
      undoFill:undone[1].fill,tags,old}));
  } finally { c.stop(); }
})().catch(e=>{process.stderr.write(String(e.stack));process.exit(1)});
"""
    env = {**os.environ, "CANVAS_TEST": json.dumps({"home": str(tmp_path / "home"), "cache": str(tmp_path / "cache"),
           "python": sys.executable, "server": str(SEED / "server.py"), "cwd": str(SEED),
           "look": str(look), "ui": str(ui)})}
    r = subprocess.run([shutil.which("node"), "-e", script], cwd=desk, env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["fill"] == "#123456" and out["undoFill"] == "#005480"
    assert (out["label"]["size"], out["label"]["color"]) == (29, "#abcdef")
    assert out["tags"] == ["GVL.B", "GVL.A"] and out["old"] == ["GVL.A", "GVL.A"]


def test_check_warns_about_compact_opaque_widget():
    import ebpedit
    p = ebpcmtp.Project.load(_look_project())
    w = p.window("Экран")
    compact = b"\x01\x80" + ebpcmtp._OPAQUE_PART_4A + bytes(200)
    count_at = w["_first"] - 4
    insert_at = w["objects"][3]["offset"]
    p._splice([(count_at, count_at + 4, struct.pack("<I", w["declared"] + 1)),
               (insert_at, insert_at, compact)])
    w = p.window("Экран")
    assert len(w["objects"]) == w["declared"]
    assert w["objects"][3]["kind"] == "part_4a"
    assert any(x["code"] == "opaque_object" and x["object"] == 3 for x in ebpedit.check(p, "Экран"))


def test_clone_multistate_keeps_each_picture_and_independent_geometry():
    p = ebpcmtp.Project.load(_look_project())
    lamp = p.window("Экран")["objects"][3]
    assert lamp["kind"] == "BitLamp"
    # Use the same two-state picture-group layout with a MultiState widget type.
    at = lamp["offset"] + 2
    p.payload = p.payload[:at] + b"\x18" + p.payload[at + 1:]
    before = p.scene("Экран")["objects"][3]
    index = p.clone_object("Экран", 3, to="Клавиатура", dx=-390, dy=-90)
    copy = p.scene("Клавиатура")["objects"][index]
    assert copy["kind"] == "MultiState?" and copy["bbox"] == [10, 10, 70, 70]
    assert copy["picture"] == before["picture"] and len(copy["picture"]) == 2
    assert p.scene("Экран")["objects"][3]["bbox"] == [400, 100, 460, 160]
    assert ebpcmtp.Project.load(p.dump()).scene("Клавиатура")["objects"][index]["picture"] == before["picture"]


def test_picture_states_are_independent_and_checked(tmp_path):
    import ebpedit
    import server
    p = ebpcmtp.Project.load(_look_project())
    clone = p.clone_object("Экран", 3, to="Клавиатура", dx=-390, dy=-90)
    old_copy = p.scene("Клавиатура")["objects"][clone]["picture"]
    before = p.payload
    with pytest.raises(ValueError, match="count cannot be changed"):
        ebpedit.picture_states(p, "Экран", 3, [{"png": _png()}])
    assert p.payload == before
    with pytest.raises(ValueError):
        ebpedit.picture_states(p, "Экран", 3, [{"png": b"not png"}, {"image": IMG2.hex()}])
    assert p.payload == before
    corrupt = bytearray(_png())
    corrupt[-5] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        ebpedit.picture_states(p, "Экран", 3, [{"png": bytes(corrupt)}, {"image": IMG2.hex()}])
    assert p.payload == before
    green = _png((0, 255, 0, 255))
    green_file = tmp_path / "green.png"
    green_file.write_bytes(green)
    src, dst = tmp_path / "project.cmtp", tmp_path / "out.cmtp"
    src.write_bytes(p.dump())
    result = server._call("cmtp_edit", {"path": str(src), "out_file": str(dst), "edits": [
        {"op": "picture_states", "window": "Экран", "object": 3, "states": [
            {"image_file": str(green_file)}, {"image": IMG2.hex(), "transparent": "#00D2FF"}]}]})
    assert not result.get("isError"), result
    q = ebpcmtp.Project.load(dst.read_bytes())
    changed = q.scene("Экран")["objects"][3]["picture"]
    assert q.picture(changed[0]["image"])[0] == green
    assert changed[1]["image"] == IMG2.hex() and changed[1]["transparent"] == "#00d2ff"
    assert q.scene("Клавиатура")["objects"][clone]["picture"] == old_copy
    assert src.read_bytes() == p.dump()
    result = server._call("cmtp_edit", {"path": str(src), "out_file": str(dst), "edits": [
        {"op": "picture_states", "window": "Экран", "object": 3, "states": [
            {"image_file": "relative.png"}, {"image": IMG2.hex()}]}]})
    assert result.get("isError")


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
        assert rpc(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"] == {
            "name": "ebpro-cmtp", "title": "EasyBuilder Pro .cmtp/.emtp", "version": "1.5.0"}
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
        look = tmp_path / "look.cmtp"
        look.write_bytes(_look_project())
        err, text = call(17, "cmtp_scene", path=str(look), window="0", images_dir=str(tmp_path / "li"))
        sc = json.loads(text)
        assert not err and sc["window"]["background"] == "#0080c0" and len(sc["images"]) == 2
        res = rpc(18, "tools/call", {"name": "cmtp_edit", "arguments": {"path": str(look), "out_file": str(look), "edits": [
            {"op": "move", "window": "Экран", "object": 2, "dx": 5, "dy": 0},
            {"op": "clone_object", "window": "Экран", "object": 2, "dy": 100}]}})["result"]
        assert not res.get("isError"), res
        assert res["structuredContent"]["results"] == [{"op": "move", "window": 0, "object": 2, "bbox": [25, 15, 125, 50]},
                                                       {"op": "clone_object", "window": 0, "object": 5}]
        err, text = call(19, "cmtp_scene", path=str(look), window="Экран")      # the same file, edited in place
        assert [o["bbox"] for o in json.loads(text)["objects"][2::3]] == [[25, 15, 125, 50], [25, 115, 125, 150]]
        assert not list(tmp_path.glob("*.sysai-tmp"))
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert proc.returncode == 0


CANVAS = r"""
const { Canvas } = require('./lib/canvas');
const a = JSON.parse(process.env.CANVAS_TEST);
(async () => {
  const cv = new Canvas({ home: a.home, cache: a.cache, tool: { command: a.python, args: [a.server], cwd: a.cwd }, watch: 60000 });
  const out = { open: await cv.open(a.file) };
  const s = cv.state();
  out.kinds = s.scene.objects.map((o) => o.kind);
  out.images = Object.values(s.scene.images);
  out.edit = await cv.edit('Агент', [{ op: 'move', object: 1, dx: 5, dy: 7 }, { op: 'text', object: 2, old: 'Нория 3', new: 'Нория 33' }]);
  require('node:fs').copyFileSync(a.file, a.mid);
  out.cursor = cv.state().cursors[0];
  out.look = cv.look();
  out.copy = await cv.edit('Помощник 1', [{ op: 'clone_object', object: 3, dx: 0, dy: 60 }]);
  out.objects = cv.state().scene.objects.length;
  out.undo = [await cv.undo(), await cv.undo()];
  cv.stop();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e.stack)); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="нужен Node.js")
def test_the_window_s_canvas_draws_and_edits_with_this_tool(tmp_path):
    """«Холст» окна «Агент SYSAI» (agents/desktop/lib/canvas.js) runs this very server: the scene, edits in place, undo."""
    from urllib.parse import unquote, urlparse
    desk = SEED.parent.parent.parent / "agents" / "desktop"
    src = tmp_path / "велута.cmtp"
    src.write_bytes(_look_project())
    original = src.read_bytes()
    env = {**os.environ, "CANVAS_TEST": json.dumps({"home": str(tmp_path / "home"), "cache": str(tmp_path / "cache"),
           "python": sys.executable, "server": str(SEED / "server.py"), "cwd": str(SEED), "file": str(src),
           "mid": str(tmp_path / "после правки.cmtp")})}
    r = subprocess.run([shutil.which("node"), "-e", CANVAS], cwd=desk, env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["open"] == {"open": True, "file": str(src), "window": {"number": 0, "name": "Экран", "width": 800, "height": 480},
                           "objects": 4, "windows": 2}
    assert out["kinds"] == ["Window", "Rect", "Text", "BitLamp", "ToggleSwitch"]
    assert len(out["images"]) == 2 and all(Path(unquote(urlparse(u).path.lstrip("/") if os.name == "nt" else urlparse(u).path)).is_file()
                                           for u in out["images"])
    assert out["edit"]["objects"] == [1, 2] and out["edit"]["window"] == 0 and out["edit"]["undo"] == 1
    assert (out["cursor"]["who"], out["cursor"]["x"], out["cursor"]["y"]) == ("Агент", 160, 42), "at the moved rectangle"
    rect, text = out["look"]["objects"][:2]
    assert rect["bbox"] == [15, 17, 305, 67] and text["text"] == "Нория 33"
    assert out["look"]["recent"][-1]["who"] == "Агент"
    assert out["copy"]["objects"] == [5] and out["objects"] == 6
    w = ebpcmtp.Project.load(tmp_path / "после правки.cmtp").window("Экран")
    assert w["objects"][1]["bbox"] == [15, 17, 305, 67], "the project itself was edited"
    assert [lb["text"] for lb in w["objects"][2]["labels"]] == ["Нория 33"]
    assert [u["undone"].split(" ")[0] for u in out["undo"]] == ["копия", "сдвиг"]
    assert src.read_bytes() == original, "undone to the very bytes"


# -- building a whole project: the menu (function keys), deleting, new tags, sizes, checking ----------------------------
import ebpedit  # noqa: E402
import ebprender  # noqa: E402


def _fk(bbox, action, target, text):
    """A FunctionKey: what it does sits right after one of its strings — u8 action, 0x0d, u16 window, 08 00."""
    rec = _part(0x21, bbox, labels=[(text, "Arial", 16, bbox[0] + 4, bbox[1] + 4)])
    return rec[:-8] + _cs("") + bytes([action, 0x0d]) + struct.pack("<H", target) + b"\x08\x00" + rec[-8:]


def _menu_payload(tag="GVL.A") -> bytes:
    main = _window("Главное", 1024, 768, "Layer 1", [
        _part(0x2D, (0, 0, 1023, 767), first=True),
        _part(0x15, (100, 20, 220, 60), labels=[("Главное", "Arial", 24, 100, 20)]),
        _fk((20, 700, 140, 750), 0, 1, "Нория 1"),          # change the window to 1
        _fk((160, 700, 280, 750), 2, 2, "Настройки"),       # open the pop-up 2
        _draw(DRAW_DEF, 4, (10, 10, 500, 90)),
    ])
    noria = _window("Нория 1", 1024, 768, "Layer 1", [
        _part(0x2D, (0, 0, 1023, 767)),
        _part(0x15, (100, 20, 220, 60), labels=[("Нория 1", "Arial", 24, 100, 20)]),
        _part(0x1B, (300, 100, 380, 140), devices=("OPC UA Client", "OPC UA Client", "Local HMI"), atypes=b"\xc1\xc1\x00",
              addrs=(_opc(tag), _opc(tag), "0"), labels=[("", "Arial", 16, 300, 100)]),
        _fk((20, 700, 140, 750), 0, 0, "Назад"),
    ])
    popup = _window("Настройки", 400, 300, "Layer 1", [
        _part(0x2D, (0, 0, 399, 299)),
        _part(0x15, (10, 10, 200, 40), labels=[("Уставки", "Arial", 16, 10, 10)]),
        _fk((10, 250, 100, 290), 3, 0, "Закрыть"),
    ])
    tags = _tags(_tag("GVL.A", "BOOL"), _tag("GVL.B", "BOOL"), _tag("GVL.N", "INT"), _tag("UaStatus Code", "STRING", ns=10))
    # class 15 = 1 + 2 classes + 12 objects
    settings = b"\xff\xff\x01\x00\x0a\x00CTagObject" + bytes(12) + (b"\x0f\x80" + bytes(14)) * 3
    body = b"\x00" * 8 + main + noria + popup + bytes(2000) + tags + settings
    return body + b"\x00" * ((-len(body)) % 16)


def _menu_project(**kw) -> ebpcmtp.Project:
    return ebpcmtp.Project.load(ebpcmtp.Project(378, "6.10.02.300", True, _menu_payload(**kw)).dump())


def _reload(p) -> ebpcmtp.Project:
    return ebpcmtp.Project.load(p.dump())


def test_menu_links_and_redirecting_a_key():
    p = _menu_project()
    assert [(w["number"], w["name"]) for w in p.windows()] == [(0, "Главное"), (1, "Нория 1"), (2, "Настройки")]
    assert [(x["window"], x["object"], x["action"], x["target"]) for x in p.links()] == [
        (0, 2, "window", 1), (0, 3, "popup", 2), (1, 3, "window", 0), (2, 2, "close", None)]
    assert [x["object"] for x in p.links("Нория 1")] == [3]
    assert ebpedit.link(p, "Главное", 2, "Настройки") == 2
    q = _reload(p)
    assert q.links(0)[0]["target"] == 2 and q.links(0)[1]["target"] == 2
    with pytest.raises(ValueError):
        ebpedit.link(q, "Настройки", 2, 1)          # «close» does not open a window
    with pytest.raises(ValueError):
        ebpedit.link(q, "Главное", 1, 1)            # a Text is no key
    with pytest.raises(ValueError):
        ebpedit.link(q, "Главное", 2, "Нет такого")  # no window of that name
    assert ebpedit.link(q, "Главное", 2, 77) == 77    # a number: the window may come later, cmtp_check tells
    assert {(i["object"], i["code"]) for i in ebpedit.check(q, 0)} == {(2, "link_missing")}


def test_delete_objects_and_windows():
    p = _menu_project()
    assert p.clone_window("Нория 1", "Нория 2", number=12) == 12
    ebpedit.link(p, "Главное", 2, 12)
    left = ebpedit.delete_window(p, 12)
    assert left == [{"window": 0, "object": 2}] or [(x["window"], x["object"]) for x in left] == [(0, 2)]
    q = _reload(p)
    assert [(w["number"], w["name"]) for w in q.windows()] == [(0, "Главное"), (1, "Нория 1"), (2, "Настройки")]
    assert q.payload.count(b"\x0f\x80" + bytes(14)) == 3                   # the class numbers are back
    with pytest.raises(ValueError):
        ebpedit.delete_window(q, 1)                                       # windows 0-9 belong to EasyBuilder
    ebpedit.delete_object(q, "Нория 1", 1)
    r = _reload(q)
    assert [o["kind"] for o in r.window(1)["objects"]] == ["Window", "ToggleSwitch", "FunctionKey"]
    assert r.window(1)["declared"] == 3 and r.payload.count(b"\x0e\x80" + bytes(14)) == 3
    assert [(x["object"], x["target"]) for x in r.links(1)] == [(2, 0)]
    ebpedit.delete_object(r, "Нория 1", 2)                                # the last object of a window
    s = _reload(r)
    assert [o["kind"] for o in s.window(1)["objects"]] == ["Window", "ToggleSwitch"]
    assert s.window("Настройки")["objects"][1]["labels"][0]["text"] == "Уставки"
    assert [(x["window"], x["object"], x["target"]) for x in s.links()] == [(0, 2, 12), (0, 3, 2), (2, 2, None)]
    # the window frame; the Rect holding the CDrawObject class; an object before that class definition
    for bad in ((0, 0), (0, 4), (0, 1), (1, 0)):
        with pytest.raises(ValueError):
            ebpedit.delete_object(s, *bad)


def test_new_tags_and_their_use():
    p = _menu_project()
    assert ebpedit.add_tags(p, [{"name": "GVL.C", "type": "BOOL"},
                                {"name": "GVL.M", "type": "int", "node": "ns:2;s:Other.M"}]) == ["GVL.C", "GVL.M"]
    q = _reload(p)
    assert [(t["name"], t["type"], t["node"]) for t in q.opcua_tags()] == [
        ("GVL.A", "BOOL", "ns:2;s:GVL.A"), ("GVL.B", "BOOL", "ns:2;s:GVL.B"), ("GVL.N", "INT", "ns:2;s:GVL.N"),
        ("GVL.C", "BOOL", "ns:2;s:GVL.C"), ("GVL.M", "INT", "ns:2;s:Other.M"), ("UaStatus Code", "STRING", "ns:10;s:UaStatus Code")]
    assert ebpedit.add_tags(q, [{"name": "GVL.C", "type": "BOOL"}]) == []              # already there
    for bad in ({"name": "GVL.R", "type": "REAL"}, {"name": "GVL.C", "type": "INT"}, {"name": "GVL.X", "type": "BOOL", "node": "x"}):
        with pytest.raises(ValueError):
            ebpedit.add_tags(q, [bad])
    assert q.retag("Нория 1", "GVL.A", "GVL.C") == 2
    r = _reload(q)
    assert [a["tag"] for a in r.window(1)["objects"][2]["addresses"]] == ["GVL.C", "GVL.C"]


def test_resize_keeps_the_corner_and_moves_centred_labels():
    p = ebpcmtp.Project.load(_look_project())
    assert ebpedit.resize(p, "Экран", 4, 101, 61) == [500, 100, 600, 160]
    assert ebpedit.resize(p, "Экран", 3, 80, None) == [400, 100, 479, 160]
    q = _reload(p)
    lamp, sw = q.scene(0)["objects"][3:5]
    assert sw["bbox"] == [500, 100, 600, 160] and (sw["labels"][0]["x"], sw["labels"][0]["y"]) == (520, 120)
    assert lamp["bbox"] == [400, 100, 479, 160] and (lamp["labels"][0]["x"], lamp["labels"][0]["y"]) == (400, 100)
    for w, h in ((2, 50), (50, 5000)):
        with pytest.raises(ValueError):
            ebpedit.resize(q, "Экран", 4, w, h)
    with pytest.raises(ValueError):
        ebpedit.resize(q, "Экран", 0, 100, 100)     # the window frame


def test_compact_opaque_widget_is_a_boundary_but_cannot_be_copied():
    compact = b"\x01\x80" + ebpcmtp._OPAQUE_PART_4A + bytes(200)
    normal = _part(0x15, (10, 10, 100, 50), first=True)
    blob = normal + compact + _part(0x15, (10, 80, 100, 120))
    heads = ebpcmtp._heads(blob)
    assert [h[2] for h in heads] == [0x15, 0x4a, 0x15]
    assert heads[1][0] == len(normal)
    info = {"ptag": b"\x01\x80", "nested": [], "defs": []}
    with pytest.raises(ValueError, match="opaque part_4a"):
        ebpcmtp._check_range(blob, len(normal), len(normal) + len(compact), info)
    ebpcmtp._check_range(blob, 0, len(normal), info)
    # Partial lookalikes inside other data must not create a boundary.
    assert len(ebpcmtp._heads(normal + b"\x01\x80\x4a\x01\x00\x06" + bytes(20))) == 1


@pytest.mark.parametrize("left", [48, 568])
def test_rectangle_detection_when_bottom_is_multiple_of_256(left):
    import ebpedit
    p = _ui_project()
    p.move_object("Главное", 1, left - 100, 715)
    ebpedit.resize(p, "Главное", 1, 225, 34)
    obj = p.window("Главное")["objects"][1]
    assert obj["bbox"] == [left, 735, left + 224, 768]
    assert (obj["labels"][0]["x"], obj["labels"][0]["y"]) == (left, 735)
    p.move_object("Главное", 1, 2, 3)
    assert p.window("Главное")["objects"][1]["bbox"] == [left + 2, 738, left + 226, 771]


def test_check_finds_what_the_panel_would_show_wrong():
    p = _menu_project(tag="GVL.LOST")
    assert p.clone_window("Нория 1", "Нория 2", number=12) == 12
    assert p.clone_window("Нория 1", number=13) == 13                   # the same name twice
    ebpedit.resize(p, 12, 1, 2000, 40)                                   # beyond the window
    p.edit_text(12, "Назад", "Назад к главному окну проекта", 3)        # a key label wider than the key
    at, _, _ = ebpcmtp.fk_action(p.payload, p.window(0)["objects"][3])
    p.payload = p.payload[:at + 2] + struct.pack("<H", 50) + p.payload[at + 4:]   # a key into no window
    p._windows_cache = None
    q = _reload(p)
    got = {(i["level"], i["window"], i["object"], i["code"]) for i in ebpedit.check(q)}
    assert ("error", 0, 3, "link_missing") in got
    assert ("error", 1, 2, "tag_missing") in got
    assert ("warning", 12, 1, "outside") in got and ("warning", 12, 3, "text_fit") in got
    assert any(i[3] == "duplicate_name" for i in got)
    assert ("info", 12, None, "unreachable") in got
    assert not any(i[1] in (0, 1, 2) and i[3] == "outside" for i in got)
    assert {i["code"] for i in ebpedit.check(q, 0)} == {"link_missing"}


# -- taking a window and objects from a donor project: pictures, tags, keys ----------------------------------------------
IMG3, IMG4 = bytes(range(64, 80)), bytes(range(80, 96))


def _donor_payload(rect=False) -> bytes:
    """Another project of the same EasyBuilder: a green PNG (new), the same bitmap as the project's under another GUID,
    a picture object under the project's own key, a switch on a tag the project does not have, a key into «Экран»."""
    green, bmp = _png((0, 255, 0, 255)), _bmp()
    lib = (struct.pack("<I", 2) + b"\xff" + IMG3 + struct.pack("<I", len(green)) + green
           + b"\xff" + IMG4 + struct.pack("<I", len(bmp)) + bmp)
    groups = _groups([("lamp", [(IMG3, None), (IMG4, (0, 210, 255))], PIC)])
    plain = _attrs(0, (240, 240, 240), (240, 240, 240), 1)
    first = _window("Экран", 800, 480, "Слой 1", [
        _wid(0x2D, (0, 0, 799, 479), _attrs(0, (0, 128, 192), (192, 192, 192), 1), first=True)]
        + ([_draw(DRAW_DEF, 4, (10, 10, 300, 60), _attrs(1, (0, 84, 128), (255, 255, 255), 2))] if rect else [])
        + [_wid(0x15, (20, 15, 120, 50), plain, [("Донор", "Arial", 20, 20, 15, (255, 255, 255), 0)])])
    noria = _window("Нория 5", 800, 480, "Слой 1", [
        _wid(0x2D, (0, 0, 799, 479), _attrs(0, (0, 128, 192), (192, 192, 192), 1)),
        _wid(0x15, (20, 15, 120, 50), plain, [("Нория 5", "Arial", 20, 20, 15, (255, 255, 255), 0)]),
        _wid(0x17, (400, 100, 460, 160), plain, [("", "Arial", 12, 400, 100, (0, 0, 0), 1)] * 2, pic=PIC),
        _wid(0x1B, (500, 100, 580, 140), plain, [("Пуск", "Arial", 12, 510, 110, (0, 0, 0), 1)], pic=SHAPE,
             devices=("OPC UA Client", "OPC UA Client", "Local HMI"), atypes=b"\xc1\xc1\x00",
             addrs=(_opc("GVL.D"), _opc("GVL.D"), "0")),
        _fk((20, 400, 140, 450), 0, 0, "Назад"),
        _wid(0x15, (200, 15, 300, 50), plain, [("5", "Arial", 20, 200, 15, (255, 255, 255), 0)])])
    # the class after the windows: 1 + classes (CPartObject, CDrawObject with the rectangle) + objects (2 or 3, 6)
    ref = struct.pack("<H", 0x8000 | (12 if rect else 10))
    settings = b"\xff\xff\x01\x00\x0a\x00CTagObject" + bytes(12) + (ref + bytes(14)) * 3
    body = (b"\x00" * 8 + lib + groups + bytes(16) + first + noria + bytes(2000)
            + _tags(_tag("GVL.A", "BOOL"), _tag("GVL.D", "BOOL")) + settings)
    return body + b"\x00" * ((-len(body)) % 16)


def _donor(revision=378, rect=False) -> ebpcmtp.Project:
    return ebpcmtp.Project.load(ebpcmtp.Project(revision, "6.10.02.300", True, _donor_payload(rect)).dump())


def test_donor_project_reads():
    assert [o["kind"] for o in _donor(rect=True).window(0)["objects"]] == ["Window", "Rect", "Text"]
    d = _donor()
    assert [(w["number"], w["name"]) for w in d.windows()] == [(0, "Экран"), (1, "Нория 5")]
    assert [o["kind"] for o in d.window(1)["objects"]] == ["Window", "Text", "BitLamp", "ToggleSwitch", "FunctionKey", "Text"]
    assert d.links(1)[0]["target"] == 0 and [t["name"] for t in d.opcua_tags()] == ["GVL.A", "GVL.D"]
    ebpcmtp._map_info(d.payload, d.windows())


def test_import_window_brings_pictures_tags_and_keys():
    p, d = ebpcmtp.Project.load(_look_project()), _donor()
    lib0, sec0 = len(p.pictures()), p.picture_section()[0]["count"]
    r = ebpedit.import_window(p, d, "Нория 5", 5, "Нория 5 (донор)", windows={"Экран": "Экран"})
    assert r["number"] == 5 and r["pending"] == []
    q = _reload(p)
    assert [(w["number"], w["name"]) for w in q.windows()] == [(0, "Экран"), (1, "Клавиатура"), (5, "Нория 5 (донор)")]
    assert len(q.pictures()) == lib0 + 1                    # the green PNG; the same bitmap is not taken twice
    sec = q.picture_section()[0]
    assert sec["count"] == sec0 + 1 and sec["complete"]
    lamp = q.scene(5)["objects"][2]
    green, same = (s["image"] for s in lamp["picture"])
    assert same == IMG2.hex() and green not in (IMG.hex(), IMG2.hex())
    assert q.picture(green)[0] == _png((0, 255, 0, 255))
    assert q.scene(0)["objects"][3]["picture"][0]["image"] == IMG.hex(), "the project's own lamp keeps its red picture"
    assert [t["name"] for t in q.opcua_tags()] == ["GVL.A", "GVL.D"]
    assert [a["tag"] for a in q.window(5)["objects"][3]["addresses"]] == ["GVL.D", "GVL.D"]
    assert q.links(5)[0]["target"] == 0
    ebpcmtp._map_info(q.payload, q.windows())
    with pytest.raises(ValueError):
        ebpedit.import_window(q, _donor(433), "Нория 5")     # saved by another EasyBuilder
    with pytest.raises(ValueError, match="two rectangles"):  # the project's only rectangle holds the class: no reference
        ebpedit.import_window(q, _donor(rect=True), "Экран", 6)


def test_import_objects_with_a_tag_map():
    p, d = ebpcmtp.Project.load(_look_project()), _donor()
    r = ebpedit.import_objects(p, d, "Нория 5", [2, 3], "Экран", 0, 200, tags={"GVL.D": "GVL.A"})
    assert (r["first"], r["count"]) == (5, 2)
    q = _reload(p)
    objs = q.window(0)["objects"]
    assert [o["kind"] for o in objs] == ["Window", "Rect", "Text", "BitLamp", "ToggleSwitch", "BitLamp", "ToggleSwitch"]
    assert objs[5]["bbox"] == [400, 300, 460, 360] and [a["tag"] for a in objs[6]["addresses"]] == ["GVL.A", "GVL.A"]
    assert [t["name"] for t in q.opcua_tags()] == ["GVL.A"]
    assert q.scene(0)["objects"][5]["picture"][1]["image"] == IMG2.hex()
    with pytest.raises(ValueError):
        ebpedit.import_objects(q, d, "Нория 5", [0], "Экран")          # the window frame
    with pytest.raises(ValueError):
        ebpedit.import_objects(q, d, "Нория 5", [3], "Экран", tags={"GVL.D": "GVL.NOPE"})


# -- a picture of the window without EasyBuilder; fonts; the server's new tools; the lock ---------------------------------
def test_render_draws_the_window():
    from PIL import Image
    p = ebpcmtp.Project.load(_look_project())
    im = Image.open(io.BytesIO(ebprender.render(p, "Экран", 1))).convert("RGB")
    assert im.size == (800, 480)
    assert im.getpixel((700, 400)) == (0, 128, 192)            # the window background
    assert im.getpixel((250, 50)) == (0, 84, 128)              # the filled rectangle
    assert im.getpixel((430, 110)) == (255, 0, 0)              # the lamp: its first state, the red PNG, stretched
    small = Image.open(io.BytesIO(ebprender.render(p, 0, 0.5, marks=True)))
    assert small.size == (400, 240)
    w, h, rgba = ebprender.decode_png(_png((1, 2, 3, 255)))
    assert (w, h, bytes(rgba[:4])) == (3, 2, b"\x01\x02\x03\xff")
    w, h, rgba = ebprender.decode_bmp(_bmp(), "#00d2ff")
    assert (w, h) == (2, 2) and rgba[3] == 0 and bytes(rgba[8:12]) == b"\xff\xff\xff\xff"


def test_render_selects_picture_states_without_connecting_to_plc(tmp_path):
    from PIL import Image
    import server
    p = ebpcmtp.Project.load(_look_project())
    im = Image.open(io.BytesIO(ebprender.render(p, "Экран", states={"3": 1}))).convert("RGB")
    assert im.getpixel((430, 110)) == (0, 128, 192)  # transparent blue BMP reveals background
    assert im.getpixel((405, 150)) == (255, 255, 255)
    for states in ({"3": 2}, {"3": -1}, {"03": 0}, {"999": 0}, {"3": True}, {"2": 0}):
        with pytest.raises(ValueError):
            ebprender.render(p, "Экран", states=states)
    src, dst = tmp_path / "p.cmtp", tmp_path / "state.png"
    src.write_bytes(p.dump())
    result = server._call("cmtp_render", {"path": str(src), "window": "Экран", "out_file": str(dst), "states": {"3": 1}})
    assert not result.get("isError"), result
    assert Image.open(dst).convert("RGB").getpixel((430, 110)) == (0, 128, 192)
    dst.unlink()
    result = server._call("cmtp_render", {"path": str(src), "window": "Экран", "out_file": str(dst), "states": {"3": 3}})
    assert result.get("isError") and not dst.exists()


def test_negative_draw_coordinates_render_and_resize():
    from PIL import Image
    p = ebpcmtp.Project.load(_look_project())
    rect = p.window("Экран")["objects"][1]
    payload = bytearray(p.payload)
    struct.pack_into("<iiii", payload, rect["_bbox_at"], -1, -1, 300, 60)
    p.payload = bytes(payload)
    assert p.scene("Экран")["objects"][1]["bbox"] == [-1, -1, 300, 60]
    image = Image.open(io.BytesIO(ebprender.render(p, "Экран"))).convert("RGB")
    assert image.getpixel((250, 40)) == (0, 84, 128)
    assert ebpedit.resize(p, "Экран", 1, width=302) == [-1, -1, 300, 60]
    q = ebpcmtp.Project.load(p.dump())
    assert q.scene("Экран")["objects"][1]["bbox"] == [-1, -1, 300, 60]
    assert q.move_object("Экран", 1, 1, 1) == [0, 0, 301, 61]


def test_fonts_render_glyphs():
    import ttf
    chain = ttf.find("Arial") or ttf.find("Droid Sans Fallback")
    if chain is None:
        pytest.skip("на этом компьютере нет шрифтов TrueType")
    assert chain.width("Нория 3", 20) > chain.width("Н", 20) > 0
    w, h, base, alpha = chain.render("Нп", 24)
    assert w > 10 and h > 10 and max(alpha) > 200


def test_server_links_check_render_and_building_from_a_donor(tmp_path):
    import server
    menu, donor, out = tmp_path / "menu.cmtp", tmp_path / "donor.cmtp", tmp_path / "out.cmtp"
    menu.write_bytes(ebpcmtp.Project(378, "6.10.02.300", True, _menu_payload()).dump())
    donor.write_bytes(ebpcmtp.Project(378, "6.10.02.300", True, _donor_payload()).dump())
    links = json.loads(server._call("cmtp_links", {"path": str(menu)})["content"][0]["text"])
    assert links["count"] == 4 and links["links"][0]["target_name"] == "Нория 1"
    chk = json.loads(server._call("cmtp_check", {"path": str(menu)})["content"][0]["text"])
    assert chk["summary"]["error"] == 0
    r = server._call("cmtp_render", {"path": str(menu), "window": "Главное", "out_file": str(tmp_path / "w" / "0.png"),
                                     "scale": 0.25, "show": True})
    assert not r.get("isError") and (tmp_path / "w" / "0.png").read_bytes()[:4] == b"\x89PNG"
    assert r["content"][1]["type"] == "image" and r["content"][1]["mimeType"] == "image/png"
    look = tmp_path / "look.cmtp"
    look.write_bytes(_look_project())
    r = server._call("cmtp_edit", {"path": str(look), "out_file": str(out), "edits": [
        {"op": "add_tags", "tags": [{"name": "GVL.D", "type": "BOOL"}]},
        {"op": "import_window", "from": str(donor), "window": "Нория 5", "number": 12, "new": "Нория 5"},
        {"op": "import_window", "from": str(donor), "window": "Экран", "number": 13},   # its key now leads here
        {"op": "clone_object", "window": "12", "object": 4, "dx": 300},
        {"op": "link", "window": "12", "object": 6, "to": "Экран"},
        {"op": "resize", "window": "12", "object": 6, "width": 200},
        {"op": "delete_object", "window": "12", "object": 5},
        {"op": "delete_window", "window": "13"}]})
    assert not r.get("isError"), r["content"][0]["text"]
    res = r["structuredContent"]["results"]
    assert [x["op"] for x in res] == ["add_tags", "import_window", "import_window", "clone_object", "link", "resize",
                                      "delete_object", "delete_window"]
    assert res[0]["added"] == ["GVL.D"] and (res[1]["window"], res[2]["window"]) == (12, 13)
    assert (res[3]["object"], res[4]["target"], res[5]["bbox"]) == (6, 0, [320, 400, 519, 450])
    assert [(x["window"], x["object"]) for x in res[7]["links"]] == [(12, 4)]
    assert "кнопка #4 окна 12 → окно 13" in r["content"][0]["text"]
    q = ebpcmtp.Project.load(out)
    assert [(w["number"], w["name"]) for w in q.windows()] == [(0, "Экран"), (1, "Клавиатура"), (12, "Нория 5")]
    assert [(x["object"], x["target"]) for x in q.links(12)] == [(4, 13), (5, 0)]
    assert [t["name"] for t in q.opcua_tags()] == ["GVL.A", "GVL.D"]
    assert {i["code"] for i in ebpedit.check(q, 12) if i["level"] == "error"} == {"link_missing"}
    assert not list(tmp_path.glob("*.sysai-lock"))
    bad = server._call("cmtp_edit", {"path": str(menu), "out_file": str(tmp_path / "bad.cmtp"), "edits": [
        {"op": "import_window", "from": str(tmp_path / "нет.cmtp"), "window": "Нория 5"}]})
    assert bad.get("isError") and not (tmp_path / "bad.cmtp").exists()


def test_the_lock_keeps_two_writers_apart(tmp_path):
    import server
    out = tmp_path / "p.cmtp"
    with server._Lock(out, wait=0.1):
        assert Path(f"{out}.sysai-lock").exists()
        with pytest.raises(ValueError):
            with server._Lock(out, wait=0.3):
                pass
    assert not Path(f"{out}.sysai-lock").exists()
    stale = Path(f"{out}.sysai-lock")
    stale.write_text("1 0")
    os.utime(stale, (1, 1))                       # a lock left two minutes ago by a writer that died
    with server._Lock(out, wait=0.1):
        pass
    assert not stale.exists()
