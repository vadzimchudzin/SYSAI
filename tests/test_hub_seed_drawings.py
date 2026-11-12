"""hub/seed/drawing-kit — schemes in PDF are read without a model: words with their places, designators, mechanisms,
the channels of the controller. The PDF of the tests is written here by hand, letter by letter, the way a CAD draws a
text; it holds no customer data. On real schemes of three objects (kept outside the repository) the same code was
checked by hand: see docs/opo/plan-2026-11/STATUS.md, П5."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")
SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "drawing-kit"
sys.path.append(str(SEED))
import drawkit  # noqa: E402

from app.net import hub  # noqa: E402

TOOLS = ["draw_sheets", "draw_devices", "draw_channels", "draw_find", "draw_text", "draw_table"]


# the advance of a Helvetica glyph, 1/1000 of the size: letters of a word stand as a CAD puts them
_WIDTH = {" ": 278, ".": 278, "(": 333, ")": 333, "I": 278, "F": 611, "T": 611, "L": 556, "M": 833, "Q": 778, "O": 778,
          "W": 944, "i": 222, "l": 222, "r": 333, "t": 278, "f": 278, "w": 722, "m": 833, "s": 500, "c": 500, "v": 500, "k": 500}


def _pdf(pages, size=(1191, 842)) -> bytes:
    """A PDF whose every character is placed on its own, as a CAD exports text: pages — [[(x, y_from_top, text)]]."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for words in pages:
        ops = ["BT /F1 8 Tf"]
        for x, y, text in words:
            cx = x
            for ch in text:
                if ch != " ":
                    esc = ch.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                    ops.append(f"1 0 0 1 {cx:.2f} {size[1] - y:.1f} Tm ({esc}) Tj")
                cx += _WIDTH.get(ch, 556 if ch.isdigit() or ch.islower() else 700) * 8 / 1000
        ops.append("ET")
        stream = "\n".join(ops)
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] /Contents {len(objs)} 0 R "
                    "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


@pytest.fixture
def scheme(tmp_path):
    power = [(180, 300, "10QF1"), (180, 420, "10KM1"), (180, 560, "10M1"), (300, 300, "11QF1"), (300, 420, "11KM1"),
             (300, 560, "11M1"), (900, 800, "Noria 10 power")]
    inputs = [(600, 80, "TM3DI32K (A1.1)"), (640, 300, "10SR1"), (700, 300, "10SQ1"), (760, 300, "10SQ2"), (820, 300, "1X17"),
              (640, 440, "I14"), (700, 440, "I15"), (760, 440, "I16"), (820, 440, "I17"), (640, 200, "speed"), (700, 200, "belt low"),
              (300, 520, "TM3DQ32TK"), (300, 600, "Q3"), (300, 700, "10K1"), (100, 500, "1K8")]
    file = tmp_path / "materials" / "E3.pdf"
    file.parent.mkdir()
    file.write_bytes(_pdf([power, inputs, []]))
    return file


def test_the_tool_passes_the_hub_check():
    files = {str(p.relative_to(SEED)).replace(os.sep, "/"): p.read_bytes() for p in SEED.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts}
    meta, report = hub.check(files)
    assert report["errors"] == [] and meta["name"] == "drawing-kit" and meta["kind"] == "mcp"
    assert [t.strip() for t in meta["tools"].split(",")] == TOOLS
    assert "pypdfium2" in files["requirements.txt"].decode()
    assert not any(w in p.read_text(encoding="utf-8") for p in SEED.glob("*.py") for w in ("import socket", "urllib", "requests", "subprocess"))


def test_cyrillic_a_cad_wrote_as_bytes_is_put_back():
    broken = "".join(chr(int.from_bytes(ch.encode("utf-8"), "big")) for ch in "Нория") + " НС-А.10.08 10KM1"
    assert drawkit.fix_text(broken) == "Нория НС-А.10.08 10KM1"
    assert drawkit.fix_text("Нория 10KM1 한글") == "Нория 10KM1 한글"   # what is not such a pair stays
    assert drawkit.designator("10КМ1")[:3] == ("10", "KM", "1") and drawkit.designator("10km1")[3] == "контактор"   # Cyrillic «КМ»
    assert drawkit.designator("QF2")[:3] == ("", "QF", "2") and drawkit.designator("L11") is None and drawkit.designator("2.5") is None


def test_sheets_devices_mechanisms_and_channels_of_a_scheme(scheme, tmp_path):
    cache = tmp_path / "cache"
    doc = drawkit.load(str(scheme), str(cache))
    assert len(list(cache.iterdir())) == 1 and drawkit.load(str(scheme), str(cache))["pages"] == doc["pages"]   # read once
    words = {w["t"] for w in doc["pages"][0]["words"]}
    assert {"10QF1", "10KM1", "10M1", "Noria", "power"} <= words          # letters placed one by one are words again
    s = drawkit.sheets(doc)
    assert s["pages"] == 3 and s["scans"] == 1 and s["sheets"][2]["scan"]  # a sheet without words is said to be one
    assert s["sheets"][0]["kinds"] == {"QF": 2, "KM": 2, "M": 2} and s["sheets"][0]["mechanisms"] == ["10", "11"]
    assert s["sheets"][1]["modules"] == ["TM3DI32K", "TM3DQ32TK"] and s["sheets"][1]["channels"] == 5
    assert [d["name"] for d in drawkit.devices(doc, kind="km")] == ["10KM1", "11KM1"]
    assert sorted(d["name"] for d in drawkit.devices(doc, mechanism="10")) == ["10K1", "10KM1", "10M1", "10QF1", "10SQ1", "10SQ2", "10SR1"]
    mech = {m["mechanism"]: m for m in drawkit.mechanisms(doc)}
    assert mech["10"]["drive"] and mech["11"]["drive"] and mech["10"]["sheets"] == [1, 2]
    assert not mech["1"]["drive"] and "1X17" not in mech["1"]["devices"]    # a terminal is not a device of a mechanism
    chan = {c["channel"]: c for c in drawkit.channel_map(doc["pages"][1])}
    assert chan["I14"]["device"] == "10SR1" and chan["I15"]["device"] == "10SQ1" and chan["I16"]["device"] == "10SQ2"
    assert "device" not in chan["I17"]                                       # a terminal above it is not what the input reads
    assert chan["Q3"]["device"] == "10K1" and chan["Q3"]["dir"] == "out" and chan["I14"]["module"] == "TM3DI32K" and chan["Q3"]["module"] == "TM3DQ32TK"
    hit = drawkit.find(doc, "10sq2")
    assert [(h["page"], h["zone"]) for h in hit] == [(2, 6)] and "10SQ1" in hit[0]["near"]
    assert drawkit.text(doc, 2, 5)["lines"] and all("TM3DQ32TK" not in line for line in drawkit.text(doc, 2, 5)["lines"])
    assert drawkit.text(doc, 1, limit=12)["truncated"]
    with pytest.raises(drawkit.DrawError, match="листа 9 нет"):
        drawkit.text(doc, 9)
    with pytest.raises(drawkit.DrawError, match="нужен PDF"):
        drawkit.load(str(Path(__file__)))


def test_the_server_answers_in_pages(scheme, tmp_path):
    def call(name, **args):
        return {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": args}}
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
             call("draw_sheets", dir=str(scheme.parent)), call("draw_devices", file=str(scheme), mechanisms=True),
             call("draw_channels", file=str(scheme), limit=2), call("draw_find", dir=str(scheme.parent), query="10M1"),
             call("draw_text", file=str(scheme), page=2, zone=5), call("draw_table", file=str(scheme), page=1),
             call("draw_text", file=str(scheme), page=7)]
    out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(json.dumps(x) for x in lines) + "\n",
                         capture_output=True, text=True, timeout=120, encoding="utf-8",
                         env={**os.environ, "SYSAI_DRAWING_CACHE": str(tmp_path / "c")}).stdout.splitlines()
    replies = [json.loads(x) for x in out]
    assert replies[0]["result"]["serverInfo"] == {"name": "drawing-kit", "title": "Схемы и чертежи в PDF", "version": "1.0.0"}
    assert [t["name"] for t in replies[1]["result"]["tools"]] == TOOLS
    assert len(json.dumps(replies[1]["result"]["tools"], ensure_ascii=False)) < 7000     # the definitions stay small
    data = [json.loads(r["result"]["content"][0]["text"]) for r in replies[2:8]]
    assert data[0]["files"][0]["pages"] == 3 and data[0]["files"][0]["scans"] == 1
    assert data[1]["with_drive"] == 2
    assert data[2]["count"] == 5 and data[2]["returned"] == 2 and data[2]["next_offset"] == 2 and data[2]["with_device"] == 4
    assert data[3]["found"][0]["page"] == 1 and data[4]["lines"] and data[5]["rows"]
    assert replies[8]["result"]["isError"] and "листа 7 нет" in replies[8]["result"]["content"][0]["text"]
