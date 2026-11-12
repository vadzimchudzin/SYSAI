"""hub/seed/plc-live — a controller is read over OPC UA and nothing else is done to it. The controller of the tests is
an OPC UA server started here, with a variable at the node a Schneider M241 would give it."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import zlib
from pathlib import Path

import pytest

pytest.importorskip("asyncua")
ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "hub" / "seed" / "plc-live"
sys.path.append(str(SEED))
sys.path.append(str(ROOT / "agents" / "sync"))
import plclive  # noqa: E402
from sysai_agent import mcpwrap, rules as sync_rules  # noqa: E402

from app.net import access, hub  # noqa: E402

TOOLS = ["plc_scan", "plc_read", "plc_watch"]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def controller():
    """An OPC UA server on this computer: GVL.Start (BOOL), GVL.Speed (INT, counts up) at the M241 node numbers."""
    from asyncua import Server, ua
    port = _port()
    ready, stop = threading.Event(), threading.Event()
    box = {}

    async def run():
        server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
        idx = await server.register_namespace("urn:test:plc")
        box["ns"] = idx

        def node(name):
            return ua.NodeId(zlib.crc32(name.encode()) ^ 0xFFFFFFFF, idx)
        objects = server.nodes.objects
        await objects.add_variable(node("GVL.Start"), "Start", True)
        speed = await objects.add_variable(node("GVL.Speed"), "Speed", 0, ua.VariantType.Int16)
        async with server:
            ready.set()
            n = 0
            while not stop.is_set():
                await asyncio.sleep(0.1)
                n += 1
                await speed.write_value(ua.Variant(n, ua.VariantType.Int16))   # the test's controller changes its own variable
    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start()
    assert ready.wait(20), "the test OPC UA server did not start"
    yield {"port": port, "ns": box["ns"], "plc": f"127.0.0.1:{port}"}
    stop.set()
    thread.join(5)


def test_the_tool_passes_the_hub_check_and_holds_no_way_to_write():
    files = {str(p.relative_to(SEED)).replace(os.sep, "/"): p.read_bytes() for p in SEED.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts}
    meta, report = hub.check(files)
    assert report["errors"] == [] and meta["name"] == "plc-live" and meta["kind"] == "mcp"
    assert [t.strip() for t in meta["tools"].split(",")] == TOOLS
    code = "".join(p.read_text(encoding="utf-8") for p in SEED.glob("*.py"))
    # reading only: no call of the client that changes anything in a controller
    for word in ("write_value", "write_values", "write_attribute", "set_value", "call_method", "uaclient.write", "uaclient.call",
                 "delete_nodes", "add_nodes", "subprocess", "os.system"):
        assert word not in code, word


def test_which_controller_may_be_reached_is_one_rule_in_three_places():
    """The server, the synchronizer's wrapper and the tool decide alike; nothing is open until an address is named."""
    cases = [({"hosts": []}, "192.168.1.10", False), ({}, "opc.tcp://192.168.1.10:4840", False),
             ({"hosts": ["192.168.1.10"]}, "192.168.1.10", True), ({"hosts": ["192.168.1.10"]}, "opc.tcp://192.168.1.10:4840/x", True),
             ({"hosts": ["192.168.1.10"]}, "192.168.1.11", False), ({"hosts": ["192.168.1.10:4840"]}, "192.168.1.10:502", False),
             ({"hosts": ["192.168.1.10:4840"]}, "192.168.1.10:4840", True), ({"hosts": ["192.168.1.0/24"]}, "192.168.1.77:4840", True),
             ({"hosts": ["192.168.1.0/24"]}, "192.168.2.1", False), ({"hosts": ["192.168.1.0/24"]}, "192.168.1.0/28", True),
             ({"hosts": ["192.168.1.0/28"]}, "192.168.1.0/24", False), ({"hosts": ["192.168.1.10"]}, "192.168.1.0/30", False),
             ({"hosts": ["plc-line1"]}, "PLC-Line1:4840", True), ({"hosts": ["*"]}, "10.0.0.1", True), ({"hosts": ["*"]}, "", False)]
    for plc, target, want in cases:
        rules = {"plc": plc} if plc else {}
        got = [f(rules, target) for f in (access.plc_allowed, sync_rules.plc_allowed, plclive.plc_allowed)]
        assert got == [want] * 3, (plc, target, got)
    norm = access.normalize({"plc": {"hosts": ["192.168.1.10", " 192.168.1.0/24 ", "rm -rf /", "PLC-Line1"]}})
    assert norm["plc"]["hosts"] == ["192.168.1.10", "192.168.1.0/24", "plc-line1"] and access.normalize({})["plc"] == {"hosts": []}
    assert access.rules_from_form({"plc_hosts": "10.1.1.5\n10.1.2.0/24"})["plc"]["hosts"] == ["10.1.1.5", "10.1.2.0/24"]
    assert all(access.normalize(rules)["plc"] == {"hosts": []} for _, _, _, rules in access.BUILTIN)   # closed until somebody opens


def test_the_wrapper_refuses_a_controller_that_is_not_named(tmp_path):
    guard = mcpwrap.Guard(tmp_path, "plc-live", tmp_path / "access.json")
    guard.load = lambda: {"mcp": {"plc-live": ["*"]}, "plc": {"hosts": ["192.168.1.10"]}, "internet": {"mode": "all"},
                          "commands": {"run": True, "deny": []}, "folders": {"read": ["*"], "write": ["*"], "deny": []}}
    assert guard.check_call("plc_read", {"plc": "192.168.1.10", "names": ["GVL.Start"]}) == ""
    assert "не открыт" in guard.check_call("plc_read", {"plc": "192.168.1.11", "names": ["GVL.Start"]})
    assert "не открыт" in guard.check_call("plc_read", {"plc": "opc.tcp://10.0.0.5:4840"})    # the whole internet is open, a controller is not
    assert "не открыт" in guard.check_call("plc_scan", {"plc_network": "192.168.1.0/24"})
    assert "не открыт" in guard.check_call("plc_scan", {"plcs": ["192.168.1.10", "192.168.1.12"]})
    assert "не открыт" in guard.check_call("anything", {"url": "opc.tcp://10.0.0.5:4840"})     # by the look of the address too


def test_reading_and_watching_a_controller(controller, tmp_path):
    open_ = {"plc": {"hosts": ["127.0.0.1"]}}
    with pytest.raises(plclive.LiveError, match="не открыт"):
        plclive.read(controller["plc"], ["GVL.Start"], ns=controller["ns"], rules={"plc": {"hosts": []}})
    got = plclive.read(controller["plc"], ["GVL.Start", "GVL.Speed", "GVL.Нет_такой"], ns=controller["ns"], rules=open_)
    rows = {r["name"]: r for r in got["values"]}
    assert got["count"] == 3 and got["bad"] == 1 and got["plc"] == f"opc.tcp://127.0.0.1:{controller['port']}"
    assert rows["GVL.Start"]["value"] is True and rows["GVL.Start"]["type"] == "Boolean"
    assert isinstance(rows["GVL.Speed"]["value"], int) and rows["GVL.Speed"]["type"] == "Int16"
    assert not rows["GVL.Нет_такой"]["ok"] and "BadNodeIdUnknown" in rows["GVL.Нет_такой"]["error"]
    assert rows["GVL.Start"]["node"] == plclive.m241_node("GVL.Start", controller["ns"])
    out = plclive.save_csv(got["values"], str(tmp_path / "_work" / "live" / "snap.csv"))
    assert plclive.names_from_csv(out) == ["GVL.Start", "GVL.Speed", "GVL.Нет_такой"]
    seen = plclive.watch(controller["plc"], ["GVL.Start", "GVL.Speed"], seconds=1.2, period=0.2, ns=controller["ns"], rules=open_)
    by = {r["name"]: r for r in seen["variables"]}
    assert seen["reads"] >= 4 and seen["changed"] == 1
    assert by["GVL.Start"]["changes"] == 0 and by["GVL.Start"]["first"] is True
    assert by["GVL.Speed"]["changes"] >= 2 and by["GVL.Speed"]["max"] > by["GVL.Speed"]["min"] and by["GVL.Speed"]["events"][0]["t"] > 0
    found = plclive.scan([f"127.0.0.1:{controller['port']}"], rules=open_)
    assert found == [] or found[0]["host"] == "127.0.0.1"        # the standard ports are not open here; nothing is invented
    ep = asyncio.run(plclive._endpoints(f"opc.tcp://127.0.0.1:{controller['port']}"))
    assert ep["anonymous"] and any(s.startswith("None") for s in ep["security"])
    with pytest.raises(plclive.LiveError, match="не ответил по OPC UA"):
        plclive.read(f"127.0.0.1:{_port()}", ["GVL.Start"], rules=open_)
    with pytest.raises(plclive.LiveError, match="больше 256"):
        plclive.hosts_of(network="10.0.0.0/16")
    with pytest.raises(plclive.LiveError, match="opc.tcp"):
        plclive.endpoint("http://127.0.0.1")


def test_the_server_reads_only_what_the_signed_rules_name(controller, tmp_path):
    access_file = tmp_path / "access.json"
    access_file.write_text(json.dumps({"doc": {"rules": {"plc": {"hosts": ["127.0.0.1"]}}}}), encoding="utf-8")

    def run(calls, env_file):
        lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        lines += [{"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": n, "arguments": a}} for n, a in calls]
        out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(json.dumps(x) for x in lines) + "\n",
                             capture_output=True, text=True, timeout=120, encoding="utf-8",
                             env={**os.environ, "SYSAI_ACCESS_FILE": str(env_file)}).stdout.splitlines()
        return [json.loads(x) for x in out]
    args = {"plc": controller["plc"], "names": ["GVL.Start"], "ns": controller["ns"], "out_file": str(tmp_path / "snap.csv")}
    r = run([("plc_read", args), ("plc_read", {"plc": "192.168.250.250", "names": ["GVL.Start"]})], access_file)
    assert r[0]["result"]["serverInfo"]["name"] == "plc-live" and [t["name"] for t in r[1]["result"]["tools"]] == TOOLS
    got = json.loads(r[2]["result"]["content"][0]["text"])
    assert got["values"][0]["value"] is True and (tmp_path / "snap.csv").is_file()
    assert r[3]["result"]["isError"] and "Сеть контроллеров" in r[3]["result"]["content"][0]["text"]
    r = run([("plc_read", args)], tmp_path / "нет.json")                 # no rules — no controller
    assert r[2]["result"]["isError"] and "не открыт" in r[2]["result"]["content"][0]["text"]
