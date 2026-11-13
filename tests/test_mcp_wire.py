"""Every tool server answers in ASCII only. Hermes on Windows reads a server's answers with the code page of the
system ('charmap' / cp1251): the arrow «→» in a description made the connection fail on a real PC («'charmap' codec
can't encode character '\\u2192' in position 315») and the tool went «not connected» for the whole session. JSON
escapes the characters, so the answer is the same for a reader that understands JSON and safe for one that does not."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVERS = ["plc-kit", "machine-expert", "drawing-kit", "pic-index", "doc-kit", "plc-live", "ebpro-gui", "ebpro-cmtp"]


@pytest.mark.parametrize("tool", SERVERS)
def test_the_answers_of_a_tool_server_are_ascii(tool):
    calls = ['{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}', '{"jsonrpc":"2.0","id":1,"method":"tools/list"}']
    out = subprocess.run([sys.executable, str(ROOT / "hub" / "seed" / tool / "server.py")], input=("\n".join(calls) + "\n").encode("ascii"),
                         capture_output=True, timeout=120).stdout
    lines = [x for x in out.split(b"\n") if x.strip()]
    assert len(lines) == 2, tool
    for line in lines:
        line.decode("ascii")  # raises if one character is not ASCII
        assert json.loads(line)  # and it is still JSON: the arrows come back when it is read
