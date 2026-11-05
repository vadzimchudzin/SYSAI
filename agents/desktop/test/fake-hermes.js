#!/usr/bin/env node
'use strict';
// A stand-in for `hermes -p <profile> acp` in the tests: the part of ACP the window uses, with the shapes Hermes
// 0.22 sends (titles «tool: preview», configOptions model / reasoning_effort / edit_approval_policy, the history
// replayed on session/load). FAKE_HERMES_LOG — a file where every request is written, for the assertions.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

if (!process.argv.includes('acp')) {
  process.stderr.write("hermes: error: argument command: invalid choice: 'x'\n");
  process.exit(2);
}
if (process.env.FAKE_HERMES_CRASH) {
  process.stderr.write(process.env.FAKE_HERMES_CRASH + '\n');
  process.exit(1);
}

const LOG = process.env.FAKE_HERMES_LOG || '';
const FILES = process.env.FAKE_HERMES_FILES || '/tmp';
let nextId = 1000;
let n = 0;
const waiting = new Map();
const cancelled = new Set();
const configs = new Map();
const servers = new Map(); // sessionId → the mcpServers the client gave

function write(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
function note(obj) { if (LOG) fs.appendFileSync(LOG, JSON.stringify(obj) + '\n'); }
function update(sessionId, u) { write({ jsonrpc: '2.0', method: 'session/update', params: { sessionId, update: u } }); }
function ask(method, params) {
  const id = nextId++;
  write({ jsonrpc: '2.0', id, method, params });
  return new Promise((resolve) => waiting.set(id, resolve));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (t) => ({ type: 'text', text: t });

function config(sessionId) {
  if (!configs.has(sessionId)) {
    configs.set(sessionId, [
      { id: 'model', name: 'Model', category: 'model', type: 'select', currentValue: 'sysai/main',
        options: [{ value: 'sysai/main', name: 'SYSAI · основная' }, { value: 'sysai/fast', name: 'SYSAI · быстрая' }] },
      { id: 'reasoning_effort', name: 'Reasoning effort', type: 'select', currentValue: 'medium',
        options: ['default', 'low', 'medium', 'high'].map((v) => ({ value: v, name: v })) },
      { id: 'edit_approval_policy', name: 'Edit approval', type: 'select', currentValue: 'ask',
        options: ['ask', 'workspace_session', 'session'].map((v) => ({ value: v, name: v })) },
    ]);
  }
  return configs.get(sessionId);
}

// A stdio MCP client as small as the test needs: initialize → notifications/initialized → tools/list → tools/call.
function mcpCall(server, name, args) {
  return new Promise((resolve) => {
    const env = { ...process.env };
    for (const e of server.env || []) env[e.name] = e.value;
    const child = spawn(server.command, server.args || [], { env, stdio: ['pipe', 'pipe', 'inherit'] });
    const waiters = new Map();
    let buf = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        let m;
        try { m = JSON.parse(line); } catch { continue; }
        const w = waiters.get(m.id);
        if (w) { waiters.delete(m.id); w(m); }
      }
    });
    child.on('error', (err) => resolve({ error: err.message }));
    const rpc = (rid, method, params) => new Promise((r) => {
      waiters.set(rid, r);
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: rid, method, params }) + '\n');
    });
    (async () => {
      const init = await rpc(1, 'initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'fake-hermes', version: '0' } });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
      const listed = await rpc(2, 'tools/list', {});
      const got = await rpc(3, 'tools/call', { name, arguments: args });
      child.stdin.end();
      resolve({ server: init.result && init.result.serverInfo, tools: (listed.result.tools || []).map((t) => t.name), result: got.result, error: got.error && got.error.message });
    })().catch((err) => { child.kill(); resolve({ error: err.message }); });
  });
}

const DAY = 86400000;
const SESSIONS = [
  { sessionId: 'old-1', title: 'Сводка по продажам за март', updatedAt: new Date(Date.now() - 2 * 3600000).toISOString() },
  { sessionId: 'old-2', title: 'Разобрать папку «Загрузки»', updatedAt: new Date(Date.now() - DAY - 3600000).toISOString() },
  { sessionId: 'old-3', title: 'Письмо поставщику о сроках', updatedAt: new Date(Date.now() - 3 * DAY).toISOString() },
  { sessionId: 'old-4', title: 'Проверить чертежи узла крепления', updatedAt: new Date(Date.now() - 12 * DAY).toISOString() },
];

async function replay(sessionId) {
  update(sessionId, { sessionUpdate: 'user_message_chunk', content: text('Сделай сводку по продажам за март') });
  update(sessionId, { sessionUpdate: 'agent_thought_chunk', content: text('Нужно найти таблицу продаж и посчитать итоги.') });
  update(sessionId, { sessionUpdate: 'tool_call', toolCallId: 'r1', title: `read_file: ${FILES}/продажи.csv`, kind: 'read', status: 'completed',
    locations: [{ path: path.join(FILES, 'продажи.csv') }] });
  update(sessionId, { sessionUpdate: 'agent_message_chunk', content: text('Итого за март: **1 240 000 ₽**, лучший день — 14 марта.') });
}

async function prompt(id, params) {
  const sid = params.sessionId;
  const said = (params.prompt || []).filter((c) => c.type === 'text').map((c) => c.text).join('\n');
  await sleep(150);
  if (/стоп-тест/.test(said)) {
    update(sid, { sessionUpdate: 'agent_thought_chunk', content: text('Долгая работа…') });
    update(sid, { sessionUpdate: 'tool_call', toolCallId: `t${++n}`, title: 'terminal: ping -n 100 127.0.0.1', kind: 'execute', status: 'in_progress', rawInput: { command: 'ping -n 100 127.0.0.1' } });
    for (let i = 0; i < 600 && !cancelled.has(sid); i++) await sleep(50);
    cancelled.delete(sid);
    return write({ jsonrpc: '2.0', id, result: { stopReason: 'cancelled' } });
  }
  if (/покажи|запусти/.test(said)) {
    // as Hermes does with the client's MCP servers: start it, initialize, list the tools, call one
    const show = /покажи/.test(said);
    const rel = (said.match(/запусти\s+(\S+)/) || [])[1] || 'запуск.bat';
    const target = show ? path.join(FILES, 'отчёт.md') : path.join(FILES, ...rel.split('/'));
    const tool = show ? 'show_file' : 'open_file';
    const tc = `w${++n}`;
    update(sid, { sessionUpdate: 'tool_call', toolCallId: tc, title: `mcp_window_${tool}: ${path.basename(target)}`, kind: 'other', status: 'in_progress',
      rawInput: { path: target } });
    const server = (servers.get(sid) || []).find((s) => s.name === 'window');
    const got = server ? await mcpCall(server, tool, show ? { path: target, line: 3 } : { path: target }) : { error: 'окно не дало MCP-сервер' };
    note({ mcp: got });
    const result = got.result || {};
    const said2 = (result.content || []).map((c) => c.text).join('\n') || got.error || '';
    const ok = Boolean(got.result) && !result.isError;
    update(sid, { sessionUpdate: 'tool_call_update', toolCallId: tc, status: ok ? 'completed' : 'failed', content: [{ type: 'content', content: text(said2) }] });
    update(sid, { sessionUpdate: 'agent_message_chunk', content: text(ok ? (show ? 'Открыл отчёт в окне справа.' : 'Запустил.') : `Не вышло: ${said2}`) });
    return write({ jsonrpc: '2.0', id, result: { stopReason: 'end_turn' } });
  }
  if (/вопрос/.test(said)) {
    const got = await ask('elicitation/create', { sessionId: sid, mode: 'form', message: 'В каком формате сделать отчёт?',
      requestedSchema: { type: 'object', properties: { answer: { type: 'string', enum: ['Excel', 'Word', 'PDF'] } }, required: ['answer'] } });
    note({ answered: got });
    const choice = got && got.action === 'accept' ? got.content.answer : 'никакой';
    update(sid, { sessionUpdate: 'agent_message_chunk', content: text(`Хорошо, формат: ${choice}.`) });
    return write({ jsonrpc: '2.0', id, result: { stopReason: 'end_turn' } });
  }
  for (const t of ['Человек просит ', 'разобрать отчёт. ', 'Сначала прочитаю файл, потом поправлю итог.']) {
    update(sid, { sessionUpdate: 'agent_thought_chunk', content: text(t) });
    await sleep(60);
  }
  update(sid, { sessionUpdate: 'plan', entries: [
    { content: 'Прочитать отчёт', status: 'completed', priority: 'medium' },
    { content: 'Поправить итог', status: 'in_progress', priority: 'medium' },
    { content: 'Показать результат', status: 'pending', priority: 'medium' }] });
  const file = path.join(FILES, 'отчёт.md');
  update(sid, { sessionUpdate: 'tool_call', toolCallId: `r${++n}`, title: `read_file: ${file}`, kind: 'read', status: 'in_progress',
    rawInput: { path: file }, locations: [{ path: file }] });
  await sleep(120);
  update(sid, { sessionUpdate: 'tool_call_update', toolCallId: `r${n}`, status: 'completed',
    content: [{ type: 'content', content: text('# Отчёт\n\nИтого: 100') }] });
  update(sid, { sessionUpdate: 'tool_call', toolCallId: `s${++n}`, title: 'search_files: *.xlsx', kind: 'search', status: 'completed',
    rawInput: { pattern: '*.xlsx', path: FILES } });
  const old = '# Отчёт\n\nИтого: 100\n';
  const fresh = '# Отчёт\n\nИтого: 120\nПроверено агентом.\n';
  const permission = await ask('session/request_permission', { sessionId: sid, toolCall: { toolCallId: `e${++n}`, title: `patch: ${file}`, kind: 'edit',
    content: [{ type: 'diff', path: file, oldText: old, newText: fresh }] },
    options: [{ optionId: 'allow_once', name: 'Allow once', kind: 'allow_once' }, { optionId: 'allow_always', name: 'Allow always', kind: 'allow_always' },
      { optionId: 'reject_once', name: 'Reject', kind: 'reject_once' }] });
  note({ permission });
  const allowed = permission && permission.outcome && permission.outcome.optionId && permission.outcome.optionId.startsWith('allow');
  update(sid, { sessionUpdate: 'tool_call', toolCallId: `e${n}`, title: `patch: ${file}`, kind: 'edit', status: allowed ? 'completed' : 'failed',
    content: [{ type: 'diff', path: file, oldText: old, newText: fresh }], locations: [{ path: file }] });
  update(sid, { sessionUpdate: 'plan', entries: [
    { content: 'Прочитать отчёт', status: 'completed', priority: 'medium' },
    { content: 'Поправить итог', status: 'completed', priority: 'medium' },
    { content: 'Показать результат', status: 'completed', priority: 'medium' }] });
  const answer = `Готово. Исправил итог в \`${file}\`:\n\n| Было | Стало |\n|---|---|\n| 100 | **120** |\n\n` +
    'Проверка в PowerShell:\n\n```powershell\nGet-Content -Path "отчёт.md" | Select-String "Итого"\n```\n\nЕсли нужно, сделаю то же для **апреля**.';
  for (let i = 0; i < answer.length; i += 18) {
    update(sid, { sessionUpdate: 'agent_message_chunk', content: text(answer.slice(i, i + 18)) });
    await sleep(15);
  }
  update(sid, { sessionUpdate: 'usage_update', used: 18400, size: 200000 });
  update(sid, { sessionUpdate: 'session_info_update', title: 'Исправить итог в отчёте' });
  update(sid, { sessionUpdate: 'available_commands_update', availableCommands: [{ name: 'compress', description: 'Сжать историю разговора' },
    { name: 'model', description: 'Сменить модель' }] });
  write({ jsonrpc: '2.0', id, result: { stopReason: 'end_turn' } });
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method) note({ method: msg.method, params: msg.params });
  if (msg.id !== undefined && !msg.method) {
    const resolve = waiting.get(msg.id);
    waiting.delete(msg.id);
    if (resolve) resolve(msg.result || null);
    return;
  }
  const { id, method, params = {} } = msg;
  if (method === 'initialize') {
    process.stdout.write('starting hermes acp (a library printed this)\n');
    write({ jsonrpc: '2.0', id, result: { protocolVersion: 1, agentInfo: { name: 'hermes-agent', title: 'Hermes Agent', version: '0.22.0' },
      agentCapabilities: { loadSession: true, sessionCapabilities: { list: {} } }, authMethods: [] } });
  } else if (method === 'session/new') {
    const sessionId = `new-${++n}`;
    servers.set(sessionId, params.mcpServers || []);
    write({ jsonrpc: '2.0', id, result: { sessionId, configOptions: config(sessionId) } });
  } else if (method === 'session/list') {
    write({ jsonrpc: '2.0', id, result: { sessions: SESSIONS, nextCursor: null } });
  } else if (method === 'session/load') {
    servers.set(params.sessionId, params.mcpServers || []);
    await replay(params.sessionId);
    write({ jsonrpc: '2.0', id, result: { configOptions: config(params.sessionId) } });
  } else if (method === 'session/prompt') {
    prompt(id, params);
  } else if (method === 'session/cancel') {
    cancelled.add(params.sessionId);
  } else if (method === 'session/set_config_option') {
    const opts = config(params.sessionId);
    const o = opts.find((x) => x.id === params.configId);
    if (o) o.currentValue = params.value;
    write({ jsonrpc: '2.0', id, result: { configOptions: opts } });
  } else if (id !== undefined) {
    write({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
  }
});
rl.on('close', () => process.exit(0));
