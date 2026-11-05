// «Просмотр» in the working pane: one file at a time, shown the way its program would show it — pictures, PDF
// (Chromium's own viewer), video and sound, text and code with line numbers, Markdown, Excel and CSV (SheetJS),
// Word (mammoth) — and, for everything else, a card with «Запустить», «Открыть в программе», «Показать в папке».
import { h, icon, clear, size, when, basename, dirname, kindIcon } from './dom.js';
import { render as renderMd } from './md.js';
import { toast } from './ui.js';

const api = window.sysai;
const SHEET_ROWS = 3000;
const SHEET_COLS = 200;
const SCRIPTS = {};

function script(src, name) {
  if (window[name]) return Promise.resolve(window[name]);
  if (!SCRIPTS[src]) {
    SCRIPTS[src] = new Promise((resolve, reject) => {
      const el = document.createElement('script');
      el.src = src;
      el.onload = () => (window[name] ? resolve(window[name]) : reject(new Error(`${src}: нет ${name}`)));
      el.onerror = () => { delete SCRIPTS[src]; reject(new Error(`не загрузилась библиотека ${src}`)); };
      document.head.append(el);
    });
  }
  return SCRIPTS[src];
}

function btn(label, ico, onclick, cls = 'outline') {
  return h('button', { class: `btn sm ${cls}`, title: label, onclick }, icon(ico, 'sm'), h('span', { class: 'lbl', text: label }));
}

function iconBtn(title, ico, onclick) {
  return h('button', { class: 'icon-btn sm', title, onclick }, icon(ico, 'sm'));
}

const NAME_KIND = { video: 'Видео', audio: 'Звук', image: 'Картинка', pdf: 'PDF', sheet: 'Таблица', doc: 'Документ Word',
  markdown: 'Markdown', html: 'Страница HTML', code: 'Текст', run: 'Программа', archive: 'Архив', cad: 'Чертёж / модель',
  other: 'Файл' };

export class Preview {
  constructor(hooks = {}) {
    this.hooks = hooks; // attach(path), openFolder(path), onShown(target)
    this.el = h('div', { class: 'pane-section preview' });
    this.target = null;
    this.info = null;
    this.seq = 0;
    this.mode = '';
    this.empty();
  }

  get path() {
    return this.target ? this.target.path : '';
  }

  empty() {
    this.target = null;
    this.info = null;
    clear(this.el).append(h('div', { class: 'pv-body center' }, h('div', { class: 'pv-empty' },
      h('div', { class: 'big' }, icon('eye', 'xl')), h('b', { text: 'Здесь откроется файл' }),
      h('div', { text: 'Нажмите на путь в ответе агента, на файл в «Файлах» или в «Хранилище» — или попросите агента показать файл.' }))));
  }

  // target: {path, line?, stored?: {path, version, updated_at}} — stored: a copy from SYSAI's storage
  async show(target, { keepMode = false } = {}) {
    const seq = ++this.seq;
    const same = this.target && this.target.path === target.path;
    this.target = { ...target };
    if (!keepMode && !same) this.mode = '';
    const info = await api.fs.info(target.path).catch((err) => ({ exists: false, path: target.path, name: basename(target.path), error: err.message }));
    if (seq !== this.seq) return;
    this.info = info;
    if (info.exists && info.dir) {
      if (this.hooks.openFolder) this.hooks.openFolder(info.path);
      return;
    }
    const body = h('div', { class: 'pv-body' });
    clear(this.el).append(this._bar(info), ...this._notes(info), body);
    if (!info.exists) {
      body.classList.add('center');
      body.append(this._card(info, 'Файла нет', `Не найден: ${target.path}`, false));
      return;
    }
    try {
      await this._view(info, body, seq);
    } catch (err) {
      if (seq !== this.seq) return;
      clear(body).classList.add('center');
      body.append(this._card(info, 'Не получилось показать файл', err.message || String(err)));
    }
    if (seq === this.seq && this.hooks.onShown) this.hooks.onShown(this.target);
  }

  refresh() {
    if (this.target) this.show(this.target, { keepMode: true });
  }

  // after the agent changed a file: the one on the screen is drawn again
  refreshIf(paths) {
    if (!this.target) return;
    const low = this.target.path.toLowerCase();
    if (paths.some((p) => String(p || '').toLowerCase() === low)) this.refresh();
  }

  _bar(info) {
    const t = this.target;
    const acts = h('div', { class: 'pv-acts' });
    if (info.exists && info.runnable) acts.append(btn('Запустить', 'play', () => this._do(api.shell.run(info.path), 'Запущено'), 'primary'));
    if (info.exists) {
      acts.append(btn('Открыть в программе', 'external', () => this._do(api.shell.open(info.path))));
      acts.append(iconBtn('Показать в папке', 'folderOpen', () => this._do(api.shell.reveal(info.path))));
      acts.append(iconBtn('В чат — прикрепить к сообщению', 'paperclip', () => this.hooks.attach && this.hooks.attach(info.path)));
    }
    const toggles = { markdown: ['Исходный текст', 'Как документ'], html: ['Исходный код', 'Как страницу'] }[info.kind];
    if (info.exists && toggles) {
      const src = this.mode === 'source';
      acts.append(iconBtn(src ? toggles[1] : toggles[0], src ? 'eye' : 'code', () => { this.mode = src ? '' : 'source'; this.refresh(); }));
    }
    acts.append(iconBtn('Обновить', 'refresh', () => this.refresh()));
    const where = t.stored ? `Хранилище SYSAI · ${t.stored.path}` : dirname(info.path);
    return h('div', { class: 'pv-bar' }, h('span', { class: 'pv-kind' }, icon(kindIcon(info.kind || 'other'))),
      h('div', { class: 'pv-name' }, h('b', { text: info.name || basename(info.path), title: info.path }),
        h('span', { text: `\u200E${where}\u200E`, title: where })), acts);
  }

  _notes(info) {
    const out = [];
    const t = this.target;
    this._meta = '';
    if (t.stored) {
      const bits = ['Копия файла из Хранилища SYSAI'];
      if (t.stored.version) bits.push(`версия ${t.stored.version}`);
      if (t.stored.updated_at) bits.push(`изменён ${when(t.stored.updated_at)}`);
      out.push(h('div', { class: 'pv-note', text: `${bits.join(' · ').replace(/\.$/, '')}. Правки в программе останутся на этом компьютере.` }));
    }
    if (info.exists && info.size !== null && info.size !== undefined) {
      const meta = [NAME_KIND[info.kind] || 'Файл', size(info.size), info.mtime ? `изменён ${when(info.mtime)}` : ''].filter(Boolean);
      this._meta = meta.join(' · ');
    }
    return out;
  }

  async _do(promise, ok) {
    try {
      await promise;
      if (ok) toast(ok, 'ok', 2500);
    } catch (err) {
      toast(err.message || String(err), 'err');
    }
  }

  _card(info, title, text, actions = true) {
    const acts = h('div', { class: 'pv-card-acts' });
    if (actions && info.exists) {
      if (info.runnable) acts.append(btn('Запустить', 'play', () => this._do(api.shell.run(info.path), 'Запущено'), 'primary'));
      acts.append(btn('Открыть в программе', 'external', () => this._do(api.shell.open(info.path))));
      acts.append(btn('Показать в папке', 'folderOpen', () => this._do(api.shell.reveal(info.path))));
    }
    return h('div', { class: 'pv-empty' }, h('div', { class: 'big' }, icon(kindIcon(info.kind || 'other'), 'xl')),
      h('b', { text: title }), text ? h('div', { class: 'selectable', text }) : null,
      this._meta && info.exists ? h('div', { class: 'pv-meta', text: this._meta }) : null,
      acts.childElementCount ? acts : null);
  }

  async _url(info) {
    const url = await api.fs.url(info.path);
    return info.kind === 'pdf' ? url : `${url}?v=${Math.round(info.mtime || 0)}`;
  }

  async _view(info, body, seq) {
    const kind = info.kind;
    if (kind === 'image') return this._image(info, body);
    if (kind === 'pdf') {
      body.append(h('iframe', { class: 'pv-frame', src: await this._url(info), title: info.name }));
      return null;
    }
    if (kind === 'video') {
      body.classList.add('center');
      body.append(h('video', { class: 'pv-media', controls: true, preload: 'metadata', src: await this._url(info) }));
      return null;
    }
    if (kind === 'audio') {
      body.classList.add('center');
      body.append(h('div', { class: 'pv-empty' }, h('div', { class: 'big' }, icon('music', 'xl')), h('b', { text: info.name }),
        h('audio', { class: 'pv-audio', controls: true, preload: 'metadata', src: await this._url(info) })));
      return null;
    }
    if (kind === 'sheet') return this._sheet(info, body, seq);
    if (kind === 'doc') return this._docx(info, body, seq);
    if (kind === 'html' && this.mode !== 'source') {
      body.append(h('iframe', { class: 'pv-frame white', sandbox: '', src: await this._url(info), title: info.name }));
      return null;
    }
    if (kind === 'run' || kind === 'archive' || kind === 'cad' || /^(docm?|dotx?|pptx?|ppsx?|odt|odp|rtf|vsdx?|one|msg|eml)$/i.test(info.ext || '')) {
      body.classList.add('center');
      const why = kind === 'run' ? 'Программу можно запустить — или показать в папке.'
        : 'Такой файл окно не показывает — откройте его в программе.';
      body.append(this._card(info, info.name, why));
      return null;
    }
    return this._text(info, body, seq);
  }

  _image(info, body) {
    const wrap = h('div', { class: 'pv-imgwrap', title: 'Нажмите — в натуральную величину' });
    const img = h('img', { class: 'pv-img', alt: info.name });
    img.addEventListener('error', () => {
      clear(body).classList.add('center');
      body.append(this._card(info, info.name, 'Картинку этого формата окно не показывает — откройте её в программе.'));
    });
    wrap.addEventListener('click', () => wrap.classList.toggle('actual'));
    wrap.append(img);
    body.append(wrap);
    return this._url(info).then((src) => { img.src = src; });
  }

  async _text(info, body, seq) {
    const got = await api.fs.text(info.path);
    if (seq !== this.seq) return;
    if (got.binary) {
      body.classList.add('center');
      body.append(this._card(info, info.name, 'Это не текст — откройте файл в программе.'));
      return;
    }
    const notes = [];
    if (got.truncated) notes.push(`Показано начало файла: первые ${size(got.text.length)} из ${size(got.size)}.`);
    if (got.encoding && got.encoding !== 'UTF-8') notes.push(`Кодировка: ${got.encoding}.`);
    for (const n of notes) body.before(h('div', { class: 'pv-note', text: n }));
    if (info.kind === 'markdown' && this.mode !== 'source') {
      const md = h('div', { class: 'md' });
      renderMd(md, got.text);
      body.append(h('div', { class: 'pv-md' }, md));
      return;
    }
    const text = got.text.replace(/\r\n?/g, '\n');
    const count = text.split('\n').length;
    const lines = Array.from({ length: count }, (_, i) => i + 1).join('\n');
    const src = h('div', { class: 'src', text });
    const grid = h('div', { class: 'pv-code' }, h('div', { class: 'ln', text: lines }), src);
    body.append(grid);
    const line = Number(this.target.line || 0);
    if (line > 0 && line <= count) {
      requestAnimationFrame(() => {
        const lh = parseFloat(getComputedStyle(src).lineHeight) || 19.6;
        const top = (line - 1) * lh + (parseFloat(getComputedStyle(grid).paddingTop) || 0);
        grid.style.position = 'relative';
        grid.append(h('div', { class: 'hl', style: { position: 'absolute', left: 0, right: 0, top: `${top}px`, height: `${lh}px`, pointerEvents: 'none' } }));
        body.scrollTop = Math.max(0, top - body.clientHeight / 3);
      });
    }
  }

  async _sheet(info, body, seq) {
    body.classList.add('center');
    body.append(h('div', { class: 'spinner' }));
    const XLSX = await script('vendor/xlsx.full.min.js', 'XLSX');
    let book;
    if (/^(csv|tsv)$/.test(info.ext)) {
      const got = await api.fs.text(info.path);
      book = XLSX.read(got.text, { type: 'string', raw: false, FS: info.ext === 'tsv' ? '\t' : undefined });
    } else {
      const bytes = await api.fs.bytes(info.path);
      book = XLSX.read(bytes, { type: 'array', cellDates: true, cellNF: false, cellStyles: false });
    }
    if (seq !== this.seq) return;
    clear(body).classList.remove('center');
    const names = book.SheetNames || [];
    if (!names.length) {
      body.classList.add('center');
      body.append(this._card(info, info.name, 'В книге нет листов.'));
      return;
    }
    const scroll = h('div', { class: 'sheet-scroll' });
    const tabs = h('div', { class: 'sheet-tabs' });
    const note = h('div', { class: 'pv-note', hidden: true });
    const draw = (name) => {
      for (const b of tabs.children) b.classList.toggle('on', b.dataset.name === name);
      const { table, cut } = this._table(XLSX, book.Sheets[name]);
      clear(scroll).append(table);
      scroll.scrollTop = 0;
      scroll.scrollLeft = 0;
      note.hidden = !cut;
      note.textContent = cut ? `Показаны первые ${SHEET_ROWS} строк и ${SHEET_COLS} столбцов — целиком лист откроется в программе.` : '';
    };
    for (const name of names) tabs.append(h('button', { 'data-name': name, text: name, onclick: () => draw(name) }));
    body.append(h('div', { class: 'sheet-wrap' }, note, scroll, names.length > 1 ? tabs : null));
    draw(names[0]);
  }

  _table(XLSX, ws) {
    const table = h('table', { class: 'sheet' });
    if (!ws || !ws['!ref']) {
      table.append(h('tbody', {}, h('tr', {}, h('td', { text: 'Лист пуст' }))));
      return { table, cut: false };
    }
    const range = XLSX.utils.decode_range(ws['!ref']);
    const r1 = Math.min(range.e.r, range.s.r + SHEET_ROWS - 1);
    const c1 = Math.min(range.e.c, range.s.c + SHEET_COLS - 1);
    const cut = r1 < range.e.r || c1 < range.e.c;
    const head = h('tr', {}, h('th'));
    for (let c = range.s.c; c <= c1; c++) head.append(h('th', { text: XLSX.utils.encode_col(c) }));
    const tbody = h('tbody');
    const dense = Array.isArray(ws['!data']) ? ws['!data'] : null;
    for (let r = range.s.r; r <= r1; r++) {
      const tr = document.createElement('tr');
      const th = document.createElement('th');
      th.textContent = String(r + 1);
      tr.append(th);
      for (let c = range.s.c; c <= c1; c++) {
        const cell = dense ? (dense[r] || [])[c] : ws[XLSX.utils.encode_cell({ r, c })];
        const td = document.createElement('td');
        if (cell) {
          const v = cell.w !== undefined ? cell.w : cell.v instanceof Date ? cell.v.toLocaleDateString('ru-RU') : cell.v;
          td.textContent = v === undefined || v === null ? '' : String(v);
          if (cell.t === 'n') td.className = 'num';
          if (td.textContent.length > 40) td.title = td.textContent;
        }
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(h('thead', {}, head), tbody);
    return { table, cut };
  }

  async _docx(info, body, seq) {
    if (info.ext !== 'docx') {
      body.classList.add('center');
      body.append(this._card(info, info.name, 'Старый формат Word (.doc) окно не показывает — откройте его в программе.'));
      return;
    }
    body.classList.add('center');
    body.append(h('div', { class: 'spinner' }));
    const mammoth = await script('vendor/mammoth.browser.min.js', 'mammoth');
    const bytes = await api.fs.bytes(info.path);
    const out = await mammoth.convertToHtml({ arrayBuffer: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) });
    if (seq !== this.seq) return;
    clear(body).classList.remove('center');
    const paper = h('div', { class: 'paper' });
    paper.innerHTML = window.DOMPurify.sanitize(out.value || '<p></p>', { FORBID_TAGS: ['style', 'form', 'iframe', 'object', 'embed'], FORBID_ATTR: ['style'] });
    for (const a of paper.querySelectorAll('a[href]')) a.setAttribute('rel', 'noreferrer');
    body.append(paper);
  }
}
