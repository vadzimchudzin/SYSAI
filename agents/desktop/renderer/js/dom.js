// Small DOM helpers and the icon set (drawn after Lucide, ISC — strokes on a 24 grid).
const P = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  chat: '<path d="M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
  folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.2h7.5A2.5 2.5 0 0 1 21 9.7v7.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
  folderOpen: '<path d="M3.5 18V6.5A1.5 1.5 0 0 1 5 5h4l2 2h6.5A1.5 1.5 0 0 1 19 8.5V10"/><path d="M3.5 18 6.3 11h15l-2.8 7z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  cloud: '<path d="M7 18.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9a4.75 4.75 0 0 1-.5 9.5z"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4-4"/>',
  up: '<path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
  x: '<path d="M17 7 7 17M7 7l10 10"/>',
  chevR: '<path d="m9.5 6 6 6-6 6"/>', chevD: '<path d="m6 9.5 6 6 6-6"/>', chevL: '<path d="m14.5 6-6 6 6 6"/>',
  check: '<path d="M19.5 6.5 9.5 16.5l-5-5"/>',
  alert: '<path d="M10.3 4.2 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 16v-4.5M12 8h.01"/>',
  terminal: '<path d="m5 17 5-5-5-5M12.5 18H19"/>',
  file: '<path d="M14 3.5H7A2.5 2.5 0 0 0 4.5 6v12A2.5 2.5 0 0 0 7 20.5h10a2.5 2.5 0 0 0 2.5-2.5V9z"/><path d="M14 3.5V9h5.5"/>',
  fileText: '<path d="M14 3.5H7A2.5 2.5 0 0 0 4.5 6v12A2.5 2.5 0 0 0 7 20.5h10a2.5 2.5 0 0 0 2.5-2.5V9z"/><path d="M14 3.5V9h5.5M8.5 13h7M8.5 16.5h5"/>',
  image: '<rect x="3.5" y="3.5" width="17" height="17" rx="2.5"/><circle cx="9" cy="9" r="1.8"/><path d="m20.5 15-4.5-4.5L5 21"/>',
  video: '<rect x="2.5" y="5.5" width="14" height="13" rx="2.5"/><path d="m16.5 10.5 5-3v9l-5-3"/>',
  music: '<path d="M9 18V5.5l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
  sheet: '<rect x="3.5" y="3.5" width="17" height="17" rx="2.5"/><path d="M3.5 9.5h17M3.5 15h17M9.5 3.5v17"/>',
  doc: '<path d="M14 3.5H7A2.5 2.5 0 0 0 4.5 6v12A2.5 2.5 0 0 0 7 20.5h10a2.5 2.5 0 0 0 2.5-2.5V9z"/><path d="M14 3.5V9h5.5M8 12.5l1.3 5 1.7-4 1.7 4 1.3-5"/>',
  pdf: '<path d="M14 3.5H7A2.5 2.5 0 0 0 4.5 6v12A2.5 2.5 0 0 0 7 20.5h10a2.5 2.5 0 0 0 2.5-2.5V9z"/><path d="M14 3.5V9h5.5M8 17v-4h1.3a1.3 1.3 0 0 1 0 2.6H8"/>',
  code: '<path d="m15.5 17.5 5.5-5.5-5.5-5.5M8.5 6.5 3 12l5.5 5.5"/>',
  play: '<path d="M7.5 5v14l11-7z"/>',
  external: '<path d="M14 4.5h5.5V10M10 14l9.5-9.5M18.5 14v4.5a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18.5V7A1.5 1.5 0 0 1 6 5.5h4.5"/>',
  max: '<path d="M14.5 4.5h5v5M9.5 19.5h-5v-5M19.5 4.5l-6 6M4.5 19.5l6-6"/>',
  min: '<path d="M4.5 14.5h5v5M19.5 9.5h-5v-5M14.5 9.5l6-6M3.5 20.5l6-6"/>',
  panel: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/>',
  copy: '<rect x="9" y="9" width="11.5" height="11.5" rx="2.5"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9L20 8.5M20 4v4.5h-4.5"/>',
  upload: '<path d="M12 15.5V4M7 8.5 12 4l5 4.5M5 20h14"/>',
  download: '<path d="M12 4v11.5M7 11l5 4.5 5-4.5M5 20h14"/>',
  home: '<path d="M4 11 12 4.5l8 6.5M6 9.5V20h12V9.5"/>',
  drive: '<rect x="3.5" y="13" width="17" height="6.5" rx="2"/><path d="M5.5 13 8 5.5h8l2.5 7.5M7.5 16.3h.01"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5a13 13 0 0 1 0 17M12 3.5a13 13 0 0 0 0 17"/>',
  spark: '<path d="M12 3.5 13.9 9 19.5 11l-5.6 2L12 18.5 10.1 13 4.5 11l5.6-2z"/>',
  pencil: '<path d="M4.5 19.5h4l10-10-4-4-10 10z"/><path d="m13.5 6.5 4 4"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  list: '<path d="M10 6.5h10.5M10 12h10.5M10 17.5h10.5M3.5 6.5 5 8l2.5-2.8M3.5 12 5 13.5l2.5-2.8M3.5 17.5 5 19l2.5-2.8"/>',
  users: '<circle cx="9" cy="8" r="3.3"/><path d="M3 19.5a6 6 0 0 1 12 0M15.5 4.8a3.3 3.3 0 0 1 0 6.4M17.5 13.6a6 6 0 0 1 3.5 5.9"/>',
  memory: '<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/><path d="M4.5 6v6c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6M4.5 12v6c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-6"/>',
  shield: '<path d="M12 3.5 5 6.2v5.6c0 4.2 3 7.6 7 8.7 4-1.1 7-4.5 7-8.7V6.2z"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.2a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.1-2.4 3.6M12 17h.01"/>',
  archive: '<rect x="3.5" y="4.5" width="17" height="4.5" rx="1.2"/><path d="M5 9v9.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V9M10 13h4"/>',
  cube: '<path d="M12 3.5 4 7.6v8.8l8 4.1 8-4.1V7.6zM4 7.6l8 4.1 8-4.1M12 11.7v8.8"/>',
  book: '<path d="M5 18.5V6a2.5 2.5 0 0 1 2.5-2.5H19v13H7.5A2.5 2.5 0 0 0 5 19a2.5 2.5 0 0 0 2.5 2.5H19"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  dots: '<circle cx="5.5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="18.5" cy="12" r="1.2" fill="currentColor"/>',
  paperclip: '<path d="m20 11.5-7.8 7.8a5 5 0 0 1-7.1-7.1l8.1-8.1a3.3 3.3 0 0 1 4.7 4.7l-8.1 8.1a1.7 1.7 0 0 1-2.4-2.4l7.4-7.4"/>',
  dot: '<circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
};

export function icon(name, cls = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `ico ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = P[name] || P.file;
  return svg;
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'html') el.innerHTML = v; // only for markup this code makes or DOMPurify cleaned
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function size(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '';
  if (n < 1024) return `${n} Б`;
  const units = ['КБ', 'МБ', 'ГБ', 'ТБ'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1).replace('.', ',') : Math.round(v)} ${units[i]}`;
}

const DAY = 86400000;
export function when(value, { short = false } = {}) {
  if (!value) return '';
  const d = typeof value === 'number' ? new Date(value) : new Date(String(value));
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  const hm = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  if (t >= start) return hm;
  if (t >= start - DAY) return short ? 'вчера' : `вчера, ${hm}`;
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString('ru-RU', sameYear ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' });
}

export function group(value) {
  const d = new Date(typeof value === 'number' ? value : String(value || ''));
  if (Number.isNaN(d.getTime())) return 'Раньше';
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= start) return 'Сегодня';
  if (t >= start - DAY) return 'Вчера';
  if (t >= start - 6 * DAY) return 'На этой неделе';
  if (t >= start - 29 * DAY) return 'В этом месяце';
  return 'Раньше';
}

export function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function basename(p) {
  const s = String(p || '').replace(/[\\/]+$/, '');
  const i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
  return i >= 0 ? s.slice(i + 1) : s;
}

export function dirname(p) {
  const s = String(p || '').replace(/[\\/]+$/, '');
  const i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
  return i > 0 ? s.slice(0, i) : s;
}

export function ext(p) {
  const b = basename(p).toLowerCase();
  const i = b.lastIndexOf('.');
  return i > 0 ? b.slice(i + 1) : '';
}

const KIND_ICON = { folder: 'folder', image: 'image', video: 'video', audio: 'music', pdf: 'pdf', sheet: 'sheet',
  doc: 'doc', markdown: 'fileText', html: 'code', code: 'code', run: 'play', archive: 'archive', cad: 'cube',
  section: 'folder', other: 'file' };
export function kindIcon(kind) {
  return KIND_ICON[kind] || 'file';
}

// a path the agent wrote: C:\…, \\server\…, or /… on Linux
export function looksLikePath(text) {
  const t = String(text || '').trim();
  if (!t || t.length > 400 || /\n/.test(t)) return false;
  return /^[a-zA-Z]:[\\/][^<>"|?*]*$/.test(t) || /^\\\\[^\\]+\\[^<>"|?*]+$/.test(t) || /^\/(home|tmp|mnt|data|Users)\/\S+$/.test(t);
}
