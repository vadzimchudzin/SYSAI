// «Проекты» in the working pane: a folder per project on this computer. Into a project the engineer drops everything
// there is (the terms of reference, drawings, documentation, finished projects of the PLC and of the panel) and
// presses «Создать проект»: the agent goes through the stages — materials, decisions (controller, panel, link), the
// PLC program on ST, the build in Machine Expert, the panel screens — and marks each one; the strip of stages over
// the project shows where the work is. When the screens are done the window offers the documentation. A finished
// project of the past is turned into a style with «Разобрать пример». The agent works by the skills plc-project and
// project-docs.
import { h, icon, clear, size, when, basename, kindIcon } from './dom.js';
import { toast } from './ui.js';

const api = window.sysai;
const STATE_TEXT = { todo: 'не начат', work: 'идёт', done: 'готов', problem: 'нужен инженер' };
const FACTS = [['controller', 'Контроллер'], ['panel', 'Панель'], ['resolution', 'Экран'], ['link_title', 'Связь'], ['style', 'Стиль']];
const GROUPS = [['plc', 'Программа ПЛК', 'code'], ['hmi', 'Экраны панели', 'canvas'], ['scheme', 'Схемы', 'pdf'],
  ['doc', 'Документы', 'doc'], ['image', 'Картинки', 'image'], ['other', 'Прочее', 'file']];

function message(title, text) {
  return h('div', { class: 'ex-msg' }, h('b', { text: title }), text ? h('span', { text }) : null);
}

export class ProjectsView {
  constructor(hooks = {}) {
    // open(path), ask(text, {name, path}) — put a request for the agent into the message box of a conversation
    // opened in the folder of the project
    this.hooks = hooks;
    this.el = h('div', { class: 'pane-section projects' });
    this.name = localStorage.getItem('sysai.project') || '';
    this.started = false;
    this.seq = 0;
    this.stamp = '';
    // the agent marks stages and writes files while the person looks: the open project follows it
    setInterval(() => this._watch(), 4000);
  }

  async _watch() {
    if (!this.name || !this.el.isConnected || document.hidden || this.el.querySelector('.pj-new')) return;
    const seq = this.seq;
    const info = await api.projects.info(this.name).catch(() => null);
    if (info && seq === this.seq && this._stamp(info) !== this.stamp) this.open(this.name, true);
  }

  _stamp(info) {
    const files = [...info.materials, ...info.plc, ...info.hmi, ...info.docs].map((f) => `${f.rel}:${f.size}:${f.mtime}`);
    return JSON.stringify([info.pipeline.stages.map((s) => [s.state, s.note]), info.pipeline.facts, files, info.work.files, info.work.size]);
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
        h('div', { text: 'Проект — папка, куда складывают всё по объекту: ТЗ, схемы, чертежи. По ней агент создаёт проект '
          + 'с нуля: программу ПЛК на ST, проект Machine Expert, экраны панели и документацию.' }),
        h('button', { class: 'btn primary', onclick: () => this.create() }, icon('plus', 'sm'), h('span', { text: 'Новый проект' }))));
      return;
    }
    for (const p of data.projects) {
      const tags = [p.style ? 'стиль' : '', p.plc ? 'ПЛК' : '', p.hmi ? 'экраны' : '', p.docs ? 'документы' : ''].filter(Boolean);
      list.append(h('button', { class: 'ex-row dir', title: p.path, onclick: () => this.open(p.name) },
        icon('folder', 'kind'), h('span', { class: 'nm', text: p.name }),
        h('span', { class: 'pj-dots', title: 'Этапы проекта' }, ...(p.stages || []).map((st) => h('i', { class: st }))),
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
  async open(name, quiet = false) {
    const seq = ++this.seq;
    let info = null;
    try {
      info = await api.projects.info(name);
    } catch (err) {
      if (seq !== this.seq) return;
      if (!quiet) toast(err.message, 'err');
      this.list();
      return;
    }
    if (seq !== this.seq) return;
    const scroll = quiet && this.el.querySelector('.ex-list') ? this.el.querySelector('.ex-list').scrollTop : 0;
    this.name = info.name;
    this.info = info;
    this.stamp = this._stamp(info);
    localStorage.setItem('sysai.project', info.name);
    const bar = h('div', { class: 'ex-bar' },
      h('button', { class: 'icon-btn sm', title: 'К списку проектов', onclick: () => this.list() }, icon('chevL', 'sm')),
      h('div', { class: 'pj-title' }, h('b', { text: info.name }), h('span', { text: info.path, title: info.path })),
      h('button', { class: 'icon-btn sm', title: 'Добавить файлы в материалы', onclick: () => this.pick() }, icon('upload', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Открыть папку проекта', onclick: () => api.shell.open(info.path).catch((e) => toast(e.message, 'err')) }, icon('folderOpen', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Обновить', onclick: () => this.open(info.name) }, icon('refresh', 'sm')));
    const list = h('div', { class: 'ex-list' });
    clear(this.el).append(bar, this._pipeline(info), this._actions(info), list);
    this._drop(list);
    if (!info.materials.length) {
      list.append(message('Материалов пока нет', 'Перетащите сюда всё, что есть по объекту: ТЗ, схемы, чертежи, таблицы '
        + 'сигналов, пример руководства — файлы и целые папки. Они скопируются в проект. Потом нажмите «Создать проект».'));
    }
    // the results first: what the engineer takes to the site — the Machine Expert project, the panel project, the
    // documents; what else lies among them is offered to be moved to _work; the drafts are one folded line
    const made = [['Память проекта', info.memory, 'fileText'], ['Стиль', info.style, 'fileText']].filter((x) => x[1]);
    if (info.results.length || info.extra.length) {
      list.append(h('div', { class: 'pj-group', text: info.results.length ? `Результат · ${info.results.length}` : 'Результат' }));
      for (const f of info.results) list.append(this._row(f));
      if (!info.results.length) list.append(h('div', { class: 'pj-hint', text: 'Готовых файлов пока нет: проект Machine Expert появится в plc, проект панели — в hmi, документы — в docs.' }));
      if (info.extra.length) {
        const where = [...new Set(info.extra.map((f) => f.rel.split('/')[0]))].join(', ');
        list.append(h('div', { class: 'pj-extra' }, icon('alert', 'sm'),
          h('span', { text: `Лишнее в ${where}: ${info.extra.length} — черновики среди результатов`, title: info.extra.slice(0, 40).map((f) => f.rel).join('\n') }),
          h('button', { class: 'btn sm', title: 'Перенести (не удалить) эти файлы в _work — там же по папкам', onclick: () => this.tidy() },
            h('span', { text: 'Убрать в _work' }))));
      }
    }
    if (made.length || info.work.files) {
      list.append(h('div', { class: 'pj-group', text: 'Рабочее' }));
      for (const [label, p, ico] of made) {
        list.append(h('button', { class: 'ex-row', title: p, onclick: () => this.hooks.open(p) }, icon(ico, 'kind'),
          h('span', { class: 'nm', text: `${label} — ${basename(p)}` })));
      }
      if (info.work.files) {
        list.append(h('button', { class: 'ex-row dir', title: `${info.work.path}\nЧерновики агента: исходники ST, шаблоны, таблицы, заметки. Нажмите — открыть папку.`,
          onclick: () => api.shell.open(info.work.path).catch((e) => toast(e.message, 'err')) }, icon('folder', 'kind'),
        h('span', { class: 'nm', text: '_work — черновики' }), h('span', { class: 'meta', text: `${info.work.files} ф. · ${size(info.work.size)}` })));
      }
    }
    for (const [key, label] of GROUPS) {
      const files = info.materials.filter((f) => f.group === key);
      if (!files.length) continue;
      list.append(h('div', { class: 'pj-group', text: `${label} · ${files.length}` }));
      for (const f of files) list.append(this._row(f, 'materials/'));
    }
    if (info.truncated) list.append(message('Файлов очень много', 'Показаны первые 3000.'));
    if (scroll) list.scrollTop = scroll;
  }

  // The strip of stages: a step per stage (done — a tick, in work — a pulse, a problem — an alert), under it what
  // the agent decided (controller, panel, link) and the note of the stage in hand. A click on a step asks the agent
  // to take that stage.
  _pipeline(info) {
    const pl = info.pipeline;
    const current = pl.stages.find((s) => s.state === 'problem') || pl.stages.find((s) => s.state === 'work')
      || pl.stages.find((s) => s.stage === pl.next);
    const steps = h('div', { class: 'pj-steps' }, ...pl.stages.map((s, i) => h('button', {
      class: `pj-step ${s.state}${current && current.stage === s.stage ? ' cur' : ''}`,
      title: `${s.title}: ${STATE_TEXT[s.state]}. ${s.goal}.${s.note ? `\n${s.note}` : ''}\nНажмите — поручить этот этап агенту.`,
      onclick: () => this.hooks.ask && this.hooks.ask(info.prompts.stages[s.stage], { name: info.name, path: info.path }),
    }, h('span', { class: 'n' }, s.state === 'done' ? icon('check', 'sm') : s.state === 'problem' ? icon('alert', 'sm') : h('i', { text: String(i + 1) })),
    h('span', { class: 't', text: s.title }))));
    const facts = FACTS.filter(([k]) => pl.facts[k]).map(([k, label]) => h('span', { class: 'pj-fact' },
      h('i', { text: label }), h('b', { text: String(pl.facts[k]) })));
    const note = current && (current.note || pl.started) ? h('div', { class: `pj-note ${current.state}` },
      h('b', { text: `${current.title}: ${STATE_TEXT[current.state]}` }), current.note ? h('span', { text: current.note }) : null) : null;
    return h('div', { class: 'pj-pipe' }, steps, facts.length ? h('div', { class: 'pj-facts' }, ...facts) : null, note);
  }

  _actions(info) {
    const pl = info.pipeline;
    const stage = (key) => pl.stages.find((s) => s.stage === key);
    const next = stage(pl.next);
    const ready = stage('hmi').state === 'done';
    const docsDone = stage('docs').state === 'done';
    const acts = h('div', { class: 'pj-acts' });
    if (ready && !docsDone) {
      acts.append(h('div', { class: 'pj-offer' }, icon('book', 'lg'),
        h('div', {}, h('b', { text: 'Проект собран. Сделать документацию?' }),
          h('span', { text: 'Руководство пользователя и паспорт — по вашему образцу из материалов.' })),
        h('button', { class: 'btn primary', onclick: () => this.ask('docs') }, h('span', { text: 'Создать документацию' }))));
    }
    const label = !pl.started ? 'Создать проект' : docsDone ? 'Проект готов — пройти ещё раз' : `Продолжить: ${next ? next.title.toLowerCase() : 'проект'}`;
    acts.append(h('button', { class: `btn ${ready && !docsDone ? 'soft' : 'primary'}`,
      title: 'По материалам: программа ПЛК на ST, проект Machine Expert с компиляцией, экраны панели — этап за этапом',
      disabled: !info.materials.length, onclick: () => this.ask('create') }, icon(pl.started ? 'play' : 'spark', 'sm'), h('span', { text: label })),
    h('button', { class: 'btn soft', title: 'Готовый проект прошлых лет (ПЛК + экраны) в материалах → стиль: как он построен',
      disabled: !info.materials.length, onclick: () => this.ask('analyze') }, icon('search', 'sm'), h('span', { text: 'Разобрать пример' })));
    if (docsDone || (ready && info.docs.length)) {
      acts.append(h('button', { class: 'btn soft', title: 'Руководство пользователя и технический паспорт', onclick: () => this.ask('docs') },
        icon('book', 'sm'), h('span', { text: 'Документация' })));
    }
    return acts;
  }

  _row(f, strip = '') {
    const rel = strip && f.rel.startsWith(strip) ? f.rel.slice(strip.length) : f.rel;
    const kind = f.rel.startsWith('docs/') ? 'doc' : f.group === 'plc' ? 'code' : f.group === 'hmi' ? 'image' : f.group === 'scheme' ? 'pdf' : f.group === 'doc' ? 'doc'
      : f.group === 'image' ? 'image' : 'file';
    return h('button', { class: 'ex-row', title: f.path, onclick: () => this.hooks.open(f.path) }, icon(kindIcon(kind), 'kind'),
      h('span', { class: 'nm', text: rel }), h('span', { class: 'meta', text: `${size(f.size)} · ${when(f.mtime, { short: true })}` }));
  }

  async tidy() {
    if (!this.name) return;
    try {
      const done = await api.projects.tidy(this.name);
      const bad = done.filter((d) => d.error);
      if (bad.length) toast(bad.map((d) => `${d.rel}: ${d.error}`).slice(0, 8).join('\n'), 'err', 9000);
      if (done.length > bad.length) toast(`Перенесено в _work: ${done.length - bad.length}`, 'ok', 3000);
    } catch (err) {
      toast(err.message, 'err');
    }
    this.open(this.name);
  }

  ask(what) {
    if (!this.info || !this.hooks.ask) return;
    this.hooks.ask(this.info.prompts[what], { name: this.info.name, path: this.info.path });
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
