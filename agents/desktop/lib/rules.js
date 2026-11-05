'use strict';
// The signed rules of SYSAI, checked here as the synchronizer checks them (sysai_agent/rules.py and hook.py; the
// same cases are compared with them by tests/test_desktop.py): what the agent asks of this window — show a file,
// open it in its program, start a program, show it in its folder — is done only within them. What is not allowed is
// forbidden. access.json is trusted when it is the one the synchronizer verified (its sha256 in state.json).
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { fileURLToPath } = require('node:url');

const files = require('./files');

const WHOLE_PC = '*'; // in «Папки»: the whole computer
const OURS_IN_PROFILE = ['config.yaml', '.env', 'SOUL.md', 'shell-hooks-allowlist.json'];
const SELF = /\bsysai-agentw?(?:\.exe)?\b(?!\s+(?:status|version)\b)/i;
const HERMES_SETTINGS = /\bhermes(?:\.exe)?\b[^\n|;&]*\b(?:config|hooks)\b/i;
const SETTINGS_NOTE = 'папку SYSAI, синхронизатор и настройки агента меняет только SYSAI';
const NO_RULES = 'на этом компьютере нет подписанных доступов SYSAI: синхронизатор их ещё не получил (sysai-agent sync) или файл повреждён';
const STALE_RULES = 'доступы SYSAI на этом компьютере сейчас обновляются (или файл изменён не синхронизатором) — повторите через минуту';

const list = (v) => (Array.isArray(v) ? v : []);
const words = (text) => String(text ?? '').split(/[\s\x1c-\x1f\x85]+/).filter(Boolean).join(' ');

// The rules from access.json, or why there are none: {rules} | {error}.
function load(home) {
  let raw;
  try {
    raw = fs.readFileSync(path.join(home, 'access.json'));
  } catch {
    return { error: NO_RULES };
  }
  let signed;
  try {
    signed = JSON.parse(raw.toString('utf8'));
  } catch {
    return { error: NO_RULES };
  }
  if (!signed || typeof signed !== 'object' || !signed.doc || typeof signed.doc !== 'object') return { error: NO_RULES };
  let state = {};
  try {
    state = JSON.parse(fs.readFileSync(path.join(home, 'state.json'), 'utf8').replace(/^\uFEFF/, '')) || {};
  } catch { /* none yet */ }
  if (state.access_sha !== crypto.createHash('sha256').update(raw).digest('hex')) return { error: STALE_RULES };
  const rules = signed.doc.rules;
  return rules && typeof rules === 'object' && !Array.isArray(rules) ? { rules } : { error: NO_RULES };
}

// fnmatch.fnmatchcase of Python: «*» any text, «?» one character, «[…]» / «[!…]» a set.
function translate(pat) {
  let res = '';
  let star = false;
  let i = 0;
  const n = pat.length;
  while (i < n) {
    const c = pat[i++];
    if (c === '*') {
      if (!star) res += '.*';
      star = true;
      continue;
    }
    star = false;
    if (c === '?') {
      res += '.';
    } else if (c === '[') {
      let j = i;
      if (j < n && pat[j] === '!') j++;
      if (j < n && pat[j] === ']') j++;
      while (j < n && pat[j] !== ']') j++;
      if (j >= n) {
        res += '\\[';
        continue;
      }
      let stuff = pat.slice(i, j);
      i = j + 1;
      const negate = stuff.startsWith('!');
      if (negate) stuff = stuff.slice(1);
      if (!stuff) {
        res += negate ? '.' : '(?!)';
        continue;
      }
      // ranges stay ranges; everything else in the set is literal
      let set = '';
      for (let k = 0; k < stuff.length; k++) {
        const ch = stuff[k];
        if (ch === '-' && k > 0 && k < stuff.length - 1) {
          set += '-';
        } else {
          set += /[\\\]\[^-]/.test(ch) ? `\\${ch}` : ch;
        }
      }
      res += `[${negate ? '^' : ''}${set}]`;
    } else {
      res += c.replace(/[.*+?^${}()|[\]\\/-]/g, '\\$&');
    }
  }
  return res;
}

function fnmatch(text, pattern) {
  try {
    return new RegExp(`^(?:${translate(pattern)})$`, 's').test(text);
  } catch {
    return text === pattern; // a set Python would drop (a reversed range): only the very text
  }
}

// The first forbidden pattern the command matches ("" — allowed); case-insensitive, «*» is any text.
function commandDenied(rules, command) {
  const commands = (rules && rules.commands) || {};
  if (!commands.run) return '*';
  const text = words(command).toLowerCase();
  for (const pattern of list(commands.deny)) {
    if (fnmatch(text, words(pattern).toLowerCase())) return pattern;
  }
  return '';
}

// ntpath.normpath + lower case: «..» collapsed (no way out of a folder), Windows paths, any case.
function norm(p) {
  const text = String(p ?? '').trim();
  if (!text) return '';
  let out = path.win32.normalize(text.replace(/\//g, '\\'));
  if (/^[A-Za-z]:\.$/.test(out)) out = out.slice(0, 2); // «C:» stays «C:», as in Python
  return out.replace(/\\+/g, '\\').replace(/\\+$/, '').toLowerCase();
}

function inside(target, root) {
  const r = norm(root);
  return Boolean(r) && (target === r || target.startsWith(`${r}\\`));
}

// Inside a «read» (or «write») folder and outside every forbidden one; «*» — the whole computer.
function pathAllowed(rules, p, write = false) {
  const target = norm(p);
  if (!target) return false;
  const folders = (rules && rules.folders) || {};
  const within = (root) => String(root ?? '').trim() === WHOLE_PC || inside(target, root);
  if (list(folders.deny).some(within)) return false;
  const roots = write ? list(folders.write) : [...list(folders.read), ...list(folders.write)];
  return roots.some(within);
}

const parts = (p) => String(p || '').split(/[\\/]+/).filter(Boolean);

// A path only the synchronizer writes: the SYSAI folder (but the skills the agent proposes) and SYSAI's files in
// the Hermes profile.
function guardedPath(p, home, profileDir) {
  const target = norm(p);
  if (!target) return false;
  if (inside(target, `${home}\\proposals`)) return false;
  if (inside(target, home)) return true;
  return Boolean(profileDir) && OURS_IN_PROFILE.some((name) => inside(target, `${profileDir}\\${name}`));
}

// A command that names the guarded places or manages the synchronizer or Hermes's settings.
function touchesGuard(text, home, profileDir) {
  const low = String(text || '').replace(/\//g, '\\').replace(/\\+/g, '\\').toLowerCase();
  const marks = [norm(home), 'appdata\\local\\sysai', 'localappdata%\\sysai', 'localappdata\\sysai'];
  if (profileDir) {
    const bits = parts(profileDir);
    if (bits.length > 1 && bits[bits.length - 2] === 'profiles') {
      marks.push(norm(profileDir), norm(`profiles\\${bits[bits.length - 1]}`));
    } else {
      for (const name of OURS_IN_PROFILE) marks.push(norm(`${profileDir}\\${name}`));
    }
  }
  if (marks.some((m) => m && low.includes(m))) return true;
  return SELF.test(text || '') || HERMES_SETTINGS.test(text || '');
}

// As a tool takes a path: «~» and variables expanded, file:// understood, a relative path from the person's folder.
function fullPath(p, cwd = os.homedir()) {
  let text = String(p ?? '').trim();
  if (!text) return '';
  if (/^file:\/\//i.test(text)) {
    try {
      text = fileURLToPath(text);
    } catch {
      text = text.slice(7);
    }
  }
  if (text === '~' || /^~[\\/]/.test(text)) text = path.join(os.homedir(), text.slice(1));
  text = text.replace(/%([A-Za-z_][A-Za-z0-9_()]*)%/g, (m, name) => process.env[name] ?? m)
    .replace(/\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)/g, (m, a, b) => process.env[a || b] ?? m);
  return path.resolve(cwd || os.homedir(), text);
}

// Why the window refuses what the agent asks ("" — allowed): «show» and «reveal» need reading, «open» — reading
// and, for a program, «Команды на ПК» outside the forbidden patterns; the synchronizer's places are not opened.
function check(rules, action, target, { home = '', profileDir = '' } = {}) {
  if (!rules) return NO_RULES;
  if (!pathAllowed(rules, target)) return `папка ${String(target).slice(0, 160)} агенту не разрешена (SYSAI → Доступы → Папки)`;
  if (action !== 'open') return '';
  if (home && guardedPath(target, home, profileDir)) return SETTINGS_NOTE;
  if (!files.runnable(target)) return '';
  if (!(rules.commands || {}).run) return 'запускать программы агенту можно только с «Командами на ПК» (SYSAI → Доступы)';
  const name = path.win32.basename(String(target));
  const forms = [`start "" "${target}"`, String(target), name, name.replace(/\.[^.]+$/, '')];
  for (const form of forms) {
    const pattern = commandDenied(rules, form);
    if (pattern) return `запуск попадает под запрет «${pattern}» (SYSAI → Доступы → Команды на ПК)`;
  }
  if (home && touchesGuard(forms[0], home, profileDir)) return SETTINGS_NOTE;
  return '';
}

module.exports = { WHOLE_PC, NO_RULES, STALE_RULES, SETTINGS_NOTE, load, fnmatch, commandDenied, norm, pathAllowed,
  guardedPath, touchesGuard, fullPath, check };
