"""hub/seed/ebpro-cmtp on what a real cMT project of EasyBuilder 6.10.01 holds: windows of several layers, the tag
table of the Schneider M241 server (numeric nodes, the 6.10.01 layout), arrays of nested objects (polyline points)
between the windows, and «one more mechanism» made in one edit. The same edits were opened in EasyBuilder Pro
6.10.02.421 on the real project; these synthetic projects keep that behaviour from regressing."""
import json
import struct
import zlib

import pytest

import importlib.util

from tests.test_hub_seed_ebpro import DRAW_DEF, SEED, _cs, _draw, _fk, _part, _window, ebpcmtp, ebpedit

_spec = importlib.util.spec_from_file_location("ebpro_cmtp_server", SEED / "server.py")
server = importlib.util.module_from_spec(_spec)     # under its own name: plc-kit has a server.py too
_spec.loader.exec_module(server)

POINT_DEF = b"\xff\xff\x01\x00\x0c\x00CPointObject"


def _layer(guid, name, objs):
    lay = name.encode()
    return bytes(2) + bytes([guid]) * 16 + struct.pack("<H", len(lay)) + lay + struct.pack("<I", len(objs)) + b"".join(objs)


def _layered(name, w, h, layers):
    """A window of several layers: the header says how many, each layer is a header and its objects."""
    first, rest = layers[0], layers[1:]
    lay = first[0].encode()
    head = (b"\x01\x00\x00\x00" + _cs(name) + struct.pack("<II", w, h) + bytes(10) + struct.pack("<IH", len(layers), 0)
            + bytes(range(16)) + struct.pack("<H", len(lay)) + lay + struct.pack("<I", len(first[1])) + b"".join(first[1]))
    return head + b"".join(_layer(0x40 + k, n, objs) for k, (n, objs) in enumerate(rest))


def _num(name):
    return zlib.crc32(name.encode()) ^ 0xFFFFFFFF


def _opn(name):
    return f"{name} (ns:2;i:{_num(name)})"


def _tag_b(name, typ, ns=2, numeric=True):
    """A tag entry as EasyBuilder 6.10.01 writes it: 03 'TAG' …, the identifier, nothing after it."""
    ident = str(_num(name)) if numeric else name
    full = f"{name} (ns:{ns};{'i' if numeric else 's'}:{ident})({ident})".encode()
    mid = bytes(4) + b"\xf4\x01" + bytes(9) + (b"\x00" if numeric else b"\x01")
    return (b"\x03TAG" + len(full).to_bytes(2, "big") + full + b"\x00" + bytes([len(typ)]) + typ.encode() + mid
            + ns.to_bytes(2, "big") + len(ident).to_bytes(2, "big") + ident.encode())


def _toggle(bbox, tag):
    return _part(0x1B, bbox, devices=("OPC UA Client", "OPC UA Client", "Local HMI"), atypes=b"\xc1\xc1\x00",
                 addrs=(_opn(tag), _opn(tag), "0"), labels=[("", "Arial", 16, bbox[0], bbox[1])])


def _polyline(tag, points, first_tag=None):
    """A shape with an array of nested objects: u16 count, then per point its class and 8 bytes."""
    rec = _draw(tag, 8, (10, 10, 90, 90))
    out = struct.pack("<H", len(points))
    for k, (x, y) in enumerate(points):
        out += (POINT_DEF if first_tag is None and k == 0 else (first_tag or b"\x00\x00")) + struct.pack("<II", x, y)
    return rec, out


def _payload() -> bytes:
    # map: 1 CPartObject, 2-4 the widgets of «Главное», 5 CDrawObject, 6 the rectangle, 7 the polyline,
    # 8 CPointObject, 9-11 its three points; then the objects of the other windows
    pt = b"\x08\x80"
    poly, arr = _polyline(b"\x05\x80", [(1, 2), (3, 4), (5, 6)])
    arr = struct.pack("<H", 3) + POINT_DEF + struct.pack("<II", 1, 2) + pt + struct.pack("<II", 3, 4) + pt + struct.pack("<II", 5, 6)
    main = _window("Главное", 1024, 768, "Layer 1", [
        _part(0x2D, (0, 0, 1023, 767), first=True),
        _part(0x15, (100, 20, 220, 60), labels=[("Главное", "Arial", 24, 100, 20)]),
        _fk((20, 700, 140, 750), 2, 1, "Нория 4"),
        _draw(DRAW_DEF, 4, (10, 10, 500, 90)),
        poly + arr + b"\x02",
    ])
    card = _layered("Нория 4", 600, 400, [
        ("Основной слой", [_part(0x2D, (0, 0, 599, 399)), _draw(b"\x05\x80", 4, (10, 10, 590, 80))]),
        ("Текст", [_part(0x15, (20, 20, 200, 60), labels=[("Нория 4", "Arial", 24, 20, 20)]),
                   _part(0x15, (20, 100, 200, 130), labels=[("Ручной режим", "Arial", 16, 20, 100)])]),
        ("Область касания", [_toggle((300, 100, 380, 130), "GVL.HM_noria_4"),
                             _fk((500, 10, 590, 60), 3, 0, "Закрыть")]),
    ])
    last = _window("Настройки", 400, 300, "Layer 1", [
        _part(0x2D, (0, 0, 399, 299)),
        _part(0x15, (10, 10, 200, 40), labels=[("Уставки", "Arial", 16, 10, 10)]),
        _fk((10, 250, 100, 290), 3, 0, "Закрыть"),
    ])
    tags = (struct.pack("<I", 4) + b"\x01\x00\x08\x00" + bytes(7)
            + _tag_b("GVL.HM_noria_4", "BOOL") + _tag_b("GVL.HM_noria_8", "BOOL") + _tag_b("GVL.SP_4", "INT")
            + _tag_b("UaStatus Code", "STRING", ns=10, numeric=False))
    # settings: the class after 2 classes + CPointObject, 14 objects and 3 points: 1 + 3 + 14 + 3 = 21
    settings = b"\xff\xff\x01\x00\x0a\x00CTagObject" + bytes(12) + (b"\x15\x80" + bytes(14)) * 3
    body = b"\x00" * 8 + main + card + last + bytes(2000) + tags + bytes(3) + settings
    return body + b"\x00" * ((-len(body)) % 16)


def _project() -> ebpcmtp.Project:
    return ebpcmtp.Project.load(ebpcmtp.Project(471, "6.10.01.371", True, _payload()).dump())


def _reload(p):
    return ebpcmtp.Project.load(p.dump())


def test_layers_are_read_and_every_object_knows_its_layer():
    p = _project()
    w = p.window("Нория 4")
    assert w["layers"] == [{"name": "Основной слой", "objects": 2}, {"name": "Текст", "objects": 2},
                           {"name": "Область касания", "objects": 2}]
    assert w["declared"] == len(w["objects"]) == 6
    assert [(o["kind"], o["layer"]) for o in w["objects"]] == [
        ("Window", "Основной слой"), ("Rect", "Основной слой"), ("Text", "Текст"), ("Text", "Текст"),
        ("ToggleSwitch", "Область касания"), ("FunctionKey", "Область касания")]
    assert [x["number"] for x in p.windows()] == [0, 1, 2]
    assert "layer" not in p.window("Главное")["objects"][1]          # one layer: nothing to tell
    assert not [i for i in ebpedit.check(p) if i["code"] == "not_parsed"]


def test_tag_table_of_6_10_01_with_numeric_nodes():
    p = _project()
    tab = ebpcmtp.opcua_table(p.payload)
    assert tab["style"] == "b" and tab["count"] == 4
    assert [(t["name"], t["type"], t["node"]) for t in p.opcua_tags()][:2] == [
        ("GVL.HM_noria_4", "BOOL", f"ns:2;i:{_num('GVL.HM_noria_4')}"),
        ("GVL.HM_noria_8", "BOOL", f"ns:2;i:{_num('GVL.HM_noria_8')}")]
    # the number the Schneider M241 server gives a name (the real project: VariableList.HMI_Err_DP_22_1 → 932174207)
    assert ebpcmtp.m241_node_id("VariableList.HMI_Err_DP_22_1") == 932174207
    assert ebpcmtp.m241_node_id("MOTOHOURS.MTH_KSP") == 3019551959
    # a new tag of such a project gets its number from its name, and stays before «UaStatus Code»
    assert ebpedit.add_tags(p, [{"name": "GVL.HM_noria_30", "type": "BOOL"}]) == ["GVL.HM_noria_30"]
    q = _reload(p)
    names = [(t["name"], t["node"]) for t in q.opcua_tags()]
    assert names[3] == ("GVL.HM_noria_30", f"ns:2;i:{_num('GVL.HM_noria_30')}") and names[4][0] == "UaStatus Code"
    assert ebpcmtp.opcua_table(q.payload)["count"] == 5
    with pytest.raises(ValueError):
        ebpedit.add_tags(q, [{"name": "GVL.X", "type": "BOOL", "node": "ns:2;i:abc"}])


def test_nested_arrays_are_counted_so_the_class_numbers_hold():
    p = _project()
    info = ebpcmtp._map_info(p.payload, p.windows())
    assert [(c["name"], c["tag"], len(c["refs"])) for c in info["nested"]] == [("CPointObject", 0x8008, 2)]
    assert info["range"][0] == 0x8015 and len(info["tail_refs"]) == 3


def test_copy_into_the_layer_of_the_original_and_delete_from_a_layer():
    p = _project()
    k = p.clone_object("Нория 4", 3, dx=0, dy=40)                     # a Text of the layer «Текст»
    assert k == 4
    q = _reload(p)
    w = q.window("Нория 4")
    assert [x["objects"] for x in w["layers"]] == [2, 3, 2] and w["declared"] == len(w["objects"]) == 7
    assert (w["objects"][4]["kind"], w["objects"][4]["layer"], w["objects"][4]["bbox"]) == ("Text", "Текст", [20, 140, 200, 170])
    assert w["objects"][5]["kind"] == "ToggleSwitch"                 # the next layer starts right after
    assert ebpcmtp._map_info(q.payload, q.windows())["range"][0] == 0x8016      # one more object before the settings
    # an explicit layer, by name or number; the last one — the end of the window
    assert q.clone_object("Нория 4", 3, layer="Основной слой") == 2
    assert _reload(q).window("Нория 4")["layers"][0]["objects"] == 3
    with pytest.raises(ValueError, match="no layer"):
        q.clone_object("Нория 4", 3, layer="Нет такого")
    ebpedit.delete_object(q, "Нория 4", 2)
    assert [x["objects"] for x in _reload(q).window("Нория 4")["layers"]] == [2, 3, 2]
    # the copy of the whole window keeps its layers and gets its own GUIDs
    n = q.clone_window("Нория 4", "Нория 8")
    r = _reload(q)
    a, b = r.window("Нория 4"), r.window(n)
    assert b["layers"] == a["layers"] and b["declared"] == len(b["objects"])
    assert not {x["guid"] for x in a["_layers"]} & {x["guid"] for x in b["_layers"]}


def test_one_more_mechanism_in_one_edit(tmp_path):
    src, out = tmp_path / "p.cmtp", tmp_path / "out.cmtp"
    _project().save(src)
    r = server._call("cmtp_edit", {"path": str(src), "out_file": str(out), "edits": [
        {"op": "add_tags", "tags": [{"name": "GVL.HM_noria_30", "type": "BOOL"}]},
        {"op": "clone_window", "window": "Нория 4", "new": "Нория 30",
         "map_tags": {"GVL.HM_noria_4": "GVL.HM_noria_30"}, "map_texts": {"Нория 4": "Нория 30"}},
        {"op": "clone_object", "window": "Главное", "objects": [1, 2], "dx": 300, "dy": 0, "link_to": "Нория 30"},
    ]})
    assert not r.get("isError"), r["content"][0]["text"]
    res = r["structuredContent"]["results"]
    assert res[1]["retagged"] == 2 and res[1]["tags_left"] == [] and res[2]["linked"] == 1
    q = ebpcmtp.Project.load(out)
    card = q.window("Нория 30")
    assert [lab["text"] for lab in card["objects"][2]["labels"]] == ["Нория 30"]
    assert {a["tag"] for a in card["objects"][4]["addresses"]} == {"GVL.HM_noria_30"}
    assert {a["tag"] for a in q.window("Нория 4")["objects"][4]["addresses"]} == {"GVL.HM_noria_4"}
    main = q.window("Главное")
    copies = res[2]["objects"]
    assert [main["objects"][c]["kind"] for c in copies] == ["Text", "FunctionKey"]
    assert [ln["target"] for ln in q.links("Главное")] == [1, card["number"]]
    assert not [i for i in ebpedit.check(q) if i["level"] == "error"]
    text = json.loads(server._call("cmtp_windows", {"path": str(out)})["content"][0]["text"])
    assert text["count"] == 4


def test_text_fit_is_measured_by_the_fonts_of_the_project():
    # a narrow font: every text of the project sits in a frame narrower than the usual estimate — no warning for
    # them, a warning for the one that is really cut
    def texts(n, width):
        return [_part(0x15, (20, 20 + 30 * k, 20 + width, 45 + 30 * k),
                      labels=[("Задержка перед запуском", "GOST Type AU", 20, 20, 20 + 30 * k)]) for k in range(n)]
    system = [_window(f"Системное {k}", 320, 240, "Layer 1", [_part(0x2D, (0, 0, 319, 239), first=not k)]) for k in range(10)]
    win = _window("Экран", 1024, 768, "Layer 1", [_part(0x2D, (0, 0, 1023, 767))] + texts(6, 240)
                  + [_part(0x15, (500, 20, 560, 45), labels=[("Задержка перед запуском", "GOST Type AU", 20, 500, 20)])])
    settings = b"\xff\xff\x01\x00\x0a\x00CTagObject" + bytes(12) + (b"\x13\x80" + bytes(14)) * 3   # 1 + 1 + 18
    body = b"\x00" * 8 + b"".join(system) + win + bytes(2000) + settings
    p = ebpcmtp.Project(471, "6.10.01.371", True, body + b"\x00" * ((-len(body)) % 16))
    fit = [i for i in ebpedit.check(p) if i["code"] == "text_fit"]
    assert [i["object"] for i in fit] == [7]
