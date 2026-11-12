// The message box under the conversation: text (Enter sends, Shift+Enter — a new line), files (the paperclip,
// dragging, pasting a picture), «правки без вопросов» (the window allows the agent's edits inside the project
// folders itself — lib/autoapprove.js), the pickers of the session (model, thinking), «/» commands, send and stop;
// above it the agent's plan while it works.
import { h, icon, clear, basename } from './dom.js';
import { openMenu, menuOpenFor, closeMenu, toast } from './ui.js';

const api = window.sysai;

const EFFORT_RU = { default: 'обычно', none: 'без размышлений', minimal: 'минимально', low: 'немного', medium: 'средне',
  high: 'глубоко', xhigh: 'очень глубоко', max: 'максимум', ultra: 'ультра' };
const AUTO_OFF = 'Агент спросит перед каждой правкой файла';
const AUTO_ON = 'Правки в папках проектов — сразу. Материалы (materials/), секреты, удаление и файлы вне проектов — с вопросом';
const PICKERS = [
  { id: 'model', icon: 'cube', title: 'Модель', label: (o, v) => (v && v.name) || o.currentValue || 'модель' },
  { id: 'reasoning_effort', icon: 'spark', title: 'Глубина размышлений', label: (o) => `Мышление: ${EFFORT_RU[o.currentValue] || o.currentValue}`,
    name: (v) => (EFFORT_RU[v.value] ? EFFORT_RU[v.value][0].toUpperCase() + EFFORT_RU[v.value].slice(1) : v.name || v.value) },
];

function flat(options) {
  const out = [];
  for (const o of options || []) {
    if (o && Array.isArray(o.options)) for (const x of o.options) out.push({ ...x, group: o.name || o.group || '' });
    else if (o) out.push(o);
  }
  return out;
}

export class Composer {
  constructor(hooks = {}) {
    // send({text, files}) → Promise<bool>, stop(), setConfig(conv, id, value), ready() → bool,
    // auto() → {on, count} of «правки без вопросов», setAuto(on)
    this.hooks = hooks;
    this.conv = null;
    this.files = [];
    this.drafts = new Map();
    // what was typed and not sent survives a restart of the window: by the conversation, and for a conversation
    // not started yet — by its folder
    try {
      this.kept = JSON.parse(localStorage.getItem('sysai.drafts') || '{}') || {};
    } catch {
      this.kept = {};
    }
    this.slashHot = 0;
    this.plan = h('div', { class: 'plan', hidden: true });
    this.planFolded = false;
    this.list = h('div', { class: 'attach-list', hidden: true });
    this.input = h('textarea', { rows: '1', placeholder: 'Напишите агенту…', spellcheck: 'true', 'aria-label': 'Сообщение агенту' });
    this.attachBtn = h('button', { class: 'icon-btn', title: 'Прикрепить файлы' }, icon('paperclip'));
    this.autoText = h('span');
    this.autoBtn = h('button', { class: 'picker auto-toggle' }, icon('shield', 'sm'), this.autoText);
    this.pickers = h('div', { class: 'pickers' });
    this.sendBtn = h('button', { class: 'send', title: 'Отправить (Enter)' }, icon('up'));
    this.slash = null;
    this.box = h('div', { class: 'composer' }, this.list, this.input,
      h('div', { class: 'bar' }, this.attachBtn, this.autoBtn, this.pickers, h('span', { class: 'grow' }), this.sendBtn));
    this.hint = h('div', { class: 'hint' }, h('span', { text: 'Enter — отправить · Shift+Enter — новая строка' }),
      h('span', { class: 'more', text: ' · агент работает в пределах доступов SYSAI' }));
    this.el = h('div', { class: 'composer-inner' }, this.plan, this.box, this.hint);
    this._wire();
  }

  _wire() {
    const input = this.input;
    input.addEventListener('input', () => { this._size(); this._slash(); this.update(); this._keep(); });
    input.addEventListener('keydown', (e) => {
      if (this.slash && this._slashKey(e)) return;
      if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.isComposing) {
        e.preventDefault();
        this.submit();
      } else if (e.key === 'Escape' && this.conv && this.conv.running) {
        e.preventDefault();
        this.hooks.stop();
      }
    });
    input.addEventListener('blur', () => setTimeout(() => this._closeSlash(), 150));
    input.addEventListener('paste', (e) => this._paste(e));
    this.attachBtn.addEventListener('click', async () => {
      const paths = await api.dialog.files({ title: 'Прикрепить к сообщению' }).catch(() => []);
      this.attachMany(paths || []);
    });
    this.autoBtn.addEventListener('click', () => {
      if (menuOpenFor(this.autoBtn)) { closeMenu(); return; }
      const on = this._auto().on;
      openMenu(this.autoBtn, [{ header: 'Правки файлов' },
        { label: 'Спрашивать', sub: AUTO_OFF, value: false, checked: !on },
        { label: 'Без вопросов в папках проектов', sub: `${AUTO_ON}. Старый файл копируется, правка пишется в журнал`, value: true, checked: on }],
      { above: true, minWidth: 320, onPick: (value) => { if (value !== on) this.hooks.setAuto(value); } });
    });
    this.sendBtn.addEventListener('click', () => (this.conv && this.conv.running ? this.hooks.stop() : this.submit()));
  }

  get text() {
    return this.input.value;
  }

  _auto() {
    return (this.hooks.auto && this.hooks.auto()) || { on: false, count: 0 };
  }

  _drawAuto() {
    const { on, count } = this._auto();
    this.autoBtn.classList.toggle('on', on);
    this.box.classList.toggle('auto', on);
    this.autoText.textContent = on ? `Правки: без вопросов${count ? ` · ${count}` : ''}` : 'Правки: спрашивать';
    this.autoBtn.title = on ? `${AUTO_ON}.${count ? ` Разрешено без вопроса: ${count}.` : ''} Ctrl+Shift+A — выключить`
      : `${AUTO_OFF}. Нажмите, чтобы разрешить правки в папках проектов без вопросов (Ctrl+Shift+A)`;
  }

  focus() {
    this.input.focus();
  }

  fill(text) {
    this.input.value = text;
    this._size();
    this.update();
    this._keep();
    this.focus();
    this.input.setSelectionRange(text.length, text.length);
  }

  _key(conv) {
    return conv ? (conv.draft ? `new:${conv.cwd || ''}` : conv.id) : '';
  }

  _keep() {
    const key = this._key(this.conv);
    if (!key) return;
    const text = this.input.value;
    if (text.trim()) this.kept[key] = text.slice(0, 20000);
    else delete this.kept[key];
    const keys = Object.keys(this.kept);
    for (const k of keys.slice(0, Math.max(0, keys.length - 50))) delete this.kept[k]; // the oldest go
    clearTimeout(this.keepTimer);
    this.keepTimer = setTimeout(() => {
      try { localStorage.setItem('sysai.drafts', JSON.stringify(this.kept)); } catch { /* not kept */ }
    }, 400);
  }

  setConversation(conv) {
    if (this.conv === conv) { this.update(); this.drawPlan(); return; }
    if (this.conv) this.drafts.set(this.conv.id, { text: this.input.value, files: this.files });
    this.conv = conv;
    const d = (conv && this.drafts.get(conv.id)) || { text: (conv && this.kept[this._key(conv)]) || '', files: [] };
    this.input.value = d.text;
    this.files = [...d.files];
    this._size();
    this._drawFiles();
    this.update();
    this.drawPlan();
  }

  attach(path) {
    this.attachMany([path]);
  }

  attachMany(paths) {
    let added = 0;
    for (const p of paths || []) {
      if (p && !this.files.includes(p)) { this.files.push(p); added++; }
    }
    if (added) { this._drawFiles(); this.update(); this.focus(); }
  }

  _drawFiles() {
    clear(this.list);
    this.files.forEach((p, i) => {
      const x = h('span', { class: 'x', title: 'Убрать' }, icon('x', 'sm'));
      x.addEventListener('click', (e) => { e.stopPropagation(); this.files.splice(i, 1); this._drawFiles(); this.update(); });
      this.list.append(h('span', { class: 'file-chip', title: p }, icon('paperclip', 'sm'), h('span', { text: basename(p) }), x));
    });
    this.list.hidden = !this.files.length;
  }

  _size() {
    const el = this.input;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, Math.round(window.innerHeight * 0.38))}px`;
  }

  async submit() {
    const text = this.input.value.trim();
    if ((!text && !this.files.length) || !this.hooks.ready() || (this.conv && this.conv.running)) return;
    const files = [...this.files];
    const sent = { text: this.input.value, files };
    const key = this._key(this.conv);
    this.input.value = '';
    delete this.kept[key];
    this._keep();
    this.files = [];
    this._size();
    this._drawFiles();
    this._closeSlash();
    this.update();
    const ok = await this.hooks.send({ text, files });
    if (ok === false && !this.input.value) { // not sent: the text comes back
      this.input.value = sent.text;
      this.files = sent.files;
      this._size();
      this._drawFiles();
      this.update();
    }
  }

  update() {
    const conv = this.conv;
    const running = Boolean(conv && conv.running);
    const ready = this.hooks.ready();
    this.sendBtn.classList.toggle('stop', running);
    this.sendBtn.replaceChildren(icon(running ? 'stop' : 'up'));
    this.sendBtn.title = running ? 'Остановить (Esc)' : 'Отправить (Enter)';
    this.sendBtn.disabled = running ? false : !ready || (!this.input.value.trim() && !this.files.length);
    this.input.placeholder = !ready ? (this.hooks.idle ? this.hooks.idle() : 'Агент запускается…') : running ? 'Агент работает — можно писать следующее сообщение' : 'Напишите агенту…';
    this._drawAuto();
    this._pickers();
  }

  _pickers() {
    const conv = this.conv;
    clear(this.pickers);
    if (!conv) return;
    for (const spec of PICKERS) {
      const opt = (conv.config || []).find((o) => o.id === spec.id);
      if (!opt) continue;
      const values = flat(opt.options);
      if (values.length < 2 && spec.id !== 'model') continue;
      const cur = values.find((v) => v.value === opt.currentValue);
      const b = h('button', { class: 'picker', title: spec.title }, icon(spec.icon, 'sm'), h('span', { text: spec.label(opt, cur) }), icon('chevD', 'sm'));
      b.addEventListener('click', () => {
        if (menuOpenFor(b)) { closeMenu(); return; }
        const items = [{ header: spec.title }];
        let group = '';
        for (const v of values) {
          if (v.group && v.group !== group) { group = v.group; items.push({ header: group }); }
          items.push({ label: spec.name ? spec.name(v) : v.name || v.value, sub: spec.sub ? spec.sub(v) : v.description || '',
            value: v.value, checked: v.value === opt.currentValue });
        }
        openMenu(b, items, { above: true, minWidth: 240, onPick: (value) => {
          if (value !== opt.currentValue) this.hooks.setConfig(conv, spec.id, value);
        } });
      });
      this.pickers.append(b);
    }
  }

  // ---------- «/» commands of the agent
  _slash() {
    const m = /^\/(\S*)$/.exec(this.input.value);
    const cmds = (this.conv && this.conv.commands) || [];
    if (!m || !cmds.length) { this._closeSlash(); return; }
    const q = m[1].toLowerCase();
    const found = cmds.filter((c) => String(c.name || '').toLowerCase().startsWith(q)).slice(0, 30);
    if (!found.length) { this._closeSlash(); return; }
    if (!this.slash) {
      this.slash = h('div', { class: 'menu slash', role: 'listbox' });
      this.box.append(this.slash);
    }
    this.slashItems = found;
    this.slashHot = Math.min(this.slashHot, found.length - 1);
    clear(this.slash);
    found.forEach((c, i) => {
      const b = h('button', { class: `mi${i === this.slashHot ? ' hot' : ''}` },
        h('span', { class: 'txt' }, `/${c.name}`, c.description ? h('small', { text: c.description }) : null));
      b.addEventListener('mousedown', (e) => { e.preventDefault(); this._pickSlash(c); });
      this.slash.append(b);
    });
  }

  _slashKey(e) {
    const n = this.slashItems ? this.slashItems.length : 0;
    if (!n) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      this.slashHot = (this.slashHot + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
      this._slash();
      return true;
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault();
      this._pickSlash(this.slashItems[this.slashHot]);
      return true;
    }
    if (e.key === 'Escape') { e.preventDefault(); this._closeSlash(); return true; }
    return false;
  }

  _pickSlash(c) {
    this.input.value = `/${c.name} `;
    this._closeSlash();
    this._size();
    this.update();
    this.focus();
  }

  _closeSlash() {
    if (this.slash) this.slash.remove();
    this.slash = null;
    this.slashItems = null;
    this.slashHot = 0;
  }

  // ---------- a picture pasted from the clipboard becomes a file the agent can open
  async _paste(e) {
    const files = [...((e.clipboardData && e.clipboardData.files) || [])];
    if (!files.length) return;
    const withPath = files.map((f) => api.pathForFile(f)).filter(Boolean);
    if (withPath.length) { e.preventDefault(); this.attachMany(withPath); return; }
    e.preventDefault();
    for (const f of files) {
      try {
        const bytes = new Uint8Array(await f.arrayBuffer());
        const name = f.name && f.name !== 'image.png' ? f.name : `вставка-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.${(f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`;
        this.attach(await api.fs.paste(name, bytes));
      } catch (err) {
        toast(`Вставка не удалась: ${err.message}`, 'err');
      }
    }
  }

  // ---------- the plan of the agent (ACP «plan»): above the box while there is something to do
  drawPlan() {
    const conv = this.conv;
    const entries = (conv && conv.plan) || [];
    const open = entries.some((e) => e.status !== 'completed');
    if (!entries.length || (!open && !(conv && conv.running))) { this.plan.hidden = true; return; }
    const done = entries.filter((e) => e.status === 'completed').length;
    const head = h('h4', {}, icon('list', 'sm'), 'План', h('span', { class: 'n', text: `${done} из ${entries.length}` }),
      icon(this.planFolded ? 'chevR' : 'chevD', 'sm'));
    head.addEventListener('click', () => { this.planFolded = !this.planFolded; this.drawPlan(); });
    const ul = h('ul', { hidden: this.planFolded }, entries.map((e) => h('li', { class: e.status || 'pending' }, h('span', { class: 'dot' }),
      h('span', { text: e.content || '' }))));
    clear(this.plan).append(head, ul);
    this.plan.hidden = false;
  }
}
