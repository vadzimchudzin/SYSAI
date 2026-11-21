// «Проекты» in the working pane: a folder per project on this computer. Into a project the engineer drops everything
// there is (the terms of reference, drawings, documentation, finished projects of the PLC and of the panel) and
// presses «Создать проект»: the agent goes through the stages — materials, decisions (controller, panel, link), the
// PLC program on ST, the build in Machine Expert, the panel screens — and marks each one; the strip of stages over
// the project shows where the work is. When the screens are done the window offers the documentation. A finished
// project of the past is turned into a style with «Разобрать пример». The agent works by the skills plc-project and
// project-docs.
import { h, icon, clear, size, when, basename, kindIcon } from './dom.js';
import { openMenu, toast } from './ui.js';
import { decisions, compileCard, workBeforeDocs } from './decisions.mjs';

const api = window.sysai;
const STATE_TEXT = { todo: 'не начат', work: 'идёт', done: 'готов', problem: 'нужен инженер' };
// the scope of a new project: the same keys as SCOPES of lib/projects.js and plcstage.py
const SCOPES = [
  ['full', 'Проект полностью', 'ПЛК → компиляция → экраны → документы'],
  ['plc', 'Только ПЛК', 'программа и компиляция'],
  ['hmi', 'Только экраны', 'по готовой программе ПЛК'],
];
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
    return JSON.stringify([info.pipeline.stages.map((s) => [s.state, s.note]), info.pipeline.facts, files, info.work.files, info.work.size,
      (info.sources || []).map((x) => [x.path, x.role]), (info.dialogs || []).map((d) => [d.id, d.messages])]);
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
    // what to make: the whole project, only the PLC program or only the screens — chosen before the folder is made
    let scope = 'full';
    const scopes = h('div', { class: 'pj-scope', role: 'radiogroup', 'aria-label': 'Что делаем' }, ...SCOPES.map(([key, label, sub]) => {
      const b = h('button', { class: `pj-scope-b${key === scope ? ' on' : ''}`, type: 'button', role: 'radio', 'aria-checked': String(key === scope),
        onclick: () => {
          scope = key;
          for (const x of scopes.children) {
            const on = x === b;
            x.classList.toggle('on', on);
            x.setAttribute('aria-checked', String(on));
          }
          input.focus();
        } }, h('b', { text: label }), h('span', { text: sub }));
      return b;
    }));
    const go = async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const info = await api.projects.create(name, scope);
        this.open(info.name);
      } catch (err) {
        toast(err.message, 'err');
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') go();
      else if (e.key === 'Escape') this.list();
    });
    const form = h('div', { class: 'pj-new' }, h('div', { class: 'pj-new-row' }, h('label', { class: 'field' }, icon('folder', 'sm'), input),
      h('button', { class: 'btn sm primary', onclick: go }, h('span', { text: 'Создать' })),
      h('button', { class: 'btn sm', onclick: () => this.list() }, h('span', { text: 'Отмена' }))), scopes);
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
      h('button', { class: 'icon-btn sm', title: 'Добавить из Хранилища SYSAI: откроется Хранилище, у файла или папки нажмите «В материалы проекта»',
        onclick: () => this.hooks.storage && this.hooks.storage({ name: info.name }) }, icon('archive', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Открыть папку проекта', onclick: () => api.shell.open(info.path).catch((e) => toast(e.message, 'err')) }, icon('folderOpen', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Обновить', onclick: () => this.open(info.name) }, icon('refresh', 'sm')),
      h('button', { class: 'icon-btn sm danger', title: 'Удалить проект…', onclick: () => this.remove() }, icon('trash', 'sm')));
    const list = h('div', { class: 'ex-list' });
    // what will open where and what the person chose: one answer of the main process for the tree, the compilation
    // card and the where-block. A failure of apps.detect/environment is not silence: the blocks would stay hidden and
    // the person would read it as «ничего не выбрано» — say it in a toast and in the where-block.
    const plan = api.apps.plan(info.path, info.pipeline.facts || {}).catch((err) => {
      toast(`Не удалось определить программы: ${err.message}`, 'err');
      return { error: err.message || String(err) };
    });
    clear(this.el).append(bar, this._pipeline(info), this._decisions(info, plan), this._compile(info, plan), this._where(info, plan),
      this._openers(info), this._actions(info), list);
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
    // the conversations about this project, kept with it (.sysai/dialogs.json)
    if ((info.dialogs || []).length) {
      list.append(h('div', { class: 'pj-group', text: `Разговоры · ${info.dialogs.length}` }));
      for (const d of info.dialogs.slice(0, 8)) {
        list.append(h('button', { class: 'ex-row', title: `${d.title || 'Без названия'}\nНачат ${String(d.started || '').slice(0, 16).replace('T', ' ')} UTC · сообщений: ${d.messages || 0}`,
          onclick: () => this.hooks.dialog && this.hooks.dialog(d.id) }, icon('chat', 'kind'),
        h('span', { class: 'nm', text: d.title || 'Без названия' }), h('span', { class: 'meta', text: when(Date.parse(d.updated), { short: true }) })));
      }
    }
    for (const [key, label] of GROUPS) {
      const files = info.materials.filter((f) => f.group === key);
      if (!files.length) continue;
      list.append(h('div', { class: 'pj-group', text: `${label} · ${files.length}` }));
      for (const f of files) list.append(this._row(f, 'materials/', this._source(info, f)));
    }
    if (info.truncated) list.append(message('Файлов очень много', 'Показаны первые 3000.'));
    if (scroll) list.scrollTop = scroll;
  }

  // «Открыть в программе»: кнопка на каждый формат, который уже есть в проекте. Нажатие открывает среду вендора на
  // этом компьютере и кладёт в неё наш результат — файл проекта (ONI, EasyBuilder, Machine Expert) или, у Haiwell,
  // программу в буфер обмена. Нет ни одного результата — строки нет вовсе, чтобы не занимать место.
  // «Где и что будет»: контроллер → программа, панель → программа и разрешение — до начала работы, а не после
  // первого нажатия. Строки берутся из фактов проекта (pipeline.json) и из файлов, которые в нём есть.
  _where(info, planned) {
    const box = h('div', { class: 'pj-where', hidden: true });
    planned.then((plan) => {
      if (!plan) return;
      if (plan.error) {   // the main process could not say what opens where: say why, do not stay silent
        box.append(h('div', { class: 'pj-where-row' }, icon('alert', 'sm'),
          h('span', { class: 'what', text: `Программы не определить: ${plan.error}` }),
          h('button', { class: 'btn sm', text: 'Повторить', onclick: () => this.open(info.name) })));
        box.hidden = false;
        return;
      }
      const env = (plan && plan.environment) || {};
      const openers = (plan && plan.openers) || [];
      const row = (side) => h('div', { class: 'pj-where-row' },
        h('span', { class: 'w', text: side.what === 'ПЛК' ? 'ПЛК' : 'Экраны' }),
        h('span', { class: 'what', text: side.fact || 'не выбран' }),
        h('span', { class: 'arr', text: '→' }),
        side.ready
          ? h('button', { class: 'prog', title: `${side.vendor}: ${side.serves}\n${side.file}\nНажмите — открыть в программе`,
            onclick: () => this.openIn({ id: side.id, title: side.program, file: side.file }) },
          h('img', { src: `img/format-${side.id}.svg`, alt: '' }),
          h('span', { text: side.program }))
          : h('span', { class: 'prog none', text: 'программы пока нет' }),
        side.what === 'Экраны' && env.resolution ? h('span', { class: 'tail', text: env.resolution }) : null);
      box.append(row(env.plc || { what: 'ПЛК' }), row(env.hmi || { what: 'Экраны' }));
      if (env.link) box.append(h('div', { class: 'pj-where-note', text: `Связь: ${String(env.link).toUpperCase()}` }));
      if (!(env.plc && env.plc.fact) || !(env.hmi && env.hmi.fact)) {
        box.append(h('div', { class: 'pj-where-hint', text: 'Скажите агенту, что за железо: «контроллер M241, панель cMT3072X, 1024×768» — '
          + 'он запишет это в память проекта, и окно будет открывать проекты в нужной программе.' }));
      }
      box.hidden = false;
    }).catch(() => {});
    return box;
  }

  _openers(info) {
    const row = h('div', { class: 'pj-open' });
    row.append(h('span', { class: 'pj-open-hint', text: 'Открыть в программе' }));
    api.apps.detect(info.path).then((list) => {
      if (!list || !list.length) {
        row.remove();
        return;
      }
      for (const a of list) {
        const hint = a.open === 'paste'
          ? 'откроется среда, программа — в буфере обмена: F7 → Ctrl+V → F9'
          : 'проект откроется в среде';
        row.append(h('button', {
          class: 'open-app', title: `${a.title}\n${a.file}\n${hint}`, onclick: () => this.openIn(a),
        }, h('img', { src: `img/format-${a.id}.svg`, alt: '' }), h('span', { text: a.title })));
      }
    }).catch(() => row.remove());
    return row;
  }

  async openIn(a) {
    try {
      const res = await api.apps.open(a.id, a.file);
      if (res && res.clipboard) {
        toast(res.auto
          ? `${a.title} открыт, вставка отправлена. Если текст не появился — F7, Ctrl+V, F9.`
          : `${a.title} открыт, программа в буфере обмена: F7 (окно STL) → Ctrl+V → F9.`, res.auto ? 'ok' : 'update', 0);
      } else {
        toast(`${a.title}: проект открыт`, 'ok');
      }
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  // The strip of stages: a step per stage (done — a tick, in work — a pulse, a problem — an alert, left out by the
  // scope — a dash) and the note of the stage in hand. A click on a step asks the agent to take that stage; a skipped
  // step is not work to do and is not a button.
  _pipeline(info) {
    const pl = info.pipeline;
    const live = pl.stages.filter((s) => !s.skipped);
    const current = live.find((s) => s.state === 'problem') || live.find((s) => s.state === 'work')
      || live.find((s) => s.stage === pl.next);
    let n = 0;
    const steps = h('div', { class: 'pj-steps' }, ...pl.stages.map((s) => {
      if (s.skipped) {
        return h('div', { class: 'pj-step skipped', title: `${s.title}: не делаем — объём проекта «${pl.facts.scope_title || ''}»` },
          h('span', { class: 'n' }, h('i', { text: '–' })), h('span', { class: 't', text: s.title }));
      }
      n += 1;
      return h('button', {
        class: `pj-step ${s.state}${current && current.stage === s.stage ? ' cur' : ''}`,
        title: `${s.title}: ${STATE_TEXT[s.state]}. ${s.goal}.${s.note ? `\n${s.note}` : ''}\nНажмите — поручить этот этап агенту.`,
        onclick: () => this.hooks.ask && this.hooks.ask(info.prompts.stages[s.stage], { name: info.name, path: info.path }),
      }, h('span', { class: 'n' }, s.state === 'done' ? icon('check', 'sm') : s.state === 'problem' ? icon('alert', 'sm') : h('i', { text: String(n) })),
      h('span', { class: 't', text: s.title }));
    }));
    const note = current && (current.note || pl.started) ? h('div', { class: `pj-note ${current.state}` },
      h('b', { text: `${current.title}: ${STATE_TEXT[current.state]}` }), current.note ? h('span', { text: current.note }) : null) : null;
    return h('div', { class: 'pj-pipe' }, steps, note);
  }

  // «Что выбрано»: the decisions of the project as a tree (decisions.mjs). A row that is not decided yet is a button: it
  // asks the agent to put that question to the person — a card, not a guess.
  _decisions(info, planned) {
    const box = h('div', { class: 'pj-tree', hidden: true });
    planned.then((plan) => {
      // The plan failed: the where-block below says why and offers to retry. An empty environment would draw
      // every row as «не выбран» — a lie about the project, not a state of it.
      if (!plan || plan.error) return;
      const pictures = (info.sources || []).filter((x) => x.role === 'picture-library').map((x) => basename(x.path));
      const root = decisions(info.pipeline, (plan && plan.environment) || {}, pictures);
      const decide = (info.prompts && info.prompts.decide) || {};
      let open = 0;
      const draw = (r) => {
        if (r.missing && r.ask) open += 1;
        const head = [h('span', { class: 'k', text: r.label }), h('span', { class: 'v', text: r.value }),
          r.why ? h('span', { class: 'why', text: r.why }) : null];
        const line = r.missing && r.ask && decide[r.ask]
          ? h('button', { class: 'pj-tree-row ask', type: 'button', title: 'Не решено. Нажмите — агент спросит карточкой',
            onclick: () => this.hooks.ask && this.hooks.ask(decide[r.ask], { name: info.name, path: info.path }) }, ...head)
          : h('div', { class: `pj-tree-row${r.missing ? ' none' : ''}` }, ...head);
        return h('li', {}, line, r.children.length ? h('ul', {}, ...r.children.map(draw)) : null);
      };
      const tree = h('ul', {}, draw(root));
      box.append(h('div', { class: 'pj-tree-hd' }, h('span', { text: 'Что выбрано' }),
        h('span', { text: open ? `не решено: ${open}` : 'всё решено' })), tree);
      box.hidden = false;
    });
    return box;
  }

  // «Компиляция»: the window does not compile. It opens the program of the controller with our project and asks the
  // person how it went; the answer goes to the agent — «да, окей», the next stage; errors, it goes through them.
  _compile(info, planned) {
    const box = h('div', { class: 'pj-compile', hidden: true });
    planned.then((plan) => {
      // The plan failed: there is no environment to lean on. An empty one would claim the project file
      // does not exist yet — the reason lives in the where-block, with a way to retry.
      if (!plan || plan.error) return;
      const card = compileCard(info.pipeline, (plan && plan.environment) || {});
      if (!card) return;
      const send = (what) => this.ask(what);
      box.append(h('div', { class: 'pj-compile-q' }, icon(card.state === 'problem' ? 'alert' : 'code', 'sm'),
        h('div', {}, h('b', { text: card.state === 'problem' ? 'Компиляция не прошла' : `Компиляция в ${card.program}` }),
          h('span', { text: card.open ? 'Откройте среду, скомпилируйте и скажите агенту, что вышло.'
            : 'Файла проекта ещё нет: агент соберёт его, потом — компиляция в среде.' }))),
      h('div', { class: 'pj-compile-acts' },
        card.open ? h('button', { class: 'open-app', onclick: () => this.openIn(card.open) },
          h('img', { src: `img/format-${card.open.id}.svg`, alt: '' }), h('span', { text: `Открыть ${card.open.title}` })) : null,
        h('button', { class: 'btn sm ok', onclick: () => send('compiled') }, icon('check', 'sm'), h('span', { text: 'Скомпилировалось' })),
        h('button', { class: 'btn sm warn', onclick: () => send('compileErrors') }, icon('alert', 'sm'), h('span', { text: 'Есть ошибки' }))));
      box.hidden = false;
    });
    return box;
  }

  _actions(info) {
    const pl = info.pipeline;
    const stage = (key) => pl.stages.find((s) => s.stage === key);
    const next = stage(pl.next);
    // the work before the documents is done: the last stage the scope has (the screens, or the build for «только ПЛК») —
    // decisions.mjs: workBeforeDocs, covered by a unit test
    const ready = workBeforeDocs(pl);
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

  // the material a file belongs to: the file or the folder lying right in materials/
  _source(info, f) {
    const top = f.rel.split('/').slice(0, 2).join('/');
    return (info.sources || []).find((x) => x.path === top) || null;
  }

  _role(info, f, src, anchor) {
    openMenu(anchor, [{ header: src.kind === 'folder' ? `Папка ${basename(src.path)} — для чего она` : 'Для чего этот файл' },
      ...(info.roles || []).map(([value, label, sub]) => ({ label, sub, value, checked: src.role === value }))], {
      minWidth: 260,
      onPick: (role) => api.projects.role(info.name, src.path, role).then(() => this.open(info.name, true))
        .catch((e) => toast(e.message, 'err')),
    });
  }

  _row(f, strip = '', src = null) {
    const rel = strip && f.rel.startsWith(strip) ? f.rel.slice(strip.length) : f.rel;
    const kind = f.rel.startsWith('docs/') ? 'doc' : f.group === 'plc' ? 'code' : f.group === 'hmi' ? 'image' : f.group === 'scheme' ? 'pdf' : f.group === 'doc' ? 'doc'
      : f.group === 'image' ? 'image' : 'file';
    const label = src && ((this.info && this.info.roles) || []).find(([key]) => key === src.role);
    const tag = src ? h('span', { class: `tag role${src.role === 'material' ? ' plain' : ''}`,
      title: `${src.origin === 'storage' ? `Из Хранилища: ${src.from || ''}\n` : ''}Нажмите — сказать, для чего это агенту`,
      text: `${label ? label[1] : 'Материал'}${src.origin === 'storage' ? ' · Хранилище' : ''}` }) : null;
    if (tag) tag.addEventListener('click', (e) => { e.stopPropagation(); this._role(this.info, f, src, tag); });
    return h('button', { class: 'ex-row', title: f.path, onclick: () => this.hooks.open(f.path) }, icon(kindIcon(kind), 'kind'),
      h('span', { class: 'nm', text: rel }), tag, h('span', { class: 'meta', text: `${size(f.size)} · ${when(f.mtime, { short: true })}` }));
  }

  // «Удалить проект»: a native question first, then the folder goes to the trash of the projects folder (it can be
  // brought back) or is erased with everything in it. The conversations about the project go with it.
  async remove() {
    const info = this.info;
    if (!this.name || !info) return;
    const parts = [`материалов ${info.materials.length}`, `результатов ${info.results.length}`];
    if (info.work && info.work.files) parts.push(`черновиков ${info.work.files}`);
    const ask = await api.ui.confirm({
      kind: 'warning',
      title: 'Удалить проект',
      message: `Удалить проект «${info.name}»?`,
      detail: `В проекте: ${parts.join(', ')}.\n\n`
        + '«В корзину» — папка переедет в скрытую .trash рядом с остальными проектами: место на диске освободится, '
        + 'вернуть можно, переименовав папку обратно.\n'
        + '«Удалить навсегда» — папка стирается вместе с материалами и результатами, вернуть нечем.',
      buttons: ['Отмена', 'В корзину', 'Удалить навсегда'],
      defaultId: 0,
      cancelId: 0,
    }).catch(() => ({ response: 0 }));
    if (!ask.response) return;
    const mode = ask.response === 1 ? 'trash' : 'erase';
    try {
      const done = await api.projects.delete(info.name, mode);
      toast(`Проект «${done.name}» ${done.mode === 'trash' ? `в корзине: ${done.path}` : 'удалён'}`
        + `${done.dialogs ? `, разговоров удалено: ${done.dialogs}` : ''}`, 'ok', 8000);
      this.name = '';
      this.info = null;
      if (this.hooks.deleted) this.hooks.deleted(info.name);
      this.list();
    } catch (err) {
      toast(`Проект не удалился: ${err.message}`, 'err', 9000);
    }
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
