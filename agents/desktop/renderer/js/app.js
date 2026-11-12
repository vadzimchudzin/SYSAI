// The window: conversations with the agent on the left, the conversation in the middle, the working pane (a file,
// this computer's folders, SYSAI's storage) on the right, the state of the agent at the bottom. Hermes is reached
// through the main process (ACP over stdio); everything here is drawing and the person's choices.
import { h, icon, clear, when, group, basename, debounce } from './dom.js';
import { Conversation } from './chat.js';
import { Preview } from './preview.js';
import { Explorer, StorageView } from './explorer.js';
import { ProjectsView } from './projects.js';
import { CanvasView, isProject } from './canvas.js';
import { Composer } from './composer.js';
import { openMenu, toast } from './ui.js';

const api = window.sysai;
const $ = (id) => document.getElementById(id);
const BG = '#121210';
const BG2 = '#181816';

const S = {
  info: {},
  acp: { state: 'starting' },
  readyGen: -1,
  sessions: [],
  cursor: null,
  listing: false,
  listError: '',
  convs: new Map(),
  current: '',
  draft: '',
  creating: null,
  askKind: new Map(),
  stuck: true,
};

let composer;
let pane;

const SUGGEST = [
  ['spark', 'Что ты умеешь на этом компьютере?'],
  ['folder', 'Разбери папку «Загрузки»: что там и что можно удалить?'],
  ['sheet', 'Сделай короткую сводку по таблице — я прикреплю файл'],
  ['cloud', 'Что есть для меня в Хранилище SYSAI?'],
];

// ---------------------------------------------------------------------------------------------- conversations
function current() {
  return S.convs.get(S.current) || null;
}

function makeConv(id) {
  const conv = new Conversation(id, {
    onGrow: (c) => { if (c.id === S.current) stick(); },
    onChange: onConvChange,
    openPath: (p, line) => openPath(p, line),
    respond: async (reqId, result) => {
      S.askKind.delete(reqId);
      await api.acp.respond(reqId, result);
    },
    copy: (text) => api.clipboard.write(text).then(() => toast('Скопировано', 'ok', 1500)),
  });
  S.convs.set(id, conv);
  return conv;
}

function onConvChange(conv, what) {
  const here = conv.id === S.current;
  if (what === 'title') {
    const s = S.sessions.find((x) => x.sessionId === conv.id);
    if (s) s.title = conv.title;
    drawSessions();
    if (here) drawTop();
  } else if (what === 'config' || what === 'commands') {
    if (here) { composer.update(); drawStatus(); }
  } else if (what === 'usage') {
    if (here) drawStatus();
  } else if (what === 'plan') {
    if (here) composer.drawPlan();
  } else if (what === 'running' || what === 'ask') {
    drawSessions();
    if (here) { composer.update(); composer.drawPlan(); }
    drawStatus();
    const busy = [...S.convs.values()].some((c) => c.running);
    $('brand-tile').classList.toggle('live', busy);
  }
}

// The project a folder belongs to: «SYSAI Проекты/<проект>/…» → {name, path}; null — not a project.
function projectOf(cwd) {
  const root = String((S.auto && S.auto.root) || '');
  const dir = String(cwd || '');
  if (!root || dir.length <= root.length + 1 || dir.slice(0, root.length).toLowerCase() !== root.toLowerCase() || !/[\\/]/.test(dir[root.length])) return null;
  const name = dir.slice(root.length + 1).split(/[\\/]/)[0];
  return name ? { name, path: `${root}${dir[root.length]}${name}` } : null;
}

function projectOfSession(id) {
  const conv = S.convs.get(id);
  const s = S.sessions.find((x) => x.sessionId === id);
  return projectOf((conv && conv.cwd) || (s && s.cwd) || '');
}

// A request from «Проекты» (a stage, «Создать проект», «Документация»): a new conversation opened in the folder of
// the project — one stage, one conversation. The state of the work is in project.md and pipeline.json, not in what
// was said before, and a conversation that starts anew costs a fraction of one that carries every earlier stage.
async function askInProject(text, project) {
  const conv = current();
  const here = conv && project && conv.empty && !conv.running && conv.cwd === project.path;
  if (project && !here) {
    try {
      await newDraft({ cwd: project.path });
      if (conv && !conv.empty) toast(`Новый разговор в проекте «${project.name}». Прежний остался в списке слева.`, 'info', 6000);
    } catch (err) {
      toast(`Разговор в проекте не начался: ${err.message}`, 'err');
    }
  }
  composer.fill(text);
}

async function newChat() {
  const conv = current();
  if (conv && conv.draft && conv.empty) { composer.focus(); return; }
  try {
    const project = conv && projectOf(conv.cwd); // «новый разговор» from a project's conversation stays in the project
    await newDraft(project ? { cwd: project.path } : {});
  } catch (err) {
    toast(`Новый разговор не начался: ${err.message}`, 'err');
  }
}

// cwd — the folder the conversation works in (a project); without it — the person's home folder.
async function newDraft({ select = true, cwd = '' } = {}) {
  const ready = S.convs.get(S.draft);
  if (ready && ready.draft && ready.empty && (ready.cwd || '') === cwd) {
    if (select) selectConv(ready.id);
    return ready;
  }
  if (S.creating && S.creatingCwd !== cwd) await S.creating.catch(() => {});
  if (!S.creating) {
    S.creatingCwd = cwd;
    S.creating = (async () => {
      const res = await api.acp.request('session/new', cwd ? { cwd } : {});
      const conv = makeConv(res.sessionId);
      conv.loaded = true;
      conv.cwd = cwd;
      conv.config = res.configOptions || [];
      S.draft = conv.id;
      return conv;
    })().finally(() => { S.creating = null; });
  }
  const conv = await S.creating;
  if (select) selectConv(conv.id);
  return conv;
}

function selectConv(id) {
  saveScroll();
  S.current = id;
  let conv = S.convs.get(id);
  if (!conv) {
    conv = makeConv(id);
    conv.draft = false;
  }
  const s = S.sessions.find((x) => x.sessionId === id);
  if (s && !conv.title && s.title) conv.title = s.title;
  if (s && s.cwd && !conv.cwd) conv.cwd = s.cwd;
  if (!conv.draft) localStorage.setItem('sysai.session.last', id); // opened again when the window starts
  drawThread();
  drawSessions();
  drawTop();
  drawStatus();
  composer.setConversation(conv);
  if (!conv.loaded && !conv.loading && S.acp.state === 'ready') loadConv(conv);
  composer.focus();
}

async function loadConv(conv) {
  conv.loading = true;
  drawThread();
  try {
    const res = await api.acp.request('session/load', conv.cwd ? { sessionId: conv.id, cwd: conv.cwd } : { sessionId: conv.id });
    if (res && Array.isArray(res.configOptions)) conv.config = res.configOptions;
    conv.loaded = true;
  } catch (err) {
    conv.notice(`Разговор не открылся: ${err.message}`, 'err');
  } finally {
    conv.loading = false;
    if (S.current === conv.id) {
      drawThread();
      composer.setConversation(conv);
      drawStatus();
      S.stuck = true;
      stick();
    }
  }
}

async function send({ text, files }) {
  let conv = current();
  try {
    if (!conv || (!conv.loaded && !conv.draft)) conv = conv || await newDraft();
  } catch (err) {
    toast(`Сообщение не отправлено: ${err.message}`, 'err');
    return false;
  }
  if (conv.running) return false;
  const wasDraft = conv.draft;
  // a big text file is named by its path, not linked: linked, its whole text would go into every next request
  let attached;
  let linked;
  let urls;
  try {
    attached = files.length ? await api.fs.attachments(files) : { items: [], text: '' };
    linked = attached.items.filter((x) => x.link).map((x) => x.path);
    urls = await Promise.all(linked.map((p) => api.fs.url(p)));
  } catch (err) {
    toast(err.message, 'err');
    return false;
  }
  const said = attached.text ? `${text}${text ? '\n\n' : ''}${attached.text}` : text;
  const prompt = [{ type: 'text', text: said }, ...linked.map((p, i) => ({ type: 'resource_link', uri: urls[i], name: basename(p) }))];
  conv.addUser(text, files.map((p) => ({ name: basename(p), path: p })));
  conv.start();
  if (wasDraft) {
    S.draft = '';
    S.sessions.unshift({ sessionId: conv.id, title: text.slice(0, 80) || basename(files[0] || ''), updatedAt: new Date().toISOString(), local: true,
      cwd: conv.cwd || '' });
    if (!conv.title) conv.title = '';
    localStorage.setItem('sysai.session.last', conv.id);
  }
  touch(conv.id);
  S.stuck = true;
  drawThread();
  drawTop();
  try {
    const res = await api.acp.request('session/prompt', { sessionId: conv.id, prompt });
    conv.finish(res && res.stopReason);
  } catch (err) {
    conv.finish(null, err.message || String(err));
  }
  touch(conv.id);
  return true;
}

function stop() {
  const conv = current();
  if (!conv || !conv.running) return;
  for (const reqId of [...conv.asks.keys()]) {
    const kind = S.askKind.get(reqId);
    S.askKind.delete(reqId);
    api.acp.respond(reqId, kind === 'elicitation/create' ? { action: 'cancel' } : { outcome: { outcome: 'cancelled' } }).catch(() => {});
    conv.dropAsk(reqId, 'Остановлено');
  }
  api.acp.notify('session/cancel', { sessionId: conv.id }).catch((err) => toast(err.message, 'err'));
}

async function setConfig(conv, configId, value) {
  try {
    const res = await api.acp.request('session/set_config_option', { sessionId: conv.id, configId, value });
    if (res && Array.isArray(res.configOptions)) conv.config = res.configOptions;
    else {
      const opt = conv.config.find((o) => o.id === configId);
      if (opt) opt.currentValue = value;
    }
    if (conv.id === S.current) { composer.update(); drawStatus(); }
  } catch (err) {
    toast(`Не переключилось: ${err.message}`, 'err');
  }
}

function touch(id) {
  const s = S.sessions.find((x) => x.sessionId === id);
  if (s) {
    s.updatedAt = new Date().toISOString();
    S.sessions = [s, ...S.sessions.filter((x) => x !== s)];
  }
  drawSessions();
}

// ---------------------------------------------------------------------------------------------- Hermes (ACP)
function onAcpState(st) {
  S.acp = st || { state: 'stopped' };
  if (S.acp.state === 'ready' && S.acp.generation !== S.readyGen) {
    const restarted = S.readyGen !== -1;
    S.readyGen = S.acp.generation;
    afterReady(restarted);
  } else if (S.acp.state !== 'ready') {
    for (const conv of S.convs.values()) {
      for (const reqId of [...conv.asks.keys()]) conv.dropAsk(reqId, 'Агент перезапускается — вопрос снят');
      if (conv.running) conv.finish(null, 'Агент остановился посреди работы. Если задача шла в шлюзе Hermes, она могла завершиться — откройте разговор снова.');
    }
  }
  drawThread();
  drawStatus();
  composer.update();
}

const LAST_WAIT_MS = 4000;

async function afterReady(restarted) {
  const keep = S.current && !S.convs.get(S.current)?.draft ? S.current : '';
  if (restarted) {
    S.convs.clear();
    S.askKind.clear();
    S.draft = '';
    S.current = '';
  }
  const listed = loadSessions();
  if (keep && restarted) selectConv(keep);
  else if (!current()) {
    // the window opens where the person left: the last conversation, when Hermes still has it; otherwise a new one
    const last = localStorage.getItem('sysai.session.last') || '';
    // the list is waited for a few seconds only: a Hermes that is slow with it must not keep the window empty
    if (last) await Promise.race([listed, new Promise((done) => setTimeout(done, LAST_WAIT_MS))]);
    if (current()) return;
    if (last && S.sessions.some((s) => s.sessionId === last)) {
      selectConv(last);
      return;
    }
    try {
      await newDraft();
    } catch (err) {
      drawThread(`Разговор не начался: ${err.message}`);
    }
  }
}

async function loadSessions(more = false) {
  if (S.listing) return;
  S.listing = true;
  try {
    const res = await api.acp.request('session/list', more && S.cursor ? { cursor: S.cursor } : {});
    const got = ((res && res.sessions) || []).filter((s) => s && s.sessionId);
    if (more) {
      const seen = new Set(S.sessions.map((s) => s.sessionId));
      S.sessions.push(...got.filter((s) => !seen.has(s.sessionId)));
    } else {
      const local = S.sessions.filter((s) => s.local && !got.some((g) => g.sessionId === s.sessionId));
      for (const g of got) {
        const conv = S.convs.get(g.sessionId);
        if (conv && conv.title && !g.title) g.title = conv.title;
        if (conv && conv.cwd && !g.cwd) g.cwd = conv.cwd;
      }
      S.sessions = [...local, ...got];
    }
    S.sessions.sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || '')));
    S.cursor = (res && res.nextCursor) || null;
    S.listError = '';
  } catch (err) {
    S.listError = err.message || String(err);
  } finally {
    S.listing = false;
  }
  drawSessions();
}

function onNotification({ method, params }) {
  if (method !== 'session/update' || !params) return;
  const conv = S.convs.get(params.sessionId);
  const u = params.update || {};
  if (!conv) {
    if (u.sessionUpdate === 'session_info_update' && u.title) {
      const s = S.sessions.find((x) => x.sessionId === params.sessionId);
      if (s) { s.title = u.title; drawSessions(); }
    }
    return;
  }
  if (u.sessionUpdate === 'agent_message_chunk' && u.content && /^Attached to the gateway's existing session policy/.test(u.content.text || '')) return;
  conv.apply(u);
  if (u.sessionUpdate === 'tool_call_update' && u.status === 'completed') {
    const t = conv.tools.get(u.toolCallId);
    const d = t ? t.data : u;
    const paths = [...(d.locations || []).map((l) => l.path), ...(d.content || []).filter((c) => c.type === 'diff').map((c) => c.path)];
    if (paths.length) pane.preview.refreshIf(paths);
  }
}

// «Правки без вопросов» (lib/autoapprove.js of the main process): on — the window allows edits inside the project
// folders itself.
// folders itself; project — only inside that project (the profile of SYSAI may allow nothing wider).
async function setAuto(on, project = '') {
  try {
    if (on && !project && S.auto && S.auto.policy === 'project') {
      const here = current() && projectOf(current().cwd);
      project = here ? here.name : '';
    }
    S.auto = await api.auto.set(on, project);
    composer.update();
    toast(!on ? 'Агент снова спрашивает перед каждой правкой файла.'
      : `Правки ${project ? `в проекте «${project}»` : 'в папках проектов'} — без вопросов. Материалы, секреты, удаление и `
        + `файлы ${project ? 'других проектов и ' : ''}вне проектов агент по-прежнему спросит.`, on ? 'ok' : 'info', 7000);
  } catch (err) {
    toast(err.message, 'err');
  }
}

// «Вернуть»: an edit allowed without a question is taken back from the copy the window made before it.
async function undoEdit(id) {
  try {
    const done = await api.auto.undo(id);
    toast(`Возвращено: ${[...done.restored, ...done.removed.map((n) => `${n} (убран)`)].join(', ') || 'нечего возвращать'}`, 'ok', 6000);
    if (pane && pane.projects && pane.projects.name) pane.projects.open(pane.projects.name, true);
  } catch (err) {
    toast(`Не возвращено: ${err.message}`, 'err', 8000);
  }
}

function onRequest({ id, method, params }) {
  const conv = S.convs.get(params && params.sessionId);
  if (!conv) {
    api.acp.respond(id, method === 'elicitation/create' ? { action: 'cancel' } : { outcome: { outcome: 'cancelled' } }).catch(() => {});
    return;
  }
  if (conv.asks.has(id)) return; // sent again after a reload of the page
  S.askKind.set(id, method);
  if (method === 'session/request_permission') conv.addPermission(id, params);
  else if (method === 'elicitation/create') conv.addQuestion(id, params);
  if (conv.id !== S.current) {
    const t = toast(`Агент ждёт ответа в разговоре «${conv.title || 'без названия'}» — нажмите, чтобы перейти`, 'info', 12000);
    t.addEventListener('click', () => selectConv(conv.id));
  }
}

function onRequestGone({ id, why }) {
  S.askKind.delete(id);
  for (const conv of S.convs.values()) if (conv.asks.has(id)) conv.dropAsk(id, why || 'Вопрос снят');
}

// ---------------------------------------------------------------------------------------------- files
async function openPath(p, line) {
  if (!p) return;
  let info;
  try {
    info = await api.fs.info(p);
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  if (!info.exists) {
    toast(`Нет такого файла: ${p}`, 'err');
    return;
  }
  if (info.dir) {
    pane.show('files');
    pane.explorer.go(info.path);
    return;
  }
  if (isProject(info.path)) { // проект EasyBuilder — на холст
    pane.show('canvas');
    pane.canvas.open(info.path);
    return;
  }
  pane.show('preview');
  pane.preview.show({ path: info.path, line });
}

class Pane {
  constructor() {
    this.el = $('pane');
    this.head = $('pane-head');
    this.body = $('pane-body');
    this.tab = 'preview';
    this.opened = false;
    this.max = false;
    this.preview = new Preview({
      attach: (p) => composer.attach(p),
      openFolder: (p) => { this.show('files'); this.explorer.go(p); },
    });
    this.explorer = new Explorer({ open: (p) => openPath(p), attach: (p) => composer.attach(p) });
    this.storage = new StorageView({
      open: (p, item) => { this.show('preview'); this.preview.show({ path: p, stored: item }); },
      attach: (p) => composer.attach(p),
      project: () => (this.projects && this.projects.name) || '', // «В материалы проекта» of a row of the storage
      attached: (name) => { if (this.projects.name === name && this.projects.info) this.projects.open(name, true); },
    });
    this.canvas = new CanvasView({ focus: () => this.show('canvas'), mention: (t) => composer.fill(t) });
    this.projects = new ProjectsView({ open: (p) => openPath(p), ask: (t, project) => askInProject(t, project),
      storage: (project) => {
        this.show('storage');
        this.storage.refresh();
        toast(`Хранилище: у файла или папки нажмите «В материалы проекта «${project.name}»» — они скопируются в проект.`, 'info', 7000);
      } });
    this.sections = { preview: this.preview, canvas: this.canvas, files: this.explorer, storage: this.storage,
      projects: this.projects };
    this.tabs = {};
    for (const [key, label, ico] of [['preview', 'Просмотр', 'eye'], ['canvas', 'Холст', 'canvas'], ['files', 'Файлы', 'monitor'],
      ['storage', 'Хранилище', 'cloud'], ['projects', 'Проекты', 'folder']]) {
      this.tabs[key] = h('button', { class: 'tab', onclick: () => this.show(key) }, icon(ico, 'sm'), h('span', { text: label }));
    }
    this.maxBtn = h('button', { class: 'icon-btn sm', title: 'Развернуть', onclick: () => this.setMax(!this.max) }, icon('max', 'sm'));
    this.head.append(...Object.values(this.tabs), h('span', { class: 'grow' }), this.maxBtn,
      h('button', { class: 'icon-btn sm', title: 'Закрыть панель (Ctrl+B)', onclick: () => this.close() }, icon('x', 'sm')));
    this.body.addEventListener('click', linkClicks);
    const w = Number(localStorage.getItem('sysai.pane.w') || 0);
    if (w) document.documentElement.style.setProperty('--pane-w', `${w}px`);
  }

  show(tab = this.tab) {
    this.tab = tab;
    for (const [k, b] of Object.entries(this.tabs)) b.classList.toggle('on', k === tab);
    const section = this.sections[tab];
    if (this.body.firstChild !== section.el) this.body.replaceChildren(section.el);
    if (tab === 'files') this.explorer.start();
    if (tab === 'storage') this.storage.start();
    if (tab === 'canvas') this.canvas.start();
    if (tab === 'projects') this.projects.start();
    if (!this.opened) {
      this.opened = true;
      this.el.hidden = false;
      $('pane-resizer').hidden = false;
      $('app').classList.add('pane-open');
      overlay();
    }
    localStorage.setItem('sysai.pane.open', tab);
    this.nav();
    drawTop();
  }

  nav() { // the sidebar shows which part of the pane is open
    for (const el of document.querySelectorAll('.nav-item[data-tab]')) el.classList.toggle('on', this.opened && el.dataset.tab === this.tab);
  }

  close() {
    this.opened = false;
    this.setMax(false);
    this.el.hidden = true;
    $('pane-resizer').hidden = true;
    $('app').classList.remove('pane-open');
    overlay();
    localStorage.removeItem('sysai.pane.open');
    this.nav();
    drawTop();
  }

  toggle(tab) {
    if (this.opened && (!tab || tab === this.tab)) this.close();
    else this.show(tab || this.tab);
  }

  setMax(v) {
    this.max = v;
    $('app').classList.toggle('pane-max', v);
    this.maxBtn.replaceChildren(icon(v ? 'min' : 'max', 'sm'));
    this.maxBtn.title = v ? 'Вернуть разговор' : 'Развернуть';
    overlay();
  }
}

function overlay() {
  api.win.overlay(pane && pane.opened ? BG2 : BG).catch(() => {});
}

// ---------------------------------------------------------------------------------------------- drawing
function firstName(person) {
  const p = String(person || '').trim();
  if (!p) return '';
  const parts = p.split(/\s+/);
  return parts.length >= 2 && /^[А-ЯЁA-Z][а-яёa-z]+$/.test(parts[1]) && parts.length === 3 ? parts[1] : parts[0];
}

function initials(person, agent) {
  const p = String(person || agent || 'A').trim().split(/\s+/);
  return ((p[0] || 'A')[0] + (p[1] ? p[1][0] : '')).toUpperCase();
}

function drawSidebar() {
  const nc = $('new-chat');
  nc.replaceChildren(icon('plus'), h('span', { text: 'Новый разговор' }), h('kbd', { text: 'Ctrl N' }));
  nc.onclick = newChat;
  $('nav-files').replaceChildren(icon('monitor'), h('span', { text: 'Файлы компьютера' }));
  $('nav-files').onclick = () => pane.toggle('files');
  $('nav-storage').replaceChildren(icon('cloud'), h('span', { text: 'Хранилище SYSAI' }));
  $('nav-storage').onclick = () => pane.toggle('storage');
  $('nav-projects').replaceChildren(icon('folder'), h('span', { text: 'Проекты' }));
  $('nav-projects').onclick = () => pane.toggle('projects');
  $('search-field').prepend(icon('search', 'sm'));
  $('search').addEventListener('input', debounce(drawSessions, 120));
  const more = h('button', { class: 'icon-btn', title: 'Ещё' }, icon('dots'));
  more.addEventListener('click', () => openMenu(more, [
    { label: 'Перезапустить агента', sub: 'Hermes на этом компьютере', value: 'restart', icon: 'refresh' },
    { label: 'Журнал окна', value: 'logs', icon: 'fileText' },
    { label: 'Папка SYSAI', sub: S.info.home || '', value: 'home', icon: 'folder' },
    { sep: true },
    { label: `Окно ${S.info.version || ''}`, sub: `Electron ${S.info.electron || ''} · Hermes ${(S.acp.agentInfo && S.acp.agentInfo.version) || '—'}`, value: 'about', icon: 'info' },
  ], { above: true, onPick: (v) => {
    if (v === 'restart') restartAcp();
    else if (v === 'logs') api.win.logs().then((p) => openPath(p)).catch((e) => toast(e.message, 'err'));
    else if (v === 'home') api.shell.open(S.info.home).catch((e) => toast(e.message, 'err'));
  } }));
  $('side-foot').replaceChildren(h('span', { class: 'avatar', text: initials(S.info.person, S.info.agent) }),
    h('div', { class: 'who' }, h('b', { text: S.info.person || S.info.agent || 'Агент SYSAI' }),
      h('span', { text: S.info.agent ? `агент ${S.info.agent}` : 'компьютер не подключён' })), more);
}

function drawSessions() {
  const el = $('sessions');
  const q = $('search').value.trim().toLowerCase();
  const items = S.sessions.filter((s) => !q || String(s.title || '').toLowerCase().includes(q)
    || ((projectOfSession(s.sessionId) || {}).name || '').toLowerCase().includes(q));
  clear(el);
  if (!items.length) {
    el.append(h('div', { class: 's-empty', text: q ? 'Ничего не нашлось.' : S.listError ? `Список разговоров не загрузился: ${S.listError}`
      : S.acp.state !== 'ready' ? 'Разговоры появятся, когда агент запустится.' : 'Здесь будут ваши разговоры с агентом.' }));
    return;
  }
  let last = '';
  for (const s of items) {
    const g = group(s.updatedAt);
    if (g !== last) { el.append(h('div', { class: 's-group', text: g })); last = g; }
    const conv = S.convs.get(s.sessionId);
    const busy = conv && (conv.running || conv.waiting);
    const title = (conv && conv.title) || s.title || 'Без названия';
    const project = projectOfSession(s.sessionId);
    el.append(h('button', { class: `s-item${s.sessionId === S.current ? ' on' : ''}${conv && conv.waiting ? ' waits' : ''}`,
      title: project ? `${title}\nПроект: ${project.name}` : title, onclick: () => selectConv(s.sessionId) },
    h('span', { class: 't' }, project ? h('i', { class: 's-project', text: project.name }) : null, title), busy ? h('span', { class: 'busy', title: conv.waiting ? 'Ждёт вашего ответа' : 'Работает' })
      : h('span', { class: 'when', text: when(s.updatedAt, { short: true }) })));
  }
  if (S.cursor && !q) el.append(h('button', { class: 's-more', text: 'Показать ещё', onclick: () => loadSessions(true) }));
}

function drawTop() {
  const conv = current();
  const s = conv && S.sessions.find((x) => x.sessionId === conv.id);
  const project = conv && projectOf(conv.cwd || (s && s.cwd));
  $('top-title').textContent = conv ? `${project ? `${project.name} · ` : ''}${conv.title || (s && s.title) || (conv.draft ? 'Новый разговор' : 'Разговор')}` : '';
  const acts = $('top-actions');
  acts.replaceChildren(h('button', { class: `icon-btn${pane && pane.opened ? ' on' : ''}`, title: 'Файлы и просмотр (Ctrl+B)',
    onclick: () => pane.toggle() }, icon('panel')));
}

function bootScreen() {
  const st = S.acp;
  const tile = h('span', { class: `tile${st.state === 'starting' ? ' live' : ''}` }, h('i'), h('i'), h('i'));
  if (!S.info.connected) {
    return h('div', { class: 'boot' }, tile, h('h2', { text: 'Компьютер не подключён к SYSAI' }),
      h('div', { text: 'Запустите установщик со страницы агента в SYSAI (Сеть агентов → агент → «Подключить ПК») — он подключит этот компьютер, и здесь появится ваш агент.' }));
  }
  if (st.state === 'starting' || !st.state) {
    return h('div', { class: 'boot' }, tile, h('h2', { text: 'Запускаю агента…' }),
      h('div', { text: st.message || 'Hermes поднимается на этом компьютере — обычно это несколько секунд.' }));
  }
  const title = st.state === 'missing' ? 'Hermes не найден' : st.state === 'stopped' ? 'Агент остановился' : 'Агент не запустился';
  const acts = h('div', { class: 'boot-acts' },
    h('button', { class: 'btn primary', onclick: restartAcp }, icon('refresh', 'sm'), 'Перезапустить'),
    h('button', { class: 'btn outline', onclick: () => api.win.logs().then((p) => openPath(p)).catch((e) => toast(e.message, 'err')) }, icon('fileText', 'sm'), 'Журнал'));
  return h('div', { class: 'boot' }, tile, h('h2', { text: title }), h('div', { class: 'selectable', text: st.message || '' }),
    st.stderr ? h('pre', { text: st.stderr }) : null, acts);
}

function emptyState() {
  const name = firstName(S.info.person);
  return h('div', { class: 'empty' }, h('span', { class: 'tile' }, h('i'), h('i'), h('i')),
    h('h1', {}, 'Агент ', h('span', { class: 's', text: 'SYS' }), h('span', { class: 'a', text: 'AI' })),
    h('p', { text: `${name ? `${name}, чем` : 'Чем'} помочь? Работаю на этом компьютере: файлы, программы, Хранилище SYSAI.` }),
    h('div', { class: 'suggest' }, SUGGEST.map(([ico, text]) => h('button', { onclick: () => composer.fill(text) }, icon(ico, 'sm'), h('span', { text })))));
}

function drawThread(error = '') {
  const thread = $('thread');
  const conv = current();
  let content;
  if (!S.info.connected || (S.acp.state !== 'ready' && (!conv || conv.empty))) content = bootScreen();
  else if (error && !conv) content = h('div', { class: 'boot' }, h('h2', { text: 'Что-то пошло не так' }), h('div', { text: error }),
    h('button', { class: 'btn primary', onclick: () => newChat() }, 'Попробовать снова'));
  else if (!conv) content = h('div', { class: 'boot' }, h('span', { class: 'spinner' }));
  else if (conv.loading && conv.empty) content = h('div', { class: 'boot' }, h('span', { class: 'spinner' }), h('div', { text: 'Открываю разговор…' }));
  else if (conv.empty && conv.draft) content = emptyState();
  else content = conv.root;
  if (thread.firstChild !== content || thread.childElementCount !== 1) thread.replaceChildren(content);
  if (conv && content === conv.root && conv.scroll !== undefined && !S.stuck) thread.scrollTop = conv.scroll;
  onScroll();
}

function drawStatus() {
  const bar = $('statusbar');
  const st = S.acp.state || 'stopped';
  const conv = current();
  const running = [...S.convs.values()].some((c) => c.running);
  const label = !S.info.connected ? 'Не подключён к SYSAI' : { ready: running ? 'Агент работает' : 'Агент готов',
    starting: 'Агент запускается…', stopped: 'Агент остановлен', error: 'Агент не запустился', missing: 'Hermes не найден' }[st] || st;
  const state = h('button', { class: 'item', title: S.acp.message || 'Hermes по ACP на этом компьютере' },
    h('span', { class: `dot ${st === 'ready' && running ? 'busy' : st}` }), h('span', { text: label }));
  state.addEventListener('click', () => openMenu(state, [
    { header: S.acp.agentInfo ? `Hermes ${S.acp.agentInfo.version || ''}` : 'Hermes' },
    { label: 'Перезапустить агента', value: 'restart', icon: 'refresh' },
    { label: 'Журнал окна', value: 'logs', icon: 'fileText' },
  ], { above: true, onPick: (v) => (v === 'restart' ? restartAcp() : api.win.logs().then((p) => openPath(p))) }));
  const items = [state];
  if (S.info.agent) items.push(h('span', { class: 'item', title: 'Агент SYSAI этого компьютера' }, icon('users', 'sm'), h('span', { text: S.info.agent })));
  const model = conv && (conv.config || []).find((o) => o.id === 'model');
  if (model) {
    const all = [];
    for (const o of model.options || []) { if (Array.isArray(o.options)) all.push(...o.options); else all.push(o); }
    const cur = all.find((o) => o.value === model.currentValue);
    items.push(h('span', { class: 'item', title: 'Модель разговора' }, icon('cube', 'sm'), h('span', { text: (cur && cur.name) || model.currentValue || '' })));
  }
  if (conv && conv.usage && conv.usage.size) {
    const pct = Math.max(0, Math.min(100, Math.round((conv.usage.used / conv.usage.size) * 100)));
    const k = (n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
    items.push(h('span', { class: 'item', title: `Контекст: ${conv.usage.used} из ${conv.usage.size} токенов` },
      h('span', { class: 'meter' }, h('i', { style: { width: `${pct}%` } })), h('span', { text: `${k(conv.usage.used)} / ${k(conv.usage.size)}` })));
  }
  items.push(h('span', { class: 'grow' }));
  if (S.info.server) {
    let host = S.info.server;
    try { host = new URL(S.info.server).host; } catch { /* as is */ }
    items.push(h('span', { class: 'item', title: `Сервер SYSAI: ${S.info.server}` }, icon('cloud', 'sm'), h('span', { text: host })));
  }
  items.push(h('span', { class: 'item', title: 'Версия окна' }, h('span', { text: `окно ${S.info.version || ''}` })));
  bar.replaceChildren(...items);
}

// ---------------------------------------------------------------------------------------------- scrolling
function saveScroll() {
  const conv = current();
  if (conv) conv.scroll = $('thread').scrollTop;
}

function stick() {
  if (!S.stuck) { onScroll(); return; }
  const thread = $('thread');
  requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
}

function onScroll() {
  const thread = $('thread');
  const gap = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
  S.stuck = gap < 80;
  $('to-bottom').hidden = S.stuck || !current() || current().empty;
  $('topbar').classList.toggle('scrolled', thread.scrollTop > 4);
}

// ---------------------------------------------------------------------------------------------- the rest
function linkClicks(e) {
  const copy = e.target.closest('[data-copy]');
  if (copy) {
    const code = copy.closest('pre') && copy.closest('pre').querySelector('code');
    if (code) api.clipboard.write(code.textContent).then(() => toast('Код скопирован', 'ok', 1500));
    return;
  }
  const p = e.target.closest('[data-path]');
  if (p) {
    e.preventDefault();
    openPath(p.dataset.path);
    return;
  }
  const a = e.target.closest('a[href]');
  if (a) {
    e.preventDefault();
    const href = a.getAttribute('href') || '';
    if (/^(https?:|mailto:)/i.test(href)) api.shell.external(href).catch((err) => toast(err.message, 'err'));
  }
}

async function restartAcp() {
  try {
    await api.acp.restart();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function resizer(el, { varName, min, max, from, key }) {
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
    document.body.classList.add('resizing');
    let w = 0;
    const move = (ev) => {
      w = Math.round(Math.max(min, Math.min(max(), from(ev))));
      document.documentElement.style.setProperty(varName, `${w}px`);
    };
    const up = () => {
      el.classList.remove('dragging');
      document.body.classList.remove('resizing');
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      if (w) localStorage.setItem(key, String(w));
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });
}

function wire() {
  const thread = $('thread');
  thread.addEventListener('scroll', onScroll, { passive: true });
  thread.addEventListener('click', linkClicks);
  $('to-bottom').append(icon('chevD'));
  $('to-bottom').addEventListener('click', () => { S.stuck = true; stick(); });
  const side = Number(localStorage.getItem('sysai.side.w') || 0);
  if (side) document.documentElement.style.setProperty('--side-w', `${side}px`);
  resizer($('side-resizer'), { varName: '--side-w', min: 200, max: () => 420, from: (e) => e.clientX, key: 'sysai.side.w' });
  resizer($('pane-resizer'), { varName: '--pane-w', min: 320, max: () => Math.max(320, window.innerWidth - $('sidebar').offsetWidth - 380),
    from: (e) => window.innerWidth - e.clientX, key: 'sysai.pane.w' });
  document.addEventListener('keydown', (e) => {
    const k = e.key.toLowerCase();
    if (e.ctrlKey && !e.shiftKey && !e.altKey && (k === 'n' || k === 'т')) { e.preventDefault(); newChat(); }
    else if (e.ctrlKey && !e.shiftKey && (k === 'b' || k === 'и')) { e.preventDefault(); pane.toggle(); }
    else if (e.ctrlKey && (k === 'k' || k === 'л')) { e.preventDefault(); $('search').focus(); }
    else if (e.ctrlKey && (k === 'l' || k === 'д')) { e.preventDefault(); composer.focus(); }
    else if (e.key === 'Escape' && !document.querySelector('.menu') && current() && current().running &&
             (document.activeElement === document.body || document.activeElement === composer.input)) { e.preventDefault(); stop(); }
  });
  // files dropped anywhere become attachments; the page itself never navigates to a dropped file
  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  document.addEventListener('dragenter', (e) => { if (hasFiles(e)) { depth++; composer.box.classList.add('drop'); } });
  document.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) composer.box.classList.remove('drop'); });
  document.addEventListener('dragover', (e) => { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = hasFiles(e) ? 'copy' : 'none'; });
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    composer.box.classList.remove('drop');
    const paths = [...((e.dataTransfer && e.dataTransfer.files) || [])].map((f) => api.pathForFile(f)).filter(Boolean);
    if (paths.length) composer.attachMany(paths);
  });
}

async function main() {
  S.info = await api.info().catch(() => ({}));
  document.title = S.info.agent ? `Агент SYSAI — ${S.info.agent}` : 'Агент SYSAI';
  S.auto = await api.auto.get().catch(() => ({ on: false, count: 0 }));
  composer = new Composer({ send, stop, setConfig, auto: () => S.auto, setAuto, undoEdit, recentEdits: () => api.auto.recent(),
    project: () => { const c = current(); const p = c && projectOf(c.cwd); return p ? p.name : ''; }, ready: () => S.acp.state === 'ready' && Boolean(current()),
    idle: () => (!S.info.connected ? 'Компьютер не подключён к SYSAI' : S.acp.state === 'ready' ? 'Открываю разговор…'
      : !S.acp.state || S.acp.state === 'starting' ? 'Агент запускается…' : 'Агент не запущен — нажмите «Перезапустить»') });
  $('composer-wrap').append(composer.el);
  pane = new Pane();
  drawSidebar();
  drawTop();
  wire();
  api.acp.onState(onAcpState);
  api.acp.onNotification(onNotification);
  api.acp.onRequest(onRequest);
  api.acp.onRequestGone(onRequestGone);
  api.auto.onState((state) => { S.auto = state; composer.update(); });
  api.auto.onToggle(() => setAuto(!S.auto.on));
  api.win.onShow(({ path, line }) => openPath(path, line));
  api.win.onNotice(({ text, kind }) => toast(text, kind || 'info', 15000));
  const reopen = localStorage.getItem('sysai.pane.open');
  if (reopen === 'files' || reopen === 'storage' || reopen === 'canvas') pane.show(reopen);
  onAcpState(await api.acp.state().catch(() => ({ state: 'error', message: 'нет связи с основным процессом окна' })));
  api.ready();
}

main().catch((err) => {
  document.body.replaceChildren(h('div', { class: 'boot' }, h('h2', { text: 'Окно не запустилось' }), h('pre', { text: err.stack || String(err) })));
});
