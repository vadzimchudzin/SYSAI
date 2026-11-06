'use strict';
// Вместо инструмента хаба ebpro-cmtp (MCP по stdio) — для проверок холста: «проект» здесь — JSON
// {windows: [{number, name, width, height, background, objects: [{kind, bbox, labels, tags, picture?}]}]}.
const fs = require('node:fs');
const path = require('node:path');

const text = (t, isError = false) => ({ content: [{ type: 'text', text: t }], ...(isError ? { isError: true } : {}) });

function win(project, ref) {
  const s = String(ref);
  const w = project.windows.find((x) => x.name === s) || project.windows.find((x) => String(x.number) === s);
  if (!w) throw new Error(`no window ${s}`);
  return w;
}

function shift(o, dx, dy) {
  o.bbox = [o.bbox[0] + dx, o.bbox[1] + dy, o.bbox[2] + dx, o.bbox[3] + dy];
  for (const l of o.labels || []) {
    l.x += dx;
    l.y += dy;
  }
}

function edit(p, args) {
  const results = [];
  const done = [];
  for (const [i, e] of args.edits.entries()) {
    const n = i + 1;
    const w = win(p, e.window);
    const o = Number.isInteger(e.object) ? w.objects[e.object] : null;
    if (['move', 'clone_object'].includes(e.op) && !o) return text(`правка ${n} (${e.op}): нет объекта #${e.object}; файл не записан`, true);
    if (e.op === 'move') {
      shift(o, e.dx || 0, e.dy || 0);
      results.push({ op: e.op, window: w.number, object: e.object, bbox: o.bbox });
    } else if (e.op === 'text') {
      let k = 0;
      for (const obj of o ? [o] : w.objects) {
        for (const l of obj.labels || []) {
          if (l.text === e.old) {
            l.text = e.new;
            k++;
          }
        }
      }
      if (!k) return text(`правка ${n} (text): текст ${JSON.stringify(e.old)} не найден; файл не записан`, true);
      results.push({ op: e.op, window: w.number, object: e.object ?? null, count: k });
    } else if (e.op === 'tag') {
      let k = 0;
      for (const obj of o ? [o] : w.objects) obj.tags = (obj.tags || []).map((t) => (t === e.old ? (k++, e.new) : t));
      results.push({ op: e.op, window: w.number, object: e.object ?? null, count: k });
    } else if (e.op === 'rename_window') {
      w.name = e.new;
      results.push({ op: e.op, window: w.number });
    } else if (e.op === 'clone_window') {
      const number = e.number ?? Math.max(...p.windows.map((x) => x.number)) + 1;
      p.windows.push({ ...JSON.parse(JSON.stringify(w)), number, name: e.new || `${w.name} (копия)` });
      results.push({ op: e.op, window: number });
    } else if (e.op === 'clone_object') {
      const to = e.to ? win(p, e.to) : w;
      const copy = JSON.parse(JSON.stringify(o));
      shift(copy, e.dx || 0, e.dy || 0);
      to.objects.push(copy);
      results.push({ op: e.op, window: to.number, object: to.objects.length - 1 });
    } else if (e.op === 'delete_object') {
      if (!o || e.object === 0) return text(`правка ${n} (${e.op}): нет объекта #${e.object}; файл не записан`, true);
      w.objects.splice(e.object, 1);
      results.push({ op: e.op, window: w.number, object: e.object });
    } else if (e.op === 'delete_window') {
      p.windows = p.windows.filter((x) => x !== w);
      results.push({ op: e.op, window: w.number, links: [] });
    } else {
      return text(`правка ${n}: нет такого op: ${e.op}`, true);
    }
    done.push(`${n}. ${e.op}`);
  }
  if (process.env.FAKE_CMTP_SLOW) {
    const until = Date.now() + Number(process.env.FAKE_CMTP_SLOW);
    while (Date.now() < until) { /* a slow save */ }
  }
  fs.writeFileSync(`${args.out_file}.tmp`, JSON.stringify(p));
  fs.renameSync(`${args.out_file}.tmp`, args.out_file);
  return { ...text([...done, `Записано → ${args.out_file}`].join('\n')), structuredContent: { results, out_file: args.out_file } };
}

function call(name, args) {
  if (process.env.FAKE_CMTP_CRASH === name) process.exit(3);
  let p;
  try {
    p = JSON.parse(fs.readFileSync(args.path, 'utf8'));
  } catch (err) {
    return text(`файл не найден: ${err.message}`, true);
  }
  try {
    if (name === 'cmtp_windows') {
      return text(JSON.stringify({ count: p.windows.length, windows: p.windows.map((w) => ({ number: w.number, name: w.name,
        width: w.width, height: w.height, objects: w.objects.length })) }));
    }
    if (name === 'cmtp_scene') {
      const w = win(p, args.window);
      const objects = w.objects.map((o, index) => ({ index, ...o }));
      const images = {};
      for (const o of objects) {
        for (const st of o.picture || []) {
          st.key = st.image;
          if (!args.images_dir) continue;
          fs.mkdirSync(args.images_dir, { recursive: true });
          images[st.key] = path.join(args.images_dir, `${st.image}.png`);
          fs.writeFileSync(images[st.key], 'png');
        }
      }
      return text(JSON.stringify({ window: { number: w.number, name: w.name, width: w.width, height: w.height,
        background: w.background || null }, objects, images }));
    }
    if (name === 'cmtp_edit') return edit(p, args);
  } catch (err) {
    return text(`не выходит: ${err.message}`, true);
  }
  return text(`нет такого инструмента: ${name}`, true);
}

function out(msg) {
  process.stdout.write(`${JSON.stringify(msg)}\n`);
}

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let i;
  while ((i = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, i).trim();
    buffer = buffer.slice(i + 1);
    if (!line) continue;
    const msg = JSON.parse(line);
    if (msg.id === undefined) continue;
    if (msg.method === 'initialize') out({ jsonrpc: '2.0', id: msg.id, result: { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fake-cmtp', version: '0' } } });
    else if (msg.method === 'tools/call') out({ jsonrpc: '2.0', id: msg.id, result: call(msg.params.name, msg.params.arguments || {}) });
    else out({ jsonrpc: '2.0', id: msg.id, error: { code: -32601, message: 'Method not found' } });
  }
});
process.stdin.on('end', () => process.exit(0));
