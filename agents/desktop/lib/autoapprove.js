'use strict';
// «Правки без вопросов»: Hermes asks the person before every write_file / patch (ACP session/request_permission,
// kind «edit», the answers are one-use). With the switch on, the window answers «allow once» itself — only for an
// edit of files inside a project folder («SYSAI Проекты/<проект>/…») that the signed rules of SYSAI open for
// writing. Everything else still comes to the person as a card, with the reason: files outside the projects, the
// engineer's originals (materials/), secrets, deletions and moves, big texts, commands, and anything this file
// could not read for sure. Off by default; the state is kept in the SYSAI folder, which the agent cannot write.
// Before the answer the old file is copied aside, and every edit allowed here is a line of the journal.
const fs = require('node:fs');
const nodePath = require('node:path');

const PROTECTED = ['materials']; // parts of a project the agent only reads
const SECRET_DIRS = new Set(['.git', '.ssh', '.sysai', '.gnupg', '.aws']);
const SECRET_NAME = /^(\.env(\..*)?|id_rsa.*|id_ed25519.*|id_ecdsa.*|.*\.(pem|key|pfx|p12|kdbx|ppk))$/i;
const MAX_TEXT = 1024 * 1024; // characters of the old or the new text of one file
const MAX_PER_HOUR = 300;
const KEEP_DAYS = 30;
const PATCH_FILE = /^\*\*\* (Update|Add|Delete) File:\s*(.+?)\s*$/gm;
const PATCH_MOVE = /^\*\*\* Move (?:to|File):/m;

class AutoApprove {
  // file — where the switch is kept; root — the folder of the projects; audit — the journal and the copies;
  // loadRules() → {rules} | {error} (lib/rules.js); guarded(path) → true for the places only SYSAI writes
  constructor({ file, root, audit, loadRules, pathAllowed, guarded = () => false, log = () => {}, path = nodePath, now = () => Date.now() }) {
    Object.assign(this, { file, audit, loadRules, pathAllowed, guarded, log, path, now });
    this.root = path.resolve(root);
    this.on = false;
    this.count = 0;
    this.recent = [];
    try {
      const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
      this.on = Boolean(saved && saved.on === true);
      this.count = this.on && Number.isInteger(saved.count) && saved.count > 0 ? saved.count : 0;
    } catch { /* never switched on, or a damaged file: off */ }
  }

  state() {
    return { on: this.on, count: this.count, root: this.root };
  }

  set(on) {
    this.on = Boolean(on);
    this.count = 0;
    this.recent = [];
    this._save();
    this.log(`правки без вопросов: ${this.on ? 'включены' : 'выключены'}`);
    return this.state();
  }

  _save() {
    try {
      fs.mkdirSync(this.path.dirname(this.file), { recursive: true });
      fs.writeFileSync(this.file, JSON.stringify({ version: 1, on: this.on, count: this.count, at: new Date(this.now()).toISOString() }));
    } catch (err) {
      this.log('правки без вопросов: состояние не сохранено:', err.message);
    }
  }

  // The files an edit touches, or null when it is not an edit this window understands for sure.
  _paths(params) {
    const tc = (params && params.toolCall) || {};
    if (tc.kind !== 'edit') return { why: 'это не правка файла' };
    const out = new Set();
    for (const c of Array.isArray(tc.content) ? tc.content : []) {
      if (!c || c.type !== 'diff') continue;
      if (typeof c.path !== 'string' || !c.path.trim()) return { why: 'в правке не назван файл' };
      if (String(c.oldText || '').length > MAX_TEXT || String(c.newText || '').length > MAX_TEXT) return { why: 'большой файл (больше 1 МБ текста)' };
      if (c.newText === undefined || c.newText === null) return { why: 'удаление файла' };
      out.add(c.path);
    }
    for (const l of Array.isArray(tc.locations) ? tc.locations : []) if (l && typeof l.path === 'string' && l.path.trim()) out.add(l.path);
    const raw = tc.rawInput && typeof tc.rawInput === 'object' ? tc.rawInput : {};
    const args = raw.arguments && typeof raw.arguments === 'object' ? raw.arguments : raw;
    if (typeof args.path === 'string' && args.path.trim()) out.add(args.path);
    const patch = typeof args.patch === 'string' ? args.patch : '';
    if (patch) {
      if (PATCH_MOVE.test(patch)) return { why: 'перенос файла' };
      for (const m of patch.matchAll(PATCH_FILE)) {
        if (m[1] === 'Delete') return { why: 'удаление файла' };
        out.add(m[2]);
      }
    }
    return out.size ? { paths: [...out] } : { why: 'в правке не назван файл' };
  }

  // Why this file is not edited without a question ("" — it may be).
  _refuse(target, rules) {
    const P = this.path;
    const name = P.basename(target);
    if (name.includes(':')) return 'необычное имя файла';
    const rel = P.relative(this.root, target);
    if (!rel || rel.startsWith('..') || P.isAbsolute(rel)) return 'файл вне папки проектов';
    const parts = rel.split(/[\\/]+/).filter(Boolean);
    if (parts.length < 2) return 'файл не в папке проекта';
    const low = parts.map((p) => p.toLowerCase());
    if (PROTECTED.includes(low[1])) return `исходные материалы (${parts[1]}/) агент только читает`;
    if (low.some((p) => SECRET_DIRS.has(p)) || SECRET_NAME.test(name)) return 'служебный файл или секрет';
    if (!this.pathAllowed(rules, target, true)) return 'папка не открыта агенту на запись (SYSAI → Доступы → Папки)';
    if (this.guarded(target)) return 'это место меняет только SYSAI';
    // a link or a junction inside the project must not lead out of the projects
    try {
      const root = fs.realpathSync.native(this.root);
      let near = P.dirname(target);
      while (!fs.existsSync(near) && P.dirname(near) !== near) near = P.dirname(near);
      const real = fs.realpathSync.native(fs.existsSync(target) ? target : near);
      const back = P.relative(root, real);
      if (back.startsWith('..') || P.isAbsolute(back)) return 'ссылка ведёт из папки проектов';
      if (fs.existsSync(target) && !fs.statSync(target).isFile()) return 'это не файл';
    } catch {
      return 'папку проектов не удалось проверить';
    }
    return '';
  }

  // {auto: true, optionId, paths} — the window answers itself; {auto: false, why} — the person is asked
  // (why — only while the switch is on: it is shown on the card). cwd — the folder of the session: Hermes counts
  // a path that is not full from it.
  decide(params, cwd = '') {
    if (!this.on) return { auto: false, why: '' };
    try {
      const got = this._paths(params);
      if (!got.paths) return { auto: false, why: got.why };
      const loaded = this.loadRules();
      if (!loaded || !loaded.rules) return { auto: false, why: 'нет подписанных доступов SYSAI на этом компьютере' };
      const full = [];
      for (const file of got.paths) {
        if (!this.path.isAbsolute(file) && !cwd) return { auto: false, why: `${file}: путь к файлу не полный` };
        const target = this.path.resolve(cwd || this.root, file);
        const why = this._refuse(target, loaded.rules);
        if (why) return { auto: false, why: `${this.path.basename(target)}: ${why}` };
        if (!full.includes(target)) full.push(target);
      }
      const allow = (Array.isArray(params.options) ? params.options : []).find((o) => o && o.kind === 'allow_once' && o.optionId);
      if (!allow) return { auto: false, why: 'агент не предложил разового разрешения' };
      const hour = this.now() - 3600000;
      this.recent = this.recent.filter((t) => t > hour);
      if (this.recent.length >= MAX_PER_HOUR) return { auto: false, why: `больше ${MAX_PER_HOUR} правок за час — проверьте, что агент не зациклился` };
      return { auto: true, optionId: allow.optionId, paths: full };
    } catch (err) {
      this.log('правки без вопросов: проверка не удалась:', err.message);
      return { auto: false, why: 'правку не удалось проверить' };
    }
  }

  // The old files are copied aside and the edit is written into the journal; false — it could not be done, so the
  // person is asked after all.
  allow(params, decision) {
    const stamp = new Date(this.now());
    const day = stamp.toISOString().slice(0, 10);
    try {
      const copies = [];
      decision.paths.forEach((file, i) => {
        if (!fs.existsSync(file)) return;
        const dir = this.path.join(this.audit, 'undo', day);
        fs.mkdirSync(dir, { recursive: true });
        const copy = this.path.join(dir, `${stamp.getTime().toString(36)}-${i}-${this.path.basename(file)}`);
        fs.copyFileSync(file, copy);
        copies.push(copy);
      });
      fs.mkdirSync(this.audit, { recursive: true });
      const tc = params.toolCall || {};
      fs.appendFileSync(this.path.join(this.audit, `${day.slice(0, 7)}.jsonl`), `${JSON.stringify({ at: stamp.toISOString(),
        session: params.sessionId || '', decision: 'auto', title: String(tc.title || '').slice(0, 300), paths: decision.paths, copies })}\n`, 'utf8');
    } catch (err) {
      this.log('правки без вопросов: копия или журнал не записаны:', err.message);
      return false;
    }
    this.recent.push(this.now());
    this.count += 1;
    this._save();
    return true;
  }

  // The copies of old files older than a month go.
  clean() {
    const dir = this.path.join(this.audit, 'undo');
    let names = [];
    try {
      names = fs.readdirSync(dir);
    } catch {
      return;
    }
    const cutoff = new Date(this.now() - KEEP_DAYS * 86400000).toISOString().slice(0, 10);
    for (const n of names) {
      if (/^\d{4}-\d{2}-\d{2}$/.test(n) && n < cutoff) {
        try {
          fs.rmSync(this.path.join(dir, n), { recursive: true, force: true });
        } catch { /* held: next time */ }
      }
    }
  }
}

module.exports = { AutoApprove, MAX_PER_HOUR };
