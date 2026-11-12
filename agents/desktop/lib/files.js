'use strict';
// Files of the computer for the window: what a file is (how to show it), folders, text in any of the
// encodings Windows leaves around (UTF-8, UTF-16, Windows-1251), the places and the drives.
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const MAX_ENTRIES = 5000;
const TEXT_LIMIT = 2 * 1024 * 1024;
const BYTES_LIMIT = 60 * 1024 * 1024;

const KINDS = {
  image: 'png jpg jpeg jfif gif webp bmp svg ico avif',
  video: 'mp4 m4v webm mov ogv mkv',
  audio: 'mp3 wav ogg oga opus m4a aac flac weba',
  pdf: 'pdf',
  sheet: 'xlsx xlsm xlsb xls ods csv tsv',
  doc: 'docx',
  markdown: 'md markdown mdown',
  html: 'html htm xhtml',
  code: 'txt log ini cfg conf config json jsonc json5 yaml yml toml xml xaml xsd csproj props targets resx ' +
    'py pyw pyi js mjs cjs ts tsx jsx vue css scss sass less sql sh bash zsh fish ps1 psm1 psd1 bat cmd ' +
    'c h cc cpp cxx hpp hh cs java kt kts go rs rb php lua r m mm swift vb vbs vba bas cls frm pas dpr ' +
    'st scl awl il ld lad iec sfc stl ftl gxw tia ' +
    'env gitignore gitattributes editorconfig properties reg inf nfo srt vtt tex rst adoc asciidoc diff ' +
    'patch dockerfile makefile mk cmake gradle proto graphql gql ipynb csv tsv lst map',
  run: 'exe bat cmd com msi lnk ps1 vbs appref-ms url',
  archive: 'zip 7z rar tar gz tgz bz2 xz cab iso',
  cad: 'dwg dxf dwf step stp iges igs stl sldprt sldasm ipt iam',
};
const EXT_KIND = {};
for (const [kind, list] of Object.entries(KINDS)) {
  if (kind === 'run') continue;
  for (const e of list.split(/\s+/)) if (!EXT_KIND[e]) EXT_KIND[e] = kind;
}
const RUNNABLE = new Set(KINDS.run.split(/\s+/));
const HIDDEN = new Set(['desktop.ini', 'thumbs.db', '$recycle.bin', 'system volume information', 'pagefile.sys',
  'hiberfil.sys', 'swapfile.sys', 'dumpstack.log', 'dumpstack.log.tmp', '$windows.~bt', '$winreagent', 'recovery',
  'config.msi', 'msocache', 'ntuser.dat', 'ntuser.ini', 'ntuser.pol']);
const MIME = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', jfif: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
  bmp: 'image/bmp', svg: 'image/svg+xml', ico: 'image/x-icon', avif: 'image/avif', pdf: 'application/pdf',
  txt: 'text/plain', md: 'text/markdown', csv: 'text/csv', json: 'application/json', html: 'text/html',
};

function ext(name) {
  const base = path.basename(String(name || '')).toLowerCase();
  if (base === 'dockerfile' || base === 'makefile') return base;
  const i = base.lastIndexOf('.');
  return i > 0 ? base.slice(i + 1) : '';
}

function kindOf(name) {
  return EXT_KIND[ext(name)] || (RUNNABLE.has(ext(name)) ? 'run' : 'other');
}

function runnable(name) {
  return RUNNABLE.has(ext(name));
}

function mimeOf(name) {
  return MIME[ext(name)] || '';
}

function hidden(name) {
  const low = String(name).toLowerCase();
  return low.startsWith('.') || low.startsWith('~$') || HIDDEN.has(low) || low.startsWith('ntuser.dat');
}

function decode(buf, truncated = false) {
  if (buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) {
    return { text: new TextDecoder('utf-8').decode(buf.subarray(3)), encoding: 'UTF-8' };
  }
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) {
    return { text: new TextDecoder('utf-16le').decode(buf.subarray(2)), encoding: 'UTF-16' };
  }
  if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) {
    return { text: new TextDecoder('utf-16be').decode(buf.subarray(2)), encoding: 'UTF-16' };
  }
  try {
    // a cut in the middle of a letter is not an error: the rest of it is past the limit
    return { text: new TextDecoder('utf-8', { fatal: true }).decode(buf, { stream: truncated }), encoding: 'UTF-8' };
  } catch {
    return { text: new TextDecoder('windows-1251').decode(buf), encoding: 'Windows-1251' };
  }
}

function binary(buf) {
  const head = buf.subarray(0, 8192);
  if (head.length >= 2 && ((head[0] === 0xff && head[1] === 0xfe) || (head[0] === 0xfe && head[1] === 0xff))) return false;
  return head.includes(0);
}

async function readText(file, limit = TEXT_LIMIT) {
  const st = await fsp.stat(file);
  if (st.isDirectory()) throw new Error('это папка');
  const size = st.size;
  const handle = await fsp.open(file, 'r');
  try {
    const want = Math.min(size, limit);
    const buf = Buffer.alloc(want);
    let got = 0;
    while (got < want) {
      const { bytesRead } = await handle.read(buf, got, want - got, got);
      if (!bytesRead) break;
      got += bytesRead;
    }
    const data = buf.subarray(0, got);
    if (binary(data)) return { binary: true, size, text: '', truncated: false, encoding: '' };
    const truncated = size > got;
    return { ...decode(data, truncated), size, truncated, binary: false };
  } finally {
    await handle.close();
  }
}

async function readBytes(file, limit = BYTES_LIMIT) {
  const st = await fsp.stat(file);
  if (st.size > limit) throw new Error(`файл больше ${Math.round(limit / 1024 / 1024)} МБ — откройте его в программе`);
  return fsp.readFile(file);
}

async function info(file) {
  try {
    const st = await fsp.stat(file);
    const dir = st.isDirectory();
    return { exists: true, path: file, name: path.basename(file) || file, dir, size: dir ? null : st.size,
      mtime: st.mtimeMs, ext: dir ? '' : ext(file), kind: dir ? 'folder' : kindOf(file), runnable: !dir && runnable(file) };
  } catch (err) {
    return { exists: false, path: file, name: path.basename(file) || file, error: err.code || err.message };
  }
}

// A file attached to a message. Hermes puts the text of a linked text file into the request, and what is in a request
// stays in the conversation and is paid for with every next one: a 200 KB table costs its 60 thousand tokens again and
// again. So a big text file is not linked — the agent is told where it lies and how big it is, and reads the part it
// needs with its own tools. Pictures, PDF and small texts go as before.
const INLINE_TEXT_MAX = 30 * 1024;
const TEXT_KINDS = new Set(['code', 'markdown', 'html']);

async function attachment(file) {
  const i = await info(file);
  const texty = TEXT_KINDS.has(i.kind) || ['csv', 'tsv'].includes(i.ext);
  const link = !(i.exists && !i.dir && texty && i.size > INLINE_TEXT_MAX);
  return { path: file, name: i.name, size: i.size || 0, kind: i.kind, link,
    note: link ? '' : `${file} — ${Math.round((i.size || 0) / 1024)} КБ текста, в сообщение не вложен: читай нужную часть сам (поиск по файлу, строки с … по …), целиком не читай` };
}

// What goes into the message for the files attached: the line of the text and which of them are linked.
async function attachments(paths) {
  const items = await Promise.all((paths || []).map((p) => attachment(p)));
  const linked = items.filter((x) => x.link);
  const big = items.filter((x) => !x.link);
  const lines = [];
  if (linked.length) lines.push(`[Вложения: ${linked.map((x) => x.path).join('; ')}]`);
  if (big.length) lines.push(`[Большие файлы — по пути, не вложены:\n${big.map((x) => x.note).join('\n')}]`);
  return { items, text: lines.join('\n') };
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

function collator() {
  try { return new Intl.Collator('ru', { numeric: true, sensitivity: 'base' }); } catch { return null; }
}
const COLLATOR = collator();

function sortEntries(entries) {
  const cmp = COLLATOR ? COLLATOR.compare : (a, b) => a.localeCompare(b);
  return entries.sort((a, b) => (a.dir === b.dir ? cmp(a.name, b.name) : a.dir ? -1 : 1));
}

async function list(dir, { showHidden = false } = {}) {
  const full = path.resolve(String(dir || ''));
  const dirents = await fsp.readdir(full, { withFileTypes: true });
  const more = dirents.length > MAX_ENTRIES;
  const picked = dirents.filter((d) => showHidden || !hidden(d.name)).slice(0, MAX_ENTRIES);
  const entries = await mapLimit(picked, 48, async (d) => {
    const p = path.join(full, d.name);
    let st = null;
    try { st = await fsp.stat(p); } catch { /* a broken link, no rights: listed without details */ }
    const isDir = st ? st.isDirectory() : d.isDirectory();
    return { name: d.name, path: p, dir: isDir, size: st && !isDir ? st.size : null, mtime: st ? st.mtimeMs : null,
      ext: isDir ? '' : ext(d.name), kind: isDir ? 'folder' : kindOf(d.name), runnable: !isDir && runnable(d.name) };
  });
  const parent = path.dirname(full);
  return { path: full, parent: parent === full ? '' : parent, entries: sortEntries(entries), more };
}

function places(getPath) {
  const out = [];
  const add = (label, key, icon) => {
    let p = '';
    try { p = key === 'home' ? os.homedir() : getPath(key); } catch { p = ''; }
    // without such a folder some systems answer with the home folder itself: no second «Домашняя папка»
    if (key !== 'home' && p && path.resolve(p) === path.resolve(os.homedir())) return;
    if (p && fs.existsSync(p) && !out.some((o) => o.path === p)) out.push({ label, path: p, icon });
  };
  add('Рабочий стол', 'desktop', 'desktop');
  add('Документы', 'documents', 'doc');
  add('Загрузки', 'downloads', 'download');
  add('Изображения', 'pictures', 'image');
  add('Домашняя папка', 'home', 'home');
  return out;
}

async function drives(platform = process.platform) {
  if (platform !== 'win32') return [{ label: '/', path: '/', icon: 'drive' }];
  const letters = 'CDEFGHIJKLMNOPQRSTUVWXYZAB'.split('');
  const probe = (l) => Promise.race([
    fsp.stat(`${l}:\\`).then(() => true, () => false),
    new Promise((r) => setTimeout(() => r(false), 600)), // a mapped network drive that is gone answers slowly
  ]);
  const found = await Promise.all(letters.map(probe));
  return letters.filter((_, i) => found[i]).map((l) => ({ label: `${l}:`, path: `${l}:\\`, icon: 'drive' }));
}

module.exports = { ext, kindOf, runnable, mimeOf, hidden, decode, binary, readText, readBytes, info, list, places,
  drives, attachment, attachments, TEXT_LIMIT, BYTES_LIMIT, MAX_ENTRIES, INLINE_TEXT_MAX };
