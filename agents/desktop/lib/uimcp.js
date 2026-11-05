'use strict';
// The MCP server of this window, which the window hands to every session it starts: Hermes runs it as
// `electron.exe lib/uimcp.js` with ELECTRON_RUN_AS_NODE=1 (nothing else to install, nothing of the synchronizer held
// open). The agent shows files to the person in the window, opens them in their programs, starts programs and shows
// files in their folder; each call goes to the window through the bridge (bridge.json in the SYSAI folder), and the
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
const ROUTES = { show_file: 'show', open_file: 'open', show_in_folder: 'reveal' };
const INSTRUCTIONS = 'Человек говорит с вами в окне «Агент SYSAI». Готовый или найденный файл показывайте ему в окне '
  + '(show_file), а не пересказывайте; открыть в программе — open_file. Доступы — те же, что дал SYSAI.';

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

async function call(name, args) {
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
