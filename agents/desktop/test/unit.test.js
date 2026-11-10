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
const { Projects, prompts: projectPrompts, pipeline: projectPipeline, groupOf, cleanName, STAGES } = require('../lib/projects');
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
    assert.deepEqual(listed.result.tools.map((t) => t.name), ['show_file', 'open_file', 'show_in_folder', 'canvas_open',
      'canvas_look', 'canvas_window', 'canvas_cursor', 'canvas_edit']);
    for (const t of listed.result.tools.slice(0, 4)) assert.deepEqual(t.inputSchema.required, ['path']);
    assert.match(init.result.instructions, /canvas_open/);
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

// ------------------------------------------------------------------------------------------------ the canvas
const canvasLib = require('../lib/canvas');

const FAKE_CMTP = path.join(__dirname, 'fake-cmtp.js');
const label = (text, x, y) => ({ text, font: 'Arial', size: 20, x, y, color: '#ffffff', align: 'center' });
const PROJECT = {
  windows: [
    { number: 3, name: 'Служебное', width: 400, height: 300, objects: [{ kind: 'Window', bbox: [0, 0, 399, 299], labels: [], tags: [] }] },
    { number: 10, name: 'Главное', width: 800, height: 480, background: '#0080c0', objects: [
      { kind: 'Window', bbox: [0, 0, 799, 479], labels: [], tags: [], fill: '#0080c0' },
      { kind: 'Rect', bbox: [10, 10, 300, 60], labels: [], tags: [], fill: '#005480', frame: '#ffffff', frame_width: 2 },
      { kind: 'Text', bbox: [20, 15, 200, 55], labels: [label('Нория 3', 20, 15)], tags: [] },
      { kind: 'ToggleSwitch', bbox: [400, 20, 470, 50], labels: [label('', 400, 20)], tags: ['GVL.OFF_DSL'], shape: 'abc' },
      { kind: 'BitLamp', bbox: [500, 20, 540, 60], labels: [], tags: ['GVL.RUN'], picture: [{ image: 'aa11', transparent: null }] },
    ] },
    { number: 23, name: 'Нория 3', width: 600, height: 1015, objects: [{ kind: 'Window', bbox: [0, 0, 599, 1014], labels: [], tags: [] }] },
  ],
};

function canvasHome(extra = {}) {
  const home = tmp();
  const file = path.join(home, 'Проекты', 'велута.cmtp');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(PROJECT));
  const events = { state: [], cursors: [], edited: [], notice: [] };
  const cv = new canvasLib.Canvas({ home, cache: path.join(home, 'desktop', 'cache', 'canvas'), watch: 40,
    tool: { command: process.execPath, args: [FAKE_CMTP], cwd: __dirname, env: extra }, ...extra.opts });
  for (const k of Object.keys(events)) cv.on(k, (v) => events[k].push(v));
  return { home, file, cv, events, read: () => JSON.parse(fs.readFileSync(file, 'utf8')) };
}

const until = async (fn, ms = 4000) => {
  const end = Date.now() + ms;
  while (!fn()) {
    if (Date.now() > end) throw new Error('не дождался');
    await new Promise((r) => setTimeout(r, 20));
  }
};

test('canvas: opens a project at window 10, with its pictures as file URLs', async () => {
  const { file, cv, events } = canvasHome();
  try {
    await assert.rejects(cv.open(file.replace('.cmtp', '.txt')), /\.cmtp или \.emtp/);
    await assert.rejects(cv.open(path.join(path.dirname(file), 'нет.cmtp')), /нет такого файла/);
    const got = await cv.open(file);
    assert.deepEqual(got, { open: true, file, window: { number: 10, name: 'Главное', width: 800, height: 480 }, objects: 4, windows: 3 });
    const st = cv.state();
    assert.equal(st.name, 'велута.cmtp');
    assert.deepEqual(st.windows.map((w) => w.number), [3, 10, 23]);
    assert.equal(st.scene.window.background, '#0080c0');
    assert.equal(st.scene.objects[4].picture[0].key, 'aa11');
    assert.match(st.scene.images.aa11, /^file:\/\/.*aa11\.png$/);
    assert.ok(events.state.length >= 1 && events.state.at(-1).scene);
    assert.equal((await cv.show('Нория')).window.number, 23, 'a window by a part of its name');
    await assert.rejects(cv.show('нет такого'), /нет окна/);
    assert.equal((await cv.show(3)).window.number, 3);
    await cv.close();
    assert.equal((await cv.open(file)).window.number, 3, 'the window last shown in this project');
    assert.equal((await cv.open(file, '10')).window.number, 10);
  } finally {
    cv.stop();
  }
});

test('canvas: the agent edits in the file, its cursor goes there, the person undoes', async () => {
  const { file, cv, events, read } = canvasHome();
  try {
    await cv.open(file);
    const out = await cv.edit('Агент', [{ op: 'move', object: 2, dx: 30, dy: 5 }, { op: 'text', object: 2, old: 'Нория 3', new: 'Нория 4' }]);
    assert.deepEqual(out.objects, [2]);
    assert.equal(out.window, 10);
    assert.equal(out.undo, 1);
    assert.deepEqual(read().windows[1].objects[2].bbox, [50, 20, 230, 60], 'written into the project itself');
    assert.equal(read().windows[1].objects[2].labels[0].text, 'Нория 4');
    assert.deepEqual(cv.state().scene.objects[2].bbox, [50, 20, 230, 60], 'the canvas shows it');
    const c = cv.state().cursors.find((x) => x.who === 'Агент');
    assert.deepEqual([c.x, c.y], [140, 40], 'the cursor at the edited object');
    assert.match(c.say, /сдвиг #2 на \(30, 5\); текст #2 «Нория 3» → «Нория 4»/);
    assert.equal(events.edited.at(-1).who, 'Агент');
    assert.deepEqual(events.edited.at(-1).objects, [2]);
    assert.ok(events.cursors.length >= 2, 'the cursor moved before the edit and after');
    assert.equal(cv.state().history.at(-1).who, 'Агент');
    // a refused edit leaves the file as it was
    const before = fs.readFileSync(file, 'utf8');
    await assert.rejects(cv.edit('Помощник 1', [{ op: 'text', old: 'нет такого', new: 'x' }]), /не найден/);
    assert.equal(fs.readFileSync(file, 'utf8'), before);
    assert.equal(cv.state().undo, 1);
    await assert.rejects(cv.edit('Агент', [{ op: 'drop', object: 1 }]), /op — одно из/);
    await assert.rejects(cv.edit('Агент', [{ op: 'move', object: 'два' }]), /целое число/);
    await assert.rejects(cv.edit('Агент', []), /список правок/);
    // the person undoes
    const undone = await cv.undo();
    assert.match(undone.undone, /сдвиг #2/);
    assert.deepEqual(read().windows[1].objects[2].bbox, [20, 15, 200, 55]);
    assert.equal(read().windows[1].objects[2].labels[0].text, 'Нория 3');
    assert.deepEqual(cv.state().scene.objects[2].bbox, [20, 15, 200, 55]);
    await assert.rejects(cv.undo(), /отменять нечего/);
    assert.deepEqual(fs.readdirSync(path.join(cv.cache, 'undo')), [], 'the copy for undo is gone');
  } finally {
    cv.stop();
  }
});

test('canvas: copies — of an object (selected next), of a window (shown); edits one at a time', async () => {
  const { file, cv, read } = canvasHome();
  try {
    await cv.open(file);
    const copy = await cv.edit('Человек', [{ op: 'clone_object', object: 3, dx: 0, dy: 40 }]);
    assert.deepEqual(copy.objects, [5]);
    assert.deepEqual(cv.state().scene.objects[5].bbox, [400, 60, 470, 90]);
    assert.equal(cv.state().cursors.length, 0, 'the person has no drawn cursor: it is the mouse');
    const win = await cv.edit('Агент', [{ op: 'clone_window', window: 10, new: 'Нория 5' }]);
    assert.equal(win.window, 24);
    assert.equal(cv.state().window, 24, 'the new window is shown');
    assert.deepEqual(cv.state().windows.map((w) => w.name), ['Служебное', 'Главное', 'Нория 3', 'Нория 5']);
    // two helpers at once: their edits go one after another
    await Promise.all([
      cv.edit('Помощник 1', [{ op: 'move', window: 24, object: 1, dx: 1 }]),
      cv.edit('Помощник 2', [{ op: 'move', window: 24, object: 1, dx: 2 }]),
    ]);
    assert.deepEqual(read().windows[3].objects[1].bbox, [13, 10, 303, 60]);
    const colors = cv.state().cursors.map((c) => c.color);
    assert.equal(new Set(colors).size, colors.length, 'each one its own colour');
    assert.equal(cv.state().undo, 4);
    // an edit of another window shows that window
    await cv.edit('Агент', [{ op: 'rename_window', window: 23, new: 'Нория 3 (старая)' }]);
    assert.equal(cv.state().window, 23);
  } finally {
    cv.stop();
  }
});

test('canvas: deleting — objects from the end, the shown window (the next one is shown), undo brings it back', async () => {
  const { file, cv, read } = canvasHome();
  try {
    await cv.open(file);
    const del = await cv.edit('Человек', [{ op: 'delete_object', object: 4 }, { op: 'delete_object', object: 2 }]);
    assert.deepEqual(del.objects, [4, 2]);
    assert.deepEqual(read().windows[1].objects.map((o) => o.kind), ['Window', 'Rect', 'ToggleSwitch']);
    await cv.edit('Агент', [{ op: 'delete_window' }]);
    assert.equal(cv.state().window, 23, 'the window after the deleted one is shown');
    assert.deepEqual(cv.state().windows.map((w) => w.number), [3, 23]);
    await cv.undo();
    assert.deepEqual(cv.state().windows.map((w) => w.number), [3, 10, 23]);
  } finally {
    cv.stop();
  }
  assert.throws(() => canvasLib.normalize([{ op: 'import_window', window: 'Нория 3' }], 10), /донор/);
  assert.deepEqual(canvasLib.normalize([{ op: 'import_objects', from: 'C:\\d.cmtp', window: 'Меню', objects: ['5', 6],
    map_tags: { 'GVL.A': 'GVL.B' } }], 10),
  [{ op: 'import_objects', window: 'Меню', from: 'C:\\d.cmtp', to: '10', objects: [5, 6], map_tags: { 'GVL.A': 'GVL.B' } }]);
  assert.deepEqual(canvasLib.normalize([{ op: 'add_tags', tags: [{ name: 'GVL.C', type: 'BOOL' }] }], 10),
    [{ op: 'add_tags', tags: [{ name: 'GVL.C', type: 'BOOL' }] }]);
  assert.equal(canvasLib.describe([{ op: 'delete_object', object: 4 }, { op: 'resize', object: 2, width: 90, height: 30 },
    { op: 'link', object: 3, to: '23' }]), 'удаление #4; размер #2 → 90×30; кнопка #3 → окно 23');
});

test('canvas: what the agent sees — the person\'s pointer and selection, the last edits, the cursors', async () => {
  const { file, cv } = canvasHome();
  try {
    assert.deepEqual(cv.look(), { open: false, hint: 'на холсте нет проекта — откройте его (canvas_open)' });
    await cv.open(file);
    cv.pointer({ x: 430, y: 30, selected: [2, 3, 'x'] });
    cv.cursor('Помощник 1', { object: 4, say: 'смотрю лампу' });
    assert.throws(() => cv.cursor('Человек', { x: 1, y: 1 }), /мышь/);
    assert.throws(() => cv.cursor('Агент', { object: 99 }), /нет объекта #99/);
    assert.throws(() => cv.cursor('Агент', {}), /куда навести/);
    const far = cv.cursor('Агент', { x: 5000, y: -3 });
    assert.deepEqual([far.x, far.y], [800, 0], 'inside the window');
    await cv.edit('Агент', [{ op: 'tag', object: 3, old: 'GVL.OFF_DSL', new: 'GVL.OFF_DSL_2' }]);
    const seen = cv.look();
    assert.equal(seen.window.number, 10);
    assert.equal(seen.objects.length, 4, 'the window itself is not an object to edit');
    assert.deepEqual(seen.objects[1], { index: 2, kind: 'Text', bbox: [20, 15, 200, 55], text: 'Нория 3' });
    assert.deepEqual(seen.person.pointer, { x: 430, y: 30 });
    assert.equal(seen.person.over.index, 3);
    assert.deepEqual(seen.person.over.tags, ['GVL.OFF_DSL_2']);
    assert.deepEqual(seen.person.selected.map((o) => o.index), [2, 3]);
    assert.equal(seen.recent.at(-1).who, 'Агент');
    assert.match(seen.recent.at(-1).text, /тег #3 GVL\.OFF_DSL → GVL\.OFF_DSL_2/);
    assert.deepEqual(seen.cursors.map((c) => c.who).sort(), ['Агент', 'Помощник 1']);
    assert.equal(seen.cursors.find((c) => c.who === 'Помощник 1').say, 'смотрю лампу');
    assert.deepEqual(seen.windows.map((w) => w.number), [3, 10, 23]);
  } finally {
    cv.stop();
  }
});

test('canvas: a change from outside (EasyBuilder saved) redraws it and drops the undo copies', async () => {
  const { file, cv, events } = canvasHome();
  try {
    await cv.open(file);
    await cv.edit('Агент', [{ op: 'move', object: 1, dx: 4 }]);
    assert.equal(cv.state().undo, 1);
    const p = JSON.parse(fs.readFileSync(file, 'utf8'));
    p.windows[1].objects[2].labels[0].text = 'Из EasyBuilder';
    await new Promise((r) => setTimeout(r, 30));
    fs.writeFileSync(file, JSON.stringify(p));
    await until(() => events.notice.length);
    await until(() => cv.state().scene.objects[2].labels[0].text === 'Из EasyBuilder');
    assert.match(events.notice[0].text, /изменён снаружи/);
    assert.equal(cv.state().undo, 0, 'undo would wipe the change made outside');
    // its own edits are not news
    await cv.edit('Агент', [{ op: 'move', object: 1, dx: 4 }]);
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(events.notice.length, 1);
  } finally {
    cv.stop();
  }
});

test('canvas: the tool — where it is, which Python, what is said when it is missing or falls', async () => {
  const home = tmp();
  assert.equal(canvasLib.toolSpec(home), null);
  const none = new canvasLib.Canvas({ home, cache: path.join(home, 'cache') });
  const file = path.join(home, 'a.cmtp');
  fs.writeFileSync(file, '{}');
  await assert.rejects(none.open(file), /ebpro-cmtp нет на этом компьютере/);
  const dir = path.join(home, 'tools', 'ebpro-cmtp');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'server.py'), '');
  assert.deepEqual(canvasLib.toolSpec(home), { command: process.platform === 'win32' ? 'python' : 'python3', args: [path.join(dir, 'server.py')], cwd: dir });
  const sync = process.execPath;
  config.writeJson(path.join(home, 'desktop', 'desktop.json'), { python: sync });
  assert.equal(canvasLib.toolSpec(home).command, sync, 'the synchronizer\'s Python');
  const own = path.join(dir, 'python-own');
  fs.writeFileSync(own, '');
  config.writeJson(path.join(home, 'state.json'), { tools: { 'ebpro-cmtp': { python: own } } });
  assert.equal(canvasLib.toolSpec(home).command, own, 'the tool\'s own environment first');
  // the tool falls: said, and started again for the next call
  const { file: f2, cv } = canvasHome({ FAKE_CMTP_CRASH: 'cmtp_scene' });
  try {
    await assert.rejects(cv.open(f2), /ebpro-cmtp остановился \(код 3\)/);
    const t = new canvasLib.Tool({ command: path.join(home, 'нет-python'), args: [] });
    await assert.rejects(t.call('cmtp_windows', {}), /не найден Python/);
  } finally {
    cv.stop();
  }
  assert.deepEqual(canvasLib.normalize([{ op: 'style', object: 2, fill: null, frame: '#112233',
    frame_width: 2, text_color: '#abcdef', font_size: 18, label: 0 },
  { op: 'bind_tag', object: 3, slot: 1, new: 'GVL.RUN' }], 10), [
    { op: 'style', window: '10', object: 2, frame_width: 2, font_size: 18, label: 0,
      fill: null, frame: '#112233', text_color: '#abcdef' },
    { op: 'bind_tag', window: '10', object: 3, slot: 1, new: 'GVL.RUN' },
  ]);
  assert.equal(canvasLib.describe([{ op: 'move', object: 1, dx: 2, dy: 0 }, { op: 'move', object: 4, dx: 2, dy: 0 }]), 'сдвиг #1, #4 на (2, 0)');
  assert.deepEqual(canvasLib.normalize([{ op: 'move', object: '3', dx: 1 }], 10), [{ op: 'move', window: '10', object: 3, dx: 1 }]);
});

test('the MCP server of the window: the canvas tools go to the window and say what is on the canvas', async () => {
  const home = tmp();
  const calls = [];
  const look = { open: true, file: '/x/велута.cmtp', window: { number: 23, name: 'Нория 3' }, person: { selected: [{ index: 9 }] } };
  const bridge = new Bridge({ file: path.join(home, 'desktop', 'bridge.json'), handlers: {
    canvas_open: async (b) => { calls.push(['open', b]); return { open: true, file: b.path, window: { number: 23, name: 'Нория 3', width: 600, height: 1015 }, objects: 48, windows: 88 }; },
    canvas_look: async (b) => { calls.push(['look', b]); return look; },
    canvas_window: async (b) => { calls.push(['window', b]); return { open: true, file: '/x/велута.cmtp', window: { number: 10, name: 'Главное', width: 800, height: 480 }, objects: 5, windows: 88 }; },
    canvas_cursor: async (b) => { calls.push(['cursor', b]); return { cursor: { who: b.who, x: 140, y: 40, say: b.say } }; },
    canvas_edit: async (b) => {
      calls.push(['edit', b]);
      if (b.who === 'Чужой') throw new Error('SYSAI: папка /x агенту не разрешена на запись (SYSAI → Доступы → Папки)');
      return { text: '1. объект #9 сдвинут', results: [{ op: 'move', window: 23, object: 9 }], window: 23, objects: [9], undo: 1 };
    },
  } });
  const server = mcp(home);
  try {
    await bridge.start();
    await server.rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '1' } });
    const opened = await server.rpc('tools/call', { name: 'canvas_open', arguments: { path: ' /x/велута.cmtp ', window: 23 } });
    assert.match(opened.result.content[0].text, /^На холсте: \/x\/велута\.cmtp, окно 23 «Нория 3» \(600×1015, объектов: 48; окон в проекте: 88\)/);
    const seen = await server.rpc('tools/call', { name: 'canvas_look', arguments: {} });
    assert.deepEqual(JSON.parse(seen.result.content[0].text), look);
    await server.rpc('tools/call', { name: 'canvas_window', arguments: { window: 'Главное' } });
    const cur = await server.rpc('tools/call', { name: 'canvas_cursor', arguments: { who: 'Помощник 1', object: 9, say: 'вот он' } });
    assert.equal(cur.result.content[0].text, 'Курсор «Помощник 1» — на (140, 40): вот он');
    const edited = await server.rpc('tools/call', { name: 'canvas_edit', arguments: { who: 'Агент', edits: [{ op: 'move', object: 9, dx: 10 }] } });
    assert.match(edited.result.content[0].text, /объект #9 сдвинут\nНа холсте — окно 23, объекты #9/);
    assert.deepEqual(edited.result.structuredContent, { results: [{ op: 'move', window: 23, object: 9 }], window: 23, objects: [9] });
    const refused = await server.rpc('tools/call', { name: 'canvas_edit', arguments: { who: 'Чужой', edits: [{ op: 'move', object: 9 }] } });
    assert.equal(refused.result.isError, true);
    assert.match(refused.result.content[0].text, /^SYSAI: папка/);
    for (const [name, args] of [['canvas_open', {}], ['canvas_window', {}], ['canvas_edit', { edits: [] }]]) {
      assert.equal((await server.rpc('tools/call', { name, arguments: args })).result.isError, true, name);
    }
    assert.deepEqual(calls.map((c) => c[0]), ['open', 'look', 'window', 'cursor', 'edit', 'edit']);
    assert.deepEqual(calls[0][1], { path: '/x/велута.cmtp', window: 23 });
    assert.deepEqual(calls[3][1], { who: 'Помощник 1', object: 9, say: 'вот он' });
  } finally {
    bridge.stop();
    await server.close();
  }
});

// ------------------------------------------------------------------------------------------------ projects
test('projects: a folder per project — create, drop materials in, what is inside, the requests for the agent', async () => {
  const root = path.join(tmp(), 'SYSAI Проекты');
  const pj = new Projects(root);
  assert.deepEqual(await pj.list(), { root, projects: [] }); // the folder appears with the first project
  const made = await pj.create('  ЕФБШ-621   Достоево ');
  assert.equal(made.name, 'ЕФБШ-621 Достоево');
  assert.deepEqual(fs.readdirSync(made.path).sort(), ['docs', 'hmi', 'materials', 'plc', 'project.md']);
  assert.match(fs.readFileSync(made.memory, 'utf8'), /^# ЕФБШ-621 Достоево\n/);
  await assert.rejects(pj.create('ЕФБШ-621 Достоево'), /уже есть/);
  for (const bad of ['', 'a/b', '..', 'con', 'x'.repeat(81), 'a:b']) await assert.rejects(pj.create(bad));
  await assert.rejects(pj.info('Нет такого'), /нет проекта/);

  const src = tmp();
  fs.writeFileSync(path.join(src, 'Dostoevo.export'), '<ExportFile/>');
  fs.writeFileSync(path.join(src, 'Э3 схема.pdf'), 'pdf');
  fs.mkdirSync(path.join(src, 'HMI'));
  fs.writeFileSync(path.join(src, 'HMI', 'line.cmtp'), 'x');
  fs.writeFileSync(path.join(src, 'HMI', '~$lock.docx'), 'x');
  const done = await pj.add(made.name, [path.join(src, 'Dostoevo.export'), path.join(src, 'Э3 схема.pdf'), path.join(src, 'HMI'),
    path.join(src, 'Dostoevo.export'), path.join(src, 'нет.txt'), made.path]);
  assert.deepEqual(done.map((d) => d.error || path.basename(d.to)),
    ['Dostoevo.export', 'Э3 схема.pdf', 'HMI', 'Dostoevo (2).export', 'нет такого файла', 'это папка самого проекта']);
  assert.equal(fs.readFileSync(path.join(src, 'Dostoevo.export'), 'utf8'), '<ExportFile/>'); // copied, not moved

  fs.mkdirSync(path.join(made.path, 'plc'), { recursive: true });
  fs.writeFileSync(path.join(made.path, 'plc', 'Main.st'), 'PROGRAM Main');
  fs.writeFileSync(path.join(made.path, 'style.md'), '# стиль');
  const info = await pj.info(made.name);
  assert.deepEqual(info.materials.map((f) => [f.rel, f.group]).sort(), [['materials/Dostoevo (2).export', 'plc'],
    ['materials/Dostoevo.export', 'plc'], ['materials/HMI/line.cmtp', 'hmi'], ['materials/Э3 схема.pdf', 'scheme']]);
  assert.deepEqual(info.plc.map((f) => f.rel), ['plc/Main.st']);
  assert.equal(info.style, path.join(made.path, 'style.md'));
  const listed = await pj.list();
  assert.deepEqual(listed.projects.map((p) => [p.name, p.materials, p.style, p.plc, p.hmi]), [['ЕФБШ-621 Достоево', 4, true, true, false]]);

  const pr = projectPrompts(info);
  assert.ok(pr.analyze.includes(made.path) && pr.analyze.includes('plc-project') && pr.analyze.includes('style.md'));
  assert.ok(pr.build.includes(made.path) && pr.build.includes('plc_hmi_tags') && pr.build.includes('«ОПО»') && pr.build.endsWith('Стиль: '));
  assert.deepEqual(['a.EXPORT', 'b.emtp', 'c.dwg', 'd.xlsx', 'e.png', 'f.bin'].map(groupOf), ['plc', 'hmi', 'scheme', 'doc', 'image', 'other']);
  assert.equal(cleanName(' Линия  1. '), 'Линия 1');

  // the stages: nothing started → the agent marks them in pipeline.json → the window shows them and what to do next
  assert.deepEqual(info.pipeline.stages.map((s) => [s.stage, s.state]), STAGES.map(([k]) => [k, 'todo']));
  assert.equal(info.pipeline.next, 'materials');
  assert.ok(!info.pipeline.started && pr.create.includes('Начни с материалов') && pr.create.includes('plc_link') && pr.create.includes('me_create'));
  fs.writeFileSync(path.join(made.path, 'pipeline.json'), JSON.stringify({ stages: {
    materials: { state: 'done', note: '12 механизмов' }, decisions: { state: 'done' }, plc: { state: 'work', note: 'пишу классы' },
    build: { state: 'нет такого' }, junk: { state: 'done' } },
  facts: { controller: 'TM241CE40R', panel: 'cMT3072X', link: 'opcua', resolution: [1024, 600], password: 'x' } }));
  fs.mkdirSync(path.join(made.path, 'docs'), { recursive: true });
  fs.writeFileSync(path.join(made.path, 'docs', 'Руководство.docx'), 'x');
  const going = await pj.info(made.name);
  assert.deepEqual(going.pipeline.stages.map((s) => s.state), ['done', 'done', 'work', 'todo', 'todo', 'todo']);
  assert.deepEqual(going.pipeline.facts, { controller: 'TM241CE40R', panel: 'cMT3072X', resolution: '1024×600', link: 'opcua', link_title: 'OPC UA' });
  assert.equal(going.pipeline.next, 'plc');
  assert.deepEqual(going.docs.map((f) => f.rel), ['docs/Руководство.docx']);
  assert.deepEqual(going.other, []); // pipeline.json is the window's own, not a file of the project
  const pr2 = projectPrompts(going);
  assert.ok(pr2.create.includes('продолжай с этапа «Программа ПЛК»') && pr2.docs.includes('project-docs') && pr2.docs.includes('doc_outline'));
  assert.ok(pr2.stage('plc').includes('Сейчас о нём записано: пишу классы') && pr2.stage('nope') === '');
  assert.deepEqual((await pj.list()).projects[0].stages, ['done', 'done', 'work', 'todo', 'todo', 'todo']);
  fs.writeFileSync(path.join(made.path, 'pipeline.json'), '{broken');
  assert.equal(projectPipeline(made.path).started, false);
});
