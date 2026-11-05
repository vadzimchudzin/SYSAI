// «Файлы» and «Хранилище» in the working pane: the folders of this computer (as the person sees them — the agent's
// own rights are checked where it acts, not here) and SYSAI's storage as it is opened to the agent.
import { h, icon, clear, size, when, debounce, basename, ext, kindIcon } from './dom.js';
import { openMenu, toast } from './ui.js';

const api = window.sysai;

const KINDS = {
  image: 'png jpg jpeg jfif gif webp bmp svg ico avif', video: 'mp4 m4v webm mov ogv mkv avi wmv',
  audio: 'mp3 wav ogg oga opus m4a aac flac weba', pdf: 'pdf', sheet: 'xlsx xlsm xlsb xls ods csv tsv',
  doc: 'docx doc odt rtf', markdown: 'md markdown', html: 'html htm', run: 'exe bat cmd msi lnk ps1 vbs url',
  archive: 'zip 7z rar tar gz tgz', cad: 'dwg dxf step stp stl', code: 'txt log json yaml yml xml py js ts sql ini cfg',
};
const EXT_KIND = {};
for (const [kind, list] of Object.entries(KINDS)) for (const e of list.split(' ')) EXT_KIND[e] = kind;
export function kindOfName(name) {
  return EXT_KIND[ext(name)] || 'other';
}

// C:\Users\Ivan → C:, Users, Ivan; \\server\share\x → \\server\share, x; /home/x → /, home, x
export function crumbsOf(p) {
  const s = String(p || '');
  if (!s) return [];
  if (s.startsWith('\\\\')) {
    const parts = s.slice(2).split('\\').filter(Boolean);
    const root = `\\\\${parts.slice(0, 2).join('\\')}`;
    const out = [{ label: root, path: root }];
    let acc = root;
    for (const part of parts.slice(2)) { acc += `\\${part}`; out.push({ label: part, path: acc }); }
    return out;
  }
  if (/^[a-zA-Z]:/.test(s)) {
    const parts = s.split(/[\\/]+/).filter(Boolean);
    const out = [{ label: parts[0].toUpperCase(), path: `${parts[0].toUpperCase()}\\` }];
    let acc = parts[0].toUpperCase();
    for (const part of parts.slice(1)) { acc += `\\${part}`; out.push({ label: part, path: acc }); }
    return out;
  }
  const out = [{ label: '/', path: '/' }];
  let acc = '';
  for (const part of s.split('/').filter(Boolean)) { acc += `/${part}`; out.push({ label: part, path: acc }); }
  return out;
}

function crumbsEl(items, go) {
  const el = h('div', { class: 'crumbs' });
  items.forEach((c, i) => {
    if (i) el.append(h('span', { class: 'sep' }, icon('chevR', 'sm')));
    el.append(h('button', { text: c.label, title: c.title || c.path, onclick: () => go(c.path) }));
  });
  requestAnimationFrame(() => { el.scrollLeft = el.scrollWidth; });
  return el;
}

function rowActs(...buttons) {
  return h('span', { class: 'row-acts' }, buttons.filter(Boolean).map(([title, ico, fn]) =>
    h('button', { class: 'icon-btn sm', title, onclick: (e) => { e.stopPropagation(); fn(); } }, icon(ico, 'sm'))));
}

function message(title, text) {
  return h('div', { class: 'ex-msg' }, h('b', { text: title }), text ? h('span', { text }) : null);
}

// ------------------------------------------------------------------------------------------------ this computer
export class Explorer {
  constructor(hooks = {}) {
    this.hooks = hooks; // open(path), attach(path)
    this.el = h('div', { class: 'pane-section explorer' });
    this.dir = localStorage.getItem('sysai.explorer.dir') || '';
    this.data = null;
    this.filter = '';
    this.seq = 0;
    this.places = null;
    this.started = false;
  }

  async start() {
    if (this.started) return;
    this.started = true;
    const [places, drives] = await Promise.all([api.fs.places().catch(() => []), api.fs.drives().catch(() => [])]);
    this.places = [...places, ...drives];
    let dir = this.dir;
    if (dir) {
      const info = await api.fs.info(dir).catch(() => null);
      if (!info || !info.exists || !info.dir) dir = '';
    }
    await this.go(dir || (places[0] && places[0].path) || (drives[0] && drives[0].path) || '/');
  }

  async go(dir) {
    if (!this.started) { this.dir = dir; await this.start(); if (this.dir === dir && this.data && this.data.path === dir) return; }
    const seq = ++this.seq;
    this.filter = '';
    let data = null;
    let error = '';
    try {
      data = await api.fs.list(dir);
    } catch (err) {
      error = err.message || String(err);
    }
    if (seq !== this.seq) return;
    this.dir = data ? data.path : dir;
    this.data = data;
    localStorage.setItem('sysai.explorer.dir', this.dir);
    this.draw(error);
  }

  refresh() {
    if (this.dir) this.go(this.dir);
  }

  draw(error = '') {
    const data = this.data;
    const up = h('button', { class: 'icon-btn sm', title: 'На уровень выше', disabled: !(data && data.parent),
      onclick: () => data && data.parent && this.go(data.parent) }, icon('up', 'sm'));
    const bar = h('div', { class: 'ex-bar' }, up, crumbsEl(crumbsOf(this.dir), (p) => this.go(p)),
      h('button', { class: 'icon-btn sm', title: 'Открыть папку в Проводнике', onclick: () => api.shell.open(this.dir).catch((e) => toast(e.message, 'err')) }, icon('external', 'sm')),
      h('button', { class: 'icon-btn sm', title: 'Обновить', onclick: () => this.refresh() }, icon('refresh', 'sm')));
    const places = h('div', { class: 'places' }, (this.places || []).map((p) =>
      h('button', { class: 'place', title: p.path, onclick: () => this.go(p.path) }, icon(p.icon === 'drive' ? 'drive' : p.icon === 'desktop' ? 'monitor' : p.icon === 'download' ? 'download' : p.icon === 'image' ? 'image' : p.icon === 'doc' ? 'fileText' : 'home', 'sm'), h('span', { text: p.label }))));
    const input = h('input', { type: 'search', placeholder: 'Фильтр по имени', spellcheck: 'false', value: this.filter });
    const list = h('div', { class: 'ex-list', tabindex: '0' });
    input.addEventListener('input', () => { this.filter = input.value; this._rows(list); });
    list.addEventListener('keydown', (e) => { if (e.key === 'Backspace' && data && data.parent) this.go(data.parent); });
    clear(this.el).append(bar, places, h('div', { class: 'ex-filter' }, h('label', { class: 'field' }, icon('search', 'sm'), input)), list);
    if (error) {
      list.append(message(/EPERM|EACCES|operation not permitted|permission/i.test(error) ? 'Нет доступа к папке' : 'Папку не открыть', error));
      return;
    }
    this._rows(list);
  }

  _rows(list) {
    clear(list);
    const data = this.data;
    if (!data) return;
    const q = this.filter.trim().toLowerCase();
    const entries = q ? data.entries.filter((e) => e.name.toLowerCase().includes(q)) : data.entries;
    if (!entries.length) {
      list.append(message(q ? 'Ничего не нашлось' : 'Папка пуста', q ? `В этой папке нет файлов со словом «${this.filter.trim()}».` : ''));
      return;
    }
    const frag = document.createDocumentFragment();
    for (const e of entries) {
      const row = h('button', { class: `ex-row${e.dir ? ' dir' : ''}`, title: e.path },
        icon(kindIcon(e.kind), 'kind'), h('span', { class: 'nm', text: e.name }),
        h('span', { class: 'meta', text: e.dir ? '' : [size(e.size), when(e.mtime, { short: true })].filter(Boolean).join(' · ') }),
        rowActs(e.dir ? null : ['В чат — прикрепить к сообщению', 'paperclip', () => this.hooks.attach(e.path)],
          ['Показать в Проводнике', 'folderOpen', () => api.shell.reveal(e.path).catch((err) => toast(err.message, 'err'))]));
      row.addEventListener('click', () => (e.dir ? this.go(e.path) : this.hooks.open(e.path)));
      row.addEventListener('dblclick', () => { if (!e.dir) api.shell.open(e.path).catch((err) => toast(err.message, 'err')); });
      row.addEventListener('contextmenu', (ev) => { ev.preventDefault(); this._menu(row, e); });
      frag.append(row);
    }
    list.append(frag);
    if (data.more) list.append(h('div', { class: 'ex-msg', text: 'Показаны первые 5000 — остальное в Проводнике.' }));
  }

  _menu(anchor, e) {
    const items = [];
    if (!e.dir) items.push({ label: 'Показать здесь', value: 'show', icon: 'eye' });
    else items.push({ label: 'Открыть папку', value: 'go', icon: 'folderOpen' });
    if (e.runnable) items.push({ label: 'Запустить', value: 'run', icon: 'play' });
    items.push({ label: e.dir ? 'Открыть в Проводнике' : 'Открыть в программе', value: 'open', icon: 'external' });
    items.push({ label: 'Показать в Проводнике', value: 'reveal', icon: 'folderOpen' });
    items.push({ sep: true });
    items.push({ label: 'Прикрепить к сообщению', value: 'attach', icon: 'paperclip' });
    items.push({ label: 'Копировать путь', value: 'copy', icon: 'copy' });
    openMenu(anchor, items, { onPick: (v) => {
      const fail = (err) => toast(err.message, 'err');
      if (v === 'show') this.hooks.open(e.path);
      else if (v === 'go') this.go(e.path);
      else if (v === 'run') api.shell.run(e.path).catch(fail);
      else if (v === 'open') api.shell.open(e.path).catch(fail);
      else if (v === 'reveal') api.shell.reveal(e.path).catch(fail);
      else if (v === 'attach') this.hooks.attach(e.path);
      else if (v === 'copy') api.clipboard.write(e.path).then(() => toast('Путь скопирован', 'ok', 2000));
    } });
  }
}

// ------------------------------------------------------------------------------------------------ SYSAI storage
export class StorageView {
  constructor(hooks = {}) {
    this.hooks = hooks; // open(localPath, stored), attach(path)
    this.el = h('div', { class: 'pane-section storage' });
    this.folder = '';
    this.data = null;
    this.q = '';
    this.seq = 0;
    this.started = false;
  }

  start() {
    if (this.started) return;
    this.started = true;
    this.go(localStorage.getItem('sysai.storage.dir') || '');
  }

  async go(folder, q = '') {
    this.started = true;
    const seq = ++this.seq;
    this.folder = folder || '';
    this.q = q;
    if (!q) this._loading();
    let data = null;
    let error = '';
    try {
      data = await api.storage.list(this.folder, q);
    } catch (err) {
      error = err.message || String(err);
    }
    if (seq !== this.seq) return;
    if (!data && folder && !q) { // a folder that went away: back to the sections
      localStorage.removeItem('sysai.storage.dir');
    }
    this.data = data;
    if (data && !q) localStorage.setItem('sysai.storage.dir', this.folder);
    this.draw(error);
  }

  refresh() {
    this.go(this.folder, this.q);
  }

  _loading() {
    if (this.el.querySelector('.ex-list')) return;
    clear(this.el).append(h('div', { class: 'pv-body center' }, h('div', { class: 'spinner' })));
  }

  draw(error = '') {
    const data = this.data;
    const parent = this.folder.includes('/') ? this.folder.slice(0, this.folder.lastIndexOf('/')) : '';
    const crumbs = [{ label: 'Хранилище', path: '', title: 'Разделы Хранилища, открытые агенту' }];
    let acc = '';
    for (const part of this.folder.split('/').filter(Boolean)) { acc = acc ? `${acc}/${part}` : part; crumbs.push({ label: part, path: acc }); }
    const canWrite = Boolean(data && data.can_write && this.folder);
    const upload = h('button', { class: 'icon-btn sm', title: canWrite ? 'Загрузить файлы сюда' : 'Сюда агенту писать нельзя', disabled: !canWrite,
      onclick: () => this.pickUpload() }, icon('upload', 'sm'));
    const bar = h('div', { class: 'ex-bar' },
      h('button', { class: 'icon-btn sm', title: 'На уровень выше', disabled: !this.folder, onclick: () => this.go(parent) }, icon('up', 'sm')),
      crumbsEl(crumbs, (p) => this.go(p)), upload,
      h('button', { class: 'icon-btn sm', title: 'Обновить', onclick: () => this.refresh() }, icon('refresh', 'sm')));
    const input = h('input', { type: 'search', placeholder: this.folder ? 'Поиск в этой папке' : 'Поиск по Хранилищу', spellcheck: 'false', value: this.q });
    const search = debounce(() => this.go(this.folder, input.value.trim().length >= 2 ? input.value.trim() : ''), 350);
    input.addEventListener('input', search);
    const list = h('div', { class: 'ex-list' });
    clear(this.el).append(bar, h('div', { class: 'ex-filter' }, h('label', { class: 'field' }, icon('search', 'sm'), input)), list);
    if (this.q) requestAnimationFrame(() => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); });
    this._drop(list, canWrite);
    if (error) {
      list.append(message(/не открыт|403|запрещ|нет доступа/i.test(error) ? 'Хранилище агенту не открыто' : 'Хранилище недоступно', error));
      return;
    }
    const items = (data && data.items) || [];
    if (!items.length) {
      list.append(message(this.q ? 'Ничего не нашлось' : this.folder ? 'Папка пуста' : 'Нет открытых разделов',
        this.q ? '' : this.folder ? (canWrite ? 'Перетащите сюда файлы, чтобы загрузить их.' : '') : 'Разделы Хранилища открывают агенту в SYSAI → Доступы → Хранилище.'));
      return;
    }
    for (const item of items) list.append(this._row(item));
  }

  _row(item) {
    const folder = item.is_folder || item.kind === 'section' || item.kind === 'folder';
    const kind = folder ? 'folder' : kindOfName(item.name);
    const meta = folder ? (item.files ? `${item.files} ф.` : '') : [size(item.size), when(item.updated_at, { short: true })].filter(Boolean).join(' · ');
    const row = h('button', { class: `ex-row${folder ? ' dir' : ''}`, title: item.note || item.path },
      icon(item.kind === 'section' ? 'archive' : kindIcon(kind), 'kind'), h('span', { class: 'nm', text: item.name }),
      item.by_agent ? h('span', { class: 'tag', text: 'агент' }) : null,
      h('span', { class: 'meta', text: meta }),
      folder ? null : rowActs(['В чат — прикрепить к сообщению', 'paperclip', () => this._fetch(item, row).then((p) => p && this.hooks.attach(p))],
        ['Открыть в программе', 'external', () => this._fetch(item, row).then((p) => p && api.shell.open(p).catch((e) => toast(e.message, 'err')))]));
    row.addEventListener('click', () => {
      if (folder) this.go(item.path);
      else this._fetch(item, row).then((p) => p && this.hooks.open(p, item));
    });
    return row;
  }

  async _fetch(item, row) {
    if (row.classList.contains('busy')) return null;
    row.classList.add('busy');
    const meta = row.querySelector('.meta');
    const was = meta ? meta.textContent : '';
    if (meta) meta.replaceChildren(h('span', { class: 'spinner' }));
    try {
      return await api.storage.fetch(item);
    } catch (err) {
      toast(`${item.name}: ${err.message}`, 'err');
      return null;
    } finally {
      row.classList.remove('busy');
      if (meta) meta.textContent = was;
    }
  }

  async pickUpload() {
    const paths = await api.dialog.files({ title: 'Загрузить в Хранилище SYSAI' }).catch(() => []);
    if (paths && paths.length) await this.upload(paths);
  }

  async upload(paths) {
    if (!(this.data && this.data.can_write && this.folder)) {
      toast('В эту папку Хранилища агенту писать нельзя', 'err');
      return;
    }
    const t = toast(`Загружаю ${paths.length === 1 ? basename(paths[0]) : `${paths.length} файла(ов)`}…`, 'info', 60000);
    try {
      const done = await api.storage.upload(paths, this.folder);
      t.remove();
      const bad = done.filter((d) => d.error);
      if (bad.length) toast(bad.map((d) => `${basename(d.path)}: ${d.error}`).join('\n'), 'err', 9000);
      if (done.length > bad.length) toast(`Загружено: ${done.length - bad.length}`, 'ok', 3000);
    } catch (err) {
      t.remove();
      toast(err.message, 'err');
    }
    this.refresh();
  }

  _drop(list, canWrite) {
    if (!canWrite) return;
    list.addEventListener('dragover', (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { e.preventDefault(); e.stopPropagation(); list.classList.add('drop'); } });
    list.addEventListener('dragleave', () => list.classList.remove('drop'));
    list.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      list.classList.remove('drop');
      const paths = [...(e.dataTransfer.files || [])].map((f) => api.pathForFile(f)).filter(Boolean);
      if (paths.length) this.upload(paths);
    });
  }
}
