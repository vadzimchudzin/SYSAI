'use strict';
// The window «Агент SYSAI» in an ordinary browser, to look at it and take screenshots without Electron and Hermes:
// renderer/ as is, window.sysai from mock.js (a scripted conversation), the projects from the real lib/projects.js over
// /rpc on a demo folder. A tool for the developer, not part of the window.
//   node agents/desktop/test/ui/serve.js [port]  →  http://127.0.0.1:<port>/?scene=work
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const { Projects, prompts, STAGES } = require('../../lib/projects');

const ROOT = path.join(__dirname, '..', '..', 'renderer');
const STATIC = path.join(__dirname, '..', '..', '..', '..', 'app', 'static');
const PORT = Number(process.argv[2] || process.env.PORT || 5178);
const DEMO = path.join(os.tmpdir(), 'sysai-ui-demo');
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ttf': 'font/ttf', '.woff2': 'font/woff2', '.woff': 'font/woff', '.json': 'application/json' };

function demo() {
  const dir = path.join(DEMO, 'Элеватор Благовещенск');
  for (const p of ['materials', 'plc', 'hmi', 'docs', '_work/spec']) fs.mkdirSync(path.join(dir, p), { recursive: true });
  const put = (rel, text) => { const p = path.join(dir, rel); if (!fs.existsSync(p)) fs.writeFileSync(p, text); };
  put('materials/ТЗ на АСУ ТП.pdf', 'x'.repeat(184000));
  put('materials/Э3 Схема электрическая.pdf', 'x'.repeat(2400000));
  put('materials/Перечень механизмов.xlsx', 'x'.repeat(32000));
  put('plc/Elevator.project', 'x'.repeat(5100000));
  put('_work/spec/project.spec.json', '{}');
  put('project.md', '# Элеватор Благовещенск\n');
  fs.writeFileSync(path.join(dir, 'pipeline.json'), JSON.stringify({ stages: {
    materials: { state: 'done', note: '12 механизмов, 96 сигналов по Э3 и перечню' },
    decisions: { state: 'done', note: 'TM241CE40R + cMT3072X, OPC UA, стиль ОПО' },
    plc: { state: 'done', note: 'ST: 12 FB механизмов, блокировки по ТЗ п.4' },
    build: { state: 'work', note: 'Компилирую в Machine Expert — 2 предупреждения, исправляю' },
  }, facts: { scope: 'full', controller: 'TM241CE40R', panel: 'cMT3072X', resolution: [1024, 600], link: 'opcua', language: 'st',
    style: 'ОПО' } }, null, 2));
  // ?scene=scope — «только экраны»: шаги ПЛК и компиляции погашены, стиль и контроллер не выбраны — дерево так и говорит
  const hmi = path.join(DEMO, 'Шкаф СП-2 экраны');
  for (const p of ['materials', 'plc', 'hmi', 'docs']) fs.mkdirSync(path.join(hmi, p), { recursive: true });
  if (!fs.existsSync(path.join(hmi, 'materials', 'ТЗ на экраны.pdf'))) fs.writeFileSync(path.join(hmi, 'materials', 'ТЗ на экраны.pdf'), 'x'.repeat(90000));
  fs.writeFileSync(path.join(hmi, 'pipeline.json'), JSON.stringify({ stages: {
    materials: { state: 'done', note: '9 механизмов по перечню' }, decisions: { state: 'done' },
    hmi: { state: 'work', note: 'Окна из пака «ОПО»; теги беру из программы, которая уже есть в materials' },
  }, facts: { scope: 'hmi', panel: 'cMT3072X', resolution: [1024, 600], link: 'modbus_tcp' } }, null, 2));
  return new Projects(DEMO);
}

const projects = demo();
const RPC = {
  'projects:list': () => projects.list(),
  'projects:info': async (name) => {
    const info = await projects.info(String(name || ''));
    const { stage, ...pr } = prompts(info);
    return { ...info, prompts: { ...pr, stages: Object.fromEntries(STAGES.map(([key]) => [key, stage(key)])) } };
  },
  'projects:create': (name, scope) => projects.create(String(name || ''), String(scope || 'full')),
  'projects:dialog': () => ({}),
};

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/rpc' && req.method === 'POST') {
    let body = '';
    for await (const chunk of req) body += chunk;
    const { channel, args } = JSON.parse(body || '{}');
    try {
      if (!RPC[channel]) throw new Error(`стенд не умеет ${channel}`);
      const value = await RPC[channel](...(args || []));
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ ok: true, value }));
    } catch (err) {
      res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ ok: false, error: err.message }));
    }
    return;
  }
  if (url.pathname === '/__mock.js') {
    res.writeHead(200, { 'content-type': 'text/javascript' }).end(fs.readFileSync(path.join(__dirname, 'mock.js')));
    return;
  }
  const rel = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname).replace(/^\/+/, '');
  if (rel.startsWith('fonts/')) { // the package takes them from SYSAI's pages (app/net/desktop.py _fonts): so does this
    const name = rel.slice(6);
    if (name === 'fonts.css') {
      res.writeHead(200, { 'content-type': 'text/css' })
        .end(fs.readFileSync(path.join(STATIC, 'fonts.css'), 'utf8').replace(/url\(\/static\/fonts\//g, 'url(./'));
    } else if (/^[\w.-]+\.woff2$/.test(name) && fs.existsSync(path.join(STATIC, 'fonts', name))) {
      res.writeHead(200, { 'content-type': 'font/woff2' }).end(fs.readFileSync(path.join(STATIC, 'fonts', name)));
    } else res.writeHead(404).end('нет');
    return;
  }
  const file = path.resolve(ROOT, rel);
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end('нет');
    return;
  }
  let data = fs.readFileSync(file);
  if (rel === 'index.html') {
    data = String(data).replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/, '')
      .replace('<script type="module" src="js/app.js">', '<script src="/__mock.js"></script>\n  <script type="module" src="js/app.js">');
  }
  res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream' }).end(data);
}).listen(PORT, '127.0.0.1', () => console.log(`окно на http://127.0.0.1:${PORT}/ (проекты: ${DEMO})`));
