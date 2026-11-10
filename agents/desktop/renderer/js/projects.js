// «Проекты» in the working pane: a folder per project on this computer. Into a project the engineer drops everything
// there is (the terms of reference, drawings, documentation, finished projects of the PLC and of the panel); two
// buttons hand the work to the agent — «Проанализировать» (a finished project → its style) and «Собрать проект»
// (the materials → the PLC program and the panel screens). The agent works by the skill plc-project.
import { h, icon, clear, size, when, basename, kindIcon } from './dom.js';
import { toast } from './ui.js';

const api = window.sysai;
const GROUPS = [['plc', 'Программа ПЛК', 'code'], ['hmi', 'Экраны панели', 'canvas'], ['scheme', 'Схемы', 'pdf'],
  ['doc', 'Документы', 'doc'], ['image', 'Картинки', 'image'], ['other', 'Прочее', 'file']];

function message(title, text) {
  return h('div', { class: 'ex-msg' }, h('b', { text: title }), text ? h('span', { text }) : null);
}

export class ProjectsView {
  constructor(hooks = {}) {
    this.hooks = hooks; // open(path), ask(text) — put a request for the agent into the message box
    this.el = h('div', { class: 'pane-section projects' });
    this.name = localStorage.getItem('sysai.project') || '';
    this.started = false;
    this.seq = 0;
  }

  start() {
    if (this.started) return;
    this.started = true;
    if (this.name) this.open(this.name);
    else this.list();
  }

  refresh() {
    if (this.name) this.open(this.name);
    else this.list();
  }

  // --- the list of projects
  async list() {
    const seq = ++this.seq;
    this.name = '';
    localStorage.removeItem('sysai.project');
    let data = null;
    let error = '';
    try {
      data = await api.projects.list();
    } catch (err) {
      error = err.message || String(err);
    }
    if (seq !== this.seq) return;
    const bar = h('div', { class: 'ex-bar' }, h('div', { class: 'pj-title' }, h('b', { text: 'Проекты' }),
      data ? h('span', { text: data.root, title: 'Папка проектов на этом компьютере' }) : null),
    data ? h('button', { class: 'icon-btn sm', title: 'Открыть папку проектов', onclick: () => api.shell.open(data.root).catch(() => toast('Папки проектов ещё нет: создайте первый проект', 'info')) }, icon('folderOpen', 'sm')) : null,
    h('button', { class: 'icon-btn sm', title: 'Обновить', onclick: () => this.list() }, icon('refresh', 'sm')),
    h('button', { class: 'btn sm primary', onclick: () => this.create() }, icon('plus', 'sm'), h('span', { text: 'Новый проект' })));
    const list = h('div', { class: 'ex-list' });
    clear(this.el).append(bar, list);
    if (error) {
      list.append(message('Проекты не открылись', error));
      return;
    }
    if (!data.projects.length) {
      list.append(h('div', { class: 'pv-empty pj-empty' }, h('div', { class: 'big' }, icon('folder', 'xl')), h('b', { text: 'Проектов пока нет' }),
        h('div', { text: 'Проект — папка, куда складывают всё по объекту: ТЗ, схемы, документацию, готовые проекты ПЛК и '
          + 'панели. По ней агент разбирает готовое и собирает новое: программу ПЛК и экраны.' }),
        h('button', { class: 'btn primary', onclick: () => this.create() }, icon('plus', 'sm'), h('span', { text: 'Новый проект' }))));
      return;
    }
    for (const p of data.projects) {
      const tags = [p.style ? 'стиль' : '', p.plc ? 'ПЛК' : '', p.hmi ? 'экраны' : ''].filter(Boolean);
      list.append(h('button', { class: 'ex-row dir', title: p.path, onclick: () => this.open(p.name) },
        icon('folder', 'kind'), h('span', { class: 'nm', text: p.name }),
        ...tags.map((t) => h('span', { class: 'tag', text: t })),
        h('span', { class: 'meta', text: `${p.materials} ф. · ${when(p.updated, { short: true })}` })));
    }
  }

  create() {
    const input = h('input', { type: 'text', placeholder: 'Название проекта, например «ЕФБШ-621 Достоево»', maxlength: '80', spellcheck: 'false' });
    const go = async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const info = await api.projects.create(name);
        this.open(info.name);
      } catch (err) {
        toast(err.message, 'err');
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') go();
      else if (e.key === 'Escape') this.list();
    });
    const form = h('div', { class: 'pj-new' }, h('label', { class: 'field' }, icon('folder', 'sm'), input),
      h('button', { class: 'btn sm primary', onclick: go }, h('span', { text: 'Создать' })),
      h('button', { class: 'btn sm', onclick: () => this.list() }, h('span', { text: 'Отмена' })));
    const list = this.el.querySelector('.ex-list');
    if (!list) return;
    const old = list.querySelector('.pj-new');
    if (old) old.remove();
    const empty = list.querySelector('.pj-empty');
    if (empty) empty.remove();
    list.prepend(form);
    input.focus();
  }

  // --- one project
  async open(name) {
    const seq = ++this.seq;
    let info = null;
    try {
      info = await api.projects.info(name);
    } catch (err) {
      if (seq !== this.seq) return;
      toast(err.message, 'err');
      this.list();
      return;
    }
    if (seq !== this.seq) return;
    this.name = info.name;
    this.info = info;
    localStorage.setItem('sysai.project', info.name);
    const bar = h('div', { class: 'ex-bar' },
      h('button', { class: 'icon-btn sm', title: 'К списку проектов', onclick: () => this.list() }, icon('chevL', 'sm')),
      h('div', { class: 'pj-title' }, h('b', { text: info.name }), h('span', { text: info.path, title: info.path })),
      h('button', { class: 'icon-btn sm', title: 'Добавить файлы в материалы', onclick: () => this.pick() }, icon('upload', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Открыть папку проекта', onclick: () => api.shell.open(info.path).catch((e) => toast(e.message, 'err')) }, icon('folderOpen', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Обновить', onclick: () => this.open(info.name) }, icon('refresh', 'sm')));
    const acts = h('div', { class: 'pj-acts' },
      h('button', { class: 'btn soft', title: 'Готовый проект (ПЛК + экраны) в материалах → стиль: как он построен',
        disabled: !info.materials.length, onclick: () => this.ask('analyze') }, icon('search', 'sm'), h('span', { text: 'Проанализировать проект' })),
      h('button', { class: 'btn primary', title: 'По материалам: программа ПЛК на ST и экраны панели',
        disabled: !info.materials.length, onclick: () => this.ask('build') }, icon('play', 'sm'), h('span', { text: 'Собрать проект' })));
    const list = h('div', { class: 'ex-list' });
    clear(this.el).append(bar, acts, list);
    this._drop(list);
    if (!info.materials.length) {
      list.append(message('Материалов пока нет', 'Перетащите сюда всё, что есть по объекту: ТЗ, схемы, документацию, '
        + 'готовые проекты ПЛК (.export) и панели (.cmtp, .emtp) — файлы и целые папки. Они скопируются в проект.'));
    }
    const made = [['Память проекта', info.memory, 'fileText'], ['Стиль', info.style, 'fileText']].filter((x) => x[1]);
    if (made.length || info.plc.length || info.hmi.length) {
      list.append(h('div', { class: 'pj-group', text: 'Сделано агентом' }));
      for (const [label, p, ico] of made) {
        list.append(h('button', { class: 'ex-row', title: p, onclick: () => this.hooks.open(p) }, icon(ico, 'kind'),
          h('span', { class: 'nm', text: `${label} — ${basename(p)}` })));
      }
      for (const f of [...info.plc, ...info.hmi]) list.append(this._row(f));
    }
    for (const [key, label] of GROUPS) {
      const files = info.materials.filter((f) => f.group === key);
      if (!files.length) continue;
      list.append(h('div', { class: 'pj-group', text: `${label} · ${files.length}` }));
      for (const f of files) list.append(this._row(f, 'materials/'));
    }
    if (info.truncated) list.append(message('Файлов очень много', 'Показаны первые 3000.'));
  }

  _row(f, strip = '') {
    const rel = strip && f.rel.startsWith(strip) ? f.rel.slice(strip.length) : f.rel;
    const kind = f.group === 'plc' ? 'code' : f.group === 'hmi' ? 'image' : f.group === 'scheme' ? 'pdf' : f.group === 'doc' ? 'doc'
      : f.group === 'image' ? 'image' : 'file';
    return h('button', { class: 'ex-row', title: f.path, onclick: () => this.hooks.open(f.path) }, icon(kindIcon(kind), 'kind'),
      h('span', { class: 'nm', text: rel }), h('span', { class: 'meta', text: `${size(f.size)} · ${when(f.mtime, { short: true })}` }));
  }

  ask(what) {
    if (!this.info || !this.hooks.ask) return;
    this.hooks.ask(this.info.prompts[what]);
  }

  async pick() {
    const paths = await api.dialog.files({ title: `Добавить в проект «${this.name}»` }).catch(() => []);
    if (paths && paths.length) await this.add(paths);
  }

  async add(paths) {
    if (!this.name) return;
    const t = toast(`Копирую ${paths.length === 1 ? basename(paths[0]) : `${paths.length} шт.`}…`, 'info', 120000);
    try {
      const done = await api.projects.add(this.name, paths);
      t.remove();
      const bad = done.filter((d) => d.error);
      if (bad.length) toast(bad.map((d) => `${basename(d.path)}: ${d.error}`).join('\n'), 'err', 9000);
      if (done.length > bad.length) toast(`Добавлено в материалы: ${done.length - bad.length}`, 'ok', 3000);
    } catch (err) {
      t.remove();
      toast(err.message, 'err');
    }
    this.open(this.name);
  }

  _drop(list) {
    list.addEventListener('dragover', (e) => {
      if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); e.stopPropagation(); list.classList.add('drop'); }
    });
    list.addEventListener('dragleave', () => list.classList.remove('drop'));
    list.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      list.classList.remove('drop');
      const paths = [...(e.dataTransfer.files || [])].map((f) => api.pathForFile(f)).filter(Boolean);
      if (paths.length) this.add(paths);
    });
  }
}
