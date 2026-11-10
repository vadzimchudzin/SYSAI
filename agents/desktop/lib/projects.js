'use strict';
// «Проекты» of the window: one folder per project on this computer. A project holds everything the engineer gives
// (materials/: the terms of reference, drawings, documentation, finished projects of the PLC and of the panel) and
// everything the agent makes (plc/: the PLC program as ST files and the PLCopen XML, hmi/: the panel project and its
// pictures, project.md: the memory of the project, style.md: the style it is made in). Files are copied in, never
// moved; nothing is deleted from here -- the person does that in the folder.
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

const PARTS = ['materials', 'plc', 'hmi'];
const MAX_COPY = 2 * 1024 * 1024 * 1024; // one file
const MAX_FILES = 3000;
const GROUPS = {
  plc: 'export project xml st gpc projectarchive smbp',
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
        plc: files.some((f) => f.rel.startsWith('plc/')), hmi: files.some((f) => f.rel.startsWith('hmi/')) });
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
    return { name: path.basename(dir), path: dir, materials: part('materials'), plc: part('plc'), hmi: part('hmi'),
      memory: fs.existsSync(path.join(dir, 'project.md')) ? path.join(dir, 'project.md') : '',
      style: fs.existsSync(path.join(dir, 'style.md')) ? path.join(dir, 'style.md') : '',
      other: files.filter((f) => !/^(materials|plc|hmi)\//.test(f.rel) && !/^(project|style)\.md$/.test(f.rel)),
      truncated: files.length >= MAX_FILES };
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

// What the person sends to the agent with the two buttons of a project. The work itself is the agent's (the
// skill plc-project with the tools plc-kit and ebpro-cmtp); the folder must be open to it for writing in SYSAI.
function prompts(info) {
  const where = `Папка проекта: ${info.path}`;
  return {
    analyze: `Проанализируй проект «${info.name}» по навыку plc-project. ${where}\n`
      + 'В materials лежит готовый проект: программа ПЛК и экраны панели. Разбери, как он устроен (plc_style, plc_check, '
      + 'экраны — ebpro-cmtp), запиши стиль в style.md, что сделано неудачно — отдельным разделом, и предложи стиль в Хаб.',
    build: `Собери проект «${info.name}» по навыку plc-project. ${where}\n`
      + 'По материалам в materials: выпиши в project.md все механизмы и сигналы (ничего не пропусти), напиши программу '
      + 'ПЛК на ST в plc, проверь (plc_check) и собери plc.xml, затем собери экраны панели в hmi с адресами из '
      + 'plc_hmi_tags; картинки — из Хранилища SYSAI, раздел «ОПО». Стиль: ',
  };
}

module.exports = { Projects, ProjectsError, prompts, groupOf, cleanName, PARTS };
