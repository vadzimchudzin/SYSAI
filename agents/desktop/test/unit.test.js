'use strict';
// The window's main-process parts without Electron: `node --test agents/desktop/test` (pytest runs it too).
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { AcpClient, commandLine } = require('../lib/acp');
const { Bridge } = require('../lib/bridge');
const config = require('../lib/config');
const files = require('../lib/files');
const rules = require('../lib/rules');
const storage = require('../lib/storage');

const FAKE = path.join(__dirname, 'fake-hermes.js');
const UIMCP = path.join(__dirname, '..', 'lib', 'uimcp.js');
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'sysai-desk-'));

// ------------------------------------------------------------------------------------------------ files
test('kinds of files', () => {
  assert.equal(files.kindOf('Отчёт.XLSX'), 'sheet');
  assert.equal(files.kindOf('a.md'), 'markdown');
  assert.equal(files.kindOf('scan.PDF'), 'pdf');
  assert.equal(files.kindOf('main.scl'), 'code');
  assert.equal(files.kindOf('setup.exe'), 'run');
  assert.equal(files.kindOf('Dockerfile'), 'code');
  assert.equal(files.kindOf('noext'), 'other');
  assert.ok(files.runnable('run.ps1') && files.runnable('a.BAT') && !files.runnable('a.txt'));
  assert.ok(files.hidden('desktop.ini') && files.hidden('~$Книга.xlsx') && files.hidden('.git') && !files.hidden('Книга.xlsx'));
});

test('text in the encodings Windows leaves around', () => {
  const ru = 'Привет, мир';
  assert.deepEqual(files.decode(Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(ru)])), { text: ru, encoding: 'UTF-8' });
  const le = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(ru, 'utf16le')]);
  assert.equal(files.decode(le).text, ru);
  assert.equal(files.decode(le).encoding, 'UTF-16');
  assert.equal(files.decode(Buffer.from(ru)).text, ru);
  // Windows-1251: not valid UTF-8, mostly Cyrillic letters
  const cp = Buffer.from([0xcf, 0xf0, 0xe8, 0xe2, 0xe5, 0xf2, 0x2c, 0x20, 0xec, 0xe8, 0xf0]);
  assert.deepEqual(files.decode(cp), { text: ru, encoding: 'Windows-1251' });
  assert.ok(files.binary(Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x00, 0x00, 0x08])));
  assert.ok(!files.binary(Buffer.from('обычный текст')));
});

test('folders: folders first, natural order, hidden files left out', async () => {
  const dir = tmp();
  for (const n of ['файл10.txt', 'файл2.txt', 'desktop.ini', '.secret']) fs.writeFileSync(path.join(dir, n), 'x');
  fs.mkdirSync(path.join(dir, 'Яблоки'));
  const out = await files.list(dir);
  assert.deepEqual(out.entries.map((e) => e.name), ['Яблоки', 'файл2.txt', 'файл10.txt']);
  assert.equal(out.entries[0].dir, true);
  const all = await files.list(dir, { showHidden: true });
  assert.equal(all.entries.length, 5);
  const text = await files.readText(path.join(dir, 'файл2.txt'));
  assert.equal(text.text, 'x');
  await assert.rejects(files.readText(path.join(dir, 'Яблоки')), /папка/);
});

test('places skip the ones that are the home folder itself', () => {
  const home = os.homedir();
  const got = files.places((key) => (key === 'documents' ? home : key === 'desktop' ? home + '/__nope__' : home));
  assert.deepEqual(got.map((p) => p.label), ['Домашняя папка']);
});

// ------------------------------------------------------------------------------------------------ config
test('the SYSAI folder: agent.json, desktop.json and the profile .env', () => {
  const home = tmp();
  const prof = path.join(home, 'hermes', 'profiles', 'ivan');
  fs.mkdirSync(prof, { recursive: true });
  fs.mkdirSync(path.join(home, 'desktop'));
  fs.writeFileSync(path.join(home, 'agent.json'), '\uFEFF' + JSON.stringify({ agent: 'ivan', server: 'https://old.example', profile: 'ivan' }));
  fs.writeFileSync(path.join(home, 'desktop', 'desktop.json'), JSON.stringify({ profile_dir: prof, hermes: 'C:\\h\\hermes.exe', person: 'Иван' }));
  fs.writeFileSync(path.join(prof, '.env'), '# hermes\nexport SYSAI_URL="https://sysai.example/"\nSYSAI_AGENT_TOKEN=tok\n\nBROKEN LINE\n');
  const cfg = config.load(home);
  assert.equal(cfg.connected, true);
  assert.equal(cfg.server, 'https://sysai.example');
  assert.equal(cfg.token, 'tok');
  assert.equal(cfg.hermes, 'C:\\h\\hermes.exe');
  assert.equal(cfg.person, 'Иван');
  assert.equal(cfg.profileDir, prof);
  const empty = config.load(tmp());
  assert.equal(empty.connected, false);
  assert.equal(empty.token, '');
});

test('the Hermes profile folder as the synchronizer finds it', () => {
  assert.equal(config.profileDir({ profile: 'x' }, { profile_dir: '/p' }), '/p');
  assert.equal(config.profileDir({ hermes_home: '/h' }, {}), '/h');
  assert.equal(config.profileDir({ profile: 'ivan' }, {}), path.join(config.hermesRoot(), 'profiles', 'ivan'));
  assert.equal(config.profileDir({ profile: 'default' }, {}), config.hermesRoot());
  assert.equal(config.profileDir({}, {}), '');
});

// ------------------------------------------------------------------------------------------------ ACP
test('hermes.cmd goes through cmd.exe with the arguments quoted', () => {
  const line = commandLine('C:\\Program Files\\hermes\\hermes.cmd', ['-p', 'ivan', 'acp'], 'win32');
  assert.equal(line.verbatim, true);
  assert.deepEqual(line.args.slice(0, 3), ['/d', '/s', '/c']);
  assert.equal(line.args[3], '""C:\\Program Files\\hermes\\hermes.cmd" -p ivan acp"');
  assert.deepEqual(commandLine('/usr/bin/hermes', ['acp'], 'linux'), { file: '/usr/bin/hermes', args: ['acp'], verbatim: false });
});

function hermes(env = {}) {
  const log = path.join(tmp(), 'log.jsonl');
  const client = new AcpClient({ command: process.execPath, args: [FAKE, '-p', 'ivan', 'acp'],
    env: { ...process.env, FAKE_HERMES_LOG: log, FAKE_HERMES_FILES: tmp(), ...env } });
  client.start();
  const requests = () => (fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : []);
  return { client, requests };
}

test('a turn with the fake Hermes: updates, a permission, the answer', async () => {
  const { client, requests } = hermes();
  const noise = [];
  client.on('noise', (l) => noise.push(l));
  try {
    const init = await client.initialize({ name: 'test', version: '1' }, { timeout: 10000 });
    assert.ok(init.protocolVersion);
    assert.equal(noise.length, 1, 'a line that is not JSON-RPC is set aside, not fatal');
    const created = await client.request('session/new', { cwd: os.tmpdir(), mcpServers: [] });
    assert.ok(created.sessionId);
    assert.ok(created.configOptions.some((o) => o.id === 'model'));
    const kinds = new Set();
    client.on('notification', ({ method, params }) => { if (method === 'session/update') kinds.add(params.update.sessionUpdate); });
    client.on('request', ({ id, method, params }) => {
      assert.equal(method, 'session/request_permission');
      const allow = params.options.find((o) => o.kind === 'allow_once');
      client.respond(id, { outcome: { outcome: 'selected', optionId: allow.optionId } });
    });
    const done = await client.request('session/prompt', { sessionId: created.sessionId, prompt: [{ type: 'text', text: 'сводка' }] });
    assert.equal(done.stopReason, 'end_turn');
    for (const k of ['agent_thought_chunk', 'plan', 'tool_call', 'tool_call_update', 'agent_message_chunk', 'usage_update']) {
      assert.ok(kinds.has(k), `no ${k}`);
    }
    assert.ok(requests().some((r) => r.method === 'session/prompt'));
  } finally {
    client.stop();
  }
});

test('stop: session/cancel ends the turn as cancelled', async () => {
  const { client } = hermes();
  try {
    await client.initialize({ name: 'test', version: '1' }, { timeout: 10000 });
    const { sessionId } = await client.request('session/new', { cwd: os.tmpdir(), mcpServers: [] });
    const turn = client.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'стоп-тест' }] });
    await new Promise((r) => setTimeout(r, 300));
    client.notify('session/cancel', { sessionId });
    assert.equal((await turn).stopReason, 'cancelled');
  } finally {
    client.stop();
  }
});

test('Hermes that cannot start: the pending requests fail, exit carries stderr', async () => {
  const { client } = hermes({ FAKE_HERMES_CRASH: "No module named 'acp'" });
  const exited = new Promise((r) => client.once('exit', r));
  await assert.rejects(client.initialize({ name: 't', version: '1' }, { timeout: 10000 }), /остановился/);
  const info = await exited;
  assert.equal(info.code, 1);
  assert.match(info.stderr, /No module named 'acp'/);
  assert.equal(client.running, false);
});

// ------------------------------------------------------------------------------------------------ bridge
function post(port, route, token, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request({ host: '127.0.0.1', port, path: `/${route}`, method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } }, (res) => {
      let text = '';
      res.on('data', (d) => { text += d; });
      res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(text || '{}') }));
    });
    req.on('error', reject);
    req.end(data);
  });
}

test('the bridge: token from bridge.json, only the actions it has', async () => {
  const file = path.join(tmp(), 'desktop', 'bridge.json');
  const shown = [];
  const bridge = new Bridge({ file, version: '0.1.0', handlers: {
    show: async (b) => { shown.push(b); return { shown: true }; },
    fail: async () => { throw new Error('окно занято'); },
  } });
  const port = await bridge.start();
  try {
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(saved.port, port);
    if (process.platform !== 'win32') assert.equal(fs.statSync(file).mode & 0o077, 0, 'bridge.json is the user\'s only');
    assert.equal((await post(port, 'show', '', { path: '/x' })).status, 401);
    assert.equal((await post(port, 'show', 'wrong', { path: '/x' })).status, 401);
    const ok = await post(port, 'show', saved.token, { path: '/x', line: 3 });
    assert.deepEqual(ok, { status: 200, body: { ok: true, shown: true } });
    assert.deepEqual(shown, [{ path: '/x', line: 3 }]);
    assert.equal((await post(port, 'nope', saved.token, {})).status, 404);
    assert.deepEqual(await post(port, 'fail', saved.token, {}), { status: 409, body: { error: 'окно занято' } });
  } finally {
    bridge.stop();
  }
  assert.equal(fs.existsSync(file), false, 'bridge.json goes away with the window');
});

// ------------------------------------------------------------------------------------------------ rules
const RULES = {
  folders: { read: ['C:\\Users\\Иван\\Documents'], write: ['C:\\Users\\Иван\\Documents\\Отчёты'], deny: ['C:\\Users\\Иван\\Documents\\Личное'] },
  commands: { run: true, deny: ['*format*', 'shutdown*', 'reg  delete *'] },
  internet: { mode: 'off', sites: [] },
};

test('rules: folders as the synchronizer reads them', () => {
  assert.equal(rules.norm('C:/Users/Иван/Documents/../Desktop/'), 'c:\\users\\иван\\desktop');
  assert.ok(rules.pathAllowed(RULES, 'c:\\users\\ИВАН\\documents\\a.docx'));
  assert.ok(!rules.pathAllowed(RULES, 'C:\\Users\\Иван\\Documents\\Личное\\x.txt'), 'forbidden wins');
  assert.ok(!rules.pathAllowed(RULES, 'C:\\Users\\Иван\\Documents\\..\\Desktop\\x.txt'), 'no way out by «..»');
  assert.ok(!rules.pathAllowed(RULES, 'C:\\Users\\Иван\\DocumentsX\\a.txt'));
  assert.ok(rules.pathAllowed(RULES, 'C:\\Users\\Иван\\Documents\\Отчёты\\a.md', true));
  assert.ok(!rules.pathAllowed(RULES, 'C:\\Users\\Иван\\Documents\\a.md', true));
  assert.ok(rules.pathAllowed({ folders: { read: ['*'], write: [], deny: [] } }, 'D:\\anything'));
  assert.ok(!rules.pathAllowed({ folders: {} }, 'C:\\x') && !rules.pathAllowed(RULES, ''));
});

test('rules: commands, the forbidden patterns, the synchronizer guarded', () => {
  assert.equal(rules.commandDenied(RULES, 'FORMAT   D: /q'), '*format*');
  assert.equal(rules.commandDenied(RULES, 'reg delete HKCU\\x'), 'reg  delete *');
  assert.equal(rules.commandDenied(RULES, 'dir'), '');
  assert.equal(rules.commandDenied({ commands: { run: false } }, 'dir'), '*');
  assert.ok(rules.fnmatch('abc', 'a?c') && rules.fnmatch('a.c', 'a[.]c') && !rules.fnmatch('abc', 'a[!b]c') && rules.fnmatch('a[c', 'a[c'));
  const home = 'C:\\Users\\Иван\\AppData\\Local\\SYSAI';
  const profile = 'C:\\Users\\Иван\\AppData\\Local\\hermes\\profiles\\ivan-pc';
  assert.ok(rules.guardedPath(`${home}\\access.json`, home, profile));
  assert.ok(!rules.guardedPath(`${home}\\proposals\\skill.md`, home, profile));
  assert.ok(rules.guardedPath(`${profile}\\config.yaml`, home, profile) && !rules.guardedPath(`${profile}\\notes.md`, home, profile));
  assert.ok(rules.touchesGuard('start "" "C:\\Users\\x\\.local\\bin\\sysai-agent.exe"', home, profile));
  assert.ok(!rules.touchesGuard('sysai-agent status', home, profile));
  assert.ok(rules.touchesGuard('type %LOCALAPPDATA%\\SYSAI\\state.json', home, profile));
});

test('rules: what the window does for the agent', () => {
  const ctx = { home: 'C:\\Users\\Иван\\AppData\\Local\\SYSAI', profileDir: '' };
  const doc = 'C:\\Users\\Иван\\Documents\\отчёт.docx';
  assert.equal(rules.check(RULES, 'show', doc, ctx), '');
  assert.equal(rules.check(RULES, 'open', doc, ctx), '');
  assert.match(rules.check(RULES, 'show', 'C:\\Windows\\win.ini', ctx), /не разрешена/);
  assert.match(rules.check(RULES, 'reveal', 'C:\\Users\\Иван\\Documents\\Личное\\a.txt', ctx), /не разрешена/);
  assert.equal(rules.check(RULES, 'open', 'C:\\Users\\Иван\\Documents\\run.bat', ctx), '');
  assert.match(rules.check(RULES, 'open', 'C:\\Users\\Иван\\Documents\\format.exe', ctx), /запрет «\*format\*»/);
  assert.match(rules.check(RULES, 'open', 'C:\\Users\\Иван\\Documents\\Shutdown.lnk', ctx), /запрет «shutdown\*»/, 'by the name too');
  assert.match(rules.check({ ...RULES, commands: { run: false } }, 'open', 'C:\\Users\\Иван\\Documents\\run.ps1', ctx), /Командами на ПК/);
  assert.equal(rules.check({ ...RULES, commands: { run: false } }, 'open', doc, ctx), '', 'a document needs no commands');
  const all = { ...RULES, folders: { read: ['*'], write: [], deny: [] } };
  assert.match(rules.check(all, 'open', `${ctx.home}\\agent.json`, ctx), /только SYSAI/);
  assert.equal(rules.check(all, 'show', `${ctx.home}\\logs\\desktop.log`, ctx), '', 'showing SYSAI\'s logs is reading');
  assert.equal(rules.check(null, 'show', doc, ctx), rules.NO_RULES);
  assert.equal(rules.fullPath('~/a.txt'), path.join(os.homedir(), 'a.txt'));
  assert.equal(rules.fullPath('docs/../a.txt', '/base'), path.resolve('/base', 'a.txt'));
});

test('rules: access.json only as the synchronizer verified it', () => {
  const home = tmp();
  assert.equal(rules.load(home).error, rules.NO_RULES);
  const raw = Buffer.from(JSON.stringify({ alg: 'Ed25519', sig: 'x', doc: { rules: RULES } }));
  fs.writeFileSync(path.join(home, 'access.json'), raw);
  assert.equal(rules.load(home).error, rules.STALE_RULES, 'no sha256 in state.json: not trusted');
  fs.writeFileSync(path.join(home, 'state.json'), JSON.stringify({ access_sha: crypto.createHash('sha256').update(raw).digest('hex') }));
  assert.deepEqual(rules.load(home).rules, RULES);
  fs.writeFileSync(path.join(home, 'access.json'), Buffer.concat([raw, Buffer.from(' ')]));
  assert.equal(rules.load(home).error, rules.STALE_RULES, 'changed by someone else: not trusted');
});

// ------------------------------------------------------------------------------------------------ the window's MCP server
function mcp(home) {
  const child = spawn(process.execPath, [UIMCP], { env: { ...process.env, SYSAI_HOME: home }, stdio: ['pipe', 'pipe', 'pipe'] });
  const waiters = new Map();
  let buf = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const m = JSON.parse(buf.slice(0, i));
      buf = buf.slice(i + 1);
      const w = waiters.get(m.id);
      if (w) { waiters.delete(m.id); w(m); }
    }
  });
  let next = 1;
  return {
    child,
    rpc(method, params) {
      const id = next++;
      return new Promise((resolve) => {
        waiters.set(id, resolve);
        child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
      });
    },
    notify(method) { child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method })}\n`); },
    close() { child.stdin.end(); return new Promise((r) => child.on('exit', r)); },
  };
}

test('the MCP server of the window: tools, calls through the bridge, refusals as tool errors', async () => {
  const home = tmp();
  const calls = [];
  const bridge = new Bridge({ file: path.join(home, 'desktop', 'bridge.json'), handlers: {
    show: async (b) => { calls.push(['show', b]); return { shown: true, path: b.path, folder: false }; },
    open: async (b) => { calls.push(['open', b]); if (/\.bat$/.test(b.path)) throw new Error('SYSAI: запускать программы агенту можно только с «Командами на ПК»'); return { opened: true, started: false, path: b.path }; },
    reveal: async (b) => { calls.push(['reveal', b]); return { shown: true, path: b.path }; },
  } });
  const server = mcp(home);
  try {
    const none = await server.rpc('tools/call', { name: 'show_file', arguments: { path: '/x/a.md' } });
    assert.equal(none.result.isError, true);
    assert.match(none.result.content[0].text, /не открыто/, 'no window — said plainly');
    await bridge.start();
    const init = await server.rpc('initialize', { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 't', version: '1' } });
    assert.equal(init.result.protocolVersion, '2025-03-26');
    assert.equal(init.result.serverInfo.name, 'sysai-window');
    assert.ok(init.result.capabilities.tools);
    server.notify('notifications/initialized');
    assert.deepEqual((await server.rpc('ping', {})).result, {});
    const listed = await server.rpc('tools/list', {});
    assert.deepEqual(listed.result.tools.map((t) => t.name), ['show_file', 'open_file', 'show_in_folder']);
    for (const t of listed.result.tools) assert.deepEqual(t.inputSchema.required, ['path']);
    const shown = await server.rpc('tools/call', { name: 'show_file', arguments: { path: '/x/отчёт.md', line: 4 } });
    assert.equal(shown.result.isError, undefined);
    assert.match(shown.result.content[0].text, /отчёт\.md/);
    const refused = await server.rpc('tools/call', { name: 'open_file', arguments: { path: '/x/run.bat' } });
    assert.equal(refused.result.isError, true);
    assert.match(refused.result.content[0].text, /^SYSAI: запускать программы/);
    await server.rpc('tools/call', { name: 'show_in_folder', arguments: { path: '/x/a.pdf' } });
    const empty = await server.rpc('tools/call', { name: 'open_file', arguments: {} });
    assert.equal(empty.result.isError, true);
    assert.equal((await server.rpc('tools/call', { name: 'rm', arguments: { path: '/x' } })).result.isError, true);
    assert.equal((await server.rpc('resources/list', {})).error.code, -32601);
    assert.deepEqual(calls, [['show', { path: '/x/отчёт.md', line: 4 }], ['open', { path: '/x/run.bat' }], ['reveal', { path: '/x/a.pdf' }]]);
  } finally {
    bridge.stop();
    await server.close();
  }
});

// ------------------------------------------------------------------------------------------------ storage
function fakeFetch(routes) {
  const calls = [];
  const impl = async (url, opts) => {
    calls.push({ url, ...opts });
    const u = new URL(url);
    const r = routes[`${opts.method} ${u.pathname}`];
    if (!r) return new Response(JSON.stringify({ error: { message: 'Not Found' } }), { status: 404 });
    return r(u, opts);
  };
  return { impl, calls };
}

test('storage: listing, a file checked by sha256 and cached, upload', async () => {
  const cfg = { server: 'https://sysai.example', token: 'tok' };
  const blob = Buffer.from('таблица');
  const sha = crypto.createHash('sha256').update(blob).digest('hex');
  let downloads = 0;
  let header = sha;
  const { impl, calls } = fakeFetch({
    'GET /agents/api/files': (u) => Response.json({ path: u.searchParams.get('path'), can_write: true, items: [] }),
    'GET /agents/api/files/raw': () => { downloads += 1; return new Response(blob, { headers: { 'X-SHA256': header } }); },
    'PUT /agents/api/files/raw': (u, o) => Response.json({ path: u.searchParams.get('path'), size: o.body.length, changed: true }),
  });
  const listed = await storage.list(cfg, 'Отдел продаж', 'план', { fetchImpl: impl });
  assert.equal(listed.path, 'Отдел продаж');
  assert.equal(calls[0].headers.Authorization, 'Bearer tok');
  assert.match(calls[0].url, /q=%D0%BF/);
  const cache = tmp();
  const item = { path: 'Отдел продаж/../план:1.xlsx', sha256: sha, version: 2 };
  const local = await storage.fetchFile(cfg, item, cache, { fetchImpl: impl });
  assert.ok(local.startsWith(cache) && local.endsWith('план_1.xlsx'), local);
  assert.equal(fs.readFileSync(local, 'utf8'), 'таблица');
  await storage.fetchFile(cfg, item, cache, { fetchImpl: impl });
  assert.equal(downloads, 1, 'the cached copy with the same sha256 is reused');
  header = 'f'.repeat(64); // broken on the way
  await assert.rejects(storage.fetchFile(cfg, { path: 'x.txt' }, cache, { fetchImpl: impl }), /повреждённым/);
  assert.equal(fs.existsSync(path.join(cache, 'x.txt')), false);
  const src = path.join(tmp(), 'отчёт.md');
  fs.writeFileSync(src, '# hi');
  const up = await storage.upload(cfg, src, 'Отдел продаж/', { fetchImpl: impl });
  assert.equal(up.path, 'Отдел продаж/отчёт.md');
});

test('storage: what SYSAI says is what the window shows', async () => {
  const { impl } = fakeFetch({
    'GET /agents/api/files': () => Response.json({ error: { message: 'Хранилище агенту не открыто' } }, { status: 403 }),
  });
  await assert.rejects(storage.list({ server: 'https://s', token: 't' }, '', '', { fetchImpl: impl }),
    (err) => err.status === 403 && /не открыто/.test(err.message));
  await assert.rejects(storage.list({ server: 'https://s', token: '' }), /Нет токена/);
  await assert.rejects(storage.list({ server: '', token: 't' }), /не подключён/);
  const down = async () => { throw new TypeError('fetch failed'); };
  await assert.rejects(storage.list({ server: 'https://s', token: 't' }, '', '', { fetchImpl: down }), /нет связи/);
});
