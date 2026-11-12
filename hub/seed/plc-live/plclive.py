"""Reading a controller over OPC UA — and only reading. This module holds no call that writes a value, calls a method,
downloads, starts or stops anything: what it can do to a controller is connect, ask for its endpoints and read the
values of variables. Writing, download, start and stop stay with the engineer and Machine Expert.

Which controllers may be reached is not decided here either: the list is «Сеть контроллеров» of the agent's access
in SYSAI (rules["plc"]["hosts"]). The synchronizer's wrapper checks every call against the signed rules; this module
checks again from the same file, so that a call past the wrapper is refused too. No list — no controller.

Names: a Schneider M241 / M251 gives a published variable «GVL.Start» the node ns=2;i=N, N — CRC-32 of the name
without the final inversion (checked on the 582 tags of a real panel project); any other controller — give the node
ids themselves («ns=4;s=|var|Device.Application.GVL.Start»)."""
import asyncio
import csv
import ipaddress
import json
import os
import re
import socket
import time
import zlib

PORTS = {4840: 'OPC UA', 502: 'Modbus TCP', 11740: 'Machine Expert (CODESYS gateway)'}
MAX_HOSTS = 256
MAX_NAMES = 500
MAX_WATCH = 120.0
MIN_PERIOD = 0.2
CONNECT_TIMEOUT = 5.0


class LiveError(Exception):
    pass


def plc_allowed(rules: dict, target: str) -> bool:
    """May the agent reach this controller: `target` — «192.168.1.10», «192.168.1.10:4840», «opc.tcp://host:4840/…» or
    a network to scan «192.168.1.0/28». Rules — rules["plc"]["hosts"]: an address, address:port (only that port), a
    network, a host name, «*» (any — for a bench, not for a plant network). Empty — nothing is allowed. The same
    text lives on the server (app/net/access.py), in the synchronizer (rules.py) and in the tool plc-live — a test
    compares them."""
    import ipaddress
    import re
    text = re.sub(r"^[a-z.+]+://", "", str(target or "").strip().lower())
    allowed = [str(r).strip().lower() for r in (rules.get("plc") or {}).get("hosts") or []]
    if re.fullmatch(r"[0-9a-f.:]+/\d{1,3}", text):  # a network: every address of it must be inside an allowed network
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            return False
        for rule in allowed:
            if rule == "*":
                return True
            try:
                if "/" in rule and net.subnet_of(ipaddress.ip_network(rule, strict=False)):
                    return True
            except (ValueError, TypeError):
                continue
        return False
    text = text.split("/")[0]
    host, _, port = text.rpartition(":") if re.search(r":\d+$", text) else (text, "", "")
    host = host.strip("[]")
    if not host:
        return False
    for rule in allowed:
        if rule == "*":
            return True
        if "/" in rule:
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                pass
            continue
        r_host, _, r_port = rule.rpartition(":") if re.search(r":\d+$", rule) else (rule, "", "")
        if r_host.strip("[]") == host and (not r_port or r_port == port):
            return True
    return False


def rules_now() -> dict:
    """The rules of the agent from the file the synchronizer keeps (SYSAI_ACCESS_FILE); {} — there are none."""
    path = os.environ.get('SYSAI_ACCESS_FILE') or ''
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        rules = doc['doc']['rules']
        return rules if isinstance(rules, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def need(target: str, rules=None) -> None:
    rules = rules_now() if rules is None else rules
    if not plc_allowed(rules, target):
        raise LiveError(f'контроллер {target} агенту не открыт: адрес добавляют в SYSAI → Агенты → Доступы → '
                        '«Сеть контроллеров». Без этого инструмент ни к чему не подключается')


def endpoint(plc: str) -> str:
    """«192.168.1.10» → «opc.tcp://192.168.1.10:4840»; a full address is kept."""
    text = str(plc or '').strip()
    if not text:
        raise LiveError('назовите контроллер (plc): адрес или opc.tcp://адрес:порт')
    if '://' in text:
        if not text.lower().startswith('opc.tcp://'):
            raise LiveError('контроллер читают по OPC UA: адрес вида opc.tcp://192.168.1.10:4840')
        return text
    has_port = re.search(r':\d+$', text) is not None
    return 'opc.tcp://' + (text if has_port else text + ':4840')


def m241_node(name: str, ns: int = 2) -> str:
    """The node of a published variable of a Schneider M241 / M251 by its name «List.Variable»."""
    return f"ns={int(ns)};i={zlib.crc32(name.encode('ascii', 'replace')) ^ 0xFFFFFFFF}"


def nodes_of(names=None, nodes=None, ns: int = 2) -> list:
    """[(what to call it, node id)] — names of variables (M241 numbering) and node ids given as they are."""
    out = [(str(n).strip(), m241_node(str(n).strip(), ns)) for n in (names or []) if str(n).strip()]
    out += [(str(n).strip(), str(n).strip()) for n in (nodes or []) if str(n).strip()]
    if not out:
        raise LiveError('назовите переменные: names — «Список.Переменная» (M241 / M251) или nodes — идентификаторы узлов')
    if len(out) > MAX_NAMES:
        raise LiveError(f'переменных больше {MAX_NAMES}: читайте частями')
    for name, node in out:
        if not re.match(r'^(ns=\d+;)?[isgb]=', node):
            raise LiveError(f'узел «{node}» — нужно как ns=2;i=123 или ns=4;s=имя')
    return out


def names_from_csv(file: str, limit: int = MAX_NAMES) -> list:
    """The names of the tags of a table written by plc_hmi_tags (plc-kit): the column «name» / «Имя» / the first one."""
    try:
        with open(file, encoding='utf-8-sig', newline='') as f:
            text = f.read()
    except OSError as exc:
        raise LiveError(f'файл тегов не читается: {exc}') from None
    lines = text.splitlines()
    if not lines:
        return []
    rows = list(csv.reader(lines, delimiter=';' if ';' in lines[0] else ','))
    low = [c.strip().lower() for c in rows[0]]
    titled = [k for k in ('name', 'имя', 'tag', 'тег') if k in low]
    col = low.index(titled[0]) if titled else 0
    return [r[col].strip() for r in (rows[1:] if titled else rows) if len(r) > col and r[col].strip()][:limit]


# ----------------------------------------------------------------------------- who answers
def hosts_of(plcs=None, network: str = '') -> list:
    out = [str(h).strip() for h in (plcs or []) if str(h).strip()]
    if network:
        try:
            net = ipaddress.ip_network(str(network).strip(), strict=False)
        except ValueError:
            raise LiveError(f'сеть «{network}» — нужно как 192.168.1.0/24') from None
        if net.num_addresses > MAX_HOSTS:
            raise LiveError(f'в сети {net} адресов больше {MAX_HOSTS}: назовите сеть поменьше (/24 и уже)')
        out += [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
    if not out:
        raise LiveError('назовите контроллеры (plcs) или сеть (plc_network)')
    if len(out) > MAX_HOSTS:
        raise LiveError(f'адресов больше {MAX_HOSTS}')
    return out


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(plcs=None, network: str = '', timeout: float = 0.6, rules=None) -> list:
    """Who answers: for every address the ports that accept a connection (OPC UA 4840, Modbus TCP 502, the gateway of
    Machine Expert 11740) and, where OPC UA answers, what its server says about itself. Nothing is sent to Modbus or to
    the gateway beyond opening and closing the connection."""
    rules = rules_now() if rules is None else rules
    if network:
        need(network, rules)
    hosts = hosts_of(plcs, network)
    for h in hosts if not network else []:
        need(h, rules)
    found = []
    for host in hosts:
        if not plc_allowed(rules, host):
            continue
        ports = [p for p in PORTS if _open(host.split(':')[0], p, timeout)]
        if not ports:
            continue
        row = {'host': host.split(':')[0], 'ports': {str(p): PORTS[p] for p in ports}}
        if 4840 in ports:
            try:
                row['opcua'] = asyncio.run(_endpoints(endpoint(host.split(':')[0])))
            except Exception as exc:  # noqa: BLE001 — a controller that answers badly is still a finding
                row['opcua_error'] = str(exc)[:160]
        found.append(row)
    return found


async def _endpoints(url: str) -> dict:
    from asyncua import Client
    client = Client(url=url, timeout=CONNECT_TIMEOUT)
    eps = await asyncio.wait_for(client.connect_and_get_server_endpoints(), CONNECT_TIMEOUT + 2)
    modes, name, uri = [], '', ''
    for e in eps:
        mode = str(getattr(e.SecurityMode, 'name', e.SecurityMode)).replace('_', '')
        policy = str(e.SecurityPolicyUri or '').rsplit('#', 1)[-1]
        if (mode, policy) not in modes:
            modes.append((mode, policy))
        name = name or str(getattr(e.Server.ApplicationName, 'Text', '') or '')
        uri = uri or str(e.Server.ApplicationUri or '')
    return {'server': name, 'uri': uri, 'security': [f'{m} / {p}' for m, p in modes],
            'anonymous': any(str(getattr(t.TokenType, 'name', t.TokenType)) == 'Anonymous' for e in eps for t in e.UserIdentityTokens or [])}


# ----------------------------------------------------------------------------- reading
def _plain(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value[:64]]
    if isinstance(value, bytes):
        return value[:32].hex()
    return str(value)[:120]


async def _client(url: str, user: str = '', password: str = ''):
    try:
        from asyncua import Client
    except ImportError:
        raise LiveError('нет библиотеки asyncua: инструмент ставит её сам (requirements.txt) — обновите инструмент в '
                        'Хабе') from None
    client = Client(url=url, timeout=CONNECT_TIMEOUT)
    if user:
        client.set_user(user)
        client.set_password(password or '')
    try:
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT + 3)
    except Exception as exc:  # noqa: BLE001 — asyncua raises its own and the system's
        raise LiveError(f'контроллер {url} не ответил по OPC UA: {type(exc).__name__} {str(exc)[:160]}. Проверьте: сервер '
                        'OPC UA включён в проекте, порт 4840 открыт, анонимный вход разрешён или даны user и password') from None
    return client


async def _read_once(client, pairs: list) -> list:
    from asyncua import ua
    nodes = [client.get_node(node) for _, node in pairs]
    params = ua.ReadParameters()
    for n in nodes:
        rv = ua.ReadValueId()
        rv.NodeId = n.nodeid
        rv.AttributeId = ua.AttributeIds.Value
        params.NodesToRead.append(rv)
    results = await client.uaclient.read(params)  # the Read service: nothing else of the client is used here
    rows = []
    for (name, node), dv in zip(pairs, results):
        ok = dv.StatusCode is None or dv.StatusCode.is_good()
        row = {'name': name, 'node': node, 'ok': ok}
        if ok:
            row['value'] = _plain(dv.Value.Value if dv.Value is not None else None)
            row['type'] = str(getattr(dv.Value.VariantType, 'name', '')) if dv.Value is not None else ''
        else:
            row['error'] = str(getattr(dv.StatusCode, 'name', dv.StatusCode))
        rows.append(row)
    return rows


async def _read(url, pairs, user, password):
    client = await _client(url, user, password)
    try:
        return await _read_once(client, pairs)
    finally:
        await client.disconnect()


def read(plc: str, names=None, nodes=None, ns: int = 2, user: str = '', password: str = '', rules=None) -> dict:
    """The values now: {'plc', 'count', 'bad', 'values': [{name, node, ok, value, type | error}]}."""
    url = endpoint(plc)
    need(url, rules)
    pairs = nodes_of(names, nodes, ns)
    rows = asyncio.run(_read(url, pairs, user, password))
    return {'plc': url, 'count': len(rows), 'bad': sum(1 for r in rows if not r['ok']), 'values': rows}


async def _watch(url, pairs, seconds, period, user, password):
    client = await _client(url, user, password)
    seen = {name: {'name': name, 'node': node, 'first': None, 'last': None, 'changes': 0, 'events': [], 'errors': 0}
            for name, node in pairs}
    start = time.monotonic()
    reads = 0
    try:
        while True:
            at = time.monotonic() - start
            for row in await _read_once(client, pairs):
                s = seen[row['name']]
                if not row['ok']:
                    s['errors'] += 1
                    s['error'] = row['error']
                    continue
                v = row['value']
                if 'type' not in s:                      # the first good reading of this variable
                    s['first'], s['type'] = v, row['type']
                elif v != s['last']:
                    s['changes'] += 1
                    if len(s['events']) < 40:
                        s['events'].append({'t': round(at, 2), 'value': v})
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    s['min'] = v if 'min' not in s else min(s['min'], v)
                    s['max'] = v if 'max' not in s else max(s['max'], v)
                s['last'] = v
            reads += 1
            if at + period > seconds:
                break
            await asyncio.sleep(period)
    finally:
        await client.disconnect()
    return reads, list(seen.values())


def watch(plc: str, names=None, nodes=None, seconds: float = 10.0, period: float = 0.5, ns: int = 2, user: str = '',
          password: str = '', rules=None) -> dict:
    """What changed while watching: for every variable its first and last value, how many times it changed, the
    moments of the changes (seconds from the start, the first 40) and, for a number, min and max. Polling: a change
    shorter than `period` is not seen."""
    url = endpoint(plc)
    need(url, rules)
    pairs = nodes_of(names, nodes, ns)
    seconds = max(1.0, min(MAX_WATCH, float(seconds or 10)))
    period = max(MIN_PERIOD, min(seconds, float(period or 0.5)))
    reads, rows = asyncio.run(_watch(url, pairs, seconds, period, user, password))
    return {'plc': url, 'seconds': seconds, 'period': period, 'reads': reads, 'changed': sum(1 for r in rows if r['changes']),
            'variables': rows}


def save_csv(rows: list, out_file: str) -> str:
    """A snapshot of read values as a table (the engineer opens it in Excel): name; node; value; type; error."""
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    with open(out_file, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(['name', 'node', 'value', 'type', 'error'])
        for r in rows:
            w.writerow([r['name'], r['node'], '' if r.get('value') is None else r['value'], r.get('type', ''), r.get('error', '')])
    return out_file
