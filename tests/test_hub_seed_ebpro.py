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

TOOLS = ["cmtp_info", "cmtp_labels", "cmtp_extract_images", "cmtp_unpack", "cmtp_pack", "cmtp_relabel"]


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
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert proc.returncode == 0
