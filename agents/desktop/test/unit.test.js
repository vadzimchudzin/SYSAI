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
const { Projects, prompts: projectPrompts, pipeline: projectPipeline, groupOf, cleanName, isResult, STAGES } = require('../lib/projects');
const rules = require('../lib/rules');
const { AutoApprove, MAX_PER_HOUR } = require('../lib/autoapprove');
const storage = require('../lib/storage');
const apps = require('../lib/apps');
const chats = require('../lib/chats');
const swarmLib = require('../lib/swarm');
const relaunchLib = require('../lib/relaunch');

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
test('session restore is shared by selection and send, and failed loads can retry', async () => {
  const { ensureLoaded } = await import('../renderer/js/session-load.mjs');
  const conv = { loaded: false, draft: false, loadPromise: null };
  let calls = 0;
  let release;
  const load = () => {
    calls += 1;
    return new Promise((resolve) => { release = resolve; });
  };

  const selected = ensureLoaded(conv, load);
  const sent = ensureLoaded(conv, load);
  assert.equal(selected, sent, 'both callers await the same session/load request');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 1);
  release(true);
  assert.equal(await selected, true);
  assert.equal(conv.loadPromise, null);

  const broken = { loaded: false, draft: false, loadPromise: null };
  let attempts = 0;
  assert.equal(await ensureLoaded(broken, async () => { attempts += 1; throw new Error('load failed'); }), false);
  assert.equal(broken.loadPromise, null);
  assert.equal(await ensureLoaded(broken, async () => { attempts += 1; broken.loaded = true; return true; }), true);
  assert.equal(attempts, 2, 'a failed restore does not poison the next retry');
});

test('only one send can reserve a conversation while async preparation is pending', async () => {
  const { reserveSend, releaseSend } = await import('../renderer/js/send-lock.mjs');
  const conv = { running: false, sending: false };
  let finish;
  const first = (async () => {
    if (!reserveSend(conv)) return false;
    await new Promise((resolve) => { finish = resolve; });
    releaseSend(conv);
    return true;
  })();

  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reserveSend(conv), false, 'a second send cannot enter while the first is awaiting');
  finish();
  assert.equal(await first, true);
  assert.equal(reserveSend(conv), true, 'a new send is allowed after the first reservation is released');
  releaseSend(conv);
  assert.equal(reserveSend({ running: true, sending: false }), false, 'an already-running conversation stays blocked');
});

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

test('canvas: a window is read to the last page, not only the first', async () => {
  // cmtp_scene отдаёт окно страницами (limit/offset, next_offset): холст обязан пройти все, иначе на экране
  // только первые объекты — так и было до 16e0a9b, и теста на это не было
  const { file, cv } = canvasHome({ FAKE_CMTP_PAGE: '2' });
  try {
    const got = await cv.open(file);
    assert.equal(got.objects, 4, 'все объекты окна, а не первая страница');
    const st = cv.state();
    assert.deepEqual(st.scene.objects.map((o) => o.index), [0, 1, 2, 3, 4]);
    assert.equal(st.scene.objects[4].picture[0].key, 'aa11', 'картинка с последней страницы тоже здесь');
  } finally { cv.stop(); }
});

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
  assert.deepEqual(fs.readdirSync(made.path).sort(), ['_work', 'docs', 'hmi', 'materials', 'plc', 'project.md']);
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
  assert.deepEqual(going.pipeline.facts, { controller: 'TM241CE40R', panel: 'cMT3072X', resolution: '1024×600', link: 'opcua', link_title: 'OPC UA',
    scope_title: 'Проект полностью' });
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

// ------------------------------------------------------------------------------------------------ правки без вопросов
function autoApprove({ rulesDoc = { folders: { read: ['*'], write: ['*'], deny: [] } }, guarded = () => false } = {}) {
  const base = fs.realpathSync(tmp());
  const root = path.join(base, 'SYSAI Проекты');
  const project = path.join(root, 'PLC test');
  for (const d of ['materials', 'plc', '_work/st', '.git']) fs.mkdirSync(path.join(project, d), { recursive: true });
  fs.mkdirSync(path.join(base, 'SYSAI Проекты 2'), { recursive: true });
  const state = { rules: rulesDoc, now: Date.parse('2026-11-12T10:00:00Z') };
  const make = () => new AutoApprove({ file: path.join(base, 'desk', 'autoapprove.json'), root, audit: path.join(base, 'desk', 'audit'),
    loadRules: () => (state.rules ? { rules: state.rules } : { error: 'нет' }),
    pathAllowed: (r, p, write) => (r.folders[write ? 'write' : 'read'].includes('*') && !r.folders.deny.some((d) => p.startsWith(d))),
    guarded, now: () => state.now });
  return { base, root, project, state, make, auto: make() };
}

const OPTIONS = [{ optionId: 'allow_once', name: 'Allow once', kind: 'allow_once' }, { optionId: 'deny', name: 'Deny', kind: 'reject_once' }];
const edit = (file, extra = {}) => ({ sessionId: 's1', options: OPTIONS, toolCall: { toolCallId: 'e1', title: `Approve edit: ${file}`, kind: 'edit',
  content: [{ type: 'diff', path: file, oldText: null, newText: 'x := 1;\n' }], ...extra } });

test('edits without questions: off by default, only inside a project folder, the rest is asked with a reason', () => {
  const { base, root, project, state, make, auto } = autoApprove();
  const inside = path.join(project, '_work', 'st', 'Main.st');
  assert.deepEqual(auto.decide(edit(inside)), { auto: false, why: '' }, 'off until the person switches it on');
  assert.deepEqual(auto.set(true), { on: true, count: 0, root, project: '', policy: 'projects' });
  assert.deepEqual(auto.decide(edit(inside)), { auto: true, optionId: 'allow_once', paths: [inside] });
  assert.equal(auto.decide(edit(path.join(project, 'plc', 'new', 'GVL.st'))).auto, true, 'a folder that is not there yet');
  assert.equal(auto.decide(edit('_work/st/Main.st'), project).auto, true, 'a short path counts from the folder of the session');
  const asked = (params, cwd) => {
    const d = auto.decide(params, cwd);
    assert.equal(d.auto, false);
    return d.why;
  };
  assert.match(asked(edit('_work/st/Main.st'), base), /вне папки проектов/);
  assert.match(asked(edit('_work/st/Main.st')), /не полный/);
  assert.match(asked(edit(path.join(base, 'notes.md'))), /вне папки проектов/);
  assert.match(asked(edit(path.join(project, '..', '..', 'notes.md'))), /вне папки проектов/);
  assert.match(asked(edit(path.join(base, 'SYSAI Проекты 2', 'a', 'b.st'))), /вне папки проектов/, 'a folder that only starts the same');
  assert.match(asked(edit(path.join(root, 'заметка.md'))), /не в папке проекта/);
  assert.match(asked(edit(path.join(project, 'materials', 'ТЗ.md'))), /материалы/);
  assert.match(asked(edit(path.join(project, 'MATERIALS', 'sub', 'a.csv'))), /материалы/);
  for (const name of ['.env', '.env.local', 'plc/server.pem', '.git/config', 'plc/id_rsa', 'hmi/keys.kdbx']) {
    assert.match(asked(edit(path.join(project, name))), /секрет/, name);
  }
  assert.match(asked(edit(path.join(project, 'plc', 'a.st:stream'))), /необычное имя/);
  assert.match(asked(edit(path.join(project, 'plc'))), /не файл/);
  assert.match(asked({ ...edit(inside), toolCall: { ...edit(inside).toolCall, kind: 'execute' } }), /не правка/);
  assert.match(asked({ ...edit(inside), toolCall: { kind: 'edit', title: 'patch' } }), /не назван файл/);
  assert.match(asked(edit(inside, { content: [{ type: 'diff', path: inside, oldText: 'a', newText: 'я'.repeat(1024 * 1024 + 1) }] })), /большой файл/);
  assert.match(asked(edit(inside, { content: [{ type: 'diff', path: inside, oldText: 'a', newText: null }] })), /удаление/);
  // a patch names its files itself: every one of them is checked, deletions and moves are always asked
  const patch = (text) => edit(inside, { rawInput: { tool: 'patch', arguments: { mode: 'patch', patch: text } } });
  assert.equal(auto.decide(patch(`*** Begin Patch\n*** Update File: ${inside}\n@@\n-a\n+b\n*** End Patch`)).auto, true);
  assert.match(asked(patch(`*** Begin Patch\n*** Update File: ${inside}\n*** Add File: ${path.join(base, 'evil.ps1')}\n*** End Patch`)), /evil\.ps1: файл вне/);
  assert.match(asked(patch(`*** Begin Patch\n*** Delete File: ${inside}\n*** End Patch`)), /удаление/);
  assert.match(asked(patch(`*** Begin Patch\n*** Update File: ${inside}\n*** Move to: ${inside}.bak\n*** End Patch`)), /перенос/);
  assert.match(asked(edit(inside, { locations: [{ path: path.join(project, 'materials', 'a.pdf') }] })), /материалы/);
  assert.match(asked({ ...edit(inside), options: [{ optionId: 'always', kind: 'allow_always' }, { optionId: 'deny', kind: 'reject_once' }] }), /разового/);
  // the signed rules of SYSAI stay the limit; without them nothing is allowed here
  state.rules = { folders: { read: ['*'], write: [], deny: [] } };
  assert.match(asked(edit(inside)), /не открыта агенту на запись/);
  state.rules = null;
  assert.match(asked(edit(inside)), /нет подписанных доступов/);
  state.rules = { folders: { read: ['*'], write: ['*'], deny: [] } };
  const guardedOne = autoApprove({ guarded: () => true });
  guardedOne.auto.set(true);
  assert.match(guardedOne.auto.decide(edit(path.join(guardedOne.project, 'plc', 'a.st'))).why, /только SYSAI/);
  // kept between starts of the window; a damaged file means off
  assert.equal(make().on, true);
  fs.writeFileSync(path.join(base, 'desk', 'autoapprove.json'), '{"on": "yes"');
  assert.equal(make().on, false);
});

test('edits without questions: a link out of the projects is asked', { skip: process.platform === 'win32' }, () => {
  const { base, project, auto } = autoApprove();
  auto.set(true);
  fs.mkdirSync(path.join(base, 'elsewhere'));
  fs.symlinkSync(path.join(base, 'elsewhere'), path.join(project, 'plc', 'out'));
  const d = auto.decide(edit(path.join(project, 'plc', 'out', 'a.st')));
  assert.equal(d.auto, false);
  assert.match(d.why, /ссылка ведёт из папки проектов/);
});

test('edits without questions: the old file is copied aside, the edit is in the journal, a runaway agent is asked', () => {
  const { base, project, state, auto } = autoApprove();
  auto.set(true);
  const file = path.join(project, 'plc', 'Main.st');
  fs.writeFileSync(file, Buffer.from([0xef, 0xbb, 0xbf, 0x61, 0x0d, 0x0a]));
  const params = edit(file);
  const d = auto.decide(params);
  assert.equal(auto.allow(params, d), true);
  assert.equal(auto.state().count, 1);
  const audit = path.join(base, 'desk', 'audit');
  const line = JSON.parse(fs.readFileSync(path.join(audit, '2026-11.jsonl'), 'utf8').trim());
  assert.equal(line.decision, 'auto');
  assert.deepEqual(line.paths, [file]);
  assert.equal(line.session, 's1');
  assert.deepEqual(fs.readFileSync(line.copies[0]), fs.readFileSync(file), 'the copy is byte for byte');
  const fresh = edit(path.join(project, 'plc', 'New.st'));
  assert.equal(auto.allow(fresh, auto.decide(fresh)), true, 'a new file has nothing to copy');
  // the copies of a month ago go, the journal stays
  state.now += 40 * 86400000;
  auto.clean();
  assert.equal(fs.existsSync(line.copies[0]), false);
  assert.ok(fs.existsSync(path.join(audit, '2026-11.jsonl')));
  // more edits in an hour than a person would follow: asked again
  for (let i = 0; i < MAX_PER_HOUR; i++) auto.recent.push(state.now);
  assert.match(auto.decide(params).why, /зациклился/);
  state.now += 3600001;
  assert.equal(auto.decide(params).auto, true);
  // switching off and on starts the count anew
  assert.equal(auto.set(true).count, 0);
});

test('edits without questions: the window answers the fake Hermes itself inside a project, a person outside', async () => {
  const { project, auto } = autoApprove();
  auto.set(true);
  const answered = [];
  const run = async (dir) => {
    const { client, requests } = hermes({ FAKE_HERMES_FILES: dir });
    try {
      await client.initialize({ name: 'test', version: '1' }, { timeout: 10000 });
      const { sessionId } = await client.request('session/new', { cwd: dir, mcpServers: [] });
      client.on('request', ({ id, params }) => {
        const d = auto.decide(params, dir);
        answered.push(d.auto);
        const ok = d.auto && auto.allow(params, d);
        client.respond(id, { outcome: { outcome: 'selected', optionId: ok ? d.optionId : 'reject_once' } });
      });
      await client.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'сводка' }] });
      return requests().find((r) => r.permission).permission.outcome.optionId;
    } finally {
      client.stop();
    }
  };
  assert.equal(await run(path.join(project, 'docs')), 'allow_once');
  assert.equal(await run(tmp()), 'reject_once');
  assert.deepEqual(answered, [true, false]);
  assert.equal(auto.state().count, 1);
});

test('projects: only results among the results — the rest is offered to _work and moved there, never deleted', async () => {
  const pj = new Projects(path.join(tmp(), 'SYSAI Проекты'));
  const made = await pj.create('Линия 1');
  const put = (rel, text = 'x') => {
    fs.mkdirSync(path.dirname(path.join(made.path, rel)), { recursive: true });
    fs.writeFileSync(path.join(made.path, rel), text);
  };
  for (const rel of ['plc/Line.project', 'plc/Main.st', 'plc/project.json', 'plc/gen/make_io.py', 'plc/notes.md', 'hmi/Line.cmtp',
    'hmi/tags.csv', 'hmi/pictures/noria.png', 'docs/Руководство.docx', 'docs/Руководство.md', 'docs/img/main.png',
    '_work/st/GVL.st', '_work/notes/io.csv', 'materials/ТЗ.md']) put(rel, rel);
  put('_work/plc/Main.st', 'старый черновик');
  assert.deepEqual(['plc/A.project', 'plc/a.PROJECTARCHIVE', 'hmi/a.emtp', 'docs/a.pdf', 'plc/a.st', 'hmi/a.png', 'docs/a.md', '_work/a.project',
    'materials/a.project'].map(isResult), [true, true, true, true, false, false, false, false, false]);
  const info = await pj.info(made.name);
  assert.deepEqual(info.results.map((f) => f.rel), ['plc/Line.project', 'hmi/Line.cmtp', 'docs/Руководство.docx']);
  assert.deepEqual(info.extra.map((f) => f.rel).sort(), ['docs/img/main.png', 'docs/Руководство.md', 'hmi/pictures/noria.png', 'hmi/tags.csv',
    'plc/gen/make_io.py', 'plc/Main.st', 'plc/notes.md', 'plc/project.json'].sort());
  assert.equal(info.work.files, 3);
  assert.deepEqual(info.other, []); // _work is the drafts, not «прочее»
  const pr = projectPrompts(info);
  assert.ok(pr.create.includes('_work') && pr.build.includes('_work/st') && pr.docs.includes('_work/docs'));

  const done = await pj.tidy(made.name);
  assert.equal(done.length, 8);
  assert.ok(done.every((d) => !d.error && d.to.startsWith('_work/')));
  assert.equal(done.find((d) => d.rel === 'plc/Main.st').to, '_work/plc/Main (2).st'); // a name that is taken is kept
  assert.equal(fs.readFileSync(path.join(made.path, '_work', 'plc', 'Main (2).st'), 'utf8'), 'plc/Main.st');
  assert.equal(fs.readFileSync(path.join(made.path, '_work', 'plc', 'Main.st'), 'utf8'), 'старый черновик');
  assert.equal(fs.readFileSync(path.join(made.path, '_work', 'hmi', 'pictures', 'noria.png'), 'utf8'), 'hmi/pictures/noria.png');
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'plc')), ['Line.project']);
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'hmi')), ['Line.cmtp']); // the emptied folders went
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'docs')), ['Руководство.docx']);
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'materials')), ['ТЗ.md']); // what the engineer gave is not touched
  const after = await pj.info(made.name);
  assert.deepEqual(after.extra, []);
  assert.equal(after.work.files, 11);
  assert.deepEqual(await pj.tidy(made.name), []);
});

test('projects: «Убрать в _work» puts the program from plc/ where it is built from and leaves what the programs keep', async () => {
  const { isService, workPlace } = require('../lib/projects');
  const pj = new Projects(path.join(tmp(), 'SYSAI Проекты'));
  const made = await pj.create('Линия 2');
  const put = (rel) => {
    fs.mkdirSync(path.dirname(path.join(made.path, rel)), { recursive: true });
    fs.writeFileSync(path.join(made.path, rel), rel);
  };
  // a project made before _work: the program lies right in plc/; Machine Expert has the project open
  for (const rel of ['plc/Line.project', 'plc/Main.st', 'plc/GVL.st', 'plc/project.json', 'plc/notes.md', 'plc/gen/make.py',
    'plc/Line.project.~u', 'plc/Line-ivanov-PC7.opt', 'plc/Line.Device.Application.precompilecache', 'plc/Line.backup',
    'hmi/Line.cmtp', 'hmi/Line.cmtp.bak', 'hmi/tags.csv']) put(rel);
  assert.deepEqual(['plc/a.project.~u', 'plc/A-User-PC.OPT', 'hmi/a.bak', 'plc/a.st', 'plc/a.project', '_work/a.opt', 'materials/a.bak']
    .map(isService), [true, true, true, false, false, false, false]);
  assert.deepEqual([workPlace('plc/Main.st', true), workPlace('plc/project.json', true), workPlace('plc/Main.st', false),
    workPlace('plc/sub/Main.st', true), workPlace('hmi/a.st', true), workPlace('plc/notes.md', true)],
  [['st'], ['st'], ['plc'], ['plc', 'sub'], ['hmi'], ['plc']]);
  const info = await pj.info(made.name);
  assert.deepEqual(info.results.map((f) => f.rel), ['plc/Line.project', 'hmi/Line.cmtp']);
  assert.deepEqual(info.extra.map((f) => f.rel).sort(), ['hmi/tags.csv', 'plc/GVL.st', 'plc/Main.st', 'plc/gen/make.py', 'plc/notes.md',
    'plc/project.json'].sort());
  const done = await pj.tidy(made.name);
  assert.ok(done.every((d) => !d.error));
  const to = Object.fromEntries(done.map((d) => [d.rel, d.to]));
  assert.deepEqual(to, { 'plc/Main.st': '_work/st/Main.st', 'plc/GVL.st': '_work/st/GVL.st', 'plc/project.json': '_work/st/project.json',
    'plc/notes.md': '_work/plc/notes.md', 'plc/gen/make.py': '_work/plc/gen/make.py', 'hmi/tags.csv': '_work/hmi/tags.csv' });
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'plc')).sort(), ['Line-ivanov-PC7.opt', 'Line.Device.Application.precompilecache',
    'Line.backup', 'Line.project', 'Line.project.~u'].sort());
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'hmi')).sort(), ['Line.cmtp', 'Line.cmtp.bak']);
  assert.deepEqual((await pj.info(made.name)).extra, []);
  // a program is in _work/st already: an old one from plc/ is not mixed into it
  put('plc/Main.st');
  assert.equal((await pj.tidy(made.name))[0].to, '_work/plc/Main.st');
});

test('projects: what each material is for — .sysai/sources.json, from this computer and from the storage', async () => {
  const { storageFiles, guessRole, SOURCES } = require('../lib/projects');
  const root = tmp();
  const pj = new Projects(path.join(root, 'SYSAI Проекты'));
  const made = await pj.create('Линия 3');
  const src = path.join(root, 'src');
  fs.mkdirSync(path.join(src, 'pics'), { recursive: true });
  for (const [rel, text] of [['ТЗ.docx', 'tz'], ['Rodina.project', 'proj'], ['pics/noria.png', 'png'], ['motor.png', 'm']]) fs.writeFileSync(path.join(src, rel), text);
  await pj.add(made.name, ['ТЗ.docx', 'Rodina.project', 'pics', 'motor.png'].map((n) => path.join(src, n)));
  fs.writeFileSync(path.join(made.path, 'materials', 'руками.pdf'), 'x'); // put there in the Explorer, not through the window
  assert.deepEqual([guessRole('a.png', false), guessRole('a.pdf', false), guessRole('pics', true)], ['picture-library', 'material', 'material']);
  let rows = await pj.sources(made.name);
  assert.deepEqual(Object.fromEntries(rows.map((x) => [x.path, [x.kind, x.role, x.origin, Boolean(x.guessed)]])), {
    'materials/motor.png': ['file', 'picture-library', 'local', false], 'materials/pics': ['folder', 'material', 'local', false],
    'materials/Rodina.project': ['file', 'material', 'local', false], 'materials/ТЗ.docx': ['file', 'material', 'local', false],
    'materials/руками.pdf': ['file', 'material', 'local', true] });
  assert.equal(rows.find((x) => x.path === 'materials/ТЗ.docx').sha256.length, 64);
  await pj.setRole(made.name, 'materials/Rodina.project', 'template');
  await pj.setRole(made.name, 'materials/pics', 'picture-library');
  await assert.rejects(pj.setRole(made.name, 'materials/Rodina.project', 'boss'), /нет такой роли/);
  await assert.rejects(pj.setRole(made.name, 'materials/нет.pdf', 'template'), /нет такого материала/);

  // from the storage: a file and a whole folder; the listing is the storage's
  const tree = { 'ОПО/Картинки': [{ name: 'Нории', path: 'ОПО/Картинки/Нории', is_folder: true }, { name: 'a.png', path: 'ОПО/Картинки/a.png', version: 2 }],
    'ОПО/Картинки/Нории': [{ name: 'n1.png', path: 'ОПО/Картинки/Нории/n1.png', sha256: 'f'.repeat(64) }] };
  const list = async (folder) => ({ items: tree[folder] || [] });
  const one = await storageFiles({}, { name: 'Шаблон.project', path: 'ОПО/Шаблоны/Шаблон.project', version: 3 }, list);
  assert.deepEqual(one.map((f) => [f.source, f.under, f.version]), [['ОПО/Шаблоны/Шаблон.project', '', 3]]);
  const many = await storageFiles({}, { name: 'Картинки', path: 'ОПО/Картинки', kind: 'folder' }, list);
  assert.deepEqual(many.map((f) => [f.source, f.under, f.root]), [['ОПО/Картинки/Нории/n1.png', 'Картинки/Нории', 'ОПО/Картинки'],
    ['ОПО/Картинки/a.png', 'Картинки', 'ОПО/Картинки']]);
  await assert.rejects(storageFiles({}, { name: 'Картинки', path: 'ОПО/Картинки', kind: 'folder' }, list, 1), /больше 1 файлов/);
  const cache = path.join(root, 'cache');
  fs.mkdirSync(cache);
  const local = (name) => { const p = path.join(cache, name); fs.writeFileSync(p, name); return p; };
  const done = await pj.attach(made.name, [{ ...one[0], item: undefined, path: local('Шаблон.project') },
    ...many.map((f) => ({ ...f, item: undefined, path: local(path.basename(f.source)) }))], '');
  assert.ok(done.every((d) => !d.error) && done.length === 3);
  assert.deepEqual(fs.readdirSync(path.join(made.path, 'materials', 'Картинки')).sort(), ['a.png', 'Нории']);
  rows = await pj.sources(made.name);
  const got = Object.fromEntries(rows.map((x) => [x.path, x]));
  assert.deepEqual([got['materials/Шаблон.project'].origin, got['materials/Шаблон.project'].from, got['materials/Шаблон.project'].version],
    ['storage', 'ОПО/Шаблоны/Шаблон.project', 3]);
  assert.deepEqual([got['materials/Картинки'].kind, got['materials/Картинки'].origin, got['materials/Картинки'].from], ['folder', 'storage', 'ОПО/Картинки']);
  assert.equal(got['materials/Rodina.project'].role, 'template'); // what the person said is kept
  const saved = JSON.parse(fs.readFileSync(path.join(made.path, SOURCES), 'utf8'));
  assert.equal(saved.schema, 1);
  assert.ok(!saved.inputs.some((x) => x.path === 'materials/руками.pdf')); // a guess is not written down

  // the agent is told the roles with the request of a button; the folder of the project stays a line of its own
  const info = await pj.info(made.name);
  assert.deepEqual(info.sources.length, rows.length);
  const pr = projectPrompts(info);
  assert.match(pr.create, /Папка проекта: [^\n]+\nШаблон: materials\/Rodina\.project\. Библиотека картинок: [^\n]*materials\/pics/);
  fs.rmSync(path.join(made.path, 'materials', 'Rodina.project'));
  assert.ok(!(await pj.sources(made.name)).some((x) => x.path === 'materials/Rodina.project')); // gone from the folder — gone from the list
  assert.ok(!(await pj.info(made.name)).materials.some((f) => f.rel.startsWith('.sysai'))); // the record is not a material
});

test('a big text file attached to a message is named by its path, not linked', async () => {
  const dir = tmp();
  const put = (name, bytes) => { const p = path.join(dir, name); fs.writeFileSync(p, Buffer.alloc(bytes, 97)); return p; };
  const small = put('notes.md', 2000);
  const big = put('tags.csv', 200 * 1024);
  const code = put('Main.st', 31 * 1024);
  const pdf = put('scheme.pdf', 5 * 1024 * 1024);
  const png = put('screen.png', 900 * 1024);
  const got = await files.attachments([small, big, code, pdf, png, path.join(dir, 'нет.txt')]);
  assert.deepEqual(got.items.map((x) => x.link), [true, false, false, true, true, true]); // only big texts are held back
  assert.equal(files.INLINE_TEXT_MAX, 30 * 1024);
  const [links, bigs] = got.text.split('\n[Большие файлы');
  assert.ok(links.includes(small) && links.includes(pdf) && links.includes(png) && !links.includes(big) && !links.includes(code));
  assert.ok(bigs.includes(`${big} — 200 КБ`) && bigs.includes(`${code} — 31 КБ`) && bigs.includes('целиком не читай'));
  assert.deepEqual(await files.attachments([]), { items: [], text: '' });
  assert.equal((await files.attachments([small])).text, `[Вложения: ${small}]`);
});

test('edits without questions: one project only, what the profile of SYSAI allows, and an edit taken back', () => {
  const { root, project, state, make, auto } = autoApprove();
  const other = path.join(root, 'Линия 2');
  fs.mkdirSync(path.join(other, '_work'), { recursive: true });
  const mine = path.join(project, '_work', 'st', 'Main.st');
  const theirs = path.join(other, '_work', 'a.st');

  // only inside the project named
  assert.deepEqual(auto.set(true, 'PLC test'), { on: true, count: 0, root, project: 'PLC test', policy: 'projects' });
  assert.equal(auto.decide(edit(mine)).auto, true);
  assert.match(auto.decide(edit(theirs)).why, /другой проект: без вопросов сейчас только «PLC test»/);
  assert.equal(make().state().project, 'PLC test', 'the project survives a restart of the window');
  assert.throws(() => auto.set(true, '../x'), /нет такого проекта/);

  // the profile is the ceiling: one project at a time, or nothing
  state.rules = { folders: { read: ['*'], write: ['*'], deny: [] }, edits: { auto: 'project' } };
  assert.equal(auto.state().policy, 'project');
  assert.throws(() => auto.set(true), /только в одном проекте/);
  auto.set(true, 'PLC test');
  assert.equal(auto.decide(edit(mine)).auto, true);
  const all = make();
  all.on = true;                     // switched on for every project before the profile was narrowed
  all.project = '';
  assert.match(all.decide(edit(mine)).why, /только в одном проекте/);
  state.rules = { folders: { read: ['*'], write: ['*'], deny: [] }, edits: { auto: 'off' } };
  assert.match(auto.decide(edit(mine)).why, /не разрешает/);
  assert.throws(() => auto.set(true, 'PLC test'), /не разрешает/);
  assert.equal(auto.set(false).on, false, 'switching off is always possible');
  state.rules = { folders: { read: ['*'], write: ['*'], deny: [] } };

  // «Вернуть»: the changed file gets its old text, the file the edit made is moved away, nothing is deleted
  auto.set(true);
  fs.writeFileSync(mine, 'старый текст');
  const made = path.join(project, '_work', 'st', 'GVL.st');
  const params = edit(mine, { content: [{ type: 'diff', path: mine, oldText: 'старый текст', newText: 'новый' },
    { type: 'diff', path: made, oldText: null, newText: 'VAR_GLOBAL END_VAR' }] });
  const d = auto.decide(params);
  assert.equal(d.auto, true);
  assert.ok(auto.allow(params, d));
  fs.writeFileSync(mine, 'новый');   // the agent's edit itself
  fs.writeFileSync(made, 'VAR_GLOBAL END_VAR');
  const [last] = auto.recentEdits();
  assert.deepEqual([last.files, last.project, last.undone], [['Main.st', 'GVL.st'], 'PLC test', false]);
  state.now += 60000;
  assert.deepEqual(auto.undo(last.id), { restored: ['Main.st'], removed: ['GVL.st'] });
  assert.equal(fs.readFileSync(mine, 'utf8'), 'старый текст');
  assert.ok(!fs.existsSync(made));
  const kept = fs.readdirSync(path.join(path.dirname(auto.file), 'audit', 'undo', '2026-11-12'));
  assert.ok(kept.some((n) => n.endsWith('-u0-Main.st')) && kept.some((n) => n.endsWith('-un0-GVL.st')), 'what was there is kept aside');
  assert.equal(auto.recentEdits()[0].undone, true);
  assert.throws(() => auto.undo(last.id), /уже возвращена/);
  assert.throws(() => auto.undo('2020-01-01T00:00:00.000Z'), /нет в журнале/);
  // an edit whose copy is gone, or whose file may no longer be written, is not taken back
  fs.writeFileSync(mine, 'ещё раз');
  const p2 = edit(mine, { content: [{ type: 'diff', path: mine, oldText: 'ещё раз', newText: 'x' }] });
  state.now += 60000;
  assert.ok(auto.allow(p2, auto.decide(p2)));
  const id2 = auto.recentEdits()[0].id;
  state.rules = { folders: { read: ['*'], write: [], deny: [] } };
  assert.throws(() => auto.undo(id2), /не открыта агенту на запись/);
  state.rules = { folders: { read: ['*'], write: ['*'], deny: [] } };
  fs.rmSync(path.join(path.dirname(auto.file), 'audit', 'undo'), { recursive: true });
  assert.throws(() => auto.undo(id2), /копия старого файла не сохранилась/);
});

test('a conversation is saved as a Markdown file and a project remembers its conversations', async () => {
  const convExport = require('../lib/export');
  const at = new Date('2026-11-13T09:05:00Z');
  const md = convExport.markdown({ title: 'Этап: программа ПЛК', project: 'Линия 1', agent: 'opo-ivan', at, blocks: [
    { type: 'user', text: 'Собери проект\n\n[Вложения: C:\\ТЗ.pdf]', files: [{ name: 'ТЗ.pdf', path: 'C:\\ТЗ.pdf' }] },
    { type: 'tools', tools: ['plc_spec_check', 'plc_check'] }, { type: 'agent', text: 'Готово: **12 механизмов**.' },
    { type: 'thought', text: 'не для файла' }, { type: 'user', text: 'спасибо', files: [] }] });
  assert.ok(md.startsWith('# Этап: программа ПЛК\n\nПроект: Линия 1 · Агент: opo-ivan · Сохранён: 2026-11-13 09:05 UTC\n'));
  assert.ok(md.includes('## Человек\n\nСобери проект\n\n- вложение: C:\\ТЗ.pdf') && !md.includes('[Вложения'));
  assert.ok(md.includes('*Инструменты: plc_spec_check, plc_check*\n\n## Агент\n\nГотово: **12 механизмов**.'));
  assert.ok(!md.includes('не для файла') && md.trimEnd().endsWith('спасибо'));
  assert.equal(convExport.fileName('Этап: «плк» / 1?', at), '2026-11-13-09-05 Этап «плк» 1.md');
  const dir = path.join(tmp(), 'notes');
  const one = await convExport.save(dir, { title: 'Разговор', at, blocks: [{ type: 'user', text: 'привет' }] });
  const two = await convExport.save(dir, { title: 'Разговор', at, blocks: [] });
  assert.ok(fs.readFileSync(one, 'utf8').includes('привет') && two.endsWith(' (2).md'));

  const pj = new Projects(path.join(tmp(), 'SYSAI Проекты'));
  const made = await pj.create('Линия 4');
  assert.deepEqual(await pj.dialogs(made.name), []);
  await pj.noteDialog(made.name, { id: 's-1', title: 'Материалы' });
  await pj.noteDialog(made.name, { id: 's-2', title: 'Программа ПЛК' });
  const again = await pj.noteDialog(made.name, { id: 's-1', title: 'другое название не затирает первое' });
  assert.deepEqual([again.title, again.messages], ['Материалы', 2]);
  const info = await pj.info(made.name);
  assert.deepEqual(info.dialogs.map((d) => [d.id, d.messages]).sort(), [['s-1', 2], ['s-2', 1]]);
  assert.equal(await pj.noteDialog('Нет такого', { id: 'x' }), null);
  assert.equal(await pj.noteDialog(made.name, {}), null);
  assert.ok(!info.materials.length && !info.other.some((f) => f.rel.includes('dialogs')));
});

test('rows of actions: MCP tools of Hermes (mcp__server__tool, the older mcp_server_tool, through tool_call) by name', async () => {
  const { describeTool, splitMcp, answeredError } = await import('../renderer/js/toolname.mjs');
  assert.deepEqual(splitMcp('mcp__plc_kit__plc_stage_update'), { server: 'plc-kit', tool: 'plc_stage_update' });
  assert.deepEqual(splitMcp('mcp_machine_expert_me_build'), { server: 'machine-expert', tool: 'me_build' });
  assert.deepEqual(splitMcp('mcp_window_show_file'), { server: 'window', tool: 'show_file' });
  assert.equal(splitMcp('terminal'), null);
  const stage = describeTool({ title: 'mcp__plc_kit__plc_stage_update: D:/p',
    rawInput: { project_dir: 'D:/p', stage: 'hmi', state: 'done', note: 'экраны собраны' } });
  assert.equal(stage.label, 'ПЛК');
  assert.equal(stage.detail, 'этап «экраны» → готово · экраны собраны');
  const viaSearch = describeTool({ title: 'tool_call: mcp__ebpro_cmtp__cmtp_render',
    rawInput: { name: 'mcp__ebpro_cmtp__cmtp_render', arguments: { window: 10 } } });
  assert.equal(viaSearch.label, 'EasyBuilder');
  assert.equal(viaSearch.server, 'ebpro-cmtp');
  assert.equal(describeTool({ title: 'mcp__window__canvas_open: Line.cmtp' }).label, 'Открывает на холсте');
  assert.equal(describeTool({ title: 'mcp__sysai__whoami' }).label, 'SYSAI');
  assert.equal(describeTool({ title: 'mcp__my_server__go' }).label, 'my-server');
  assert.equal(describeTool({ title: 'terminal: dir' }).label, 'Команда');
  assert.ok(answeredError({ content: [{ type: 'content', content: { type: 'text', text: 'ОШИБКА (plc-kit/plc_generate): нет файла' } }] }));
  assert.ok(!answeredError({ content: [{ type: 'content', content: { type: 'text', text: 'готово' } }] }));
});

// ------------------------------------------------------------------- «Открыть в программе»: кнопки форматов
test('apps: в папке проекта видно, что чем открывается — по одному файлу на формат', () => {
  const dir = tmp();
  fs.mkdirSync(path.join(dir, 'hawell'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'hawell', 'program.il'), 'LD X0\n');
  fs.writeFileSync(path.join(dir, 'Line.cmtp'), '');
  fs.mkdirSync(path.join(dir, 'plc'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'plc', 'Line.project'), '');
  fs.mkdirSync(path.join(dir, 'hmi', 'HMI', 'Frame'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'hmi', 'HMI', 'Frame', 'Main.wcfg'), '');
  // черновик агента с таким же расширением: в _work он не результат и кнопкой открываться не должен
  fs.mkdirSync(path.join(dir, '_work'), { recursive: true });
  fs.writeFileSync(path.join(dir, '_work', 'Old.cmtp'), '');

  const got = apps.detect(dir);
  const byId = Object.fromEntries(got.map((a) => [a.id, a]));
  assert.deepEqual(got.map((a) => a.id), ['oni', 'easybuilder', 'me', 'hawell']);
  // Haiwell: файла проекта нет, поэтому открывается среда, а программа едет буфером
  assert.equal(byId.hawell.open, 'paste');
  assert.equal(byId.hawell.file, path.join(dir, 'hawell', 'program.il'));
  assert.equal(byId.easybuilder.file, path.join(dir, 'Line.cmtp'));
  assert.equal(byId.me.file, path.join(dir, 'plc', 'Line.project'));
  // ONI открывает проект, а .wcfg лежит внутри HMI/Frame — отдаём папку, в которой есть HMI
  assert.equal(byId.oni.file, path.join(dir, 'hmi'));

  assert.deepEqual(apps.detect(tmp()), []);                       // пустая папка — ни одной кнопки
});

test('apps: где искать программу — сначала папки вендора в Program Files, EasyBuilder ещё и в корне диска', () => {
  const env = { 'ProgramFiles(x86)': 'D:\\Apps', ProgramFiles: 'D:\\Apps64' };
  const hh = apps.candidates('hawell', env);
  assert.equal(hh[0], path.join('D:\\Apps', 'Haiwell', 'HaiwellHappy', 'HaiwellHappy.exe'));
  assert.ok(hh.includes(path.join('D:\\Apps64', 'Haiwell', 'HaiwellHappy', 'HaiwellHappy.exe')));
  const eb = apps.candidates('easybuilder', env);
  assert.ok(eb.includes('C:\\EBpro\\EasyBuilderPro.exe'));
  assert.ok(eb.includes(path.join('D:\\Apps', 'EBpro', 'EasyBuilderPro.exe')));
});

test('apps: Haiwell без файла — программа едет буфером, остальные форматы открываются файлом', () => {
  const hh = apps.plan('hawell', 'C:\\p\\hawell\\program.il');
  assert.equal(hh.kind, 'paste');
  assert.deepEqual(hh.keys, ['F7', 'Ctrl+V', 'F9']);
  assert.equal(apps.plan('easybuilder', 'C:\\p\\Line.cmtp').kind, 'file');
  assert.deepEqual(apps.plan('oni', 'C:\\p\\hmi').keys, []);
  assert.throws(() => apps.plan('me', ''), /нет файла/);
  assert.throws(() => apps.plan('nope', 'C:\\x'), /нет такого формата/);
});

test('apps: сценарий вставки — F7 → Ctrl+V → F9, и путь с апострофом не ломает PowerShell', () => {
  const s = apps.pasteScript("C:\\O'Brien\\HaiwellHappy.exe", { wait: 9 });
  assert.match(s, /Start-Sleep -Seconds 9/);
  assert.match(s, /SendWait\('\{F7\}'\)/);
  assert.match(s, /SendWait\('\^v'\)/);
  assert.match(s, /SendWait\('\{F9\}'\)/);
  assert.match(s, /O''Brien/);                                    // апостроф удвоен — иначе строка закроется
  assert.match(s, /Get-Process -Name 'HaiwellHappy'/);            // ждём окно, а не «спим и надеемся»
});

// ------------------------------------------------------------------------------------------------ обновление
const updates = require('../lib/updates');

test('обновление: версия и сборка', () => {
  assert.deepEqual(updates.compare({ version: '0.4.10' }, { version: '0.4.11' }), {
    update: true, why: 'SYSAI даёт 0.4.11, на компьютере 0.4.10' });
  assert.equal(updates.compare({ version: '0.4.11' }, { version: '0.4.11' }).update, false);
  // та же версия, но собранная из других исходников — это обновление: ровно тот случай, когда «ничего не прилетало»
  assert.equal(updates.compare({ version: '0.4.11', build: 'a' }, { version: '0.4.11', build: 'b' }).update, true);
  assert.equal(updates.compare({ version: '0.4.11', build: 'b' }, { version: '0.4.11', build: 'b' }).update, false);
  assert.equal(updates.compare({ version: '0.4.12' }, { version: '0.4.11' }).update, false, 'на компьютере новее');
  assert.equal(updates.compare({}, { version: '0.4.11' }).update, true, 'версия неизвестна — пусть попробует');
});

test('обновление: что предлагает SYSAI и почему не вышло', async () => {
  const home = tmp();
  const state = { versions: { 'sysai-agent': '0.4.10' } };
  const doc = { doc: { synchronizer: { version: '0.4.11', build: 'f'.repeat(64), sha256: 'a'.repeat(64) } } };
  const got = await updates.check({ home, server: 'https://sysai.test/', token: 'tok', state,
    request: async () => ({ status: 200, body: doc }) });
  assert.equal(got.ok, true);
  assert.equal(got.update, true);
  assert.equal(got.offered.version, '0.4.11');
  assert.equal(got.autoUpdate, true);

  const refused = await updates.check({ home, server: 'https://sysai.test', token: 'tok', state,
    request: async () => ({ status: 401, body: {} }) });
  assert.equal(refused.ok, false);
  assert.equal(refused.status, 401);
  assert.match(refused.error, /не принял токен агента/);

  const offline = await updates.check({ home, server: 'https://sysai.test', token: 'tok', state,
    request: async () => { throw new Error('ENOTFOUND'); } });
  assert.match(offline.error, /Нет связи с SYSAI/);

  const lonely = await updates.check({ home, server: '', token: '', state, request: async () => ({ status: 200, body: {} }) });
  assert.equal(lonely.ok, false);
  assert.match(lonely.error, /не подключён/);
});

test('обновление: новое окно при свежем синхронизаторе — тоже «есть обновление»', async () => {
  const home = tmp();
  const state = { versions: { 'sysai-agent': '0.4.15' } };
  const doc = { doc: { synchronizer: { version: '0.4.15', build: '' }, desktop: { version: '0.10.3', sha256: 'd'.repeat(64) } } };
  const request = async () => ({ status: 200, body: doc });
  const got = await updates.check({ home, server: 'https://sysai.test', token: 'tok', state, request, window: '0.10.2' });
  assert.equal(got.update, true, 'синхронизатор свежий, а окно новее — плашка должна быть');
  assert.equal(got.desktop, true);
  assert.equal(got.offered.version, '0.10.3');
  assert.match(got.why, /окно 0\.10\.3, открыто 0\.10\.2/);
  const same = await updates.check({ home, server: 'https://sysai.test', token: 'tok', state, request, window: '0.10.3' });
  assert.equal(same.update, false, 'то же окно — обновлять нечего');
});

test('обновление: новая сборка в state.json — окно узнаёт сразу, не через полчаса', () => {
  const home = tmp();
  const file = path.join(home, 'state.json');
  const write = (s) => fs.writeFileSync(file, JSON.stringify(s));
  write({ offer: { version: '0.4.14', build: 'a' } });
  const ticks = [];
  const timer = { set: (fn) => { ticks.push(fn); return 7; }, clear: (id) => ticks.push(`stop ${id}`) };
  const heard = [];
  const stop = updates.watch({ home, changed: (k) => heard.push(k), timer });
  ticks[0]();
  assert.deepEqual(heard, [], 'то, что было при открытии, окно проверяет само');
  write({ offer: { version: '0.4.15', build: 'b' } });
  ticks[0]();
  ticks[0]();
  assert.equal(heard.length, 1, 'один раз на новую сборку');
  write({ offer: { version: '0.4.15', build: 'b' }, desktop_offer: { version: '0.10.2', sha256: 'c' } });
  ticks[0]();
  assert.equal(heard.length, 2, 'новое окно — тоже новость');
  fs.writeFileSync(file, '{битый');
  ticks[0]();
  assert.equal(heard.length, 2, 'битый файл (синхронизатор пишет его прямо сейчас) — не новость');
  stop();
  assert.equal(ticks[1], 'stop 7');
  assert.equal(updates.offerKey({}), '');
});

test('обновление: «установить» только просит синхронизатор', () => {
  const home = tmp();
  assert.equal(updates.wake(home).asking, false, 'цикла нет — будить некого');
  fs.writeFileSync(path.join(home, 'run.lock'), '1');
  assert.equal(updates.wake(home).asking, true);
  assert.equal(fs.readFileSync(path.join(home, 'wake'), 'utf8'), 'wake');
});

// ---------------------------------------------------------------- разговоры окна на этом компьютере (lib/chats.js)
test('a conversation is kept on this computer and comes back whole after a restart', async () => {
  const home = tmp();
  try {
    await chats.write(home, { id: 'abc-1', hermes: 'abc-1', title: 'Нория', project: 'Достоево',
      cwd: 'C:\\Проекты\\Достоево', messages: [{ t: 'user', text: 'посмотри шнек' }, { t: 'agent', text: 'смотрю' }] });
    const listed = await chats.list(home);
    assert.equal(listed.dialogs.length, 1);
    assert.equal(listed.dialogs[0].title, 'Нория');
    assert.equal(listed.dialogs[0].messages, 2);
    assert.equal(listed.dialogs[0].project, 'Достоево');
    const back = await chats.read(home, 'abc-1');
    assert.deepEqual(back.messages.map((m) => m.t), ['user', 'agent'], 'что было сказано — на месте');
    assert.equal(back.cwd, 'C:\\Проекты\\Достоево', 'папка разговора (в ней он и продолжится)');
    // следующий ответ дописывается: заголовок и начало разговора не теряются
    await chats.write(home, { id: 'abc-1', messages: [{ t: 'user', text: 'ещё' }] });
    const again = await chats.read(home, 'abc-1');
    assert.equal(again.title, 'Нория');
    assert.equal(again.created, back.created, 'дата начала не переписывается');
    assert.equal(again.messages.length, 1);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('a deleted conversation stays deleted even though the agent still lists its session', async () => {
  const home = tmp();
  try {
    await chats.write(home, { id: 'gone-1', title: 'Старый', messages: [{ t: 'user', text: 'привет' }] });
    await chats.remove(home, 'gone-1');
    const listed = await chats.list(home);
    assert.equal(listed.dialogs.length, 0, 'файла нет — в списке нет');
    assert.deepEqual(listed.deleted, ['gone-1'], 'надгробие: сессия Hermes не воскресит разговор');
    assert.equal(await chats.read(home, 'gone-1'), null);
    // удаление того, чего нет, — не ошибка (окно могло нажать дважды)
    await chats.remove(home, 'gone-1');
    assert.deepEqual((await chats.list(home)).deleted, ['gone-1']);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('a conversation cannot point outside the folder of conversations', async () => {
  const home = tmp();
  try {
    await chats.write(home, { id: '../../evil', title: 'x', messages: [{ t: 'user', text: 'x' }] });
    const names = fs.readdirSync(chats.dir(home)).filter((n) => n.endsWith('.json'));
    assert.deepEqual(names, ['evil.json'], 'имя очищено, за папку не вышли');
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('the conversations of a project go with the project', async () => {
  const home = tmp();
  try {
    await chats.write(home, { id: 'p-1', project: 'Достоево', messages: [{ t: 'user', text: 'x' }] });
    await chats.write(home, { id: 'p-2', project: 'Родина', messages: [{ t: 'user', text: 'y' }] });
    const done = await chats.removeProject(home, 'Достоево');
    assert.equal(done.removed, 1);
    const left = (await chats.list(home)).dialogs;
    assert.deepEqual(left.map((d) => d.id), ['p-2'], 'чужие разговоры не тронуты');
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------- «Удалить проект»
test('a project goes to the trash of the projects folder, not into nothing', async () => {
  const root = path.join(tmp(), 'SYSAI Проекты');
  const pj = new Projects(root);
  await pj.create('Достоево');
  fs.writeFileSync(path.join(pj.dir('Достоево'), 'materials', 'схема.txt'), 'Э3', 'utf8');
  const other = await pj.create('Родина');
  const done = await pj.remove('Достоево', 'trash');
  assert.equal(done.mode, 'trash');
  assert.ok(fs.existsSync(done.path), 'папка лежит в .trash');
  assert.equal(fs.readFileSync(path.join(done.path, 'materials', 'схема.txt'), 'utf8'), 'Э3', 'материалы целы');
  assert.equal(fs.existsSync(pj.dir('Достоево')), false, 'из проектов ушёл');
  const list = await pj.list();
  assert.deepEqual(list.projects.map((p) => p.name), [other.name], 'в списке проектов его нет, .trash не проект');
});

test('a project is erased together with its materials on request', async () => {
  const pj = new Projects(path.join(tmp(), 'SYSAI Проекты'));
  await pj.create('Достоево');
  const done = await pj.remove('Достоево', 'erase');
  assert.equal(done.mode, 'erase');
  assert.equal(fs.existsSync(pj.dir('Достоево')), false);
  assert.deepEqual((await pj.list()).projects, []);
  await assert.rejects(() => pj.remove('Достоево', 'erase'), /нет такого проекта/, 'второй раз — честная ошибка');
  await assert.rejects(() => pj.remove('../SYSAI Проекты', 'erase'), /нет такого проекта|названии/, 'за папку проектов не выйти');
});

// ---------------------------------------------------------------- объём проекта, дерево решений, компиляция
test('a new project is made with the scope the person chose, and the scope skips its stages', async () => {
  const root = tmp();
  try {
    const pj = new Projects(path.join(root, 'Проекты'));
    const full = await pj.create('Линия 1');
    assert.equal(full.pipeline.scope, 'full', 'без выбора — проект полностью');
    assert.deepEqual(full.pipeline.stages.filter((s) => s.skipped), []);

    const hmi = await pj.create('Шкаф СП-2', 'hmi');
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(hmi.path, 'pipeline.json'), 'utf8')).facts, { scope: 'hmi' },
      'объём лежит в pipeline.json — там его читает plc_stage_update');
    assert.equal(hmi.pipeline.scope, 'hmi');
    assert.equal(hmi.pipeline.facts.scope_title, 'Только экраны');
    assert.deepEqual(hmi.pipeline.stages.filter((s) => s.skipped).map((s) => s.stage), ['plc', 'build']);
    assert.equal(hmi.pipeline.started, false, 'записанный объём — ещё не начатая работа');
    assert.deepEqual(hmi.other, [], 'pipeline.json — файл окна, не проекта');
    // the agent closes the decisions: the next stage is the screens, not the PLC program the scope left out
    fs.writeFileSync(path.join(hmi.path, 'pipeline.json'), JSON.stringify({ stages: { materials: { state: 'done' }, decisions: { state: 'done' } },
      facts: { scope: 'hmi', panel: 'cMT3072X', language: 'st' } }));
    const going = projectPipeline(hmi.path);
    assert.equal(going.next, 'hmi');
    assert.equal(going.facts.language_title, 'ST');
    assert.deepEqual((await pj.list()).projects.find((p) => p.name === 'Шкаф СП-2').stages,
      ['done', 'done', 'skip', 'skip', 'todo', 'todo'], 'в списке проект показывает пропущенные этапы пропущенными');
    const pr = projectPrompts({ ...(await pj.info('Шкаф СП-2')), pipeline: going });
    assert.match(pr.create, /только экраны/);
    assert.match(pr.create, /«Программа ПЛК» и «Компиляция» пропущены/);
    assert.match(pr.create, /продолжай с этапа «Экраны»/);
    // the route the request lists is the scope's route: no PLC program and no build for «только экраны»
    const route = (text) => text.slice(text.indexOf('Иди по этапам'), text.indexOf('Среда —'));
    assert.doesNotMatch(route(pr.create), /программа ПЛК на ST|me_build|plc_project_build/, '«только экраны» не ведёт через ПЛК');
    assert.match(route(pr.create), /экраны панели/);

    const plc = await pj.create('Насосная', 'plc');
    assert.deepEqual(plc.pipeline.stages.filter((s) => s.skipped).map((s) => s.stage), ['hmi']);
    assert.match(projectPrompts(plc).create, /только программу ПЛК/);
    assert.doesNotMatch(route(projectPrompts(plc).create), /экраны панели|pic-index/, '«только ПЛК» не ведёт через экраны');
    assert.match(route(projectPrompts(plc).create), /me_build/);
    assert.match(route(projectPrompts(full).create), /программа ПЛК на ST.*экраны панели/s, 'полный проект — весь путь');
    await assert.rejects(pj.create('Линия 2', 'screens'), /нет такого объёма/);
    assert.equal(fs.existsSync(pj.dir('Линия 2')), false, 'папка не создаётся при неверном объёме');
    // a name of Object.prototype is not a scope: refused at creation, and in a pipeline.json it means the whole project
    for (const bad of ['constructor', 'toString', '__proto__']) {
      await assert.rejects(pj.create(`Линия ${bad}`, bad), /нет такого объёма/, bad);
      fs.writeFileSync(path.join(plc.path, 'pipeline.json'), JSON.stringify({ facts: { scope: bad } }));
      assert.equal(projectPipeline(plc.path).scope, 'full', bad);
    }
    assert.equal((await pj.list()).projects.length, 3, 'один странный pipeline.json не роняет список проектов');
    // a project of the past has no scope: it is the whole project, nothing skipped
    fs.writeFileSync(path.join(full.path, 'pipeline.json'), JSON.stringify({ stages: { materials: { state: 'done' } }, facts: {} }));
    assert.equal(projectPipeline(full.path).next, 'decisions');
    assert.equal(projectPipeline(full.path).scope, 'full');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the decisions tree shows what was chosen and what follows, and never makes a value up', async () => {
  const { decisions, compileCard } = await import('../renderer/js/decisions.mjs');
  const root = tmp();
  try {
    const dir = path.join(root, 'p');
    fs.mkdirSync(dir);
    const write = (stages, facts) => fs.writeFileSync(path.join(dir, 'pipeline.json'), JSON.stringify({ stages, facts }));
    const openers = [{ id: 'me', title: 'EcoStruxure Machine Expert', kind: 'plc', file: 'plc/E.project' },
      { id: 'easybuilder', title: 'EasyBuilder Pro', kind: 'hmi', file: 'hmi/E.cmtp' }];
    const flat = (r) => [r, ...r.children.flatMap(flat)];
    const by = (r, key) => flat(r).find((x) => x.key === key);

    // nothing decided yet: every decision says «не выбран» and names its question; the scope is the whole project
    write({}, { scope: 'full' });
    let pl = projectPipeline(dir);
    let tree = decisions(pl, apps.environment(pl.facts, openers));
    assert.equal(tree.value, 'Проект полностью');
    for (const key of ['controller', 'panel', 'link', 'style', 'language']) {
      assert.equal(by(tree, key).value, 'не выбран', key);
      assert.equal(by(tree, key).ask, key);
    }
    assert.equal(by(tree, 'plc_program').value, '', 'программа не угадывается без контроллера');

    // decided: the controller brings its program and the languages it accepts; the link says why
    write({ materials: { state: 'done' }, decisions: { state: 'done' }, plc: { state: 'done' } },
      { scope: 'full', controller: 'TM241CE40R', panel: 'cMT3072X', resolution: [1024, 600], link: 'opcua', style: 'ОПО' });
    pl = projectPipeline(dir);
    const env = apps.environment(pl.facts, openers);
    assert.deepEqual(env.plc.writes, ['st'], 'Machine Expert: генератор пишет только ST');
    assert.match(env.plc.languageNote, /LD для Machine Expert пока не генерируется/);
    tree = decisions(pl, env, ['Картинки ОПО']);
    assert.deepEqual(tree.children.map((r) => r.key), ['controller', 'panel', 'link', 'build', 'docs']);
    assert.deepEqual(by(tree, 'controller').children.map((r) => [r.key, r.value]),
      [['plc_program', 'EcoStruxure Machine Expert'], ['language', 'не выбран'], ['style', 'ОПО']],
      'Machine Expert умеет два языка — язык не подставляется, а спрашивается');
    assert.equal(by(tree, 'panel').value, 'cMT3072X · 1024×600');
    assert.deepEqual(by(tree, 'panel').children.map((r) => r.value), ['EasyBuilder Pro', 'Картинки ОПО']);
    assert.equal(by(tree, 'link').value, 'OPC UA');
    assert.match(by(tree, 'link').why, /переменные по именам/);
    assert.equal(by(tree, 'build').value, 'не начата');
    // Haiwell has one language and the generator writes it: nothing to ask
    const hw = apps.environment({ controller: 'Haiwell S16' }, [{ id: 'hawell', title: 'Haiwell Happy', kind: 'plc', file: 'hawell/program.il' }]);
    assert.equal(by(decisions({ ...pl, facts: { ...pl.facts, controller: 'Haiwell S16' } }, hw), 'language').value, 'LD');

    // the compilation card: there while the build is the stage in hand, with the program of the controller to open
    assert.deepEqual(compileCard(pl, env), { state: 'todo', program: 'EcoStruxure Machine Expert',
      open: { id: 'me', title: 'EcoStruxure Machine Expert', file: 'plc/E.project' } });
    write({ materials: { state: 'done' }, decisions: { state: 'done' }, plc: { state: 'done' }, build: { state: 'done' } }, { scope: 'full' });
    assert.equal(compileCard(projectPipeline(dir), env), null, 'скомпилировано — карточки нет');
    write({ materials: { state: 'done' } }, { scope: 'full' });
    assert.equal(compileCard(projectPipeline(dir), env), null, 'до программы — рано');
    write({ materials: { state: 'done' }, decisions: { state: 'done' } }, { scope: 'hmi', panel: 'cMT3072X' });
    pl = projectPipeline(dir);
    assert.equal(compileCard(pl, env), null, '«только экраны» — компиляции нет');
    tree = decisions(pl, apps.environment(pl.facts, openers));
    assert.deepEqual(tree.children.map((r) => r.key), ['controller', 'panel', 'link', 'docs']);
    assert.deepEqual(by(tree, 'controller').children.map((r) => r.key), ['plc_program'], 'без ПЛК — без языка и стиля');
    const failing = { ...pl, stages: pl.stages.map((s) => (s.stage === 'build' ? { ...s, skipped: false, state: 'problem' } : s)) };
    assert.deepEqual(compileCard(failing, {}), { state: 'problem', program: 'среда контроллера', open: null }, 'файла нет — открывать нечего');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the compile answers and the unanswered decisions go to the agent as requests', async () => {
  const root = tmp();
  try {
    const pj = new Projects(path.join(root, 'Проекты'));
    const info = await pj.create('Элеватор');
    const pr = projectPrompts(info);
    assert.match(pr.compiled, /Да, окей/);
    assert.match(pr.compiled, /plc_stage_update build done/);
    assert.match(pr.compileErrors, /не прошла/);
    assert.match(pr.compileErrors, /problem/);
    assert.match(pr.compileErrors, /me_build/);
    for (const key of ['scope', 'controller', 'panel', 'link', 'language', 'style']) {
      assert.ok(pr.decide[key].includes(info.path) && /карточкой/.test(pr.decide[key]), key);
    }
    assert.match(pr.decide.language, /LD для него пока не генерируется/, 'вопрос языка честен про LD');
    assert.match(pr.decide.link, /plc_link/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------- «где и что будет»
test('the window says what will open where: controller to program, panel to program', () => {
  const openers = [
    { id: 'me', title: 'EcoStruxure Machine Expert', kind: 'plc', file: 'plc/Elevator.project' },
    { id: 'easybuilder', title: 'EasyBuilder Pro', kind: 'hmi', file: 'hmi/Line.cmtp' },
  ];
  const env = apps.environment({ controller: 'M241', panel: 'cMT3072X', resolution: '1024×768', link: 'opcua' }, openers);
  assert.equal(env.plc.fact, 'M241');
  assert.equal(env.plc.program, 'EcoStruxure Machine Expert');
  assert.equal(env.plc.vendor, 'Schneider Electric');
  assert.equal(env.plc.ready, true);
  assert.equal(env.hmi.program, 'EasyBuilder Pro');
  assert.equal(env.hmi.vendor, 'Weintek');
  assert.equal(env.resolution, '1024×768');
  assert.equal(env.link, 'opcua');
  // пустой проект: строки есть, программы ещё нет — окно так и говорит, а не молчит
  const none = apps.environment({}, []);
  assert.equal(none.plc.ready, false);
  assert.equal(none.plc.program, '');
  assert.equal(none.hmi.fact, '');
  // Haiwell открывается не файлом: программа уходит в буфер обмена — это тоже видно заранее
  const hw = apps.environment({ controller: 'Haiwell S16' }, [{ id: 'hawell', title: 'Haiwell Happy', kind: 'plc', file: 'hawell/program.il' }]);
  assert.equal(hw.plc.vendor, 'Haiwell');
  assert.match(hw.plc.serves, /списком инструкций/);
});

test('a project file names the program and the hardware it serves', async () => {
  const root = tmp();
  try {
    const pj = new Projects(path.join(root, 'Система'));
    fs.mkdirSync(path.join(pj.dir('Система'), 'plc'), { recursive: true });
    fs.writeFileSync(path.join(pj.dir('Система'), 'plc', 'Elevator.project'), 'x', 'utf8');
    const found = apps.detect(pj.dir('Система'));
    assert.equal(found.length, 1);
    assert.equal(found[0].id, 'me');
    assert.equal(found[0].vendor, 'Schneider Electric', 'окно называет вендора, а не только имя программы');
    assert.match(found[0].serves, /M2xx/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the panel decides which panel program opens: Weintek is not ONI', () => {
  const openers = [
    { id: 'oni', title: 'ONI Visual Studio', kind: 'hmi', file: 'hmi' },
    { id: 'easybuilder', title: 'EasyBuilder Pro', kind: 'hmi', file: 'hmi/Line.cmtp' },
    { id: 'me', title: 'EcoStruxure Machine Expert', kind: 'plc', file: 'plc/X.project' },
    { id: 'hawell', title: 'Haiwell Happy', kind: 'plc', file: 'hawell/program.il' },
  ];
  assert.equal(apps.environment({ panel: 'cMT3072X' }, openers).hmi.id, 'easybuilder', 'Weintek cMT — EasyBuilder');
  assert.equal(apps.environment({ panel: 'eMT3120A' }, openers).hmi.id, 'easybuilder');
  assert.equal(apps.environment({ panel: 'ONI FL-1200' }, openers).hmi.id, 'oni', 'панель ONI — ONI Visual Studio');
  assert.equal(apps.environment({ controller: 'TM241CE40R' }, openers).plc.id, 'me');
  assert.equal(apps.environment({ controller: 'Haiwell S16' }, openers).plc.id, 'hawell');
  // марка не названа — берём первую найденную, но окно так и подписывает («не выбран»)
  assert.equal(apps.environment({}, openers).hmi.id, 'oni');
});

// --------------------------------------------------------------------------------------------------- рой (соты)
test('the swarm splits work into independent cells and never leaves a part out', () => {
  const items = ['Нория', 'Шнек', 'Триер', 'МУЗ', 'Весы', 'Заслонка', 'Вентилятор'];
  const parts = swarmLib.chunk({ question: 'проверь спецификацию', items, cells: 3 });
  assert.equal(parts.length, 3, 'три соты — три части, а не семь');
  const all = parts.flatMap((p) => p.items);
  assert.deepEqual(all.sort(), [...items].sort(), 'ни один механизм не потерян и не продублирован');
  // без списка: вопрос по пунктам делится, а одна строка уходит одной соте
  assert.equal(swarmLib.chunk({ question: 'первое\n\nвторое\n\nтретье\n\nчетвёртое', cells: 2 }).length, 2);
  assert.equal(swarmLib.chunk({ question: 'проверь всё', cells: 8 }).length, 1);
  assert.equal(swarmLib.clampCells(400), 40);
  assert.equal(swarmLib.clampCells('коты'), 8);
  assert.equal(swarmLib.clampCells(1), 4);
});

test('a cell answer is read from the rubric, and free text is not thrown away', () => {
  const good = swarmLib.parseAnswer('готово\n<answer>{"checked":12,"found":[{"key":"Q1","value":"1","where":"Э3:2"}],'
    + '"missing":[{"key":"Q2","why":"нет в схеме"}],"notes":"ок"}</answer>');
  assert.equal(good.parsed, true);
  assert.equal(good.checked, 12);
  assert.equal(good.found[0].where, 'Э3:2');
  assert.equal(good.missing[0].key, 'Q2');
  const fenced = swarmLib.parseAnswer('```json\n{"found":[{"key":"A","value":"1"}]}\n```');
  assert.equal(fenced.found.length, 1);
  const prose = swarmLib.parseAnswer('Схему посмотрел, но адресов не нашёл.');
  assert.equal(prose.parsed, false);
  assert.match(prose.notes, /адресов не нашёл/);
});

test('the swarm runs its cells, counts them and names what disagrees', async () => {
  const root = tmp();
  const asked = [];
  const swarm = new swarmLib.Swarm({
    question: 'сверь адреса с Э3',
    items: ['Q1', 'Q2', 'Q3'],
    cells: 3,
    minutes: 1,
    root,
    arbiter: false,
    plan: false,
    ask: async ({ prompt, cell }) => {
      asked.push(cell.title);
      if (cell.title === 'Q2') throw new Error('модель отказала');
      // одинаковый ключ Q1 с разными значениями — это спор, его нельзя затереть
      const value = cell.title === 'Q1' ? '1' : '2';
      return `<answer>{"checked":2,"found":[{"key":"Q1","value":"${value}","where":"Э3"},`
        + `{"key":"S-${cell.title}","value":"ok","where":"лист 4"}],"missing":[],"notes":""}</answer>`;
    },
  });
  try {
    const state = await swarm.run();
    assert.equal(state.stage, 'done');
    assert.equal(state.counters.done, 2);
    assert.equal(state.counters.error, 1);
    assert.equal(state.counters.found, 4);
    assert.equal(state.result.conflicts.length, 1, 'разные значения на одном ключе — спор');
    assert.equal(state.result.conflicts[0].key, 'q1');
    assert.equal(state.result.conflicts[0].values.length, 2);
    assert.match(state.result.markdown, /## Спорное/);
    assert.match(state.result.markdown, /Q2 \| ошибка: модель отказала/);
    assert.ok(fs.existsSync(path.join(root, state.id, 'result.md')), 'отчёт остаётся на диске');
    assert.ok(fs.existsSync(path.join(root, state.id, 'cell-01.md')), 'у каждой соты — свой след');
    assert.equal(asked.length, 3, 'по заданию на соту');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the swarm never runs more cells at once than the access document allows', async () => {
  // «разом» из документа: срезается до числа сот; старый документ без «at» — как раньше, шесть
  assert.deepEqual(swarmLib.allowedOf({ swarm: { on: true, cells: 12, at: 3 } }), { on: true, cells: 12, at: 3 });
  assert.equal(swarmLib.allowedOf({ swarm: { on: true, cells: 5, at: 30 } }).at, 5);
  assert.equal(swarmLib.allowedOf({ swarm: { on: true, cells: 12 } }).at, swarmLib.CONCURRENCY);
  assert.equal(swarmLib.allowedOf({ swarm: { on: true, cells: 12, at: 0 } }).at, swarmLib.CONCURRENCY);
  assert.deepEqual(swarmLib.allowedOf(undefined), { on: false, cells: 0, at: swarmLib.CONCURRENCY });

  const allow = swarmLib.allowedOf({ swarm: { on: true, cells: 8, at: 2 } });
  let now = 0;
  let most = 0;
  const swarm = new swarmLib.Swarm({
    question: 'сверь', items: ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'], cells: allow.cells, minutes: 1,
    plan: false, arbiter: false, concurrency: allow.at,
    ask: async () => {
      now += 1;
      most = Math.max(most, now);
      await new Promise((r) => setTimeout(r, 5));
      now -= 1;
      return '<answer>{"checked":1,"found":[],"missing":[],"notes":""}</answer>';
    },
  });
  const state = await swarm.run();
  assert.equal(state.counters.done, 8);
  assert.equal(most, 2, 'одновременно ровно столько, сколько разрешили, а не шесть');
});

test('the window starts the swarm with the allowed «at once», not a fixed six', () => {
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(main, /swarmAllowedOf\(got && got\.rules\)/, 'разрешённое читается из подписанных доступов');
  assert.match(main, /new Swarm\(\{[^}]*concurrency: allow\.at/s, 'рой идёт с разрешённым «разом»');
});

test('a computer without synced access is told so, not that the swarm was turned off', () => {
  // нет access.json / он обновляется: причина из rules.load, а не «выключен»
  const home = tmp();
  try {
    const got = rules.load(home);
    assert.ok(got.error);
    const said = swarmLib.offText({ ...swarmLib.allowedOf(got.rules), why: got.error });
    assert.match(said, /недоступен/);
    assert.ok(said.includes(got.error), 'причина названа словами синхронизатора');
    assert.doesNotMatch(said, /выключен/);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
  // доступы есть, рой выключен — так и сказано, и сказано, где включают
  assert.match(swarmLib.offText(swarmLib.allowedOf({ swarm: { on: false, cells: 8 } })), /выключен.*Рой агентов/);
  assert.equal(swarmLib.offText(swarmLib.allowedOf({ swarm: { on: true, cells: 8 } })), '');
  // окно отдаёт этот текст и в панель, и в ошибку запуска
  const main = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
  assert.match(main, /off: swarmOffText\(allow\)/);
  assert.match(main, /if \(!allow\.on\) throw new Error\(allow\.off\)/);
  const panel = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'js', 'swarm.js'), 'utf8');
  assert.match(panel, /this\.allowed\.off \|\|/);
});

test('a cell that does not answer in time becomes «не успел», not a broken swarm', async () => {
  const swarm = new swarmLib.Swarm({
    question: 'долгая проверка', items: ['A', 'B'], cells: 2, minutes: 0.02, plan: false, arbiter: false,
    ask: ({ cell }) => (cell.title === 'A'
      ? Promise.resolve('<answer>{"checked":1,"found":[],"missing":[],"notes":"быстро"}</answer>')
      : new Promise(() => {})), // никогда не ответит
  });
  const state = await swarm.run();
  assert.equal(state.stage, 'done');
  assert.equal(state.counters.done, 1);
  assert.equal(state.counters.timeout, 1);
  assert.match(state.result.markdown, /не успел/);
});

test('the planner may split the work itself, and a bad plan falls back to the list', async () => {
  const swarm = new swarmLib.Swarm({
    question: 'проверь всё', items: ['М1', 'М2', 'М3', 'М4'], cells: 2, minutes: 1, arbiter: false,
    ask: async ({ prompt, cell }) => {
      if (cell.i === -1) return '<answer>{"chunks":[{"title":"механизмы 1-2","prompt":"проверь М1, М2"}]}</answer>';
      return '<answer>{"checked":1,"found":[],"missing":[],"notes":""}</answer>';
    },
  });
  const state = await swarm.run();
  assert.equal(state.cells.length, 1, 'план планировщика уважается');
  assert.equal(state.cells[0].title, 'механизмы 1-2');

  // рой меньше четырёх сот не бывает, поэтому делим четыре пункта на четыре части
  const fallback = new swarmLib.Swarm({
    question: 'проверь всё', items: ['М1', 'М2', 'М3', 'М4'], cells: 4, minutes: 1, arbiter: false,
    ask: async ({ cell }) => (cell.i === -1 ? 'ничего не придумал' : '<answer>{"checked":1,"found":[],"missing":[]}</answer>'),
  });
  const second = await fallback.run();
  assert.equal(second.cells.length, 4, 'плохой план — делим список сами');
});

// --------------------------------------------------------------------------------- перезапуск в новую сборку
test('restarting after an update opens the installed build, not the copy the window runs from', () => {
  const root = tmp();
  const home = path.join(root, 'SYSAI');
  const desk = path.join(home, 'desktop');
  const oldApp = path.join(desk, 'app-0.9.0-aaa');
  const newApp = path.join(desk, 'app-0.9.1-bbb');
  const exe = path.join(desk, 'electron', 'electron.exe');
  try {
    for (const dir of [oldApp, newApp]) {
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'package.json'), '{"version":"x"}');
    }
    fs.mkdirSync(path.dirname(exe), { recursive: true });
    fs.writeFileSync(exe, 'MZ');
    assert.equal(relaunchLib.next(home, oldApp), null, 'desktop.json ещё нет — обычный перезапуск');

    const write = (app, version) => fs.writeFileSync(path.join(desk, 'desktop.json'),
      JSON.stringify({ exe, app, version, installed_at: '2026-11-20T00:00:00+00:00' }));
    write(newApp, '0.9.1');
    const into = relaunchLib.next(home, oldApp);
    assert.ok(into, 'установлена другая сборка — окно должно открыться в ней');
    assert.equal(into.version, '0.9.1');
    assert.equal(path.resolve(into.app), path.resolve(newApp));
    assert.equal(relaunchLib.next(home, newApp), null, 'уже работаем в установленной — обычный перезапуск');

    // битая установка (нет package.json) — не перезапускаемся в пустоту
    fs.rmSync(path.join(newApp, 'package.json'));
    assert.equal(relaunchLib.next(home, oldApp), null);
    assert.equal(relaunchLib.installed(home), null);

    // нет файла вовсе и битый json — тоже обычный перезапуск, а не падение
    fs.rmSync(path.join(desk, 'desktop.json'));
    assert.equal(relaunchLib.next(home, oldApp), null);
    fs.writeFileSync(path.join(desk, 'desktop.json'), '{не json');
    assert.equal(relaunchLib.next(home, oldApp), null);

    // команда запуска — как у синхронизатора: exe + папка приложения
    const run = relaunchLib.command({ exe, app: newApp });
    assert.equal(run.exe, exe);
    assert.deepEqual(run.args, [newApp]);
    assert.equal(run.cwd, newApp);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('перезапуск: старая копия уходит только когда новая открыла окно', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sysai-rel-'));
  const home = path.join(root, 'SYSAI');
  const ready = relaunchLib.readyPath(home);
  assert.equal(ready, path.join(home, 'desktop', 'relaunch.json'));

  try {
    // 1. Новая копия пишет файл (это делает markReady в ready-to-show) — старая видит окно и уходит
    const notes = [];
    const child = new (require('node:events').EventEmitter)();
    child.pid = 4242;
    const waiting = relaunchLib.waitReady(ready, child, 3000, 40);
    setTimeout(() => {
      fs.mkdirSync(path.dirname(ready), { recursive: true });
      fs.writeFileSync(ready, JSON.stringify({ pid: 4242, version: '0.9.2' }));
    }, 80);
    const ok = await waiting;
    assert.equal(ok.ok, true, 'файл появился — старая копия может закрываться');
    assert.equal(ok.got.pid, 4242);
    notes.push(ok.got.version);
    assert.deepEqual(notes, ['0.9.2']);

    // 2. Новая копия закрылась, окна нет — старая НЕ закрывается и говорит почему
    fs.rmSync(ready, { force: true });
    const dead = new (require('node:events').EventEmitter)();
    dead.pid = 4243;
    const failing = relaunchLib.waitReady(ready, dead, 3000, 40);
    setTimeout(() => dead.emit('exit', 1), 60);
    const bad = await failing;
    assert.equal(bad.ok, false, 'окно не открылось — остаёмся жить');
    assert.match(bad.error, /закрылась \(код 1\)/);

    // 3. Процесс вообще не запустился — тоже ошибка, а не вечное ожидание
    const broken = new (require('node:events').EventEmitter)();
    const never = relaunchLib.waitReady(ready, broken, 3000, 40);
    setTimeout(() => broken.emit('error', new Error('ENOENT electron.exe')), 40);
    const err = await never;
    assert.equal(err.ok, false);
    assert.match(err.error, /ENOENT/);

    // 4. Молчание дольше срока — честный отказ по времени
    const silent = await relaunchLib.waitReady(ready, null, 200, 40);
    assert.equal(silent.ok, false);
    assert.match(silent.error, /не открыла окно за/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('перезапуск: файл «окно открылось» пишет только новая копия и только по просьбе', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sysai-rel-'));
  const home = path.join(root, 'SYSAI');
  const keep = process.env.SYSAI_RELAUNCH_READY;
  try {
    delete process.env.SYSAI_RELAUNCH_READY;
    assert.equal(relaunchLib.markReady(), false, 'обычный запуск ничего не пишет');

    const ready = relaunchLib.readyPath(home);
    process.env.SYSAI_RELAUNCH_READY = ready;
    assert.equal(relaunchLib.markReady(), true);
    assert.equal(process.env.SYSAI_RELAUNCH_READY, undefined, 'просьба одноразовая');
    const got = JSON.parse(fs.readFileSync(ready, 'utf8'));
    assert.equal(got.pid, process.pid);
    assert.equal(got.version, JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8')).version);
  } finally {
    if (keep === undefined) delete process.env.SYSAI_RELAUNCH_READY;
    else process.env.SYSAI_RELAUNCH_READY = keep;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------- соты: раскладка рядами
// Соты — правильные шестиугольники, сцепленные гранями: сколько влезает в ряд и как ряды ложатся друг под друга.
// Раскладка вынесена в renderer/js/combs.mjs, чтобы её можно было проверить без окна и браузера.
test('the cells of the swarm lie in rows: a full row, a shifted next one, nothing lost', async () => {
  const { perRow, rowsOf, isOdd } = await import('../renderer/js/combs.mjs');

  // сколько шестиугольников влезает в ширину: шаг ряда равен ширине соты, соседи не наползают друг на друга
  assert.equal(perRow(640, 46), 13, '640 px при соте 46 px — 13 в ряд');
  assert.equal(perRow(380, 46), 8, 'узкое окно 380 px — 8');
  assert.equal(perRow(46, 46), 1);
  assert.equal(perRow(10, 46), 1, 'уже одной соты — всё равно одна, а не ноль');
  assert.equal(perRow(0, 46), 1);
  assert.equal(perRow(undefined, 46), 1, 'ширины нет — одна сота, а не падение');

  // ряды подряд: каждая сота ровно в одном ряду, порядок не теряется
  assert.deepEqual(rowsOf(3, 2), [[0, 1], [2]]);
  assert.deepEqual(rowsOf(16, 8), [[0, 1, 2, 3, 4, 5, 6, 7], [8, 9, 10, 11, 12, 13, 14, 15]]);
  const all = rowsOf(40, 8).flat();
  assert.equal(all.length, 40, 'ни одна сота не потеряна');
  assert.deepEqual([...all].sort((a, b) => a - b), Array.from({ length: 40 }, (_, i) => i), 'и не задвоена');
  assert.deepEqual(rowsOf(0, 8), []);
  assert.deepEqual(rowsOf(2, 8), [[0, 1]], 'неполный ряд — просто короткий');
  assert.equal(rowsOf(5, 1).length, 5, 'по одной в ряд — пять рядов');

  // нечётный ряд сдвинут на полсоты: 0 — ровный, 1 — сдвинут, 2 — снова ровный
  assert.deepEqual([0, 1, 2, 3].map(isOdd), [false, true, false, true]);
});

test('the swarm grid is drawn as rows of hexagons, not a flat table', () => {
  const js = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'js', 'swarm.js'), 'utf8');
  assert.match(js, /from '\.\/combs\.mjs'/, 'окно берёт раскладку из combs.mjs, а не считает ряды само');
  assert.match(js, /rowsOf\(/, 'ряды считаются общей раскладкой');
  assert.match(js, /comb-row/, 'ряд — свой элемент: сдвиг нечётного ряда делается рядом, а не каждой сотой');
  const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'style.css'), 'utf8');
  // правильный шестиугольник: 46×53 (2/√3 ≈ 1.155) и ряд, поднятый на четверть высоты — грани сцеплены
  assert.match(css, /--cw:\s*46px;\s*--ch:\s*53px/, 'сота правильной формы, а не сжатая');
  assert.match(css, /\.comb-row\s*\+\s*\.comb-row\s*\{[^}]*margin-top:\s*calc\(var\(--ch\)\s*\*\s*-0\.25\)/,
    'следующий ряд поднимается на четверть соты: соседи касаются гранями');
  assert.match(css, /\.comb-row\.odd\s*\{[^}]*margin-left:\s*calc\(var\(--cw\)\s*\/\s*2\)/, 'нечётный ряд сдвинут на полсоты');
  assert.match(css, /\.comb-grid \.cell::before[^}]*clip-path/, 'рамка — кольцо того же контура, а не прямоугольник');
  // галочки в карточке вопроса — свои, как на сайте, а не стоковый квадрат браузера
  assert.match(css, /\.ask \.choice input\[type=checkbox\][^{]*\{[^}]*appearance:\s*none/, 'галочка вопроса своя');
  assert.match(css, /\.ask \.choice input\[type=checkbox\][^{]*\{[^}]*width:\s*19px/, 'и того же размера, что на сайте');
});

// ---------------------------------------------------------------- «документы после работы» и видимая ошибка плана
test('the documents are offered when the last stage the scope has is done', async () => {
  // «только ПЛК»: экранов в объёме нет — документы предлагаются после компиляции, а не после экранов, которых не будет.
  // Раньше окно ждало именно экранов, и в этом объёме предложение документов не появлялось никогда.
  const { workBeforeDocs } = await import('../renderer/js/decisions.mjs');
  const st = (rows) => rows.map(([stage, state, skipped]) => ({ stage, state, skipped: Boolean(skipped) }));
  const full = (over) => st([['materials', 'done'], ['decisions', 'done'], ['plc', 'done'], ['build', 'done'], ['hmi', 'done'], ['docs', 'todo'], ...(over || [])]);

  assert.equal(workBeforeDocs({ stages: full() }), true, 'полный проект: экраны готовы — можно документы');
  assert.equal(workBeforeDocs({ stages: full([['hmi', 'work']]) }), false, 'экраны идут — рано');

  const plcOnly = (build) => st([['materials', 'done'], ['decisions', 'done'], ['plc', 'done'], ['build', build], ['hmi', 'todo', 1], ['docs', 'todo']]);
  assert.equal(workBeforeDocs({ stages: plcOnly('done') }), true, '«только ПЛК»: компиляция готова — можно документы');
  assert.equal(workBeforeDocs({ stages: plcOnly('work') }), false, '«только ПЛК»: компиляция не готова — рано');

  const hmiOnly = (hmi) => st([['materials', 'done'], ['decisions', 'done'], ['plc', 'todo', 1], ['build', 'todo', 1], ['hmi', hmi], ['docs', 'todo']]);
  assert.equal(workBeforeDocs({ stages: hmiOnly('done') }), true, '«только экраны»: экраны готовы — можно документы');
  assert.equal(workBeforeDocs({ stages: hmiOnly('todo') }), false, '«только экраны»: экранов нет — рано');
  assert.equal(workBeforeDocs({ stages: [] }), false, 'этапов нет — предлагать нечего');
  assert.equal(workBeforeDocs(null), false, 'хода нет — не падаем');
});

test('a failure of the environment plan is shown, not swallowed', () => {
  // Один запрос apps:plan кормит три блока; если он упал, блоки оставались скрытыми и человек читал это как
  // «ничего не выбрано». Ошибка должна быть видна: тост и строка в блоке «Где и что будет».
  const js = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'js', 'projects.js'), 'utf8');
  assert.doesNotMatch(js, /apps\.plan\([^)]*\)\.catch\(\(\) => null\)/, 'ошибка плана не глушится в null');
  const start = js.indexOf('apps.plan(');
  const planCall = js.slice(start, js.indexOf('clear(this.el)', start));
  assert.match(planCall, /catch\(\(err\) =>/, 'у запроса есть обработчик ошибки');
  assert.match(planCall, /toast\(/, 'ошибка видна человеку тостом, а не только в консоли');
  assert.match(js, /plan\.error/, 'блок «Где и что будет» показывает причину, а не молчит');
  // Блоки, что читают план, обязаны проверить plan.error раньше, чем превращать его в пустое окружение:
  // иначе дерево рисует все строки как «не выбран», а карточка компиляции врёт, что файла проекта нет.
  // Guard обязан стоять до вызова decisions()/compileCard(): ниже — он уже ничего не защищает.
  for (const [block, use] of [['_decisions(info, planned)', 'decisions('], ['_compile(info, planned)', 'compileCard(']]) {
    const at = js.indexOf(block);
    assert.ok(at >= 0, block);
    const body = js.slice(at, js.indexOf('return box;', at));
    assert.match(body, /plan\.error\) return/, `${block}: при ошибке плана блок не рисуется по пустому окружению`);
    // the first use inside the method body: not the method's own name in the slice (it holds 'decisions(' too)
    const useAt = body.indexOf(use, block.length);
    assert.ok(useAt >= 0, `${block}: блок строится через ${use}`);
    assert.ok(body.indexOf('plan.error) return') < useAt,
      `${block}: проверка ошибки стоит раньше, чем блок строится по окружению`);
  }
});
