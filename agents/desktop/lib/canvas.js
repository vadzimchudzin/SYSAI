'use strict';
// «Холст» справа от разговора: окно проекта EasyBuilder Pro (.cmtp/.emtp) так, как его покажет панель, — его правят
// вместе человек, агент и помощники агента и видят друг друга. Проект читает и пишет инструмент хаба ebpro-cmtp
// (HOME/tools/ebpro-cmtp, его ставит синхронизатор), запущенный здесь своим MCP-сервером: cmtp_windows, cmtp_scene,
// cmtp_edit. Правка пишется прямо в файл, по одной, после копии для «Отменить» (cache/canvas/undo); изменение снаружи
// (EasyBuilder сохранил проект, агент сам вызвал cmtp_edit) перерисовывает холст. У каждого на холсте свой курсор:
// курсор агента и каждого помощника идёт к тому, что он правит, и говорит, что он делает; куда показывает и что
// выделил человек, агент узнаёт через canvas_look.
const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const config = require('./config');

const TOOL = 'ebpro-cmtp';
const EXT = /\.(cmtp|emtp)$/i;
const OPS = ['text', 'move', 'resize', 'tag', 'rename_window', 'clone_window', 'clone_object', 'delete_object',
  'delete_window', 'link', 'add_tags', 'import_window', 'import_objects', 'style', 'bind_tag'];
const WINDOW_OPS = new Set(['rename_window', 'clone_window', 'delete_window', 'import_window']);
const DONOR_OPS = new Set(['import_window', 'import_objects']);
const PERSON = 'Человек';
const AGENT = 'Агент';
const COLORS = ['#f0a63a', '#4cc3ff', '#b78cff', '#5fd38d', '#ff7a9c', '#e8d45a', '#7ee0d2', '#ff9a5c'];
const MAX_EDITS = 50;
const UNDO = 8;
const TIMEOUT = 120000;
const NO_TOOL = 'инструмента ebpro-cmtp нет на этом компьютере: включите его агенту в SYSAI (Хаб → Инструменты), '
  + 'синхронизатор поставит его сам';

const clip = (s, n) => {
  const t = String(s ?? '').replace(/\s+/g, ' ').trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
};
const int = (v) => (v === undefined || v === null || v === '' ? null : Number.isInteger(Number(v)) ? Number(v) : NaN);

// Как запустить ebpro-cmtp: Python инструмента (своё окружение), иначе Python синхронизатора (desktop.json), иначе
// тот, что найдётся в PATH. null — инструмента нет.
function toolSpec(home) {
  const dir = path.join(home, 'tools', TOOL);
  const server = path.join(dir, 'server.py');
  if (!fs.existsSync(server)) return null;
  const state = config.readJson(path.join(home, 'state.json')) || {};
  const desk = config.readJson(path.join(home, 'desktop', 'desktop.json')) || {};
  const own = ((state.tools || {})[TOOL] || {}).python;
  const python = [own, desk.python].find((p) => typeof p === 'string' && p && fs.existsSync(p))
    || (config.WIN ? 'python' : 'python3');
  return { command: python, args: [server], cwd: dir };
}

// ------------------------------------------------------------------------------------------------- the tool
// Простой клиент MCP по stdio: JSON-RPC 2.0, сообщение на строку; инструмент запускается при первом вызове и снова —
// если остановился.
class Tool {
  constructor(spec, log = () => {}) {
    this.spec = spec;
    this.log = log;
    this.child = null;
    this.starting = null;
    this.seq = 0;
    this.waiting = new Map();
    this.stderr = '';
  }

  _start() {
    const { command, args = [], cwd, env } = this.spec;
    const childEnv = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', ...(env || {}) };
    delete childEnv.ELECTRON_RUN_AS_NODE;
    if (env && env.ELECTRON_RUN_AS_NODE) childEnv.ELECTRON_RUN_AS_NODE = env.ELECTRON_RUN_AS_NODE;
    const child = spawn(command, args, { cwd, env: childEnv, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    this.child = child;
    this.stderr = '';
    let buf = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      buf += chunk;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line) continue;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          this.log(`${TOOL} (stdout):`, line.slice(0, 200));
          continue;
        }
        const w = this.waiting.get(msg.id);
        if (!w) continue;
        this.waiting.delete(msg.id);
        clearTimeout(w.timer);
        if (msg.error) w.reject(new Error(msg.error.message || `ошибка ${TOOL}`));
        else w.resolve(msg.result);
      }
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (c) => { this.stderr = (this.stderr + c).slice(-6000); });
    child.stdin.on('error', () => {});
    const gone = (why) => {
      if (this.child !== child) return;
      this.child = null;
      this.starting = null;
      for (const w of this.waiting.values()) {
        clearTimeout(w.timer);
        w.reject(new Error(why));
      }
      this.waiting.clear();
    };
    child.on('error', (err) => gone(err.code === 'ENOENT' ? `не найден Python (${command}) — нужен Python синхронизатора SYSAI: `
      + 'обновите синхронизатор (sysai-agent update)' : err.message));
    child.on('exit', (code, signal) => gone(this._why(code ?? signal)));
  }

  _why(code) {
    const err = this.stderr;
    if (/No module named '?cryptography/.test(err)) {
      return `у Python, которым запущен ${TOOL}, нет модуля cryptography — обновите синхронизатор SYSAI (sysai-agent update): `
        + 'окно возьмёт его Python';
    }
    const last = err.trim().split(/\r?\n/).filter((l) => l.trim()).slice(-1)[0] || '';
    return `${TOOL} остановился (код ${code})${last ? `: ${last.slice(0, 300)}` : ''}`;
  }

  request(method, params, timeout = TIMEOUT) {
    const child = this.child;
    if (!child) return Promise.reject(new Error(`${TOOL} не запущен`));
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = timeout ? setTimeout(() => {
        this.waiting.delete(id);
        reject(new Error(`${TOOL} не ответил вовремя`));
      }, timeout) : null;
      this.waiting.set(id, { resolve, reject, timer });
      try {
        child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
      } catch (err) {
        this.waiting.delete(id);
        clearTimeout(timer);
        reject(err);
      }
    });
  }

  ready() {
    if (!this.starting) {
      this._start();
      const child = this.child;
      this.starting = this.request('initialize', { protocolVersion: '2025-06-18', capabilities: {},
        clientInfo: { name: 'sysai-canvas', title: 'Холст окна «Агент SYSAI»', version: '1' } }, 30000).then(() => {
        try {
          child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);
        } catch { /* gone */ }
      });
      this.starting.catch(() => {
        if (this.child === child) this.stop();
      });
    }
    return this.starting;
  }

  async call(name, args, timeout = TIMEOUT) {
    await this.ready();
    const res = await this.request('tools/call', { name, arguments: args }, timeout);
    const text = ((res && res.content) || []).filter((c) => c && c.type === 'text').map((c) => c.text).join('\n');
    if (!res || res.isError) throw new Error(text || `${name} не выполнен`);
    return { text, data: res.structuredContent || null };
  }

  stop() {
    const child = this.child;
    this.child = null;
    this.starting = null;
    for (const w of this.waiting.values()) {
      clearTimeout(w.timer);
      w.reject(new Error(`${TOOL} остановлен`));
    }
    this.waiting.clear();
    if (!child) return;
    try {
      child.stdin.end();
    } catch { /* gone */ }
    setTimeout(() => {
      try {
        child.kill();
      } catch { /* gone */ }
    }, 1500).unref();
  }
}

// ------------------------------------------------------------------------------------------------- edits
// Правки — как у cmtp_edit; окно по умолчанию — то, что на холсте.
function normalize(edits, current) {
  if (!Array.isArray(edits) || !edits.length) throw new Error('нужен список правок (edits)');
  if (edits.length > MAX_EDITS) throw new Error(`за раз — не больше ${MAX_EDITS} правок`);
  return edits.map((e, n) => {
    if (!e || typeof e !== 'object' || !OPS.includes(e.op)) throw new Error(`правка ${n + 1}: op — одно из ${OPS.join(', ')}`);
    const donor = DONOR_OPS.has(e.op);
    if (donor && (!e.from || e.window === undefined || e.window === null || e.window === '')) {
      throw new Error(`правка ${n + 1}: ${e.op} — нужен проект-донор (from) и его окно (window)`);
    }
    const win = e.window ?? current;
    if (e.op !== 'add_tags' && (win === undefined || win === null || win === '')) throw new Error(`правка ${n + 1}: какое окно (window)?`);
    const out = { op: e.op };
    if (e.op !== 'add_tags') out.window = String(win);
    for (const k of ['object', 'dx', 'dy', 'number', 'width', 'height', 'frame_width', 'font_size', 'label', 'slot']) {
      const v = int(e[k]);
      if (Number.isNaN(v)) throw new Error(`правка ${n + 1}: ${k} — целое число`);
      if (v !== null) out[k] = v;
    }
    for (const k of ['old', 'new', 'to', 'from']) if (e[k] !== undefined && e[k] !== null) out[k] = String(e[k]);
    for (const k of ['fill', 'frame', 'text_color']) if (Object.hasOwn(e, k)) out[k] = e[k] === null ? null : String(e[k]);
    if (e.op === 'import_objects' && e.to === undefined) out.to = String(current);
    if (Array.isArray(e.objects)) {
      out.objects = e.objects.map((v) => int(v));
      if (out.objects.some((v) => !Number.isInteger(v))) throw new Error(`правка ${n + 1}: objects — номера объектов`);
    }
    if (e.op === 'add_tags') {
      if (!Array.isArray(e.tags) || !e.tags.length) throw new Error(`правка ${n + 1}: add_tags — список tags [{name, type}]`);
      out.tags = e.tags.map((t) => {
        const tag = { name: String((t && t.name) || ''), type: String((t && t.type) || '') };
        if (t && t.node) tag.node = String(t.node);
        return tag;
      });
    }
    for (const k of ['map_tags', 'map_windows', 'map_devices']) {
      if (e[k] && typeof e[k] === 'object' && !Array.isArray(e[k])) {
        out[k] = Object.fromEntries(Object.entries(e[k]).map(([a, b]) => [String(a), String(b)]));
      }
    }
    return out;
  });
}

function describe(edits) {
  const one = (e) => {
    const obj = Number.isInteger(e.object) ? ` #${e.object}` : '';
    if (e.op === 'text') return `текст${obj} «${clip(e.old, 30)}» → «${clip(e.new, 30)}»`;
    if (e.op === 'move') return `сдвиг${obj} на (${e.dx || 0}, ${e.dy || 0})`;
    if (e.op === 'style') return `оформление${obj}`;
    if (e.op === 'bind_tag') return `адрес${obj} слот ${e.slot} → ${clip(e.new, 40)}`;
    if (e.op === 'tag') return `тег${obj} ${clip(e.old, 40)} → ${clip(e.new, 40)}`;
    if (e.op === 'rename_window') return `имя окна ${e.window} → «${clip(e.new, 40)}»`;
    if (e.op === 'clone_window') return `копия окна ${e.window}${e.new ? ` «${clip(e.new, 40)}»` : ''}`;
    if (e.op === 'resize') return `размер${obj} → ${e.width ?? '…'}×${e.height ?? '…'}`;
    if (e.op === 'delete_object') return `удаление${obj}`;
    if (e.op === 'delete_window') return `удаление окна ${e.window}`;
    if (e.op === 'link') return `кнопка${obj} → окно ${e.to}`;
    if (e.op === 'add_tags') return `теги: ${(e.tags || []).slice(0, 3).map((t) => t.name).join(', ')}${(e.tags || []).length > 3 ? '…' : ''}`;
    if (e.op === 'import_window') return `окно донора «${clip(e.window, 40)}»${e.new ? ` → «${clip(e.new, 40)}»` : ''}`;
    if (e.op === 'import_objects') return `объекты донора ${(e.objects || [e.object]).map((i) => `#${i}`).join(', ')} → окно ${e.to}`;
    return `копия${obj}${e.to ? ` в окно ${e.to}` : ''}`;
  };
  const moves = edits.filter((e) => e.op === 'move');
  if (moves.length > 1 && moves.length === edits.length && moves.every((e) => e.dx === moves[0].dx && e.dy === moves[0].dy)) {
    return `сдвиг ${moves.map((e) => `#${e.object}`).join(', ')} на (${moves[0].dx || 0}, ${moves[0].dy || 0})`;
  }
  const head = edits.slice(0, 3).map(one).join('; ');
  return edits.length > 3 ? `${head} и ещё ${edits.length - 3}` : head;
}

function center(bbox) {
  return [Math.round((bbox[0] + bbox[2]) / 2), Math.round((bbox[1] + bbox[3]) / 2)];
}

function labelText(o) {
  const l = (o.labels || []).find((x) => x && String(x.text || '').trim());
  return l ? clip(l.text, 80) : '';
}

// Объект коротко — для агента.
function brief(o) {
  if (!o) return null;
  const out = { index: o.index, kind: o.kind, bbox: o.bbox };
  const text = labelText(o);
  if (text) out.text = text;
  const tags = [...new Set(o.tags || [])].slice(0, 4);
  if (tags.length) out.tags = tags;
  return out;
}

function topmost(scene, x, y) {
  const objs = (scene && scene.objects) || [];
  for (let i = objs.length - 1; i >= 0; i--) {
    const o = objs[i];
    const [l, t, r, b] = o.bbox || [];
    if (o.kind !== 'Window' && x >= l && x <= r && y >= t && y <= b) return o;
  }
  return null;
}

// ------------------------------------------------------------------------------------------------- the canvas
class Canvas extends EventEmitter {
  constructor({ home, cache, tool = null, log = () => {}, undo = UNDO, watch = 1500 } = {}) {
    super();
    this.home = home;
    this.cache = cache;
    this.spec = tool; // для проверок: свой {command, args, cwd}
    this.log = log;
    this.undoMax = undo;
    this.watchMs = watch;
    this.tool = null;
    this.file = '';
    this.windows = [];
    this.window = 0;
    this.scene = null;
    this.version = 0;
    this.error = '';
    this.busy = '';
    this.cursors = new Map(); // кто → {who, color, x, y, say, at}
    this.colors = new Map();
    this.person = { x: null, y: null, selected: [], at: 0 };
    this.history = []; // {who, text, window, objects, at}
    this.undos = []; // {file, who, text}
    this.known = null; // {mtimeMs, size} файла, каким его прочли или записали здесь
    this.watched = '';
    this.watcher = null;
    this.chain = Promise.resolve();
    this.recentFile = cache ? path.join(cache, 'recent.json') : '';
    this.recent = (this.recentFile && config.readJson(this.recentFile)) || {};
    if (cache) fsp.rm(path.join(cache, 'undo'), { recursive: true, force: true }).catch(() => {});
  }

  // --- what the window shows
  state() {
    return { file: this.file, name: this.file ? path.basename(this.file) : '', windows: this.windows, window: this.window,
      scene: this.scene, version: this.version, cursors: [...this.cursors.values()], history: this.history.slice(-20),
      undo: this.undos.length, busy: this.busy, error: this.error };
  }

  brief() {
    const s = this.scene;
    if (!this.file || !s) return { open: false };
    return { open: true, file: this.file, window: { number: s.window.number, name: s.window.name, width: s.window.width,
      height: s.window.height }, objects: s.objects.filter((o) => o.kind !== 'Window').length, windows: this.windows.length };
  }

  // Что на холсте — для агента: окно, объекты коротко, куда показывает и что выделил человек, последние правки, курсоры.
  look() {
    const s = this.scene;
    if (!this.file || !s) return { open: false, hint: 'на холсте нет проекта — откройте его (canvas_open)' };
    const now = Date.now();
    const p = this.person;
    const at = p.x !== null && p.y !== null;
    const over = at ? topmost(s, p.x, p.y) : null;
    return {
      open: true, file: this.file, window: s.window,
      windows: this.windows.slice(0, 400).map((w) => ({ number: w.number, name: w.name })),
      objects: s.objects.filter((o) => o.kind !== 'Window').slice(0, 400).map(brief),
      person: { pointer: at ? { x: p.x, y: p.y } : null, over: brief(over),
        selected: p.selected.map((i) => brief(s.objects[i])).filter(Boolean),
        seconds_ago: p.at ? Math.round((now - p.at) / 1000) : null },
      recent: this.history.slice(-10).map((x) => ({ who: x.who, text: x.text, window: x.window, objects: x.objects,
        seconds_ago: Math.round((now - x.at) / 1000) })),
      cursors: [...this.cursors.values()].map(({ who, x, y, say }) => ({ who, x, y, say })),
      undo: this.undos.length,
    };
  }

  // --- opening
  open(file, window) {
    return this._serial(async () => {
      const p = path.resolve(String(file || '').trim() || '.');
      if (!EXT.test(p)) throw new Error('на холсте открываются проекты EasyBuilder Pro: .cmtp или .emtp');
      const st = await fsp.stat(p).catch(() => null);
      if (!st || !st.isFile()) throw new Error(`нет такого файла: ${p}`);
      const same = p === this.file;
      const windows = await this._list(p);
      if (!same) {
        this._unwatch();
        await this._dropUndo();
        this.history = [];
        this.person = { x: null, y: null, selected: [], at: 0 };
      }
      this.file = p;
      this.windows = windows;
      const ref = window ?? (same ? this.window : this.recent[p]);
      this.window = this._pick(ref, window !== undefined && window !== null && window !== '');
      await this._scene();
      this._remember();
      this._watch();
      this._emit();
      return this.brief();
    });
  }

  show(window) {
    return this._serial(async () => {
      if (!this.file) throw new Error('на холсте нет проекта');
      this.window = this._pick(window, true);
      await this._scene();
      this._remember();
      this._emit();
      return this.brief();
    });
  }

  reload() {
    return this._serial(async () => {
      if (!this.file) throw new Error('на холсте нет проекта');
      this.windows = await this._list(this.file);
      this.window = this._pick(this.window, false);
      await this._scene();
      this._emit();
      return this.brief();
    });
  }

  close() {
    return this._serial(async () => {
      this._unwatch();
      await this._dropUndo();
      this.file = '';
      this.windows = [];
      this.window = 0;
      this.scene = null;
      this.history = [];
      this.cursors.clear();
      this.error = '';
      this.version++;
      this._emit();
      return true;
    });
  }

  stop() {
    this._unwatch();
    if (this.tool) this.tool.stop();
    this.tool = null;
  }

  // --- editing
  edit(who, edits) {
    const name = this.who(who);
    return this._serial(async () => {
      if (!this.file || !this.scene) throw new Error('на холсте нет проекта — откройте его (canvas_open)');
      const list = normalize(edits, this.window);
      const what = describe(list);
      const before = list.filter((e) => Number.isInteger(e.object) && !DONOR_OPS.has(e.op) && this._same(e.window))
        .map((e) => e.object);
      if (name !== PERSON) this._point(name, before, what);
      this.busy = `${name}: ${what}`;
      this._emit();
      const snap = await this._snapshot();
      let res;
      try {
        res = await this._tool().call('cmtp_edit', { path: this.file, out_file: this.file, edits: list });
      } catch (err) {
        await fsp.unlink(snap).catch(() => {});
        this.busy = '';
        this._emit();
        throw err;
      }
      this.undos.push({ file: snap, who: name, text: what });
      while (this.undos.length > this.undoMax) await fsp.unlink(this.undos.shift().file).catch(() => {});
      const results = (res.data && Array.isArray(res.data.results)) ? res.data.results : [];
      if (list.some((e) => WINDOW_OPS.has(e.op))) this.windows = await this._list(this.file);
      const NEW = ['clone_window', 'clone_object', 'import_window', 'import_objects'];
      const target = [...results].reverse().find((r) => NEW.includes(r.op));
      const shown = results.some((r) => r.window === this.window && r.op !== 'delete_window');
      const whole = target && (target.op === 'clone_window' || target.op === 'import_window');
      if (target && Number.isInteger(target.window) && target.window !== this.window && (whole || !shown)) {
        this.window = target.window;
      } else if (!shown && results.length && Number.isInteger(results[results.length - 1].window)) {
        this.window = results[results.length - 1].window; // правили другое окно — показываем его
      }
      if (this.windows.length && !this.windows.some((w) => w.number === this.window)) { // окно удалили — соседнее
        const rest = this.windows.map((w) => w.number);
        this.window = rest.find((x) => x > this.window) ?? rest.filter((x) => x < this.window).pop() ?? rest[0];
      }
      this.busy = '';
      await this._scene();
      this._remember();
      const objects = [...new Set(results.filter((r) => r.window === this.window && Number.isInteger(r.object)).map((r) => r.object))];
      this._note(name, what, objects);
      if (name !== PERSON) this._point(name, objects, what);
      this.emit('edited', { who: name, color: this._color(name), window: this.window, objects, text: what });
      this._emit();
      return { text: res.text, results, window: this.window, objects, undo: this.undos.length };
    });
  }

  undo() {
    return this._serial(async () => {
      const last = this.undos.pop();
      if (!last || !this.file) throw new Error('отменять нечего');
      const tmp = `${this.file}.sysai-undo`;
      await fsp.copyFile(last.file, tmp);
      await fsp.rename(tmp, this.file);
      await fsp.unlink(last.file).catch(() => {});
      this.windows = await this._list(this.file);
      this.window = this._pick(this.window, false);
      await this._scene();
      this._note(PERSON, `отменено: ${last.text}`, []);
      this._emit();
      return { undone: last.text, left: this.undos.length };
    });
  }

  // --- cursors and the person's pointer
  cursor(who, { x, y, object, say } = {}) {
    const name = this.who(who);
    if (name === PERSON) throw new Error('курсор человека — его мышь');
    const s = this.scene;
    if (!s) throw new Error('на холсте нет проекта — откройте его (canvas_open)');
    let px = Number(x);
    let py = Number(y);
    const idx = int(object);
    if (idx !== null) {
      const o = Number.isInteger(idx) ? s.objects[idx] : null;
      if (!o) throw new Error(`в окне ${s.window.number} нет объекта #${object}`);
      [px, py] = center(o.bbox);
    }
    if (!Number.isFinite(px) || !Number.isFinite(py)) throw new Error('куда навести: object (index) или x и y в пикселях окна');
    const c = this._set(name, px, py, say);
    this._emitCursors();
    return c;
  }

  pointer({ x, y, selected } = {}) {
    const fx = Number(x);
    const fy = Number(y);
    const ok = x !== null && y !== null && Number.isFinite(fx) && Number.isFinite(fy);
    this.person = { x: ok ? Math.round(fx) : null, y: ok ? Math.round(fy) : null,
      selected: Array.isArray(selected) ? selected.filter(Number.isInteger).slice(0, 300) : this.person.selected, at: Date.now() };
  }

  // --- inside
  who(who) {
    const name = clip(who, 40) || AGENT;
    return name.toLowerCase() === PERSON.toLowerCase() ? PERSON : name;
  }

  _color(name) {
    if (!this.colors.has(name)) this.colors.set(name, COLORS[this.colors.size % COLORS.length]);
    return this.colors.get(name);
  }

  _set(name, x, y, say) {
    const s = this.scene;
    const w = s ? s.window.width : 10000;
    const h = s ? s.window.height : 10000;
    const c = { who: name, color: this._color(name), x: Math.max(0, Math.min(w, Math.round(x))), y: Math.max(0, Math.min(h, Math.round(y))),
      say: clip(say, 160), at: Date.now() };
    this.cursors.set(name, c);
    return c;
  }

  _point(name, objects, say) {
    const s = this.scene;
    const o = s && objects.map((i) => s.objects[i]).find(Boolean);
    const prev = this.cursors.get(name);
    if (o) this._set(name, ...center(o.bbox), say);
    else if (s) this._set(name, prev ? prev.x : Math.round(s.window.width / 2), prev ? prev.y : 24, say);
    this._emitCursors();
  }

  _note(who, text, objects) {
    this.history.push({ who, text, window: this.window, objects, at: Date.now() });
    if (this.history.length > 50) this.history.splice(0, this.history.length - 50);
  }

  _same(ref) {
    const w = this.windows.find((x) => String(x.number) === String(ref) || x.name === ref);
    return Boolean(w) && w.number === this.window;
  }

  _pick(ref, strict) {
    const ws = this.windows;
    if (ref !== undefined && ref !== null && ref !== '') {
      const s = String(ref).trim();
      const got = ws.find((w) => String(w.number) === s) || ws.find((w) => w.name === s)
        || ws.find((w) => w.name.toLowerCase() === s.toLowerCase())
        || ws.find((w) => w.name.toLowerCase().includes(s.toLowerCase()));
      if (got) return got.number;
      if (strict) throw new Error(`в проекте нет окна ${s}`);
    }
    return (ws.find((w) => w.number === 10) || ws[0]).number;
  }

  _tool() {
    if (!this.tool) {
      const spec = this.spec || toolSpec(this.home);
      if (!spec) throw new Error(NO_TOOL);
      this.tool = new Tool(spec, this.log);
    }
    return this.tool;
  }

  async _list(file) {
    const { text } = await this._tool().call('cmtp_windows', { path: file });
    const ws = (JSON.parse(text).windows || []).map((w) => ({ number: w.number, name: String(w.name || ''), width: w.width,
      height: w.height, objects: w.objects }));
    if (!ws.length) throw new Error('в проекте нет окон');
    return ws;
  }

  async _scene() {
    const dir = path.join(this.cache, 'img', crypto.createHash('sha1').update(this.file.toLowerCase()).digest('hex').slice(0, 12));
    let scene;
    try {
      const { text } = await this._tool().call('cmtp_scene', { path: this.file, window: String(this.window), images_dir: dir });
      scene = JSON.parse(text);
    } catch (err) {
      this.error = err.message;
      throw err;
    }
    const images = {};
    for (const [k, p] of Object.entries(scene.images || {})) images[k] = pathToFileURL(p).href;
    scene.images = images;
    this.scene = scene;
    this.window = scene.window.number;
    this.error = '';
    this.version++;
    try {
      const st = fs.statSync(this.file);
      this.known = { mtimeMs: st.mtimeMs, size: st.size };
    } catch { /* gone */ }
    for (const c of this.cursors.values()) {
      c.x = Math.min(c.x, scene.window.width);
      c.y = Math.min(c.y, scene.window.height);
    }
  }

  async _snapshot() {
    const dir = path.join(this.cache, 'undo');
    await fsp.mkdir(dir, { recursive: true });
    const p = path.join(dir, `${Date.now().toString(36)}-${crypto.randomBytes(3).toString('hex')}${path.extname(this.file)}`);
    await fsp.copyFile(this.file, p);
    return p;
  }

  async _dropUndo() {
    const list = this.undos;
    this.undos = [];
    await Promise.all(list.map((u) => fsp.unlink(u.file).catch(() => {})));
  }

  _remember() {
    if (!this.recentFile || !this.file) return;
    this.recent[this.file] = this.window;
    const keys = Object.keys(this.recent);
    for (const k of keys.slice(0, Math.max(0, keys.length - 50))) delete this.recent[k];
    try {
      config.writeJson(this.recentFile, this.recent);
    } catch { /* not kept */ }
  }

  _watch() {
    if (this.watched === this.file && this.watcher) return;
    this._unwatch();
    const file = this.file;
    this.watcher = (curr) => {
      const k = this.known;
      if (file !== this.file || !k || !curr.size || (curr.mtimeMs === k.mtimeMs && curr.size === k.size)) return;
      this._serial(async () => {
        const st = await fsp.stat(file).catch(() => null);
        const now = this.known;
        if (file !== this.file || !st || !st.size || (now && st.mtimeMs === now.mtimeMs && st.size === now.size)) return;
        this.windows = await this._list(file);
        this.window = this._pick(this.window, false);
        await this._scene();
        await this._dropUndo(); // копии — от прежнего файла: отмена стёрла бы изменение снаружи
        this._note('', 'проект изменён снаружи (EasyBuilder или инструмент агента)', []);
        this.emit('notice', { text: `Проект ${path.basename(file)} изменён снаружи — холст обновлён`, kind: 'info' });
        this._emit();
      }).catch((err) => {
        this.log('холст: не перечитал проект:', err.message);
        this._emit();
      });
    };
    fs.watchFile(file, { interval: this.watchMs }, this.watcher);
    this.watched = file;
  }

  _unwatch() {
    if (this.watcher) fs.unwatchFile(this.watched, this.watcher);
    this.watcher = null;
    this.watched = '';
  }

  _serial(fn) {
    const run = this.chain.then(() => fn());
    this.chain = run.catch(() => {});
    return run;
  }

  _emit() {
    this.emit('state', this.state());
  }

  _emitCursors() {
    this.emit('cursors', [...this.cursors.values()]);
  }
}

module.exports = { Canvas, Tool, toolSpec, normalize, describe, brief, topmost, PERSON, AGENT, OPS, TOOL };
