// One conversation: what ACP's session/update says, drawn as Hermes Desktop draws it — the person's messages on
// the right, the agent's answer as text, its thinking folded, every action a quiet row that opens for details,
// questions and permissions as cards that wait for an answer.
import { h, icon, clear, basename } from './dom.js';
import { render as renderMd } from './md.js';

const TOOL_RU = {
  terminal: 'Команда', execute_code: 'Код', read_file: 'Читает', write_file: 'Записывает', patch: 'Правит',
  search_files: 'Ищет', web_search: 'Ищет в интернете', web_extract: 'Читает страницу', vision_analyze: 'Смотрит картинку',
  delegate_task: 'Поручает помощникам', memory: 'Память', todo: 'План', skill_view: 'Навык', skills_list: 'Навыки',
  skill_manage: 'Навык', session_search: 'Прошлые разговоры', clarify: 'Вопрос', process: 'Процесс',
  process_manage: 'Процесс', computer_use: 'Управляет ПК', cronjob: 'Расписание', text_to_speech: 'Озвучивает',
  image_generate: 'Рисует', send_message: 'Сообщение', mixture_of_agents: 'Совет моделей',
};
const NAME_ICON = { terminal: 'terminal', execute_code: 'code', read_file: 'eye', write_file: 'pencil', patch: 'pencil',
  search_files: 'search', web_search: 'globe', web_extract: 'globe', vision_analyze: 'image', delegate_task: 'users',
  memory: 'memory', todo: 'list', skill_view: 'book', skills_list: 'book', skill_manage: 'book', session_search: 'clock',
  clarify: 'help', computer_use: 'monitor', cronjob: 'clock' };
const KIND_ICON = { read: 'eye', edit: 'pencil', delete: 'trash', move: 'folder', search: 'search', execute: 'terminal',
  think: 'spark', fetch: 'globe', switch_mode: 'sliders', other: 'spark' };
const WINDOW_TOOL = { show_file: 'Показывает файл', open_file: 'Открывает в программе', show_in_folder: 'Показывает в папке' };

export function describeTool(data) {
  const title = String(data.title || '').trim();
  const m = /^([A-Za-z_][\w.-]*):\s*([\s\S]*)$/.exec(title);
  let name = m ? m[1] : (/^[A-Za-z_][\w.-]*$/.test(title) ? title : '');
  let detail = m ? m[2] : (name ? '' : title);
  let label = TOOL_RU[name] || '';
  let ico = NAME_ICON[name] || KIND_ICON[data.kind] || 'spark';
  if (!label && name.startsWith('browser_')) { label = 'Браузер'; ico = 'globe'; detail = detail || name.slice(8); }
  if (!label && name.startsWith('mcp_')) {
    const rest = name.slice(4);
    if (rest.startsWith('window_')) {
      const tool = rest.slice(7);
      label = WINDOW_TOOL[tool] || 'Окно'; ico = 'monitor';
    } else if (rest.startsWith('sysai_')) {
      label = 'SYSAI'; ico = 'cloud'; detail = [rest.slice(6), detail].filter(Boolean).join(' · ');
    } else {
      const [server, ...tool] = rest.split('_');
      label = server; ico = 'cube'; detail = [tool.join('_'), detail].filter(Boolean).join(' · ');
    }
  }
  if (!label) label = name || (data.kind === 'think' ? 'Думает' : 'Действие');
  if (!detail && data.locations && data.locations.length) detail = data.locations.map((l) => l.path).join(', ');
  return { name, label, detail: detail.replace(/\s+/g, ' ').trim(), icon: ico };
}

function textOf(content) {
  if (!content) return '';
  if (typeof content === 'string') return content;
  if (content.type === 'text') return content.text || '';
  if (content.type === 'resource_link') return '';
  if (content.type === 'resource' && content.resource) return content.resource.text || '';
  return '';
}

function pretty(value) {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

// ---------- line diff (LCS; big files fall back to old/new in full)
export function diffLines(oldText, newText) {
  const a = String(oldText || '').split('\n');
  const b = String(newText || '').split('\n');
  if (!oldText) return b.map((t) => ({ t, k: 'add' }));
  let s = 0;
  while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let ea = a.length, eb = b.length;
  while (ea > s && eb > s && a[ea - 1] === b[eb - 1]) { ea--; eb--; }
  const A = a.slice(s, ea), B = b.slice(s, eb);
  const out = a.slice(0, s).map((t) => ({ t, k: 'ctx' }));
  if (A.length * B.length > 4e6) {
    out.push(...A.map((t) => ({ t, k: 'del' })), ...B.map((t) => ({ t, k: 'add' })));
  } else {
    const n = A.length, m = B.length;
    const L = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { out.push({ t: A[i], k: 'ctx' }); i++; j++; }
      else if (L[i + 1][j] >= L[i][j + 1]) { out.push({ t: A[i], k: 'del' }); i++; }
      else { out.push({ t: B[j], k: 'add' }); j++; }
    }
    while (i < n) out.push({ t: A[i++], k: 'del' });
    while (j < m) out.push({ t: B[j++], k: 'add' });
  }
  out.push(...a.slice(ea).map((t) => ({ t, k: 'ctx' })));
  return out;
}

export function diffView(path, oldText, newText, onOpen) {
  const lines = diffLines(oldText, newText);
  const keep = new Uint8Array(lines.length);
  lines.forEach((l, i) => { if (l.k !== 'ctx') for (let d = -3; d <= 3; d++) if (lines[i + d]) keep[i + d] = 1; });
  const added = lines.filter((l) => l.k === 'add').length;
  const removed = lines.filter((l) => l.k === 'del').length;
  const head = h('div', { class: 'h' }, h('a', { href: '#', 'data-path': path, class: 'path', text: basename(path) || path }),
    `  +${added} −${removed}`);
  const box = h('div', { class: 'diff' }, head);
  let skipped = 0;
  const flush = () => { if (skipped) box.append(h('div', { class: 'l gap', text: `… ещё ${skipped} строк без изменений` })); skipped = 0; };
  lines.forEach((l, i) => {
    if (!keep[i]) { skipped++; return; }
    flush();
    box.append(h('div', { class: `l ${l.k}`, text: (l.k === 'add' ? '+ ' : l.k === 'del' ? '− ' : '  ') + l.t }));
  });
  flush();
  return box;
}

const PERMISSION_RU = { allow_once: 'Разрешить', allow_always: 'Разрешать всегда', reject_once: 'Отклонить', reject_always: 'Запретить всегда' };
function optionLabel(o) {
  const name = String(o.name || '');
  if (!name || /^(allow|reject|deny|approve|always|once|yes|no)\b/i.test(name)) return PERMISSION_RU[o.kind] || name || o.optionId;
  if (/^skip$/i.test(name)) return 'Пропустить';
  return name;
}

export class Conversation {
  constructor(id, hooks = {}) {
    this.id = id;
    this.hooks = hooks; // onGrow(), onChange(what), openPath(path), respond(reqId, result)
    this.root = h('div', { class: 'conv' });
    this.blocks = [];
    this.tools = new Map();
    this.asks = new Map();
    this.title = '';
    this.config = [];
    this.commands = [];
    this.usage = null;
    this.plan = null;
    this.running = false;
    this.loading = false;
    this.loaded = false;
    this.draft = true; // nothing was said yet
    this.working = null;
  }

  get empty() {
    return !this.blocks.length;
  }

  last() {
    return this.blocks[this.blocks.length - 1] || null;
  }

  _push(block) {
    this.blocks.push(block);
    if (this.working && this.working.isConnected) this.root.insertBefore(block.el, this.working);
    else this.root.append(block.el);
    this.draft = false;
    if (block.type !== 'thought') this._foldThought();
    this._grow();
    return block;
  }

  _grow() {
    if (this.hooks.onGrow) this.hooks.onGrow(this);
  }

  _changed(what) {
    if (what === 'ask') this._status();
    if (this.hooks.onChange) this.hooks.onChange(this, what);
  }

  _foldThought() {
    for (const b of this.blocks) if (b.type === 'thought' && b.live) {
      b.live = false;
      b.label.textContent = 'Размышления';
      b.state.replaceChildren(icon('chevR', 'sm chev'));
      if (!b.touched) b.el.open = false;
    }
  }

  apply(u) {
    switch (u.sessionUpdate) {
      case 'user_message_chunk': this.userChunk(u.content, u.messageId); break;
      case 'agent_message_chunk': this.agentChunk(u.content, u.messageId); break;
      case 'agent_thought_chunk': this.thoughtChunk(u.content); break;
      case 'tool_call': this.toolCall(u); break;
      case 'tool_call_update': this.toolUpdate(u); break;
      case 'plan': this.plan = Array.isArray(u.entries) ? u.entries : []; this._changed('plan'); break;
      case 'available_commands_update': this.commands = u.availableCommands || []; this._changed('commands'); break;
      case 'config_option_update': this.config = u.configOptions || []; this._changed('config'); break;
      case 'current_mode_update': this._mode(u.currentModeId); break;
      case 'session_info_update':
        if (typeof u.title === 'string' && u.title.trim()) { this.title = u.title.trim(); this._changed('title'); }
        break;
      case 'usage_update': this.usage = { used: u.used, size: u.size, cost: u.cost }; this._changed('usage'); break;
      default: break;
    }
    this._status();
  }

  _mode(modeId) {
    const policy = { default: 'ask', accept_edits: 'workspace_session', dont_ask: 'session' }[modeId];
    const opt = this.config.find((o) => o.id === 'edit_approval_policy');
    if (opt && policy) { opt.currentValue = policy; this._changed('config'); }
  }

  // ---------- the person
  userChunk(content, messageId) {
    const last = this.last();
    let b = last && last.type === 'user' && (!messageId || !last.messageId || last.messageId === messageId) && !last.sealed ? last : null;
    if (!b) b = this.addUser('', [], messageId);
    if (content && content.type === 'text') b.text += content.text || '';
    else if (content && (content.type === 'resource_link' || content.type === 'image' || content.type === 'resource')) {
      b.files.push(content.type === 'image' ? { name: 'картинка' } : { name: content.name || basename(content.uri || (content.resource && content.resource.uri) || ''), path: pathOf(content.uri || (content.resource && content.resource.uri)) });
    }
    this._drawUser(b);
  }

  addUser(text, files = [], messageId = '') {
    const bubble = h('div', { class: 'bubble' });
    const filesEl = h('div', { class: 'files' });
    const el = h('div', { class: 'msg user' }, filesEl, bubble);
    const b = { type: 'user', el, bubble, filesEl, text, files: [...files], messageId };
    this._push(b);
    this._drawUser(b);
    return b;
  }

  _drawUser(b) {
    // what the window adds to the prompt for the agent (paths of attachments) is not shown twice
    b.bubble.textContent = b.text.replace(/\n*\[Вложения: [^\]]*\]\s*$/, '').trim();
    b.bubble.hidden = !b.bubble.textContent;
    clear(b.filesEl);
    for (const f of b.files) {
      b.filesEl.append(h('button', { class: 'file-chip', title: f.path || f.name, onclick: () => f.path && this.hooks.openPath(f.path) },
        icon(f.name === 'картинка' ? 'image' : 'paperclip', 'sm'), h('span', { text: f.name })));
    }
    b.filesEl.hidden = !b.files.length;
    this._grow();
  }

  // ---------- the agent
  agentChunk(content, messageId) {
    const text = textOf(content);
    if (content && content.type === 'image') {
      this.agentImage(content);
      return;
    }
    if (!text) return;
    const last = this.last();
    let b = last && last.type === 'agent' && (!messageId || !last.messageId || last.messageId === messageId) ? last : null;
    if (!b) {
      const md = h('div', { class: 'md' });
      const copy = h('button', { class: 'icon-btn sm', title: 'Копировать ответ' }, icon('copy', 'sm'));
      const el = h('div', { class: 'msg agent' }, md, h('div', { class: 'acts' }, copy));
      b = this._push({ type: 'agent', el, md, text: '', messageId: messageId || '', raf: 0 });
      copy.addEventListener('click', () => this.hooks.copy && this.hooks.copy(b.text));
    }
    b.text += text;
    this._renderAgent(b);
  }

  agentImage(content) {
    const src = content.data ? `data:${content.mimeType || 'image/png'};base64,${content.data}` : (content.uri || '');
    if (!/^(data:image\/|file:)/.test(src)) return;
    const img = h('img', { src, alt: 'картинка', style: { maxWidth: '100%', borderRadius: '10px' } });
    this._push({ type: 'image', el: h('div', { class: 'msg agent' }, img) });
  }

  _renderAgent(b) {
    if (b.raf) return;
    b.raf = requestAnimationFrame(() => {
      b.raf = 0;
      renderMd(b.md, b.text);
      this._grow();
    });
  }

  thoughtChunk(content) {
    const text = textOf(content);
    if (!text) return;
    const last = this.last();
    let b = last && last.type === 'thought' ? last : null;
    if (!b) {
      const label = h('span', { text: 'Размышляет…' });
      const state = h('span', { class: 'eq' }, h('i'), h('i'), h('i'));
      const body = h('div', { class: 'body' });
      const summary = h('summary', {}, state, label);
      const el = h('details', { class: 'thought msg' }, summary, body);
      b = this._push({ type: 'thought', el, body, label, state, text: '', live: true, touched: false });
      el.open = this.running;
      summary.addEventListener('click', () => { b.touched = true; });
    }
    b.text += text;
    b.body.textContent = b.text.trim();
    if (b.el.open) b.body.scrollTop = b.body.scrollHeight;
    this._grow();
  }

  // ---------- actions
  _toolsGroup() {
    const last = this.last();
    if (last && last.type === 'tools') return last;
    return this._push({ type: 'tools', el: h('div', { class: 'tools msg' }) });
  }

  toolCall(u) {
    if (this.tools.has(u.toolCallId)) { this.toolUpdate(u); return; }
    const group = this._toolsGroup();
    const row = h('button', { class: 'row' });
    const el = h('div', { class: 'tool' }, row);
    const t = { id: u.toolCallId, data: { status: 'pending', ...u }, el, row, body: null, open: false };
    row.addEventListener('click', () => { t.open = !t.open; this._drawToolBody(t); });
    this.tools.set(u.toolCallId, t);
    group.el.append(el);
    this._drawTool(t);
    this._grow();
  }

  toolUpdate(u) {
    const t = this.tools.get(u.toolCallId);
    if (!t) { this.toolCall({ ...u }); return; }
    for (const [k, v] of Object.entries(u)) if (v !== undefined && v !== null && k !== 'sessionUpdate') t.data[k] = v;
    this._drawTool(t);
    if (t.open) this._drawToolBody(t);
    this._grow();
  }

  _drawTool(t) {
    const d = describeTool(t.data);
    const st = t.data.status;
    t.el.className = `tool ${st || ''}`;
    const status = st === 'in_progress' || st === 'pending' && this.running ? h('span', { class: 'spinner' })
      : st === 'completed' ? icon('check', 'sm') : st === 'failed' ? icon('alert', 'sm') : icon('dot', 'sm');
    t.row.replaceChildren(h('span', { class: 'k' }, icon(d.icon)), h('span', { class: 'label', text: d.label }),
      h('span', { class: 'detail', text: d.detail, title: d.detail }), h('span', { class: 'st' }, status));
  }

  _drawToolBody(t) {
    if (!t.open) { if (t.body) t.body.remove(); t.body = null; return; }
    const body = h('div', { class: 'body' });
    const d = t.data;
    const desc = describeTool(d);
    const input = d.rawInput;
    if (input !== undefined && input !== null) {
      const shown = typeof input === 'object' && input && typeof input.command === 'string' && Object.keys(input).length <= 3
        ? input.command : pretty(input);
      if (shown && shown !== desc.detail) body.append(h('div', {}, h('div', { class: 'sect', text: 'Ввод' }), h('pre', { class: 'out', text: shown })));
    }
    let hadContent = false;
    for (const c of d.content || []) {
      if (c.type === 'diff') {
        hadContent = true;
        body.append(diffView(c.path, c.oldText, c.newText, this.hooks.openPath));
      } else if (c.type === 'content') {
        const text = textOf(c.content);
        if (text) {
          hadContent = true;
          const md = h('div', { class: 'md' });
          renderMd(md, text.length > 60000 ? `${text.slice(0, 60000)}\n\n…` : text);
          body.append(h('div', {}, h('div', { class: 'sect', text: 'Результат' }), md));
        }
      }
    }
    if (!hadContent && d.rawOutput !== undefined && d.rawOutput !== null && d.rawOutput !== '') {
      const out = pretty(d.rawOutput);
      body.append(h('div', {}, h('div', { class: 'sect', text: 'Результат' }), h('pre', { class: 'out', text: out.length > 60000 ? `${out.slice(0, 60000)}\n…` : out })));
    }
    const locs = (d.locations || []).filter((l) => l && l.path);
    if (locs.length) {
      body.append(h('div', { class: 'files-row' }, locs.map((l) => h('button', { class: 'file-chip', title: l.path,
        onclick: () => this.hooks.openPath(l.path, l.line) }, icon('file', 'sm'), h('span', { text: basename(l.path) + (l.line ? `:${l.line}` : '') })))));
    }
    if (!body.childElementCount) body.append(h('div', { class: 'stop-note', text: d.status === 'completed' ? 'Готово, без вывода.' : 'Ещё выполняется…' }));
    if (t.body) t.body.replaceWith(body); else t.el.append(body);
    t.body = body;
  }

  // ---------- questions and permissions
  addPermission(reqId, params) {
    const tc = params.toolCall || {};
    const d = describeTool(tc);
    const opts = h('div', { class: 'opts' });
    const verdict = h('div', { class: 'verdict' });
    const isQuestion = d.name === 'clarify' || (params.options || []).some((o) => /^skip$/i.test(o.name || ''));
    const el = h('div', { class: 'ask msg' },
      h('div', { class: 'q' }, icon(isQuestion ? 'help' : 'shield', 'lg'), h('div', {},
        h('b', { text: isQuestion ? 'Агент спрашивает' : 'Агенту нужно разрешение' }),
        h('span', { text: isQuestion ? (tc.title || d.detail) : [d.label, d.detail].filter(Boolean).join(': ') }))));
    if (params._auto) el.append(h('div', { class: 'auto-why', text: `Правки без вопросов включены, но здесь нужен ваш ответ — ${params._auto}` }));
    for (const c of tc.content || []) if (c.type === 'diff') el.append(diffView(c.path, c.oldText, c.newText, this.hooks.openPath));
    el.append(opts, verdict);
    const sorted = [...(params.options || [])];
    for (const o of sorted) {
      const primary = o.kind === 'allow_once' || (isQuestion && !/^skip$/i.test(o.name || ''));
      const b = h('button', { class: `btn sm ${primary ? 'primary' : o.kind && o.kind.startsWith('reject') ? '' : 'outline'}`, text: optionLabel(o) });
      b.addEventListener('click', () => this._answer(reqId, { outcome: { outcome: 'selected', optionId: o.optionId } },
        `${isQuestion ? 'Ответ' : 'Решение'}: ${optionLabel(o)}`));
      opts.append(b);
    }
    const block = this._push({ type: 'ask', el, verdict, reqId });
    this.asks.set(reqId, block);
    this._changed('ask');
    return block;
  }

  addQuestion(reqId, params) {
    const schema = ((params.requestedSchema || {}).properties || {}).answer || { type: 'string' };
    const choices = schema.enum || (schema.items && schema.items.enum) || null;
    const multi = schema.type === 'array';
    const verdict = h('div', { class: 'verdict' });
    const form = h('div', { class: 'choice' });
    let get;
    if (choices) {
      const name = `q${String(reqId).replace(/\W/g, '')}`;
      const inputs = choices.map((c, i) => {
        const input = h('input', { type: multi ? 'checkbox' : 'radio', name, value: c, checked: !multi && i === 0 });
        form.append(h('label', {}, input, h('span', { text: c })));
        return input;
      });
      get = () => (multi ? inputs.filter((i) => i.checked).map((i) => i.value) : (inputs.find((i) => i.checked) || {}).value);
    } else {
      const ta = h('textarea', { placeholder: 'Ваш ответ' });
      form.append(ta);
      get = () => ta.value.trim();
      setTimeout(() => ta.focus(), 50);
    }
    const ok = h('button', { class: 'btn sm primary', text: 'Ответить' });
    const skip = h('button', { class: 'btn sm', text: 'Пропустить' });
    const el = h('div', { class: 'ask msg' }, h('div', { class: 'q' }, icon('help', 'lg'), h('div', {}, h('b', { text: 'Агент спрашивает' }),
      h('span', { text: params.message || '' }))), form, h('div', { class: 'opts' }, ok, skip), verdict);
    ok.addEventListener('click', () => {
      const answer = get();
      if (!answer || (Array.isArray(answer) && !answer.length)) return;
      this._answer(reqId, { action: 'accept', content: { answer } }, `Ответ: ${Array.isArray(answer) ? answer.join(', ') : answer}`);
    });
    skip.addEventListener('click', () => this._answer(reqId, { action: 'decline' }, 'Вопрос пропущен'));
    const block = this._push({ type: 'ask', el, verdict, reqId });
    this.asks.set(reqId, block);
    this._changed('ask');
    return block;
  }

  async _answer(reqId, result, said) {
    const block = this.asks.get(reqId);
    if (!block || block.done) return;
    block.done = true;
    block.el.classList.add('done');
    block.verdict.textContent = said;
    this.asks.delete(reqId);
    this._changed('ask');
    try {
      await this.hooks.respond(reqId, result);
    } catch (err) {
      block.verdict.textContent = `${said} — не доставлено: ${err.message}`;
    }
  }

  dropAsk(reqId, why = 'Вопрос снят') {
    const block = this.asks.get(reqId);
    if (!block) return;
    block.done = true;
    block.el.classList.add('done');
    block.verdict.textContent = why;
    this.asks.delete(reqId);
    this._changed('ask');
  }

  get waiting() {
    return this.asks.size > 0;
  }

  // ---------- the turn
  notice(text, kind = '') {
    const el = h('div', { class: `notice msg ${kind}` }, icon(kind === 'err' ? 'alert' : kind === 'warn' ? 'alert' : 'info', 'sm'),
      h('div', { class: 'grow', text }));
    this._push({ type: 'notice', el });
    return el;
  }

  start() {
    this.running = true;
    if (!this.working) this.working = h('div', { class: 'working' });
    this.root.append(this.working);
    this._status();
    this._changed('running');
    this._grow();
  }

  finish(stopReason, error) {
    this.running = false;
    if (this.working) this.working.remove();
    this._foldThought();
    for (const t of this.tools.values()) if (t.data.status === 'in_progress' || t.data.status === 'pending') {
      if (stopReason === 'cancelled' || error) { t.data.status = 'failed'; } this._drawTool(t);
    }
    if (error) this.notice(error, 'err');
    else if (stopReason === 'cancelled') this.notice('Остановлено.');
    else if (stopReason === 'max_tokens') this.notice('Ответ оборвался: модель упёрлась в предел длины ответа. Попросите продолжить.', 'warn');
    else if (stopReason === 'max_turn_requests') this.notice('Агент сделал слишком много шагов подряд и остановился. Попросите продолжить.', 'warn');
    else if (stopReason === 'refusal') this.notice('Модель отказалась отвечать на это.', 'warn');
    this._changed('running');
    this._grow();
  }

  _status() {
    if (!this.running || !this.working) return;
    const busy = [...this.tools.values()].reverse().find((t) => t.data.status === 'in_progress');
    const last = this.last();
    let text = 'Думает…';
    if (this.waiting) text = 'Ждёт вашего ответа';
    else if (busy) { const d = describeTool(busy.data); text = `${d.label}${d.detail ? ` · ${d.detail}` : ''}`; }
    else if (last && last.type === 'agent') text = 'Пишет…';
    const label = this.working.querySelector('span.t');
    if (label) label.textContent = text;
    else this.working.replaceChildren(h('span', { class: 'eq' }, h('i'), h('i'), h('i')), h('span', { class: 't', text }));
  }

  reset() {
    clear(this.root);
    this.blocks = [];
    this.tools.clear();
    this.asks.clear();
    this.plan = null;
    this.working = null;
    this.running = false;
    this.draft = true;
  }
}

function pathOf(uri) {
  const s = String(uri || '');
  if (!/^file:/i.test(s)) return '';
  let p = decodeURIComponent(s.replace(/^file:\/\/(localhost)?/i, ''));
  if (/^\/[a-zA-Z]:\//.test(p)) p = p.slice(1).replace(/\//g, '\\');
  return p;
}
