// «Холст» в рабочей панели: окно проекта EasyBuilder Pro так, как его покажет панель, — человек и агент правят его
// вместе. Объекты — блоки по рамкам (bbox) в пикселях панели, в масштабе: фон окна, фигуры (заливка, рамка), тексты
// (шрифт, размер в пунктах, цвет, выравнивание), картинки первого состояния — как их описал cmtp_scene; фигуры из
// библиотеки EasyBuilder, которых нет в проекте, — их обычным видом. Мышью: выделить (Shift/Ctrl — ещё; рамкой по
// пустому месту), перетащить, двойной щелчок — текст; стрелки — сдвиг (Shift — на 10), Ctrl+D — копия, Ctrl+Z —
// отменить. Курсоры агента и помощников — стрелки с именем и тем, что они делают.
import { h, icon, basename } from './dom.js';
import { openMenu, toast } from './ui.js';

const api = window.sysai;
const FONT = 1.35; // px шрифта на пункт EasyBuilder (сверено с рамками «Текстов»)
const LINE = 1.36; // высота строки
const ZOOMS = [0.1, 0.17, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.25, 1.5, 2, 3, 4];
const FIELD = new Set(['NumericInput', 'NumericDisplay', 'ASCII', 'SetWord?', 'ASCII?']);
const LAMP = new Set(['BitLamp', 'WordLamp?', 'MultiState?']);
const SHAPES = new Set(['Rect', 'Circle', 'Ellipse', 'Polygon', 'Text', 'Line']);
const IDLE = 45000;

const css = (v) => String(v || '').replace(/[^a-z0-9_-]/gi, '_');
const light = (c) => { // светлый цвет: под белой подписью — тёмная кнопка
  const m = /^#([0-9a-f]{6})$/i.exec(String(c || ''));
  if (!m) return false;
  const n = parseInt(m[1], 16);
  return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255 > 0.62;
};
const label0 = (o) => (o.labels || []).find((l) => l && String(l.text || '') !== '') || null;

function fontOf(l) {
  const name = String(l.font || '').trim();
  const bold = /\bbold\b/i.test(name);
  const family = name.replace(/\s*\bbold\b/i, '').trim() || 'Droid Sans Fallback';
  return { family: `"${family.replace(/"/g, '')}", "Droid Sans Fallback", "Droid Sans", Arial, sans-serif`, bold };
}

function tip(o) {
  const l = label0(o);
  const tags = [...new Set(o.tags || [])];
  return [`#${o.index} ${o.kind}`, l ? `«${String(l.text).slice(0, 60)}»` : '', tags.slice(0, 3).join(', ')].filter(Boolean).join(' · ');
}

function labelEl(l, o, whole) {
  const [left, top] = o.bbox;
  const f = fontOf(l);
  const el = h('span', { class: `cv-label${whole ? ' whole' : ''}`, text: String(l.text) });
  const s = el.style;
  s.fontFamily = f.family;
  if (f.bold) s.fontWeight = '700';
  s.fontSize = `${Math.max(1, (Number(l.size) || 12) * FONT)}px`;
  s.lineHeight = String(LINE);
  s.color = l.color || '#000';
  s.textAlign = ['left', 'center', 'right'].includes(l.align) ? l.align : 'left';
  if (!whole) {
    s.left = `${(Number(l.x) || left) - left}px`;
    s.top = `${(Number(l.y) || top) - top}px`;
  }
  return el;
}

function lineEl(o, w, hh) {
  const fw = Math.max(1, Number(o.frame_width) || 1);
  const color = o.frame || o.fill || '#000';
  if (w <= fw + 2 || hh <= fw + 2) return h('div', { class: 'cv-fill', style: { background: color } });
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'cv-svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${hh}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const ln = document.createElementNS(ns, 'line');
  for (const [k, v] of [['x1', 0], ['y1', 0], ['x2', w], ['y2', hh], ['stroke', color], ['stroke-width', fw]]) ln.setAttribute(k, v);
  svg.append(ln);
  return svg;
}

// Вид объекта: картинка состояния, фигура с заливкой и рамкой, фигура EasyBuilder обычным видом, подпись.
function objectEl(o, images) {
  const [l, t, r, b] = o.bbox;
  const w = Math.max(1, r - l + 1);
  const hh = Math.max(1, b - t + 1);
  const el = h('div', { class: `cv-obj k-${css(o.kind)}`, dataset: { i: o.index }, title: tip(o) });
  Object.assign(el.style, { left: `${l}px`, top: `${t}px`, width: `${w}px`, height: `${hh}px` });
  const pic = (o.picture || [])[0];
  const url = pic && images ? images[pic.key] : '';
  let look = false;
  if (o.kind === 'Line') {
    el.append(lineEl(o, w, hh));
    look = true;
  } else if (SHAPES.has(o.kind) || /^draw_/.test(o.kind)) {
    if (o.fill) el.style.background = o.fill;
    if (o.kind !== 'Text' && o.frame && Number(o.frame_width) > 0) el.style.boxShadow = `inset 0 0 0 ${Number(o.frame_width)}px ${o.frame}`;
    if (/^(Circle|Ellipse)$/.test(o.kind)) el.style.borderRadius = '50%';
    look = Boolean(o.fill || (o.kind !== 'Text' && o.frame));
  }
  if (url) {
    el.append(h('img', { class: 'cv-pic', src: url, alt: '', draggable: 'false' }));
    look = true;
  } else if (o.shape) {
    const lab = label0(o);
    const kind = FIELD.has(o.kind) ? 'field' : LAMP.has(o.kind) ? 'lamp' : `button${lab && light(lab.color) ? ' dark' : ''}`;
    el.append(h('div', { class: `cv-shape ${kind}` }));
    look = true;
  } else if (pic) {
    el.append(h('div', { class: 'cv-shape missing' }));
    look = true;
  }
  const lab = label0(o);
  if (lab) {
    el.append(labelEl(lab, o, o.kind === 'Text'));
    look = true;
  }
  if (!look) el.classList.add('bare');
  return el;
}

function arrow(color) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'cv-arrow');
  svg.setAttribute('viewBox', '0 0 16 22');
  const p = document.createElementNS(ns, 'path');
  p.setAttribute('d', 'M1.5 1.5v16.2l4.3-4 2.9 6.6 2.9-1.3-2.9-6.4h6z');
  p.setAttribute('fill', color);
  p.setAttribute('stroke', '#fff');
  p.setAttribute('stroke-width', '1.4');
  p.setAttribute('stroke-linejoin', 'round');
  svg.append(p);
  return svg;
}

function iconBtn(title, ico, onclick) {
  return h('button', { class: 'icon-btn sm', title, onclick }, icon(ico, 'sm'));
}

export class CanvasView {
  constructor(hooks = {}) {
    this.hooks = hooks; // focus() — показать холст; mention(text) — вставить в сообщение агенту
    this.el = h('div', { class: 'pane-section canvas' });
    this.s = null; // состояние от основного процесса
    this.zoom = 0; // 0 — по размеру панели
    this.z = 1;
    this.sel = new Set();
    this.els = new Map(); // index → элемент
    this.drag = null;
    this.band = null;
    this.nudge = null;
    this.editor = null;
    this.flashes = null;
    this.pointerAt = 0;
    this.pointerTimer = null;
    this.cursors = [];
    this.build();
    this.empty();
    api.canvas.onState((s) => this.update(s));
    api.canvas.onCursors((c) => this.drawCursors(c));
    api.canvas.onEdited((e) => this.flash(e));
    api.canvas.onFocus(() => { if (this.hooks.focus) this.hooks.focus(); });
    api.canvas.state().then((s) => this.update(s)).catch(() => {});
    setInterval(() => this.idle(), 5000);
  }

  get loaded() {
    return Boolean(this.s && this.s.file && this.s.scene);
  }

  build() {
    this.nameB = h('b');
    this.nameS = h('span');
    this.winSel = h('select', { class: 'cv-win', title: 'Окно проекта', onchange: () => this.go(this.winSel.value) });
    this.zoomBtn = h('button', { class: 'btn sm cv-zoom', title: 'По размеру панели / 100 %', onclick: () => this.setZoom(this.zoom ? 0 : 1) });
    this.undoBtn = iconBtn('Отменить последнюю правку (Ctrl+Z)', 'undo', () => this.undo());
    const more = iconBtn('Ещё', 'dots', () => openMenu(more, [
      { label: 'Открыть другой проект…', value: 'pick', icon: 'folderOpen' },
      { label: 'Перечитать проект', value: 'reload', icon: 'refresh' },
      { label: 'Показать в папке', value: 'reveal', icon: 'folder' },
      { sep: true },
      { label: 'Закрыть холст', value: 'close', icon: 'x' },
    ], { align: 'right', onPick: (v) => this.menu(v) }));
    this.ebBtn = h('button', { class: 'btn sm outline', title: 'Открыть проект в EasyBuilder Pro', onclick: () => this.easybuilder() },
      icon('external', 'sm'), h('span', { class: 'lbl', text: 'EasyBuilder' }));
    this.bar = h('div', { class: 'pv-bar cv-bar' },
      h('div', { class: 'pv-name' }, this.nameB, this.nameS),
      h('div', { class: 'pv-acts' }, this.winSel,
        iconBtn('Мельче', 'minus', () => this.step(-1)), this.zoomBtn, iconBtn('Крупнее', 'plus', () => this.step(1)),
        this.undoBtn, this.ebBtn, more));
    this.note = h('div', { class: 'pv-note cv-err', hidden: true });
    this.screen = h('div', { class: 'cv-screen' });
    this.layer = h('div', { class: 'cv-layer' });
    this.sizer = h('div', { class: 'cv-sizer' }, this.screen, this.layer);
    this.stage = h('div', { class: 'cv-stage', tabindex: '0' }, this.sizer);
    this.foot = h('div', { class: 'cv-foot' });
    this.main = h('div', { class: 'cv-main' }, this.bar, this.note, this.stage, this.foot);
    this.stage.addEventListener('pointerdown', (e) => this.down(e));
    this.stage.addEventListener('pointermove', (e) => this.move(e));
    this.stage.addEventListener('pointerup', (e) => this.up(e));
    this.stage.addEventListener('pointercancel', () => this.cancel());
    this.stage.addEventListener('pointerleave', () => this.report(null));
    this.stage.addEventListener('dblclick', (e) => this.dbl(e));
    this.stage.addEventListener('contextmenu', (e) => this.context(e));
    this.stage.addEventListener('keydown', (e) => this.key(e));
    this.stage.addEventListener('wheel', (e) => this.wheel(e), { passive: false });
    new ResizeObserver(() => { if (this.loaded && !this.zoom) this.applyZoom(); }).observe(this.stage);
  }

  empty(error) {
    this.el.replaceChildren(h('div', { class: 'pv-body center' }, h('div', { class: 'pv-empty' },
      h('div', { class: 'big' }, icon('canvas', 'xl')), h('b', { text: 'Холст: окно панели EasyBuilder' }),
      h('div', { text: 'Откройте проект .cmtp или .emtp — в «Файлах», кнопкой ниже или попросите агента. Окно видно так, как '
        + 'его покажет панель; правите вы и агент вместе, курсоры агента и помощников — на холсте.' }),
      error ? h('div', { class: 'cv-empty-err', text: error }) : null,
      h('button', { class: 'btn primary', onclick: () => this.pick() }, icon('folderOpen', 'sm'), h('span', { text: 'Открыть проект…' })))));
  }

  // --- the state from the main process
  update(s) {
    const prev = this.s;
    this.s = s;
    if (!this.loaded) {
      this.sel.clear();
      this.empty(s && s.error);
      return;
    }
    if (this.el.firstChild !== this.main) this.el.replaceChildren(this.main);
    const sameWin = prev && prev.file === s.file && prev.scene && prev.scene.window.number === s.scene.window.number;
    if (!sameWin) {
      this.sel.clear();
      this.zoom = 0;
      this.stopEdit();
    }
    for (const i of [...this.sel]) if (!s.scene.objects[i]) this.sel.delete(i);
    this.drawBar();
    if (!prev || prev.version !== s.version || !sameWin) this.drawScene();
    this.drawCursors(s.cursors);
    this.drawFoot();
  }

  drawBar() {
    const { s } = this;
    this.nameB.textContent = s.name;
    this.nameS.textContent = s.file;
    this.nameS.title = s.file;
    const opts = s.windows.map((w) => h('option', { value: String(w.number), text: `${w.number} · ${w.name}` }));
    this.winSel.replaceChildren(...opts);
    this.winSel.value = String(s.window);
    this.undoBtn.disabled = !s.undo;
    this.undoBtn.title = s.undo ? `Отменить последнюю правку (Ctrl+Z) — можно отменить: ${s.undo}` : 'Отменять нечего';
    this.note.hidden = !s.error;
    this.note.textContent = s.error || '';
  }

  drawScene() {
    const { scene } = this.s;
    const W = scene.window.width;
    const H = scene.window.height;
    Object.assign(this.screen.style, { width: `${W}px`, height: `${H}px`, background: scene.window.background || '#000' });
    const els = scene.objects.filter((o) => o.kind !== 'Window').map((o) => objectEl(o, scene.images));
    this.screen.replaceChildren(...els);
    this.els = new Map(els.map((el) => [Number(el.dataset.i), el]));
    this.drag = null;
    this.nudge = null;
    this.applyZoom();
    this.markSel();
    if (this.flashes && Date.now() - this.flashes.at < 1600 && this.flashes.window === scene.window.number) this.lightUp(this.flashes);
  }

  // --- zoom
  fit() {
    const { width: W, height: H } = this.s.scene.window;
    const r = this.stage.getBoundingClientRect();
    const z = Math.min((r.width - 28) / W, (r.height - 28) / H, 2);
    return Number.isFinite(z) && z > 0.05 ? z : 1;
  }

  applyZoom() {
    if (!this.loaded) return;
    const { width: W, height: H } = this.s.scene.window;
    const z = this.zoom || this.fit();
    this.z = z;
    Object.assign(this.sizer.style, { width: `${Math.ceil(W * z)}px`, height: `${Math.ceil(H * z)}px` });
    this.screen.style.transform = `scale(${z})`;
    this.screen.style.setProperty('--z', String(z));
    this.zoomBtn.textContent = `${Math.round(z * 100)} %`;
    this.zoomBtn.classList.toggle('on', !this.zoom);
    this.drawCursors(this.cursors);
    if (this.editor) this.placeEditor();
  }

  setZoom(z, at) {
    if (!this.loaded) return;
    const old = this.z;
    this.zoom = z;
    let keep = null;
    if (at) {
      const r = this.stage.getBoundingClientRect();
      keep = { x: (this.stage.scrollLeft + at.clientX - r.left), y: (this.stage.scrollTop + at.clientY - r.top), cx: at.clientX - r.left, cy: at.clientY - r.top };
    }
    this.applyZoom();
    if (keep) {
      const k = this.z / old;
      this.stage.scrollLeft = keep.x * k - keep.cx;
      this.stage.scrollTop = keep.y * k - keep.cy;
    }
  }

  step(dir, at) {
    const z = this.z;
    const next = dir > 0 ? ZOOMS.find((v) => v > z + 0.001) : [...ZOOMS].reverse().find((v) => v < z - 0.001);
    if (next) this.setZoom(next, at);
  }

  wheel(e) {
    if (!this.loaded || !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    this.step(e.deltaY < 0 ? 1 : -1, e);
  }

  start() { // панель показала холст: подогнать масштаб
    if (this.loaded && !this.zoom) requestAnimationFrame(() => this.applyZoom());
  }

  // --- the pointer
  at(e) { // точка окна панели под мышью
    const r = this.screen.getBoundingClientRect();
    return { x: (e.clientX - r.left) / this.z, y: (e.clientY - r.top) / this.z };
  }

  objAt(e) { // верхний объект под мышью (после захвата мыши событие приходит холсту, а не объекту)
    const t = document.elementFromPoint(e.clientX, e.clientY);
    const el = t && t.closest ? t.closest('.cv-obj') : null;
    return el && this.screen.contains(el) ? Number(el.dataset.i) : null;
  }

  down(e) {
    if (!this.loaded || e.button !== 0 || this.editor) return;
    this.stage.focus({ preventScroll: true });
    const i = this.objAt(e);
    const p = this.at(e);
    const add = e.shiftKey || e.ctrlKey || e.metaKey;
    if (i === null) {
      if (!add) this.sel.clear();
      this.band = { x0: p.x, y0: p.y, base: new Set(this.sel), el: h('div', { class: 'cv-band' }) };
      this.layer.append(this.band.el);
    } else {
      if (add && this.sel.has(i)) {
        this.sel.delete(i);
        this.markSel();
        this.report(p);
        return;
      }
      if (!this.sel.has(i)) {
        if (!add) this.sel.clear();
        this.sel.add(i);
      }
      this.drag = { x0: p.x, y0: p.y, sx: e.clientX, sy: e.clientY, dx: 0, dy: 0, moved: false, ids: [...this.sel] };
    }
    this.stage.setPointerCapture(e.pointerId);
    this.markSel();
    this.report(p);
  }

  move(e) {
    if (!this.loaded) return;
    const p = this.at(e);
    if (this.drag) {
      const d = this.drag;
      if (!d.moved && Math.abs(e.clientX - d.sx) + Math.abs(e.clientY - d.sy) < 3) return;
      d.moved = true;
      d.dx = Math.round(p.x - d.x0);
      d.dy = Math.round(p.y - d.y0);
      for (const i of d.ids) {
        const el = this.els.get(i);
        if (el) el.style.transform = `translate(${d.dx}px, ${d.dy}px)`;
      }
      this.drawFoot(`сдвиг на (${d.dx}, ${d.dy})`);
    } else if (this.band) {
      const b = this.band;
      const l = Math.min(b.x0, p.x);
      const t = Math.min(b.y0, p.y);
      const r = Math.max(b.x0, p.x);
      const bt = Math.max(b.y0, p.y);
      Object.assign(b.el.style, { left: `${l * this.z}px`, top: `${t * this.z}px`, width: `${(r - l) * this.z}px`, height: `${(bt - t) * this.z}px` });
      this.sel = new Set(b.base);
      for (const o of this.s.scene.objects) {
        if (o.kind === 'Window') continue;
        const [ol, ot, or, ob] = o.bbox;
        if (ol <= r && or >= l && ot <= bt && ob >= t) this.sel.add(o.index);
      }
      this.markSel();
    }
    this.report(p);
  }

  up(e) {
    if (this.band) {
      this.band.el.remove();
      this.band = null;
      this.report(this.at(e), true);
    }
    const d = this.drag;
    this.drag = null;
    if (!d || !d.moved || (!d.dx && !d.dy)) {
      if (d) this.clearMoves(d.ids);
      this.drawFoot();
      return;
    }
    for (const i of d.ids) {
      const el = this.els.get(i);
      if (el) el.classList.add('pending');
    }
    this.apply(d.ids.map((i) => ({ op: 'move', object: i, dx: d.dx, dy: d.dy })), () => this.clearMoves(d.ids));
  }

  cancel() {
    if (this.drag) this.clearMoves(this.drag.ids);
    this.drag = null;
    if (this.band) this.band.el.remove();
    this.band = null;
  }

  clearMoves(ids) {
    for (const i of ids) {
      const el = this.els.get(i);
      if (el) {
        el.style.transform = '';
        el.classList.remove('pending');
      }
    }
  }

  // Где мышь человека и что выделено — агенту (canvas_look); не чаще, чем раз в 200 мс.
  report(p, now = false) {
    this.lastPoint = p ? { x: Math.round(p.x), y: Math.round(p.y) } : { x: null, y: null };
    const send = () => {
      this.pointerTimer = null;
      this.pointerAt = Date.now();
      api.canvas.pointer({ ...this.lastPoint, selected: [...this.sel] });
    };
    if (now || Date.now() - this.pointerAt > 200) send();
    else if (!this.pointerTimer) this.pointerTimer = setTimeout(send, 200);
  }

  markSel() {
    for (const [i, el] of this.els) el.classList.toggle('sel', this.sel.has(i));
    this.drawFoot();
  }

  // --- edits
  async apply(edits, onFail) {
    const win = String(this.s.window);
    try {
      const out = await api.canvas.edit(edits.map((e) => ({ window: win, ...e })));
      return out;
    } catch (err) {
      if (onFail) onFail();
      toast(err.message, 'err', 9000);
      return null;
    }
  }

  async duplicate() {
    const ids = [...this.sel].sort((a, b) => a - b);
    if (!ids.length) return;
    const out = await this.apply(ids.map((i) => ({ op: 'clone_object', object: i, dx: 10, dy: 10 })));
    if (out && out.objects && out.objects.length) {
      const fresh = out.results.filter((r) => r.op === 'clone_object').map((r) => r.object);
      this.sel = new Set(fresh.length ? fresh : out.objects);
      this.markSel();
      this.report(this.lastPoint && this.lastPoint.x !== null ? this.lastPoint : null, true);
    }
  }

  nudgeBy(dx, dy) {
    const ids = [...this.sel];
    if (!ids.length) return;
    if (!this.nudge) this.nudge = { dx: 0, dy: 0, ids, timer: null };
    const n = this.nudge;
    n.dx += dx;
    n.dy += dy;
    for (const i of n.ids) {
      const el = this.els.get(i);
      if (el) el.style.transform = `translate(${n.dx}px, ${n.dy}px)`;
    }
    this.drawFoot(`сдвиг на (${n.dx}, ${n.dy})`);
    clearTimeout(n.timer);
    n.timer = setTimeout(() => {
      this.nudge = null;
      if (!n.dx && !n.dy) return this.clearMoves(n.ids);
      for (const i of n.ids) {
        const el = this.els.get(i);
        if (el) el.classList.add('pending');
      }
      return this.apply(n.ids.map((i) => ({ op: 'move', object: i, dx: n.dx, dy: n.dy })), () => this.clearMoves(n.ids));
    }, 450);
  }

  key(e) {
    if (!this.loaded || this.editor) return;
    const k = e.key;
    const ctrl = e.ctrlKey || e.metaKey;
    const low = k.toLowerCase();
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[k] && this.sel.size) {
      e.preventDefault();
      const m = e.shiftKey ? 10 : 1;
      this.nudgeBy(arrows[k][0] * m, arrows[k][1] * m);
    } else if (ctrl && (low === 'z' || low === 'я')) {
      e.preventDefault();
      this.undo();
    } else if (ctrl && (low === 'd' || low === 'в')) {
      e.preventDefault();
      this.duplicate();
    } else if (ctrl && (low === 'a' || low === 'ф')) {
      e.preventDefault();
      this.sel = new Set([...this.els.keys()]);
      this.markSel();
    } else if (k === 'Escape') {
      if (this.drag || this.band) this.cancel();
      else this.sel.clear();
      this.markSel();
    } else if ((k === 'Enter' || k === 'F2') && this.sel.size === 1) {
      e.preventDefault();
      this.editText([...this.sel][0]);
    } else if (k === 'Delete' || k === 'Backspace') {
      if (this.sel.size) toast('Удалять объекты холст пока не умеет — удалите их в EasyBuilder', 'info');
    } else if (!ctrl && (k === '+' || k === '=')) {
      this.step(1);
    } else if (!ctrl && k === '-') {
      this.step(-1);
    } else if (!ctrl && k === '0') {
      this.setZoom(0);
    } else if (!ctrl && k === '1') {
      this.setZoom(1);
    }
  }

  dbl(e) {
    const i = this.objAt(e);
    if (i !== null) this.editText(i);
  }

  // --- the text of an object, in place
  editText(i) {
    const o = this.s.scene.objects[i];
    const l = o && label0(o);
    if (!l) {
      if (o) toast(`У #${i} ${o.kind} нет текста`, 'info');
      return;
    }
    this.stopEdit();
    const f = fontOf(l);
    const box = h('textarea', { class: 'cv-edit', spellcheck: 'false', rows: String(String(l.text).split('\n').length) });
    box.value = String(l.text);
    Object.assign(box.style, { fontFamily: f.family, fontWeight: f.bold ? '700' : '400', color: l.color || '#000',
      textAlign: ['left', 'center', 'right'].includes(l.align) ? l.align : 'left' });
    const span = this.els.get(i) && this.els.get(i).querySelector('.cv-label');
    if (span) span.style.visibility = 'hidden';
    const meter = h('span', { class: 'cv-meter' });
    Object.assign(meter.style, { fontFamily: f.family, fontWeight: f.bold ? '700' : '400' });
    this.editor = { i, l, o, box, span, meter };
    this.layer.append(meter, box);
    this.placeEditor();
    box.focus();
    box.select();
    box.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Escape') {
        e.preventDefault();
        this.stopEdit();
        this.stage.focus({ preventScroll: true });
      } else if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.commitText();
      }
    });
    box.addEventListener('input', () => this.placeEditor());
    box.addEventListener('blur', () => { if (this.editor && this.editor.box === box) this.commitText(); });
  }

  placeEditor() {
    const { o, l, box, meter } = this.editor;
    const z = this.z;
    const size = (Number(l.size) || 12) * FONT * z;
    const [left, top, right, bottom] = o.bbox;
    const whole = o.kind === 'Text';
    const x = whole ? left : Number(l.x) || left;
    const y = whole ? top : Number(l.y) || top;
    const lines = Math.max(1, box.value.split('\n').length);
    meter.style.fontSize = `${size}px`;
    meter.textContent = box.value || ' ';
    const w = Math.max(whole ? (right - left + 1) * z : 0, meter.offsetWidth + size * 0.6, 40);
    Object.assign(box.style, { left: `${x * z - 3}px`, top: `${y * z - 3}px`, fontSize: `${size}px`, lineHeight: String(LINE),
      width: `${w + 6}px`, height: `${Math.max(whole ? (bottom - y + 1) * z : 0, lines * size * LINE) + 6}px` });
  }

  stopEdit() {
    const ed = this.editor;
    this.editor = null;
    if (!ed) return;
    ed.box.remove();
    ed.meter.remove();
    if (ed.span) ed.span.style.visibility = '';
  }

  commitText() {
    const ed = this.editor;
    if (!ed) return;
    const next = ed.box.value.replace(/\r\n/g, '\n');
    this.stopEdit();
    this.stage.focus({ preventScroll: true });
    if (next === String(ed.l.text)) return;
    const el = this.els.get(ed.i);
    const span = el && el.querySelector('.cv-label');
    if (span) span.textContent = next;
    this.apply([{ op: 'text', object: ed.i, old: String(ed.l.text), new: next }], () => { if (span) span.textContent = String(ed.l.text); });
  }

  context(e) {
    const i = this.objAt(e);
    if (i === null || !this.loaded) return;
    e.preventDefault();
    if (!this.sel.has(i)) {
      this.sel = new Set([i]);
      this.markSel();
    }
    const o = this.s.scene.objects[i];
    const anchor = h('span', { class: 'cv-anchor', style: { left: `${e.clientX}px`, top: `${e.clientY}px` } });
    document.body.append(anchor);
    const items = [
      { header: tip(o) },
      label0(o) ? { label: 'Изменить текст', sub: 'двойной щелчок, Enter', value: 'text', icon: 'pencil' } : null,
      { label: this.sel.size > 1 ? `Копии рядом (${this.sel.size})` : 'Копия рядом', sub: 'Ctrl+D', value: 'copy', icon: 'copy' },
      this.hooks.mention ? { label: 'Спросить агента об этом', sub: 'вставить в сообщение', value: 'ask', icon: 'chat' } : null,
    ].filter(Boolean);
    openMenu(anchor, items, { onPick: (v) => {
      if (v === 'text') this.editText(i);
      else if (v === 'copy') this.duplicate();
      else if (v === 'ask') this.mention();
    } });
    setTimeout(() => anchor.remove(), 0);
  }

  mention() {
    const { scene } = this.s;
    const list = [...this.sel].map((i) => scene.objects[i]).filter(Boolean).map((o) => {
      const l = label0(o);
      const tags = [...new Set(o.tags || [])];
      return `#${o.index} ${o.kind}${l ? ` «${String(l.text).slice(0, 40)}»` : ''}${tags.length ? ` (${tags.slice(0, 2).join(', ')})` : ''}`;
    });
    if (list.length) this.hooks.mention(`На холсте, окно ${scene.window.number} «${scene.window.name}»: ${list.join('; ')} — `);
  }

  // --- the others at the canvas
  drawCursors(list) {
    this.cursors = Array.isArray(list) ? list : [];
    if (!this.loaded) return;
    const keep = new Set();
    for (const c of this.cursors) {
      keep.add(c.who);
      let el = this.layer.querySelector(`.cv-cur[data-who="${CSS.escape(c.who)}"]`);
      if (!el) {
        el = h('div', { class: 'cv-cur', dataset: { who: c.who } }, arrow(c.color),
          h('span', { class: 'cv-cur-name', text: c.who }), h('span', { class: 'cv-cur-say' }));
        el.style.setProperty('--c', c.color);
        this.layer.append(el);
      }
      el.style.transform = `translate(${c.x * this.z}px, ${c.y * this.z}px)`;
      el.classList.toggle('flip', c.x * this.z > this.sizer.offsetWidth - 200);
      const say = el.querySelector('.cv-cur-say');
      say.textContent = c.say || '';
      say.hidden = !c.say;
      el.dataset.at = String(c.at || Date.now());
      el.classList.toggle('idle', Date.now() - (c.at || 0) > IDLE);
    }
    for (const el of this.layer.querySelectorAll('.cv-cur')) if (!keep.has(el.dataset.who)) el.remove();
  }

  idle() {
    for (const el of this.layer.querySelectorAll('.cv-cur')) el.classList.toggle('idle', Date.now() - Number(el.dataset.at || 0) > IDLE);
  }

  flash(e) {
    if (!e || !this.loaded) return;
    this.flashes = { ...e, at: Date.now() };
    if (e.window === this.s.window) this.lightUp(this.flashes);
  }

  lightUp(f) {
    for (const i of f.objects || []) {
      const el = this.els.get(i);
      if (!el) continue;
      el.style.setProperty('--c', f.color || '#4cc3ff');
      el.classList.remove('flash');
      void el.offsetWidth;
      el.classList.add('flash');
    }
  }

  drawFoot(live) {
    if (!this.loaded) return;
    const { s } = this;
    const w = s.scene.window;
    const left = h('span', { class: 'cv-where' }, `Окно ${w.number} «${w.name}» · ${w.width}×${w.height}`);
    let mid = '';
    if (live) mid = live;
    else if (this.sel.size === 1) mid = tip(s.scene.objects[[...this.sel][0]] || { index: '?', kind: '' });
    else if (this.sel.size > 1) mid = `выделено: ${this.sel.size}`;
    else mid = `объектов: ${s.scene.objects.filter((o) => o.kind !== 'Window').length}`;
    const last = s.history && s.history.length ? s.history[s.history.length - 1] : null;
    const right = s.busy ? h('span', { class: 'cv-busy' }, h('i', { class: 'spin' }), s.busy)
      : last ? h('span', { class: 'cv-last', title: s.history.slice(-8).map((x) => `${x.who || 'снаружи'}: ${x.text}`).join('\n') },
        `${last.who === 'Человек' ? 'Вы' : last.who || 'Снаружи'}: ${last.text}`) : null;
    this.foot.replaceChildren(...[left, h('span', { class: 'cv-sel', text: mid }), right].filter(Boolean));
  }

  // --- actions
  async open(p, window) {
    if (this.hooks.focus) this.hooks.focus();
    try {
      await api.canvas.open(p, window);
    } catch (err) {
      toast(err.message, 'err', 12000);
      if (!this.loaded) this.empty(err.message);
    }
  }

  async pick() {
    try {
      const p = await api.canvas.pick();
      if (p) await this.open(p);
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async go(ref) {
    try {
      await api.canvas.window(ref);
    } catch (err) {
      toast(err.message, 'err');
      this.drawBar();
    }
  }

  async undo() {
    try {
      const out = await api.canvas.undo();
      toast(`Отменено: ${out.undone}`, 'info', 4000);
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async easybuilder() {
    try {
      await api.canvas.easybuilder();
      toast('Проект открыт в EasyBuilder. Сохраните там — холст обновится сам; правки холста EasyBuilder увидит, '
        + 'если открыть проект заново.', 'info', 9000);
    } catch (err) {
      toast(err.message, 'err', 9000);
    }
  }

  async menu(v) {
    try {
      if (v === 'pick') await this.pick();
      else if (v === 'reload') await api.canvas.reload();
      else if (v === 'reveal' && this.s && this.s.file) await api.shell.reveal(this.s.file);
      else if (v === 'close') await api.canvas.close();
    } catch (err) {
      toast(err.message, 'err');
    }
  }
}

export const isProject = (p) => /\.(cmtp|emtp)$/i.test(basename(p));
