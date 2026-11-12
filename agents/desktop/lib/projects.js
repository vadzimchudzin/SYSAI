'use strict';
// «Проекты» of the window: one folder per project on this computer. A project holds everything the engineer gives
// (materials/: the terms of reference, drawings, documentation, finished projects of the PLC and of the panel) and
// the results the agent makes — and only the results: plc/ holds the Machine Expert project, hmi/ the panel project,
// docs/ the manual and the passport. Everything on the way to them (ST sources, templates, tables, scripts, notes,
// pictures) lives in _work/, which the window shows folded. project.md is the memory of the project, style.md the
// style it is made in, pipeline.json where the work is — the agent marks the stages with the tool plc_stage_update of
// plc-kit, the window shows them. Materials are copied in, never moved; nothing is deleted from here — «Убрать в
// _work» only moves what does not belong among the results.
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

const PARTS = ['materials', 'plc', 'hmi', 'docs', '_work'];
const WORK = '_work';
// What a folder of results holds — the same as hub/seed/plc-kit/plcstage.py RESULTS (a test compares); the rest of
// what lies there is a draft and belongs to _work/.
const RESULTS = { plc: ['.project', '.projectarchive'], hmi: ['.cmtp', '.emtp', '.cxob', '.exob'], docs: ['.docx', '.pdf'] };

// What Machine Expert and EasyBuilder keep next to a project themselves (the lock of an open project, the options of
// the user, the caches of a compilation, backups) — the same as plcstage.py SERVICE: not a result and not a draft, it
// stays where the program put it.
const SERVICE = ['.~u', '.opt', '.precompilecache', '.bootinfo', '.bootinfo_guids', '.compileinfo', '.map', '.settings.conf', '.backup', '.bak'];

function isResult(rel) {
  const part = rel.split('/')[0];
  const low = rel.toLowerCase();
  return Boolean(RESULTS[part]) && RESULTS[part].some((e) => low.endsWith(e));
}

function isService(rel) {
  const low = rel.toLowerCase();
  return Boolean(RESULTS[rel.split('/')[0]]) && SERVICE.some((e) => low.endsWith(e));
}

// Where «Убрать в _work» puts a draft: the same path under _work/ — but the sources of the program lying right in
// plc/ (the layout before _work: *.st and project.json) go to _work/st, where the skill and plc_project_build look
// for them. Only when _work/st holds no program yet: two programs in one folder would be built as one.
function workPlace(rel, stFree) {
  const parts = rel.split('/');
  const name = parts[parts.length - 1].toLowerCase();
  if (stFree && parts.length === 2 && parts[0] === 'plc' && (name.endsWith('.st') || name === 'project.json')) return ['st'];
  return parts.slice(0, -1);
}
// The stages of a project «с нуля под ключ», in order — the same as hub/seed/plc-kit/plcstage.py (a test compares).
const STAGES = [
  ['materials', 'Материалы', 'ТЗ и схемы прочитаны, механизмы и сигналы выписаны'],
  ['decisions', 'Решения', 'Контроллер, панель, связь, стиль'],
  ['plc', 'Программа ПЛК', 'Программа на ST написана и проверена'],
  ['build', 'Компиляция', 'Проект Machine Expert компилируется без ошибок'],
  ['hmi', 'Экраны', 'Экраны панели собраны, теги привязаны'],
  ['docs', 'Документы', 'Руководство пользователя и паспорт'],
];
const STATES = ['todo', 'work', 'done', 'problem'];
const FACTS = ['controller', 'panel', 'resolution', 'link', 'style', 'machine_expert', 'project_file', 'hmi_file', 'doc_file'];
const LINKS = { opcua: 'OPC UA', modbus_tcp: 'Modbus TCP', modbus_rtu: 'Modbus RTU' };
const MAX_COPY = 2 * 1024 * 1024 * 1024; // one file
const MAX_FILES = 3000;
const GROUPS = {
  plc: 'export project xml st gpc projectarchive smbp library',
  hmi: 'cmtp emtp cxob exob mtp fsprj',
  scheme: 'pdf dwg dxf',
  doc: 'docx doc xlsx xls xlsm csv txt md rtf odt',
  image: 'png jpg jpeg bmp gif webp svg',
};
const EXT_GROUP = {};
for (const [g, list] of Object.entries(GROUPS)) for (const e of list.split(' ')) EXT_GROUP[e] = g;
const BAD_NAME = /[<>:"/\\|?*\u0000-\u001f]/;
const RESERVED = /^(con|prn|aux|nul|com\d|lpt\d)$/i;

class ProjectsError extends Error {}

function groupOf(name) {
  const e = path.extname(name).slice(1).toLowerCase();
  return EXT_GROUP[e] || 'other';
}

function cleanName(name) {
  const n = String(name || '').trim().replace(/\s+/g, ' ').replace(/[. ]+$/, '');
  if (!n) throw new ProjectsError('у проекта нужно название');
  if (n.length > 80) throw new ProjectsError('название длиннее 80 знаков');
  if (BAD_NAME.test(n) || RESERVED.test(n) || n === '.' || n === '..') {
    throw new ProjectsError('в названии нельзя: \\ / : * ? " < > |');
  }
  return n;
}

function template(name) {
  return `# ${name}\n\n`
    + 'Память проекта: её ведёт агент и читает в начале каждой работы. Коротко и по фактам.\n\n'
    + '## Объект\n\n_Что за объект, заказчик, шифр._\n\n'
    + '## Оборудование и сигналы\n\n_Перечень механизмов и их сигналов по схемам — с каналами модулей._\n\n'
    + '## Решения\n\n_Контроллер, панель, связь (Modbus / OPC UA), стиль._\n\n'
    + '## Сделано и проверено\n\n_Что готово, чем проверено, что должен проверить инженер._\n';
}

// pipeline.json of a project as the window shows it: every stage with its title, state and note; the facts the
// agent wrote down (controller, panel, link…); the stage to do next. A missing or broken file — nothing started.
function pipeline(dir) {
  let data = {};
  try {
    data = JSON.parse(fs.readFileSync(path.join(dir, 'pipeline.json'), 'utf8').replace(/^\uFEFF/, ''));
  } catch { /* not started */ }
  const src = data && typeof data.stages === 'object' && data.stages ? data.stages : {};
  const stages = STAGES.map(([stage, title, goal]) => {
    const s = src[stage] && typeof src[stage] === 'object' ? src[stage] : {};
    return { stage, title, goal, state: STATES.includes(s.state) ? s.state : 'todo', note: String(s.note || '').slice(0, 500),
      updated: String(s.updated || '') };
  });
  const facts = {};
  const f = data && typeof data.facts === 'object' && data.facts ? data.facts : {};
  for (const k of FACTS) if (f[k] !== undefined && f[k] !== null && f[k] !== '') facts[k] = f[k];
  if (typeof facts.link === 'string') facts.link_title = LINKS[facts.link] || facts.link;
  if (Array.isArray(facts.resolution)) facts.resolution = facts.resolution.join('×');
  const next = stages.find((s) => s.state !== 'done');
  return { stages, facts, next: next ? next.stage : '', done: stages.filter((s) => s.state === 'done').length,
    started: stages.some((s) => s.state !== 'todo') };
}

class Projects {
  constructor(root) {
    this.root = path.resolve(root);
  }

  dir(name) {
    const p = path.join(this.root, cleanName(name));
    if (path.dirname(p) !== this.root) throw new ProjectsError('нет такого проекта');
    return p;
  }

  async _walk(dir, rel, out, depth = 0) {
    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name, 'ru', { numeric: true }));
    for (const e of entries) {
      if (out.length >= MAX_FILES) return;
      if (e.name.startsWith('.') || e.name.startsWith('~$')) continue;
      const p = path.join(dir, e.name);
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) {
        if (depth < 6) await this._walk(p, r, out, depth + 1);
      } else if (e.isFile()) {
        const st = await fsp.stat(p).catch(() => null);
        if (st) out.push({ name: e.name, rel: r, path: p, size: st.size, mtime: st.mtimeMs, group: groupOf(e.name) });
      }
    }
  }

  async list() {
    let entries = [];
    try {
      entries = await fsp.readdir(this.root, { withFileTypes: true });
    } catch (err) {
      if (err.code !== 'ENOENT') throw new ProjectsError(`папка проектов не читается: ${err.message}`);
    }
    const out = [];
    for (const e of entries) {
      if (!e.isDirectory() || e.name.startsWith('.')) continue;
      const dir = path.join(this.root, e.name);
      const files = [];
      await this._walk(dir, '', files);
      const st = await fsp.stat(dir).catch(() => null);
      out.push({ name: e.name, path: dir, files: files.length,
        materials: files.filter((f) => f.rel.startsWith('materials/')).length,
        updated: Math.max(st ? st.mtimeMs : 0, ...files.map((f) => f.mtime)),
        style: fs.existsSync(path.join(dir, 'style.md')),
        plc: files.some((f) => f.rel.startsWith('plc/')), hmi: files.some((f) => f.rel.startsWith('hmi/')),
        docs: files.some((f) => f.rel.startsWith('docs/')), stages: pipeline(dir).stages.map((s) => s.state) });
    }
    out.sort((a, b) => b.updated - a.updated);
    return { root: this.root, projects: out };
  }

  async create(name) {
    const dir = this.dir(name);
    if (fs.existsSync(dir)) throw new ProjectsError(`проект «${cleanName(name)}» уже есть`);
    for (const part of PARTS) await fsp.mkdir(path.join(dir, part), { recursive: true });
    await fsp.writeFile(path.join(dir, 'project.md'), template(cleanName(name)), 'utf8');
    return this.info(name);
  }

  async info(name) {
    const dir = this.dir(name);
    const st = await fsp.stat(dir).catch(() => null);
    if (!st || !st.isDirectory()) throw new ProjectsError(`нет проекта «${cleanName(name)}»`);
    const files = [];
    await this._walk(dir, '', files);
    const part = (p) => files.filter((f) => f.rel.startsWith(`${p}/`));
    const made = [...part('plc'), ...part('hmi'), ...part('docs')];
    const work = part(WORK);
    return { name: path.basename(dir), path: dir, materials: part('materials'), plc: part('plc'), hmi: part('hmi'),
      docs: part('docs'), pipeline: pipeline(dir),
      // the results (what the engineer takes to the site), what lies among them but is not one, and the drafts
      results: made.filter((f) => isResult(f.rel)), extra: made.filter((f) => !isResult(f.rel) && !isService(f.rel)),
      work: { path: path.join(dir, WORK), files: work.length, size: work.reduce((n, f) => n + f.size, 0) },
      memory: fs.existsSync(path.join(dir, 'project.md')) ? path.join(dir, 'project.md') : '',
      style: fs.existsSync(path.join(dir, 'style.md')) ? path.join(dir, 'style.md') : '',
      other: files.filter((f) => !/^(materials|plc|hmi|docs|_work)\//.test(f.rel) && !/^(project\.md|style\.md|pipeline\.json)$/.test(f.rel)),
      truncated: files.length >= MAX_FILES };
  }

  // «Убрать в _work»: what lies in plc/, hmi/, docs/ and is not a result is moved (never deleted) to
  // _work/<the same path> (the program from plc/ — to _work/st, see workPlace); a name that is taken there gets
  // « (2)»; what Machine Expert and EasyBuilder keep themselves stays. -> [{rel, to} | {rel, error}]
  async tidy(name) {
    const dir = this.dir(name);
    const info = await this.info(name);
    const done = [];
    const stFree = !(await fsp.readdir(path.join(dir, WORK, 'st')).catch(() => [])).some((n) => n.toLowerCase().endsWith('.st'));
    for (const f of info.extra) {
      try {
        const target = path.join(dir, WORK, ...workPlace(f.rel, stFree));
        await fsp.mkdir(target, { recursive: true });
        const to = await this._free(target, f.name);
        await fsp.rename(f.path, to);
        done.push({ rel: f.rel, to: path.relative(dir, to).split(path.sep).join('/') });
      } catch (err) {
        done.push({ rel: f.rel, error: err.code === 'EBUSY' || err.code === 'EPERM' ? 'файл занят программой' : err.message });
      }
    }
    for (const part of Object.keys(RESULTS)) await this._prune(path.join(dir, part), false);
    return done;
  }

  // Folders left empty after the move go; the folder of results itself stays.
  async _prune(dir, self = true) {
    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) if (e.isDirectory()) await this._prune(path.join(dir, e.name));
    if (self) await fsp.rmdir(dir).catch(() => {});
  }

  // Copies files and whole folders into materials/; a name that is taken gets « (2)», « (3)»…
  async add(name, paths) {
    const target = path.join(this.dir(name), 'materials');
    await fsp.mkdir(target, { recursive: true });
    const done = [];
    for (const src of (Array.isArray(paths) ? paths : []).slice(0, 500)) {
      const from = path.resolve(String(src));
      try {
        const st = await fsp.stat(from);
        if (from === target || target.startsWith(from + path.sep)) throw new ProjectsError('это папка самого проекта');
        const to = await this._free(target, path.basename(from));
        if (st.isDirectory()) {
          await fsp.cp(from, to, { recursive: true, errorOnExist: true, force: false,
            filter: (p) => !path.basename(p).startsWith('~$') });
        } else {
          if (st.size > MAX_COPY) throw new ProjectsError('файл больше 2 ГБ');
          await fsp.copyFile(from, to, fs.constants.COPYFILE_EXCL);
        }
        done.push({ path: from, to });
      } catch (err) {
        done.push({ path: from, error: err instanceof ProjectsError ? err.message : err.code === 'ENOENT' ? 'нет такого файла' : err.message });
      }
    }
    return done;
  }

  async _free(dir, base) {
    const ext = path.extname(base);
    const stem = base.slice(0, base.length - ext.length);
    for (let i = 1; i < 1000; i++) {
      const p = path.join(dir, i === 1 ? base : `${stem} (${i})${ext}`);
      if (!fs.existsSync(p)) return p;
    }
    throw new ProjectsError('слишком много файлов с таким именем');
  }
}

// What the person sends to the agent with the buttons of a project. The work itself is the agent's (the skills
// plc-project and project-docs with the tools plc-kit, machine-expert, ebpro-cmtp, pic-index, doc-kit); the folder
// must be open to it for writing in SYSAI.
function prompts(info) {
  const where = `Папка проекта: ${info.path}`;
  const pl = info.pipeline || pipeline(info.path);
  const next = pl.stages.find((s) => s.stage === pl.next);
  const resume = pl.started && next
    ? `Работа уже начата: продолжай с этапа «${next.title}» (ход — в pipeline.json, решения — в project.md).`
    : 'Начни с материалов.';
  return {
    analyze: `Проанализируй проект «${info.name}» по навыку plc-project. ${where}\n`
      + 'В materials лежит готовый проект: программа ПЛК и экраны панели. Разбери, как он устроен (plc_style, plc_check, '
      + 'экраны — ebpro-cmtp), запиши стиль в style.md, что сделано неудачно — отдельным разделом, и предложи стиль в Хаб.',
    create: `Создай проект «${info.name}» полностью, с нуля, по навыку plc-project. ${where}\n`
      + 'В materials — всё, что есть: ТЗ, схемы, чертежи. Иди по этапам и отмечай каждый (plc_stage_update): материалы → '
      + 'решения (контроллер, панель и связь OPC UA или Modbus — plc_link по ТЗ; шаблон проекта) → программа ПЛК на ST → файл '
      + 'проекта Machine Expert (plc_project_build по шаблону; компиляция — me_build, без шаблона — me_create; исправляй ошибки '
      + 'до нуля) → экраны панели (картинки — pic-index, теги — plc_hmi_tags) → '
      + 'в конце предложи мне документацию. В plc, hmi и docs — только готовые файлы (.project, проект панели, .docx); '
      + `исходники ST, таблицы, скрипты и заметки — в _work. Чего нет в материалах — спроси, не додумывай. ${resume}\nСтиль: `,
    build: `Собери проект «${info.name}» по навыку plc-project. ${where}\n`
      + 'По материалам в materials: выпиши в project.md все механизмы и сигналы (ничего не пропусти), напиши программу '
      + 'ПЛК на ST в _work/st, проверь (plc_check) и собери проект в plc, затем собери экраны панели в hmi с адресами из '
      + 'plc_hmi_tags; картинки — из Хранилища SYSAI, раздел «ОПО». В plc, hmi и docs — только готовые файлы, всё '
      + 'остальное — в _work. Стиль: ',
    docs: `Создай техническую документацию проекта «${info.name}» по навыку project-docs. ${where}\n`
      + 'Руководство пользователя (оператора) и технический паспорт — по материалам, программе в plc и экранам в hmi; '
      + 'снимки экранов — cmtp_render. Если в materials есть пример руководства (.docx) — сделай по нему (doc_outline, '
      + 'style_from); если нет — спроси меня, по какому образцу. Готовые .docx — в docs, текст и рисунки к ним — в '
      + '_work/docs; этап docs отметь (plc_stage_update).',
    stage: (stage) => {
      const s = pl.stages.find((x) => x.stage === stage);
      return s ? `Проект «${info.name}», навык plc-project. ${where}\nЗаймись этапом «${s.title}» (${s.goal.toLowerCase()})`
        + `${s.note ? `. Сейчас о нём записано: ${s.note}` : ''}. Предыдущие этапы и решения — в pipeline.json и project.md.` : '';
    },
  };
}

module.exports = { Projects, ProjectsError, prompts, pipeline, groupOf, cleanName, isResult, isService, workPlace, PARTS, STAGES, RESULTS, SERVICE, WORK };
