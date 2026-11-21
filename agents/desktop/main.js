'use strict';
// «Агент SYSAI» — the main process. One window; Hermes behind it as ACP over stdio (`hermes -p <profile> acp`);
// the page asks for everything through preload.js, and the agent shows files here through the bridge (127.0.0.1)
// of lib/uimcp.js, the MCP server this window gives every session (run by this electron.exe as plain Node,
// ELECTRON_RUN_AS_NODE=1); on «Холст» (lib/canvas.js) the person and the agent edit a window of an EasyBuilder Pro
// project together. Started by the synchronizer's launcher
// (`sysai-agentw desktop` → `electron.exe <this folder>`); `--sysai-shortcuts` only writes the shortcuts and leaves.
const { app, BrowserWindow, Menu, clipboard, dialog, ipcMain, nativeTheme, screen, session, shell } = require('electron');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { pathToFileURL } = require('node:url');

const { AcpClient } = require('./lib/acp');
const { Canvas, PERSON } = require('./lib/canvas');
const config = require('./lib/config');
const chats = require('./lib/chats');
const files = require('./lib/files');
const storage = require('./lib/storage');
const convExport = require('./lib/export');
const { storageFiles } = require('./lib/projects');
const { Projects, prompts: projectPrompts, STAGES: projectStages } = require('./lib/projects');
const shortcuts = require('./lib/shortcuts');
const { Bridge } = require('./lib/bridge');
const rules = require('./lib/rules');
const { AutoApprove } = require('./lib/autoapprove');
const apps = require('./lib/apps');
const updates = require('./lib/updates');
const { Swarm, allowedOf: swarmAllowedOf, offText: swarmOffText } = require('./lib/swarm');
const relaunch = require('./lib/relaunch');
const pkg = require('./package.json');

const VERSION = pkg.version;
const WIN = process.platform === 'win32';
const HOME = config.sysaiHome(__dirname);
const DESK = path.join(HOME, 'desktop');
const LOG = path.join(HOME, 'logs', 'desktop.log');
const INDEX = path.join(__dirname, 'renderer', 'index.html');
const INSTALL = process.argv.includes('--sysai-shortcuts');
const BG = '#121210';
const SYMBOLS = '#c9c6bb';
const TOP = 40;
const SESSION_METHODS = new Set(['session/new', 'session/load', 'session/resume', 'session/fork']);
const METHODS = new Set([...SESSION_METHODS, 'session/list', 'session/prompt', 'session/set_config_option', 'session/set_mode',
  'session/set_model']);
const NOTIFICATIONS = new Set(['session/cancel']);
const PASTE_LIMIT = 30 * 1024 * 1024;

let win = null;
let uiReady = false;
let quitting = false;
let bridge = null;
let canvas = null;
let acp = null;
let generation = 0;
let restarts = [];
let acpInfo = { state: 'starting', message: '', generation: 0 };
let auto = null; // «правки без вопросов» (lib/autoapprove.js)
const sessionCwd = new Map(); // sessionId → the folder the session was opened in
const pending = new Map(); // questions of the agent waiting for the person: id → {id, method, params}
const queue = []; // what waits for the page: [channel, payload]
const swarmText = new Map(); // sessionId → the text this swarm cell has produced
let swarm = null; // the swarm running now (lib/swarm.js), one at a time: it eats the whole machine
let relaunchLast = null; // чем кончился последний перезапуск — для «Ещё» и app:info
let swarmBeat = 0;
let swarmCwd = ''; // папка роя: проект, по которому проверяют, — агент должен её видеть

// ------------------------------------------------------------------------------------------------- the journal
function log(...parts) {
  const text = parts.map((p) => (p instanceof Error ? p.stack || p.message : typeof p === 'string' ? p : JSON.stringify(p))).join(' ');
  try {
    fs.mkdirSync(path.dirname(LOG), { recursive: true });
    try {
      if (fs.statSync(LOG).size > 2 * 1024 * 1024) fs.renameSync(LOG, `${LOG}.1`);
    } catch { /* no journal yet */ }
    fs.appendFileSync(LOG, `${new Date().toISOString()} ${text}\n`, 'utf8');
  } catch { /* the journal must not stop the window */ }
}

function tail(text, n = 4000) {
  const s = String(text || '').trim();
  return s.length > n ? `…${s.slice(-n)}` : s;
}

app.setName('Агент SYSAI');
if (WIN) app.setAppUserModelId(shortcuts.AUMID);
app.setPath('userData', path.join(DESK, INSTALL ? 'data-install' : 'data'));

// ------------------------------------------------------------------------------------------------- shortcuts
function installShortcuts() {
  if (!WIN) return { done: [], note: 'ярлыки пишутся только в Windows' };
  const cfg = config.load(HOME);
  const ico = fs.existsSync(path.join(DESK, 'sysai.ico')) ? path.join(DESK, 'sysai.ico') : path.join(__dirname, 'assets', 'icon.ico');
  const spec = cfg.launcher && fs.existsSync(cfg.launcher)
    ? { target: cfg.launcher, args: 'desktop', icon: ico, cwd: HOME }
    : { target: process.execPath, args: `"${__dirname}"`, icon: ico, cwd: path.dirname(process.execPath) };
  const out = shortcuts.write(shell, (k) => app.getPath(k), spec);
  return { ...out, target: spec.target, args: spec.args };
}

if (INSTALL) {
  app.whenReady().then(() => {
    let code = 0;
    let result;
    try {
      result = installShortcuts();
      if (WIN && !result.done.includes('desktop')) code = 1;
    } catch (err) {
      code = 1;
      result = { error: err.message };
    }
    try {
      config.writeJson(path.join(DESK, 'shortcuts.json'), { ...result, version: VERSION, at: new Date().toISOString() });
    } catch {
      code = 1;
    }
    log('ярлыки:', result);
    app.exit(code);
  });
} else if (!app.requestSingleInstanceLock({ files: fileArgs(process.argv) })) {
  app.quit();
} else {
  app.on('second-instance', (_e, argv, _cwd, extra) => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
    const list = extra && Array.isArray(extra.files) ? extra.files : fileArgs(argv);
    for (const f of list) showInWindow({ path: f, line: 0 });
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => {
    quitting = true;
    if (bridge) bridge.stop();
    if (canvas) canvas.stop();
    if (acp) acp.stop();
  });
  app.whenReady().then(start).catch((err) => {
    log('окно не запустилось:', err);
    dialog.showErrorBox('Агент SYSAI', `Окно не запустилось: ${err.message}`);
    app.exit(1);
  });
}

function fileArgs(argv) {
  const self = path.resolve(__dirname).toLowerCase();
  return (argv || []).slice(1).filter((a) => {
    if (!a || a.startsWith('-') || !path.isAbsolute(a)) return false;
    const full = path.resolve(a).toLowerCase();
    return full !== self && full !== path.resolve(process.execPath).toLowerCase() && fs.existsSync(a);
  });
}

// ------------------------------------------------------------------------------------------------- the window
async function start() {
  log(`окно ${VERSION}, Electron ${process.versions.electron}, папка SYSAI ${HOME}`);
  nativeTheme.themeSource = 'dark';
  Menu.setApplicationMenu(null);
  guard(session.defaultSession);
  startCanvas();
  startAuto();
  ipc();
  createWindow();
  cleanPaste();
  await startBridge();
  startAcp();
  for (const f of fileArgs(process.argv)) showInWindow({ path: f, line: 0 });
}

// The page loads nothing from the network and asks for nothing but the clipboard and full screen (video).
function guard(ses) {
  ses.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'] }, (_d, cb) => cb({ cancel: true }));
  const allowed = new Set(['clipboard-sanitized-write', 'fullscreen']);
  ses.setPermissionRequestHandler((_wc, permission, cb) => cb(allowed.has(permission)));
  ses.setPermissionCheckHandler((_wc, permission) => allowed.has(permission));
}

function visible(b) {
  if (!b || ![b.x, b.y, b.width, b.height].every(Number.isFinite)) return false;
  return screen.getAllDisplays().some(({ workArea: a }) => b.x < a.x + a.width - 120 && b.x + b.width > a.x + 120 &&
    b.y >= a.y - 20 && b.y < a.y + a.height - 120);
}

function createWindow() {
  const saved = config.readJson(path.join(DESK, 'window.json')) || {};
  const bounds = visible(saved) ? { x: saved.x, y: saved.y, width: saved.width, height: saved.height } : { width: 1320, height: 860 };
  win = new BrowserWindow({
    ...bounds, minWidth: 900, minHeight: 560, show: false, backgroundColor: BG, title: 'Агент SYSAI',
    icon: path.join(__dirname, 'assets', WIN ? 'icon.ico' : 'icon.png'),
    titleBarStyle: 'hidden', titleBarOverlay: { color: BG, symbolColor: SYMBOLS, height: TOP },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false,
      plugins: true, spellcheck: WIN, webviewTag: false, navigateOnDragDrop: false,
    },
  });
  if (saved.maximized) win.maximize();
  const wc = win.webContents;
  win.once('ready-to-show', () => {
    win.show();
    // Открылись вместо прежней копии — говорим ей об этом: она ждёт файл и только потом закрывается.
    if (relaunch.markReady()) log(`окно открылось вместо прежнего (pid ${process.pid}, ${VERSION})`);
  });
  win.on('focus', () => win.flashFrame(false));
  win.on('close', () => {
    try {
      config.writeJson(path.join(DESK, 'window.json'), { ...win.getNormalBounds(), maximized: win.isMaximized(), zoom: wc.getZoomLevel() });
    } catch { /* not kept */ }
  });
  win.on('closed', () => { win = null; uiReady = false; });
  // Установлена другая сборка, а работаем в этой — говорим сразу при открытии: иначе человек живёт на старом окне и
  // не понимает, почему «обновилось, а ничего не изменилось».
  const newer = relaunch.next(HOME, app.getAppPath());
  if (newer) {
    sendOrQueue('ui:notice', { kind: 'update',
      text: `Установлено окно агента ${newer.version || 'новой сборки'}, а открыто ${VERSION}. Перезапустите, чтобы работать в новой.` });
  }
  wc.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  const index = pathToFileURL(INDEX).href;
  wc.on('will-navigate', (e, url) => {
    if (url.split('#')[0] === index) return;
    e.preventDefault();
    if (/^https?:/i.test(url)) shell.openExternal(url);
  });
  wc.on('will-attach-webview', (e) => e.preventDefault());
  wc.on('before-input-event', (e, input) => keys(wc, e, input));
  wc.on('context-menu', (_e, p) => contextMenu(p));
  wc.on('did-start-loading', () => { uiReady = false; });
  wc.on('did-finish-load', () => { if (Number.isFinite(saved.zoom)) wc.setZoomLevel(saved.zoom); });
  wc.on('render-process-gone', (_e, d) => log('страница окна упала:', d.reason));
  wc.on('console-message', (e) => {
    if (e.level === 'error') log('страница:', e.message, e.sourceId ? `${path.basename(e.sourceId)}:${e.lineNumber}` : '');
  });
  win.loadFile(INDEX);
}

function keys(wc, e, input) {
  if (input.type !== 'keyDown') return;
  const ctrl = input.control || input.meta;
  const key = String(input.key || '').toLowerCase();
  if (input.key === 'F12' || (ctrl && input.shift && key === 'i')) {
    wc.toggleDevTools();
    e.preventDefault();
  } else if (ctrl && (input.key === '=' || input.key === '+')) {
    wc.setZoomLevel(Math.min(4, wc.getZoomLevel() + 0.5));
    e.preventDefault();
  } else if (ctrl && input.key === '-') {
    wc.setZoomLevel(Math.max(-3, wc.getZoomLevel() - 0.5));
    e.preventDefault();
  } else if (ctrl && input.key === '0') {
    wc.setZoomLevel(0);
    e.preventDefault();
  } else if (ctrl && input.shift && key === 'r') {
    wc.reloadIgnoringCache();
    e.preventDefault();
  } else if (ctrl && input.shift && (key === 'a' || key === 'ф')) {
    send('auto:toggle', {}); // the page asks before the first switching on
    e.preventDefault();
  }
}

function contextMenu(p) {
  const items = [];
  if (p.misspelledWord && p.dictionarySuggestions && p.dictionarySuggestions.length) {
    for (const s of p.dictionarySuggestions.slice(0, 5)) items.push({ label: s, click: () => win && win.webContents.replaceMisspelling(s) });
    items.push({ type: 'separator' });
  }
  if (p.isEditable) {
    items.push({ label: 'Вырезать', role: 'cut', enabled: p.editFlags.canCut }, { label: 'Копировать', role: 'copy', enabled: p.editFlags.canCopy },
      { label: 'Вставить', role: 'paste', enabled: p.editFlags.canPaste }, { type: 'separator' }, { label: 'Выделить всё', role: 'selectAll' });
  } else if (p.selectionText && p.selectionText.trim()) {
    items.push({ label: 'Копировать', role: 'copy' }, { label: 'Выделить всё', role: 'selectAll' });
  }
  if (p.linkURL && /^https?:/i.test(p.linkURL)) {
    if (items.length) items.push({ type: 'separator' });
    items.push({ label: 'Открыть ссылку в браузере', click: () => shell.openExternal(p.linkURL) },
      { label: 'Копировать ссылку', click: () => clipboard.writeText(p.linkURL) });
  }
  if (items.length && win) Menu.buildFromTemplate(items).popup({ window: win });
}

function send(channel, payload) {
  if (win && !win.isDestroyed() && uiReady) win.webContents.send(channel, payload);
}

function sendOrQueue(channel, payload) {
  if (win && !win.isDestroyed() && uiReady) win.webContents.send(channel, payload);
  else queue.push([channel, payload]);
}

// The build the synchronizer installed last (desktop.json): newer than this window means «перезапустите».
function installedVersion() {
  try {
    return String(JSON.parse(fs.readFileSync(path.join(DESK, 'desktop.json'), 'utf8')).version || '');
  } catch {
    return '';
  }
}

function showInWindow(target) {
  sendOrQueue('ui:show', target);
  if (win && win.isMinimized()) win.flashFrame(true);
}

function attention() {
  if (win && !win.isFocused()) win.flashFrame(true);
}

// ------------------------------------------------------------------------------------------------- Hermes
function which(names, extra = []) {
  const dirs = [...(process.env.PATH || '').split(path.delimiter), ...extra].filter(Boolean);
  for (const d of dirs) {
    for (const n of names) {
      const p = path.join(d, n);
      try {
        if (fs.statSync(p).isFile()) return p;
      } catch { /* not here */ }
    }
  }
  return '';
}

function hermesPath(cfg) {
  if (cfg.hermes && fs.existsSync(cfg.hermes)) return cfg.hermes;
  const local = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  return WIN ? which(['hermes.exe', 'hermes.cmd', 'hermes.bat'], [path.join(local, 'hermes', 'bin'), path.join(os.homedir(), '.local', 'bin')])
    : which(['hermes'], [path.join(os.homedir(), '.local', 'bin'), '/usr/local/bin']);
}

// The MCP server of this window for every session (lib/uimcp.js under this very executable as Node): the agent
// shows files here, opens them in their programs and starts programs — the bridge below checks each call by the
// signed rules first.
function windowServer() {
  return { name: 'window', command: process.execPath, args: [path.join(__dirname, 'lib', 'uimcp.js')],
    env: [{ name: 'ELECTRON_RUN_AS_NODE', value: '1' }, { name: 'SYSAI_HOME', value: HOME }] };
}

function setState(patch) {
  acpInfo = { ...acpInfo, ...patch };
  send('acp:state', acpInfo);
}

function explain(stderr, fallback, profile) {
  const s = String(stderr || '');
  if (/invalid choice: '?acp|No module named '?acp|acp.{0,40}(not installed|extra)|agent[-_]client[-_]protocol/i.test(s)) {
    return 'В этой установке Hermes нет режима ACP — он нужен окну. Обновите Hermes (в PowerShell: hermes update) и нажмите «Перезапустить».';
  }
  if (/profile.{0,80}(not found|does not exist|doesn't exist)|no such profile|unknown profile/i.test(s)) {
    return `Профиль Hermes «${profile}» не найден. Запустите установщик SYSAI со страницы агента снова — он создаст профиль.`;
  }
  const last = s.trim().split(/\r?\n/).filter((l) => l.trim()).slice(-1)[0];
  return last ? `${fallback}: ${last.slice(0, 400)}` : fallback;
}

function startAcp() {
  const cfg = config.load(HOME);
  const gen = ++generation;
  if (!cfg.connected) {
    setState({ state: 'error', generation: gen, message: 'Компьютер не подключён к SYSAI: запустите установщик со страницы агента.', stderr: '' });
    return;
  }
  const hermes = hermesPath(cfg);
  if (!hermes) {
    setState({ state: 'missing', generation: gen, stderr: '',
      message: 'Hermes не найден на этом компьютере. Запустите установщик SYSAI со страницы агента снова — он поставит Hermes.' });
    return;
  }
  const profile = cfg.profile || cfg.agent;
  const args = profile && profile !== 'default' ? ['-p', profile, 'acp'] : ['acp'];
  const env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', SYSAI_HOME: HOME };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.NODE_OPTIONS;
  const client = new AcpClient({ command: hermes, args, env, cwd: os.homedir() });
  acp = client;
  setState({ state: 'starting', generation: gen, message: '', stderr: '', agentInfo: null });
  log(`запускаю ${hermes} ${args.join(' ')}`);
  client.on('notification', (n) => {
    // Рой слушает свои сессии отдельно: его ответы не должны попадать в открытый разговор
    const sid = n && n.params && n.params.sessionId;
    if (sid && swarmText.has(sid)) {
      const u = (n.params && n.params.update) || {};
      if (u.sessionUpdate === 'agent_message_chunk' && u.content && u.content.type === 'text') {
        swarmText.set(sid, (swarmText.get(sid) || '') + u.content.text);
      }
      return;
    }
    if (acp === client) send('acp:notification', n);
  });
  client.on('request', (r) => onAgentRequest(client, r));
  client.on('noise', (line) => log('Hermes (stdout):', line.slice(0, 300)));
  client.on('exit', (info) => onAcpExit(client, info, profile));
  client.start();
  client.initialize({ name: 'sysai-desktop', title: 'Агент SYSAI', version: VERSION }).then((res) => {
    if (acp !== client) return;
    const info = (res && res.agentInfo) || null;
    log('Hermes готов:', info || {});
    setState({ state: 'ready', message: '', stderr: '', agentInfo: info, capabilities: (res && res.agentCapabilities) || {} });
  }).catch((err) => {
    if (acp !== client) return;
    log('Hermes не ответил на initialize:', err.message, tail(client.stderr, 1500));
    setState({ state: 'error', message: explain(client.stderr, `Hermes не запустился: ${err.message}`, profile), stderr: tail(client.stderr) });
    client.stop();
  });
}

function onAcpExit(client, info, profile) {
  if (acp !== client) return;
  acp = null;
  for (const id of pending.keys()) send('acp:request-gone', { id, why: 'Агент перезапускается — вопрос снят' });
  pending.clear();
  const missing = /ENOENT/.test(info.error || '');
  const said = missing ? 'Hermes не найден' : info.error ? `Hermes не запустился: ${info.error}` : `Hermes остановился (код ${info.code ?? info.signal})`;
  const message = explain(info.stderr, said, profile);
  log('Hermes остановился:', message, tail(info.stderr, 2000));
  if (quitting) return;
  if (missing) {
    setState({ state: 'missing', message: 'Hermes не найден на этом компьютере. Запустите установщик SYSAI со страницы агента снова.', stderr: '' });
    return;
  }
  const now = Date.now();
  restarts = restarts.filter((t) => now - t < 120000);
  const wasReady = acpInfo.state === 'ready';
  if (restarts.length < 3 && (wasReady || !restarts.length)) {
    restarts.push(now);
    setState({ state: 'starting', message: 'Агент остановился — запускаю снова…' });
    setTimeout(() => { if (!acp && !quitting) startAcp(); }, [1000, 3000, 8000][restarts.length - 1]);
    return;
  }
  setState({ state: wasReady ? 'stopped' : 'error', message, stderr: tail(info.stderr) });
}

function restartAcp() {
  restarts = [];
  const old = acp;
  acp = null;
  for (const id of pending.keys()) send('acp:request-gone', { id, why: 'Агент перезапускается — вопрос снят' });
  pending.clear();
  if (old) old.stop();
  startAcp();
  return true;
}

// «Правки без вопросов»: an edit inside a project folder is allowed by the window itself; the rest is asked.
function projectsRoot() {
  return path.join(app.getPath('documents'), 'SYSAI Проекты');
}

function startAuto() {
  auto = new AutoApprove({
    file: path.join(DESK, 'autoapprove.json'), root: projectsRoot(), audit: path.join(DESK, 'audit'), log,
    loadRules: () => rules.load(HOME), pathAllowed: rules.pathAllowed,
    guarded: (p) => rules.guardedPath(p, HOME, config.load(HOME).profileDir),
  });
  auto.clean();
}

function autoAnswer(client, id, params) {
  const decision = auto.decide(params, sessionCwd.get(params && params.sessionId) || os.homedir());
  if (!decision.auto) return decision.why;
  if (!auto.allow(params, decision)) return 'не удалось сохранить копию файла перед правкой';
  try {
    client.respond(id, { outcome: { outcome: 'selected', optionId: decision.optionId } });
  } catch (err) {
    log('правки без вопросов: ответ агенту не ушёл:', err.message);
    return 'ответ агенту не ушёл';
  }
  send('auto:state', auto.state());
  return null;
}

function onAgentRequest(client, { id, method, params }) {
  if (acp !== client) return;
  if (method === 'session/request_permission' || method === 'elicitation/create') {
    if (method === 'session/request_permission') {
      const why = autoAnswer(client, id, params || {});
      if (why === null) return; // answered here
      if (why) params = { ...params, _auto: why }; // the switch is on, but this one is asked: the card says why
    }
    pending.set(id, { id, method, params });
    send('acp:request', { id, method, params });
    attention();
    return;
  }
  try {
    client.respondError(id, -32601, `окно SYSAI не выполняет ${method}`);
  } catch { /* gone */ }
}

async function acpRequest(method, params) {
  if (!METHODS.has(method)) throw new Error(`окно не отправляет агенту ${method}`);
  const client = acp;
  if (!client || acpInfo.state !== 'ready') throw new Error('агент ещё не запущен');
  const p = params && typeof params === 'object' ? { ...params } : {};
  if (!SESSION_METHODS.has(method)) return client.request(method, p, { timeout: method === 'session/prompt' ? 0 : 120000 });
  if (!p.cwd) p.cwd = os.homedir();
  if (!Array.isArray(p.mcpServers)) {
    const server = windowServer();
    p.mcpServers = server ? [server] : [];
  }
  const keep = (res) => {
    const sid = (res && res.sessionId) || p.sessionId;
    if (sid) sessionCwd.set(sid, p.cwd);
    return res;
  };
  try {
    return keep(await client.request(method, p, { timeout: 300000 }));
  } catch (err) {
    if (!p.mcpServers.length || !/mcp/i.test(`${err.message} ${JSON.stringify(err.data || '')}`)) throw err;
    log(`${method}: Hermes не принял MCP окна (${err.message}) — без него`);
    return keep(await client.request(method, { ...p, mcpServers: [] }, { timeout: 300000 }));
  }
}

// ------------------------------------------------------------------------------------------------- the canvas
// «Холст»: окно проекта EasyBuilder Pro, которое человек и агент правят вместе (lib/canvas.js); агент — через мост
// ниже, по подписанным доступам: смотреть — нужно чтение папки проекта, править — запись.
function startCanvas() {
  canvas = new Canvas({ home: HOME, cache: path.join(DESK, 'cache', 'canvas'), log });
  canvas.on('state', (s) => send('canvas:state', s));
  canvas.on('cursors', (c) => send('canvas:cursors', c));
  canvas.on('edited', (e) => send('canvas:edited', e));
  canvas.on('notice', (n) => send('ui:notice', n));
}

function agentRead(p) {
  if (!p) throw new Error('на холсте нет проекта — откройте его (canvas_open)');
  agentTarget({ path: p }, 'show');
}

function agentWrite(p) {
  agentRead(p);
  const got = rules.load(HOME);
  if (!got.rules) throw new Error(`SYSAI: ${got.error}`);
  const why = !rules.pathAllowed(got.rules, p, true) ? `папка ${p.slice(0, 160)} агенту не разрешена на запись (SYSAI → Доступы → Папки)`
    : rules.guardedPath(p, HOME, config.load(HOME).profileDir) ? rules.SETTINGS_NOTE : '';
  if (why) {
    log(`агенту отказано (правка на холсте): ${why}`);
    throw new Error(`SYSAI: ${why}`);
  }
}

function showCanvas() {
  sendOrQueue('canvas:focus', {});
  if (win && win.isMinimized()) win.flashFrame(true);
}

// EasyBuilder Pro: программа, которой открывается .cmtp на этом компьютере; если такой нет — та, что человек указал
// (desktop/easybuilder.json), или из обычных мест установки.
function ebCandidates() {
  const pf = [process.env['ProgramFiles(x86)'], process.env.ProgramFiles, 'C:\\Program Files (x86)', 'C:\\Program Files'].filter(Boolean);
  const out = ['C:\\EBpro\\EasyBuilderPro.exe'];
  for (const d of pf) out.push(path.join(d, 'EBpro', 'EasyBuilderPro.exe'), path.join(d, 'Weintek', 'EasyBuilder Pro', 'EasyBuilderPro.exe'));
  return out;
}

async function openInEasyBuilder(file) {
  if (!file) throw new Error('на холсте нет проекта');
  const keep = path.join(DESK, 'easybuilder.json');
  const saved = (config.readJson(keep) || {}).exe;
  const start = (exe) => {
    const child = spawn(exe, [file], { cwd: path.dirname(exe), detached: true, stdio: 'ignore', windowsHide: false });
    child.on('error', (err) => log('EasyBuilder:', err.message));
    child.unref();
    log(`проект открыт в EasyBuilder: ${exe} ${file}`);
    return { exe };
  };
  if (saved && fs.existsSync(saved)) return start(saved);
  const err = await shell.openPath(file);
  if (!err) return { exe: '' };
  log('EasyBuilder через ассоциацию файла:', err);
  const found = ebCandidates().find((p) => fs.existsSync(p));
  if (found) return start(found);
  const res = await dialog.showOpenDialog(win, { title: 'Где EasyBuilder Pro? Укажите EasyBuilderPro.exe', properties: ['openFile'],
    filters: [{ name: 'Программа', extensions: ['exe'] }] });
  if (res.canceled || !res.filePaths.length) throw new Error('EasyBuilder Pro не найден на этом компьютере');
  config.writeJson(keep, { exe: res.filePaths[0] });
  return start(res.filePaths[0]);
}

// --------------------------------------------------------------------------- «Открыть в программе»: все форматы
// Кнопки с иконками форматов (панель проекта) открывают среду вендора на этом компьютере и сразу кладут в неё наш
// результат. Файл проекта (ONI, EasyBuilder, Machine Expert) отдаём среде файлом — она его подхватит сама.
// У Haiwell Happy файла нет (закрытый .gpc), поэтому программа едет буфером обмена, а в окне остаются три клавиши.
async function appExe(id) {
  const f = apps.format(id);
  const keep = path.join(DESK, `${id}.json`);
  const saved = (config.readJson(keep) || {}).exe;
  if (saved && fs.existsSync(saved)) return saved;
  const found = apps.candidates(id).find((p) => fs.existsSync(p));
  if (found) return found;
  const res = await dialog.showOpenDialog(win, {
    title: `Где ${f.title}? Укажите ${f.exe[0]}`, properties: ['openFile'],
    filters: [{ name: 'Программа', extensions: ['exe'] }],
  });
  if (res.canceled || !res.filePaths.length) throw new Error(`${f.title} не найден на этом компьютере`);
  config.writeJson(keep, { ...(config.readJson(keep) || {}), exe: res.filePaths[0] });
  return res.filePaths[0];
}

function startApp(exe, args) {
  const child = spawn(exe, args, { cwd: path.dirname(exe), detached: true, stdio: 'ignore', windowsHide: false });
  child.on('error', (err) => log(`${path.basename(exe)}:`, err.message));
  child.unref();
}

// Haiwell Happy: программы-файла не существует, поэтому текст едет буфером, а вставку делает человек (F7 → Ctrl+V
// → F9) или, если в desktop/hawell.json включён autoPaste, — сценарий SendKeys. По умолчанию выключено: SendKeys
// бьёт по тому окну, что на переднем плане, и на чужой машине это лотерея.
async function pasteIntoApp(id, file) {
  const f = apps.format(id);
  const text = fs.readFileSync(file, 'utf8');
  clipboard.writeText(text);
  const cfg = config.readJson(path.join(DESK, `${id}.json`)) || {};
  const exe = await appExe(id);
  if (cfg.autoPaste && WIN) {
    const script = path.join(DESK, 'cache', `${id}-paste.ps1`);
    fs.mkdirSync(path.dirname(script), { recursive: true });
    fs.writeFileSync(script, apps.pasteScript(exe, { wait: cfg.wait || 6 }), 'utf8');
    const child = spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script],
      { cwd: path.dirname(script), detached: true, stdio: 'ignore', windowsHide: true });
    child.on('error', (err) => log('вставка в Haiwell:', err.message));
    child.unref();
    log(`${f.title}: открыт, вставка отправлена (${script})`);
    return { id, exe, file, clipboard: true, auto: true, len: text.length };
  }
  startApp(exe, []);
  log(`${f.title}: ${text.length} симв. в буфере, среда открыта`);
  return { id, exe, file, clipboard: true, auto: false, len: text.length };
}

async function openInApp(id, file) {
  const f = apps.format(id);
  if (typeof file !== 'string' || !file.trim()) throw new Error(`не указан файл для ${f.title}`);
  const p = path.resolve(file.trim());
  if (!fs.existsSync(p)) throw new Error(`нет такого файла: ${p}`);
  if (f.open === 'paste') return pasteIntoApp(id, p);
  const saved = (config.readJson(path.join(DESK, `${id}.json`)) || {}).exe;
  if (saved && fs.existsSync(saved)) {
    startApp(saved, [p]);
    log(`${f.title}: ${p} → ${saved}`);
    return { id, exe: saved, file: p, clipboard: false, auto: true };
  }
  const viaShell = await shell.openPath(p);        // ассоциация Windows — самый короткий и самый привычный путь
  if (!viaShell) {
    log(`${f.title}: ${p} открыт по ассоциации`);
    return { id, exe: '', file: p, clipboard: false, auto: true };
  }
  log(`${f.title} по ассоциации не вышло:`, viaShell);
  const exe = await appExe(id);
  startApp(exe, [p]);
  return { id, exe, file: p, clipboard: false, auto: true };
}

// ------------------------------------------------------------------------------------------------- the bridge
// What the agent asks of the window (lib/uimcp.js): checked by the signed rules as the synchronizer checks them —
// showing needs reading, opening also stays out of SYSAI's places, a program needs «Команды на ПК».
function agentTarget(body, action) {
  const p = rules.fullPath(typeof body.path === 'string' ? body.path : '');
  if (!p) throw new Error('нужен путь к файлу');
  const got = rules.load(HOME);
  const why = got.rules ? rules.check(got.rules, action, p, { home: HOME, profileDir: config.load(HOME).profileDir }) : got.error;
  if (why) {
    log(`агенту отказано (${action}): ${why}`);
    throw new Error(`SYSAI: ${why}`);
  }
  let st;
  try {
    st = fs.statSync(p);
  } catch {
    throw new Error(`нет такого файла: ${p}`);
  }
  return { p, dir: st.isDirectory() };
}

async function startBridge() {
  bridge = new Bridge({
    file: path.join(DESK, 'bridge.json'), version: VERSION, info: { app: __dirname, exe: process.execPath },
    handlers: {
      show: async (body) => {
        const { p, dir } = agentTarget(body, 'show');
        showInWindow({ path: p, line: Number(body.line) || 0 });
        return { shown: true, path: p, folder: dir };
      },
      open: async (body) => {
        const { p, dir } = agentTarget(body, 'open');
        const started = !dir && files.runnable(p);
        if (started) {
          await run(p);
          sendOrQueue('ui:notice', { text: `Агент запустил ${path.basename(p)}`, kind: 'info' });
        } else {
          const err = await shell.openPath(p);
          if (err) throw new Error(err);
        }
        log(`агент ${started ? 'запустил' : 'открыл'} ${p}`);
        return { opened: true, started, path: p };
      },
      reveal: async (body) => {
        const { p } = agentTarget(body, 'reveal');
        shell.showItemInFolder(p);
        return { shown: true, path: p };
      },
      canvas_open: async (body) => {
        const { p, dir } = agentTarget(body, 'show');
        if (dir) throw new Error('на холсте открывается файл проекта .cmtp/.emtp, а не папка');
        const out = await canvas.open(p, body.window);
        showCanvas();
        log(`агент открыл на холсте ${p}, окно ${out.window.number}`);
        return out;
      },
      canvas_look: async () => {
        if (canvas.file) agentRead(canvas.file);
        return canvas.look();
      },
      canvas_window: async (body) => {
        agentRead(canvas.file);
        const out = await canvas.show(body.window);
        showCanvas();
        return out;
      },
      canvas_cursor: async (body) => {
        agentRead(canvas.file);
        return { cursor: canvas.cursor(body.who, body) };
      },
      canvas_edit: async (body) => {
        agentWrite(canvas.file);
        if (canvas.who(body.who) === PERSON) throw new Error('правки от имени человека агент не делает — назовитесь «Агент» или своим именем');
        const out = await canvas.edit(body.who, body.edits);
        showCanvas();
        return out;
      },
      notice: async (body) => {
        const kind = ['err', 'warn', 'ok', 'update'].includes(body.kind) ? body.kind : 'info';
        sendOrQueue('ui:notice', { text: String(body.text || '').slice(0, 600), kind });
        return {};
      },
    },
  });
  try {
    await bridge.start();
    log(`мост для агента: 127.0.0.1:${bridge.port}`);
  } catch (err) {
    log('мост для агента не поднялся:', err.message);
  }
}

// ------------------------------------------------------------------------------------------------- the page's requests
function need(p) {
  if (typeof p !== 'string' || !p.trim()) throw new Error('нет пути');
  return path.resolve(p.trim());
}

async function run(p) {
  if (!files.runnable(p)) throw new Error('это не программа — откройте файл в программе');
  if (WIN && files.ext(p) === 'ps1') {
    // as «Выполнить с помощью PowerShell» does, under the computer's own execution policy (nothing is bypassed)
    const child = spawn('powershell.exe', ['-NoExit', '-File', p], { cwd: path.dirname(p), detached: true, stdio: 'ignore', windowsHide: false });
    child.on('error', (err) => log('powershell:', err.message));
    child.unref();
    return true;
  }
  const err = await shell.openPath(p);
  if (err) throw new Error(err);
  return true;
}

function cleanPaste() {
  const dir = path.join(DESK, 'cache', 'paste');
  fsp.readdir(dir).then((names) => Promise.all(names.map(async (n) => {
    const p = path.join(dir, n);
    const st = await fsp.stat(p);
    if (Date.now() - st.mtimeMs > 7 * 86400000) await fsp.unlink(p);
  }))).catch(() => {});
}

// Один запрос к SYSAI из главного процесса: {status, body}. Тело — JSON или текст; сеть здесь, потому что странице
// она запрещена (CSP), и потому что токен агента не должен попадать в renderer.
function netJson(url, headers = {}, timeout = 15000) {
  return new Promise((resolve, reject) => {
    const lib = String(url).startsWith('https:') ? require('node:https') : require('node:http');
    const req = lib.get(String(url), { headers, timeout }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let body = null;
        try {
          body = JSON.parse(text);
        } catch {
          body = text;
        }
        resolve({ status: res.statusCode || 0, body });
      });
    });
    req.on('timeout', () => req.destroy(new Error('истекло время ожидания')));
    req.on('error', reject);
  });
}

// Одна сота — одна сессия Hermes: ответы не смешиваются с открытым разговором, а состояние каждой соты считается
// отдельно. Рой не правит проекты: задание прямо запрещает запись.
async function askSwarm({ prompt }) {
  const client = acp;
  if (!client || acpInfo.state !== 'ready') throw new Error('агент ещё не запущен: дождитесь Hermes');
  const base = { cwd: swarmCwd || os.homedir(), mcpServers: windowServer() ? [windowServer()] : [] };
  let res;
  try {
    res = await client.request('session/new', base, { timeout: 120000 });
  } catch (err) {
    if (!base.mcpServers.length || !/mcp/i.test(`${err.message} ${JSON.stringify(err.data || '')}`)) throw err;
    res = await client.request('session/new', { ...base, mcpServers: [] }, { timeout: 120000 });
  }
  const sid = (res && res.sessionId) || '';
  if (!sid) throw new Error('Hermes не дал сессию для соты');
  swarmText.set(sid, '');
  try {
    await client.request('session/prompt', { sessionId: sid, prompt: [{ type: 'text', text: prompt }] }, { timeout: 0 });
  } finally {
    setTimeout(() => swarmText.delete(sid), 5000);
  }
  return swarmText.get(sid) || '';
}

// Соты — это настройка профиля из подписанного документа доступов: окно не решает само, сколько их.
function swarmAllowed() {
  const got = rules.load(HOME);
  const allow = { ...swarmAllowedOf(got && got.rules), why: (got && got.error) || '' };
  // нет доступов на ПК — рой закрыт, но причина другая, чем «администратор выключил», и окно так и говорит
  if (allow.why) allow.on = false;
  return { ...allow, off: swarmOffText(allow) };
}

function swarmSend(state) {
  const now = Date.now();
  if (state && state.stage === 'work' && now - swarmBeat < 150) return; // 40 сот не должны слать 40 событий в секунду
  swarmBeat = now;
  sendOrQueue('swarm:state', state);
}

function handle(channel, fn) {
  ipcMain.handle(channel, async (event, ...args) => {
    if (!win || event.sender !== win.webContents) return { ok: false, error: 'запрос не из окна' };
    try {
      return { ok: true, value: await fn(...args) };
    } catch (err) {
      if (!/^(fs:info|fs:list|fs:text)$/.test(channel)) log(`${channel}:`, err.message || String(err));
      return { ok: false, error: err.message || String(err) };
    }
  });
}

function ipc() {
  handle('app:info', () => {
    const cfg = config.load(HOME);
    const got = relaunch.installed(HOME);
    return { installed: got ? { version: got.version, app: got.app } : null,
      running: app.getAppPath(), version: VERSION, electron: process.versions.electron, chrome: process.versions.chrome, platform: process.platform,
      home: HOME, agent: cfg.agent, profile: cfg.profile, server: cfg.server, person: cfg.person, connected: cfg.connected,
      hermes: hermesPath(cfg), homedir: os.homedir(), relaunch_last: relaunchLast };
  });
  // Перезапуск — в установленную сборку, а не в ту программу, из которой окно сейчас работает: после обновления
  // они разные, и app.relaunch() открывал старую («перезапустил — ничего не изменилось»).
  handle('app:relaunch', async () => {
    const into = relaunch.next(HOME, app.getAppPath());
    const run = into ? relaunch.command(into) : { exe: process.execPath, args: [app.getAppPath()], cwd: app.getAppPath() };
    const ready = relaunch.readyPath(HOME);
    try { fs.rmSync(ready, { force: true }); } catch { /* файла не было */ }
    // Замок единственного экземпляра отпускаем ДО запуска новой копии: иначе новая проигрывает гонку за замок и
    // тихо выходит (окно мигает секунду и пропадает), а старая к этому моменту уже закрылась — окон не остаётся.
    try { app.releaseSingleInstanceLock(); } catch { /* Electron без такого API */ }
    let child;
    try {
      child = spawn(run.exe, run.args, {
        cwd: run.cwd, detached: true, stdio: 'ignore',
        env: { ...process.env, SYSAI_RELAUNCH_READY: ready },
      });
      child.unref();
    } catch (err) {
      relaunchLast = { at: new Date().toISOString(), error: err.message || String(err) };
      log('перезапуск не удался:', relaunchLast.error);
      throw new Error(`не удалось запустить ${run.exe}: ${relaunchLast.error}`);
    }
    log(`перезапуск: ${run.exe} ${run.args.join(' ')} (pid ${child.pid})`);
    const left = await relaunch.waitReady(ready, child);
    if (!left.ok) {
      relaunchLast = { at: new Date().toISOString(), error: left.error };
      log('новая копия не открылась:', left.error);
      throw new Error(`новая копия не открылась: ${left.error}`);
    }
    relaunchLast = { at: new Date().toISOString(), into: left.got.version || (into && into.version) || '' };
    log(`новая копия открылась (pid ${left.got.pid}, версия ${left.got.version || '?'}), закрываюсь`);
    setTimeout(() => app.exit(0), 200);
    return { into: left.got.version || '', ok: true };
  });
  handle('acp:state', () => acpInfo);
  handle('acp:restart', () => restartAcp());
  handle('acp:request', (method, params) => acpRequest(method, params));
  handle('acp:notify', (method, params) => {
    if (!NOTIFICATIONS.has(method)) throw new Error(`окно не отправляет агенту ${method}`);
    if (!acp) throw new Error('агент не запущен');
    acp.notify(method, params || {});
    return true;
  });
  handle('acp:respond', (id, result) => {
    if (!pending.has(id)) throw new Error('вопрос уже снят');
    pending.delete(id);
    if (!acp) throw new Error('агент не запущен');
    acp.respond(id, result);
    return true;
  });
  handle('fs:places', () => files.places((k) => app.getPath(k)));
  handle('fs:drives', () => files.drives());
  handle('fs:list', (dir) => files.list(need(dir)));
  handle('fs:info', (p) => files.info(need(p)));
  handle('fs:text', (p) => files.readText(need(p)));
  handle('fs:bytes', (p) => files.readBytes(need(p)));
  handle('fs:url', (p) => pathToFileURL(need(p)).href);
  handle('fs:attachments', (paths) => files.attachments((Array.isArray(paths) ? paths : []).map(need)));
  handle('fs:paste', async (name, bytes) => {
    if (!(bytes instanceof Uint8Array) || !bytes.length) throw new Error('пустая вставка');
    if (bytes.length > PASTE_LIMIT) throw new Error('вставка больше 30 МБ');
    const safe = path.basename(String(name || 'вставка.png')).replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 120) || 'вставка.png';
    const dir = path.join(DESK, 'cache', 'paste');
    await fsp.mkdir(dir, { recursive: true });
    const p = path.join(dir, `${Date.now().toString(36)}-${safe}`);
    await fsp.writeFile(p, bytes);
    return p;
  });
  handle('shell:open', async (p) => {
    const err = await shell.openPath(need(p));
    if (err) throw new Error(err);
    return true;
  });
  handle('shell:reveal', (p) => {
    shell.showItemInFolder(need(p));
    return true;
  });
  handle('shell:run', (p) => run(need(p)));
  handle('shell:external', async (url) => {
    if (typeof url !== 'string' || !/^(https?:|mailto:)/i.test(url)) throw new Error('такую ссылку окно не открывает');
    await shell.openExternal(url);
    return true;
  });
  handle('dialog:files', async (opts) => {
    const res = await dialog.showOpenDialog(win, { title: String((opts && opts.title) || 'Выберите файлы'), properties: ['openFile', 'multiSelections'] });
    return res.canceled ? [] : res.filePaths;
  });
  // «Проекты»: folders of projects in the person's documents
  const projects = new Projects(projectsRoot());
  handle('projects:list', () => projects.list());
  handle('projects:create', (name, scope) => projects.create(String(name || ''), String(scope || 'full')));
  handle('projects:info', async (name) => {
    const info = await projects.info(String(name || ''));
    const { stage, ...pr } = projectPrompts(info);
    return { ...info, prompts: { ...pr, stages: Object.fromEntries(projectStages.map(([key]) => [key, stage(key)])) } };
  });
  handle('projects:add', (name, paths) => projects.add(String(name || ''), Array.isArray(paths) ? paths.map(need) : []));
  handle('projects:tidy', (name) => projects.tidy(String(name || '')));
  handle('projects:dialog', (name, dialog) => projects.noteDialog(String(name || ''), dialog || {}));
  // «Удалить проект»: the folder goes to the trash of the projects folder or is erased. The window asks first (a
  // native dialog, so no page can do it by accident), and the conversations about the project go with it.
  handle('projects:delete', async (name, mode) => {
    const how = mode === 'erase' ? 'erase' : 'trash';
    const done = await projects.remove(String(name || ''), how);
    const convs = await chats.removeProject(HOME, done.name);
    return { ...done, dialogs: convs.removed };
  });
  // A native question with buttons, for removing things: the page names them, the system draws them.
  handle('ui:confirm', async (opts) => {
    const o = opts && typeof opts === 'object' ? opts : {};
    const buttons = Array.isArray(o.buttons) && o.buttons.length ? o.buttons.map((b) => String(b)) : ['Отмена', 'Удалить'];
    const res = await dialog.showMessageBox(win, {
      type: o.kind === 'warning' ? 'warning' : 'question',
      title: String(o.title || 'Агент SYSAI'),
      message: String(o.message || ''),
      detail: String(o.detail || ''),
      buttons,
      defaultId: Number.isInteger(o.defaultId) ? o.defaultId : 0,
      cancelId: Number.isInteger(o.cancelId) ? o.cancelId : 0,
      noLink: true,
    });
    return { response: res.response };
  });
  // «Сохранить разговор»: Markdown into the project (_work/notes) or into «SYSAI Разговоры» of the documents
  // The conversations of the window: our own copy, so a restart, an update or a profile made anew does not lose
  // them. list returns what is kept plus the ids the person deleted (Hermes may still list those sessions).
  handle('chats:list', () => chats.list(HOME));
  handle('chats:read', (id) => chats.read(HOME, String(id || '')));
  handle('chats:write', (doc) => chats.write(HOME, doc || {}));
  handle('chats:delete', async (id) => {
    const done = await chats.remove(HOME, String(id || ''));
    // Hermes' own store is asked to forget the session too; a Hermes that does not know it is not an error
    try { await acp.request('session/delete', { sessionId: done.id }); } catch { /* no such method or no session */ }
    return done;
  });
  handle('conv:export', async (data) => {
    const d = data && typeof data === 'object' ? data : {};
    const dir = d.project ? path.join(projects.dir(String(d.project)), '_work', 'notes') : path.join(app.getPath('documents'), 'SYSAI Разговоры');
    return convExport.save(dir, { ...d, at: new Date() });
  });
  handle('projects:role', (name, rel, role) => projects.setRole(String(name || ''), String(rel || ''), String(role || '')));
  // «Добавить из Хранилища»: a file or a whole folder of the storage is fetched (the cache of the window, sha256
  // checked) and copied into materials/ of the project, recorded as taken from the storage
  handle('projects:attach', async (name, item, role) => {
    if (!item || typeof item.path !== 'string') throw new Error('нет файла');
    const cfg = config.load(HOME);
    const cache = path.join(DESK, 'cache', 'storage');
    const files = await storageFiles(cfg, item, (folder) => storage.list(cfg, folder, ''));
    const fetched = [];
    const failed = [];
    for (const f of files) {
      try {
        fetched.push({ ...f, path: await storage.fetchFile(cfg, f.item, cache), item: undefined });
      } catch (err) {
        failed.push({ path: f.source, error: err.message });
      }
    }
    return [...await projects.attach(String(name || ''), fetched, String(role || '')), ...failed];
  });
  handle('auto:get', () => auto.state());
  handle('auto:set', (on, project) => {
    const state = auto.set(Boolean(on), String(project || ''));
    send('auto:state', state);
    return state;
  });
  handle('auto:recent', () => auto.recentEdits());
  handle('auto:undo', (id) => auto.undo(String(id || '')));
  handle('clipboard:text', (text) => {
    clipboard.writeText(String(text));
    return true;
  });
  handle('storage:list', (folder, q) => storage.list(config.load(HOME), String(folder || ''), String(q || '')));
  handle('storage:fetch', (item) => {
    if (!item || typeof item.path !== 'string') throw new Error('нет файла');
    return storage.fetchFile(config.load(HOME), item, path.join(DESK, 'cache', 'storage'));
  });
  handle('storage:upload', async (paths, folder) => {
    const cfg = config.load(HOME);
    const out = [];
    for (const p of Array.isArray(paths) ? paths : []) {
      try {
        out.push({ path: p, ok: true, item: await storage.upload(cfg, need(p), String(folder || '')) });
      } catch (err) {
        out.push({ path: p, error: err.message });
      }
    }
    return out;
  });
  handle('app:logs', () => {
    if (!fs.existsSync(LOG)) log('журнал окна начат');
    return LOG;
  });
  // «Проверить обновление»: the page has no network (CSP), so the request to SYSAI is made here. The window only
  // reads the offer and wakes the synchronizer — installing stays in one place, update.py.
  handle('update:check', () => {
    const cfg = config.load(HOME);
    // the window counts too: a newer window on offer with the synchronizer current is still «есть обновление»
    return updates.check({ home: HOME, server: cfg.server, token: cfg.token, request: netJson, window: VERSION });
  });
  handle('update:now', () => updates.wake(HOME));
  // «Что с обновлением сейчас»: окно спрашивает каждые пару секунд после «Установить» — ставит синхронизатор в
  // фоне, и человек должен видеть, что дело идёт (и где встало, если не идёт).
  handle('update:status', () => updates.status({ home: HOME }));
  // ------------------------------------------------------------------------------------------------ соты (рой)
  handle('swarm:allowed', () => swarmAllowed());
  handle('swarm:state', () => (swarm ? swarm.snapshot() : null));
  handle('swarm:start', async (opts) => {
    if (swarm) throw new Error('рой уже идёт: дождитесь отчёта или остановите его');
    const allow = swarmAllowed();
    if (!allow.on) throw new Error(allow.off);
    const o = opts && typeof opts === 'object' ? opts : {};
    const question = String(o.question || '').trim();
    if (!question) throw new Error('рой без задачи: напишите, что проверять');
    const cells = Math.min(Number(o.cells) || allow.cells, allow.cells);
    const root = o.project ? path.join(String(o.project), '.sysai', 'swarm') : path.join(DESK, 'swarm');
    swarmCwd = o.project ? String(o.project) : '';
    swarm = new Swarm({ question, items: Array.isArray(o.items) ? o.items : [], cells, minutes: o.minutes,
      ask: askSwarm, root, arbiter: true, concurrency: allow.at });
    const mine = swarm;
    mine.on('state', (s) => swarmSend(s));
    mine.run().then((state) => {
      log(`рой закончил: ${state.stage}, сот ${state.cells.length}, находок ${state.counters.found}`);
      if (state.result.path) send('ui:notice', { text: `Рой готов: ${state.counters.found} находок. Отчёт — ${state.result.path}`, kind: 'ok' });
    }).catch((err) => {
      log('рой упал:', err);
      send('ui:notice', { text: `Рой остановился: ${err.message}`, kind: 'err' });
    }).finally(() => {
      if (swarm === mine) swarm = null;
    });
    return mine.snapshot();
  });
  handle('swarm:stop', () => {
    if (!swarm) return false;
    swarm.abort();
    send('ui:notice', { text: 'Рой останавливается: ответы, что успели, останутся в отчёте', kind: 'warn' });
    return true;
  });
  handle('win:overlay', (color) => {
    if (typeof color !== 'string' || !/^#[0-9a-f]{6}$/i.test(color)) throw new Error('нет цвета');
    try {
      win.setTitleBarOverlay({ color, symbolColor: SYMBOLS, height: TOP });
    } catch { /* not where there is no overlay */ }
    return true;
  });
  handle('canvas:state', () => canvas.state());
  handle('canvas:open', (p, w) => canvas.open(need(p), w));
  handle('canvas:pick', async () => {
    const res = await dialog.showOpenDialog(win, { title: 'Проект EasyBuilder Pro', properties: ['openFile'],
      filters: [{ name: 'EasyBuilder Pro', extensions: ['cmtp', 'emtp'] }] });
    return res.canceled || !res.filePaths.length ? '' : res.filePaths[0];
  });
  handle('canvas:window', (w) => canvas.show(w));
  handle('canvas:edit', (edits) => canvas.edit(PERSON, edits));
  handle('canvas:undo', () => canvas.undo());
  handle('canvas:reload', () => canvas.reload());
  handle('canvas:close', () => canvas.close());
  handle('canvas:eb', () => openInEasyBuilder(canvas.file));
  handle('apps:detect', (dir) => apps.detect(need(dir)));
  // «Где и что будет»: что открывается (detect) и какая программа отвечает названному железу (environment) — одной
  // выдачей, чтобы окно не выбирало программу само.
  handle('apps:plan', (dir, facts) => {
    const openers = apps.detect(need(dir));
    return { openers, environment: apps.environment(facts || {}, openers) };
  });
  handle('apps:open', (id, file) => openInApp(String(id || ''), file));
  ipcMain.on('canvas:pointer', (event, p) => {
    if (win && event.sender === win.webContents && p && typeof p === 'object') canvas.pointer(p);
  });
  ipcMain.on('ui:ready', (event) => {
    if (!win || event.sender !== win.webContents) return;
    uiReady = true;
    event.sender.send('acp:state', acpInfo);
    for (const r of pending.values()) event.sender.send('acp:request', r);
    while (queue.length) {
      const [channel, payload] = queue.shift();
      event.sender.send(channel, payload);
    }
    // a newer build was installed while this window was closed: say so, the person restarts when it suits
    const installed = installedVersion();
    if (installed && installed !== VERSION) {
      event.sender.send('ui:notice', {
        text: `Установлена новая версия окна агента ${installed}. Перезапустите окно, чтобы её открыть.`,
        kind: 'update',
      });
    }
  });
  // Синхронизатор услышал новую сборку (пульс или толчок по живому каналу): окно говорит «есть обновление» сразу, а не
  // через полчаса. Сети тут нет — читается state.json, тот же файл, что и для полосы установки.
  updates.watch({ home: HOME, changed: () => sendOrQueue('update:offered', {}) });
}
