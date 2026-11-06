'use strict';
// The MCP server of this window, which the window hands to every session it starts: Hermes runs it as
// `electron.exe lib/uimcp.js` with ELECTRON_RUN_AS_NODE=1 (nothing else to install, nothing of the synchronizer held
// open). The agent shows files to the person in the window, opens them in their programs, starts programs and shows
// files in their folder, opens a project of EasyBuilder Pro on «Холст» and edits it there together with the person
// (each helper with a cursor of its own); each call goes to the window through the bridge (bridge.json in the SYSAI folder), and the
// window checks it by the signed rules first (lib/rules.js). MCP over stdio: JSON-RPC 2.0, a message per line.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const config = require('./config');

const VERSION = (() => {
  try {
    return require('../package.json').version;
  } catch {
    return '0.0.0';
  }
})();
const HOME = config.sysaiHome(path.join(__dirname, '..'));
const TIMEOUT = 60000;

const PATH = { type: 'string', description: 'Полный путь к файлу на этом компьютере, например C:\\Users\\Иван\\Documents\\отчёт.xlsx' };
const TOOLS = [
  {
    name: 'show_file',
    title: 'Показать файл в окне',
    description: 'Показать файл человеку в окне «Агент SYSAI», справа от разговора: документы Word, таблицы Excel и CSV, '
      + 'PDF, картинки, видео и звук, текст и код; папку — в проводнике окна. Показывайте так то, что сделали или нашли, '
      + 'вместо пересказа файла. line — строка, к которой прокрутить текст или код.',
    inputSchema: { type: 'object', properties: { path: PATH, line: { type: 'integer', minimum: 1, description: 'Номер строки (для текста и кода)' } },
      required: ['path'], additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'open_file',
    title: 'Открыть в программе',
    description: 'Открыть файл в его программе на компьютере человека (Word, Excel, просмотрщик PDF…) или папку в '
      + 'Проводнике; запустить программу, скрипт или ярлык (.exe, .bat, .cmd, .ps1, .msi, .lnk…) — только когда SYSAI '
      + 'разрешил агенту команды на ПК. Программа откроется у человека на экране.',
    inputSchema: { type: 'object', properties: { path: PATH }, required: ['path'], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  },
  {
    name: 'show_in_folder',
    title: 'Показать в папке',
    description: 'Открыть в Проводнике папку, где лежит файл, и выделить его.',
    inputSchema: { type: 'object', properties: { path: PATH }, required: ['path'], additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
];
const WHO = { type: 'string', description: 'Кто работает на холсте: «Агент» — вы сами; помощник (подагент) — своё имя, '
  + 'например «Помощник 1». У каждого свой курсор и цвет' };
const WINDOW = { type: ['string', 'integer'], description: 'Окно проекта: номер или имя, например 23 или «Нория 3»' };
const NAMES = { type: 'object', additionalProperties: { type: 'string' } };
const EDIT = { type: 'object', properties: {
  op: { type: 'string', enum: ['text', 'move', 'resize', 'tag', 'rename_window', 'clone_window', 'clone_object',
    'delete_object', 'delete_window', 'link', 'add_tags', 'import_window', 'import_objects', 'style', 'bind_tag'] },
  window: { ...WINDOW, description: 'Окно: номер или имя (по умолчанию — окно на холсте; у import_* — окно донора)' },
  object: { type: 'integer', description: 'index объекта (canvas_look)' },
  objects: { type: 'array', items: { type: 'integer' }, description: 'import_objects: index объектов донора' },
  old: { type: 'string' }, new: { type: 'string' }, dx: { type: 'integer' }, dy: { type: 'integer' },
  width: { type: 'integer' }, height: { type: 'integer' },
  fill: { type: ['string', 'null'] }, frame: { type: 'string' }, frame_width: { type: 'integer' },
  text_color: { type: 'string' }, font_size: { type: 'integer' }, label: { type: 'integer' }, slot: { type: 'integer', enum: [1, 2, 3] },
  number: { type: 'integer', description: 'clone_window / import_window: номер нового окна' },
  to: { type: 'string', description: 'clone_object / import_objects: окно, куда положить (по умолчанию — на холсте); '
    + 'link: окно, куда ведёт кнопка' },
  from: { type: 'string', description: 'import_*: файл проекта-донора (та же версия EasyBuilder)' },
  tags: { type: 'array', description: 'add_tags: [{name, type, node}]', items: { type: 'object', properties: {
    name: { type: 'string' }, type: { type: 'string' }, node: { type: 'string' } }, required: ['name', 'type'],
  additionalProperties: false } },
  map_tags: NAMES, map_windows: NAMES, map_devices: NAMES },
required: ['op'], additionalProperties: false };
TOOLS.push(
  {
    name: 'canvas_open',
    title: 'Открыть проект на холсте',
    description: 'Открыть проект EasyBuilder Pro (.cmtp/.emtp) на холсте окна «Агент SYSAI», справа от разговора: окно '
      + 'панели так, как его покажет панель (фон, фигуры, тексты, картинки). Человек видит окно и ваш курсор и правит '
      + 'вместе с вами. window — номер или имя окна (по умолчанию — последнее открытое здесь или 10).',
    inputSchema: { type: 'object', properties: { path: { ...PATH, description: 'Полный путь к проекту, например C:\\Projects\\velyuta.cmtp' },
      window: WINDOW }, required: ['path'], additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'canvas_look',
    title: 'Что на холсте',
    description: 'Что сейчас на холсте: проект, окно (номер, имя, размер, фон), его объекты коротко (index, вид, рамка '
      + 'bbox, текст, теги), окна проекта; куда показывает мышью и что выделил человек (person: pointer, over, selected); '
      + 'последние правки — кто что сделал; курсоры агента и помощников. Смотрите перед правкой: «вот этот» — это '
      + 'выделенный или тот, что под мышью. Подробный вид объектов — cmtp_scene.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'canvas_window',
    title: 'Другое окно на холсте',
    description: 'Показать на холсте другое окно открытого проекта (номер или имя).',
    inputSchema: { type: 'object', properties: { window: WINDOW }, required: ['window'], additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'canvas_cursor',
    title: 'Показать курсором',
    description: 'Навести свой курсор на объект (object — index) или точку окна (x, y в пикселях панели) и сказать '
      + 'человеку, что делаете (say — коротко). К тому, что вы правите через canvas_edit, курсор идёт и сам.',
    inputSchema: { type: 'object', properties: { who: WHO, object: { type: 'integer', description: 'index объекта' },
      x: { type: 'integer' }, y: { type: 'integer' }, say: { type: 'string', description: 'Что делаете — коротко' } },
    additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
  {
    name: 'canvas_edit',
    title: 'Править на холсте',
    description: 'Править проект, открытый на холсте, — прямо в его файле: каждая правка сразу видна человеку, ваш курсор '
      + 'идёт к объекту, человек может отменить («Отменить», Ctrl+Z). Правки — как у cmtp_edit: text (old → new, '
      + 'object — только в нём), move (object, dx, dy), resize (object, width, height), style (fill, frame, frame_width, text_color, font_size, label), bind_tag (object, slot, new), tag (old → new), rename_window '
      + '(new), clone_window (new, number), clone_object (object, dx, dy, to), delete_object (object; удаляйте с '
      + 'большего index), delete_window, link (object → окно to), add_tags (tags), import_window (from, window донора, '
      + 'new, number), import_objects (from, window донора, objects, to, dx, dy; map_tags, map_windows, map_devices); '
      + 'window по умолчанию — окно на холсте. Правки идут по порядку; не прошла одна — файл не меняется. Нужна запись '
      + 'в папку проекта и в папку донора (SYSAI → Доступы).',
    inputSchema: { type: 'object', properties: { who: WHO, edits: { type: 'array', minItems: 1, maxItems: 50, items: EDIT } },
      required: ['edits'], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
  },
);
const ROUTES = { show_file: 'show', open_file: 'open', show_in_folder: 'reveal' };
const CANVAS = new Set(['canvas_open', 'canvas_look', 'canvas_window', 'canvas_cursor', 'canvas_edit']);
const INSTRUCTIONS = 'Человек говорит с вами в окне «Агент SYSAI». Готовый или найденный файл показывайте ему в окне '
  + '(show_file), а не пересказывайте; открыть в программе — open_file. Проект EasyBuilder Pro (.cmtp/.emtp) '
  + 'открывайте на холсте (canvas_open): человек видит окно панели и ваш курсор и правит вместе с вами; перед правкой '
  + 'смотрите canvas_look (что он выделил, куда показывает), правьте canvas_edit — у каждого помощника свой who. '
  + 'Доступы — те же, что дал SYSAI.';

function out(msg) {
  process.stdout.write(`${JSON.stringify(msg)}\n`);
}

function bridge() {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(HOME, 'desktop', 'bridge.json'), 'utf8'));
    return data && data.port && data.token ? data : null;
  } catch {
    return null;
  }
}

// One request to the window: {status, body}.
function post(route, body) {
  const b = bridge();
  if (!b) return Promise.resolve({ status: 0, body: { error: 'окно «Агент SYSAI» не открыто — эти инструменты работают только в нём' } });
  const data = Buffer.from(JSON.stringify(body), 'utf8');
  return new Promise((resolve) => {
    const req = http.request({ host: '127.0.0.1', port: b.port, path: `/${route}`, method: 'POST', timeout: TIMEOUT,
      headers: { Authorization: `Bearer ${b.token}`, 'Content-Type': 'application/json', 'Content-Length': data.length } }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        let parsed = {};
        try {
          parsed = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
        } catch { /* not JSON */ }
        resolve({ status: res.statusCode, body: parsed || {} });
      });
    });
    req.on('timeout', () => req.destroy(new Error('окно «Агент SYSAI» не ответило вовремя')));
    req.on('error', (err) => resolve({ status: 0, body: { error: /ECONNREFUSED|ECONNRESET/.test(err.code || '')
      ? 'окно «Агент SYSAI» не отвечает — откройте его снова' : err.message } }));
    req.end(data);
  });
}

const text = (t, isError = false) => ({ content: [{ type: 'text', text: t }], ...(isError ? { isError: true } : {}) });

async function canvasCall(name, a) {
  let body = {};
  if (name === 'canvas_open') {
    const p = typeof a.path === 'string' ? a.path.trim() : '';
    if (!p) return text('нужен путь к проекту (path)', true);
    body = { path: p };
    if (a.window !== undefined && a.window !== null && a.window !== '') body.window = a.window;
  } else if (name === 'canvas_window') {
    if (a.window === undefined || a.window === null || a.window === '') return text('какое окно показать (window)?', true);
    body = { window: a.window };
  } else if (name === 'canvas_cursor') {
    for (const k of ['who', 'object', 'x', 'y', 'say']) if (a[k] !== undefined && a[k] !== null) body[k] = a[k];
  } else if (name === 'canvas_edit') {
    if (!Array.isArray(a.edits) || !a.edits.length) return text('нужен список правок (edits)', true);
    body = { who: a.who, edits: a.edits };
  }
  const { status, body: got } = await post(name, body);
  if (status !== 200) return text(String(got.error || `окно ответило ${status}`), true);
  delete got.ok;
  if (name === 'canvas_look') return text(JSON.stringify(got));
  if (name === 'canvas_cursor') {
    const c = got.cursor || {};
    return text(`Курсор «${c.who}» — на (${c.x}, ${c.y})${c.say ? `: ${c.say}` : ''}`);
  }
  if (name === 'canvas_edit') {
    const objs = Array.isArray(got.objects) && got.objects.length ? `, объекты ${got.objects.map((i) => `#${i}`).join(', ')}` : '';
    return { ...text(`${got.text || 'Готово'}\nНа холсте — окно ${got.window}${objs}. Человек может отменить правку.`),
      structuredContent: { results: got.results || [], window: got.window, objects: got.objects || [] } };
  }
  const w = got.window || {};
  return text(`На холсте: ${got.file}, окно ${w.number} «${w.name}» (${w.width}×${w.height}, объектов: ${got.objects}; `
    + `окон в проекте: ${got.windows}). Человек видит его справа от разговора.`);
}

async function call(name, args) {
  if (CANVAS.has(name)) return canvasCall(name, args && typeof args === 'object' ? args : {});
  const route = ROUTES[name];
  if (!route) return text(`нет такого инструмента: ${name}`, true);
  const p = args && typeof args.path === 'string' ? args.path.trim() : '';
  if (!p) return text('нужен путь к файлу (path)', true);
  const body = { path: p };
  if (name === 'show_file' && Number.isInteger(args.line) && args.line > 0) body.line = args.line;
  const { status, body: got } = await post(route, body);
  if (status !== 200) return text(String(got.error || `окно ответило ${status}`), true);
  const where = got.path || p;
  if (route === 'show') return text(`${got.folder ? 'Папка открыта' : 'Файл открыт'} в окне справа от разговора: ${where}`);
  if (route === 'open') return text(got.started ? `Программа запущена: ${where}` : `Открыто в программе: ${where}`);
  return text(`Папка открыта в Проводнике, файл выделен: ${where}`);
}

async function handle(msg) {
  if (!msg || typeof msg !== 'object' || typeof msg.method !== 'string') return; // an answer: this server asks nothing
  const { id, method } = msg;
  const params = msg.params && typeof msg.params === 'object' ? msg.params : {};
  const request = id !== undefined && id !== null;
  if (!request) return; // notifications/initialized, notifications/cancelled…
  try {
    if (method === 'initialize') {
      out({ jsonrpc: '2.0', id, result: { protocolVersion: String(params.protocolVersion || '2025-06-18'),
        capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'sysai-window', title: 'Окно «Агент SYSAI»', version: VERSION },
        instructions: INSTRUCTIONS } });
    } else if (method === 'ping') {
      out({ jsonrpc: '2.0', id, result: {} });
    } else if (method === 'tools/list') {
      out({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    } else if (method === 'tools/call') {
      out({ jsonrpc: '2.0', id, result: await call(String(params.name || ''), params.arguments || {}) });
    } else {
      out({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
    }
  } catch (err) {
    out({ jsonrpc: '2.0', id, error: { code: -32603, message: err.message || String(err) } });
  }
}

function main() {
  let buffer = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => {
    buffer += chunk;
    let i;
    while ((i = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, i).trim();
      buffer = buffer.slice(i + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        out({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
        continue;
      }
      for (const one of Array.isArray(msg) ? msg : [msg]) handle(one);
    }
  });
  process.stdin.on('end', () => process.exit(0));
}

if (require.main === module) main();

module.exports = { TOOLS, call, handle };
