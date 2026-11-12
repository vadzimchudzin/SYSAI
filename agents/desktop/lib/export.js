'use strict';
// A conversation as a Markdown file: what the person asked and what the agent answered, in order, with the files
// attached and the tools the agent used named in a line. Saved into the project (_work/notes) or, for a conversation
// outside a project, into «SYSAI Разговоры» of the person's documents. The conversation itself stays with Hermes.
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

const SERVICE_TAIL = /\n*(\[Вложения: [^\]]*\]\s*)?(\[Большие файлы[^\]]*\]\s*)?$/;

function markdown({ title = '', project = '', agent = '', at = new Date(), blocks = [] } = {}) {
  const out = [`# ${String(title || 'Разговор').trim()}`, ''];
  const meta = [project ? `Проект: ${project}` : '', agent ? `Агент: ${agent}` : '', `Сохранён: ${at.toISOString().slice(0, 16).replace('T', ' ')} UTC`]
    .filter(Boolean);
  out.push(meta.join(' · '), '');
  let tools = [];
  const flush = () => {
    if (tools.length) out.push(`*Инструменты: ${tools.join(', ')}*`, '');
    tools = [];
  };
  for (const b of Array.isArray(blocks) ? blocks : []) {
    if (!b || typeof b !== 'object') continue;
    if (b.type === 'tools') {
      for (const t of Array.isArray(b.tools) ? b.tools : []) if (t) tools.push(String(t).slice(0, 80));
      continue;
    }
    const text = String(b.text || '').replace(SERVICE_TAIL, '').trim();
    if (b.type === 'user') {
      flush();
      out.push('## Человек', '');
      if (text) out.push(text, '');
      const files = (Array.isArray(b.files) ? b.files : []).map((f) => (f && (f.path || f.name)) || '').filter(Boolean);
      if (files.length) out.push(...files.map((f) => `- вложение: ${f}`), '');
    } else if (b.type === 'agent' && text) {
      flush();
      out.push('## Агент', '', text, '');
    }
  }
  flush();
  return `${out.join('\n').replace(/\n{3,}/g, '\n\n').trim()}\n`;
}

function fileName(title, at = new Date()) {
  const stem = String(title || 'разговор').replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 60) || 'разговор';
  return `${at.toISOString().slice(0, 16).replace(/[T:]/g, '-')} ${stem}.md`;
}

// -> the path of the file written; a name that is taken gets « (2)»
async function save(dir, data) {
  await fsp.mkdir(dir, { recursive: true });
  const base = fileName(data.title, data.at || new Date());
  let file = path.join(dir, base);
  for (let i = 2; fs.existsSync(file) && i < 100; i++) file = path.join(dir, base.replace(/\.md$/, ` (${i}).md`));
  await fsp.writeFile(file, markdown(data), 'utf8');
  return file;
}

module.exports = { markdown, fileName, save };
