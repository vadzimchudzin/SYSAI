// Соты: рой агентов. Один значок в композере, панель на всю ширину разговора, и каждая сота — один живой агент:
// работает — светится, нашёл — горит ровно, ошибся — красная, не успел — серая, спорит — жёлтая.
import { h, clear, icon } from './dom.js';
import { perRow, rowsOf, isOdd } from './combs.mjs';

const STATE_RU = { idle: 'не начинал', working: 'ищет', done: 'готово', error: 'ошибка', timeout: 'не успел' };

export class SwarmPanel {
  constructor({ api, question, project }) {
    this.api = api;
    this.questionOf = question; // () => what the person typed — рой по умолчанию проверяет именно это
    this.projectOf = project;
    this.allowed = { on: false, cells: 0 };
    this.state = null;
    this.picked = null;
    this.cellsWanted = 8;
    this.minutes = 5;
    this.note = '';

    this.box = h('div', { class: 'swarm-card', role: 'dialog', 'aria-label': 'Соты: рой агентов' });
    this.el = h('div', { class: 'swarm-wrap', hidden: true }, this.box);
    this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
    document.body.append(this.el);

    this.off = null;
  }

  async init() {
    try {
      this.allowed = await this.api.swarm.allowed();
      this.cellsWanted = Math.max(4, Math.min(this.allowed.cells || 4, 40));
      const now = await this.api.swarm.state();
      if (now) this.apply(now);
    } catch { /* стенд */ }
    this.off = this.api.swarm.onState((state) => this.apply(state));
    this.onState = null;
    return this.allowed;
  }

  get busy() {
    return Boolean(this.state && ['plan', 'work', 'merge'].includes(this.state.stage));
  }

  open() {
    this.note = '';
    this.picked = null;
    this.el.hidden = false;
    this.draw();
  }

  close() {
    this.el.hidden = true;
    if (this.onClose) this.onClose();
  }

  apply(state) {
    this.state = state;
    if (this.onState) this.onState(state);
    if (!this.el.hidden) this.draw();
  }

  // Живые соты: конфликт помечаем жёлтым — он единственный цвет, который несёт инженерный смысл
  conflictCells() {
    const set = new Set();
    for (const c of (this.state && this.state.result && this.state.result.conflicts) || []) {
      for (const v of c.values) for (const x of v.cells) set.add(x.cell);
    }
    return set;
  }

  drawGrid() {
    const cells = (this.state && this.state.cells) || [];
    const bad = this.conflictCells();
    // Соты рядами: шестиугольники касаются гранями, нечётный ряд — на полсоты (combs.mjs). Сколько влезает — по
    // ширине панели, чтобы узкое окно не прятало соты за прокрутку. Нет живых сот — место, которое они займут.
    const width = this.box.clientWidth || 640;
    const inRow = perRow(width - 24, 46);
    const list = cells.length
      ? cells.map((cell) => {
        const cls = `cell ${bad.has(cell.i) ? 'conflict' : cell.state}`;
        const node = h('button', { class: cls, title: `${cell.i + 1}. ${cell.title} — ${STATE_RU[cell.state] || cell.state}`,
          onclick: () => { this.picked = this.picked === cell.i ? null : cell.i; this.draw(); } });
        node.append(h('span', { class: 'n', text: String(cell.i + 1) }));
        return node;
      })
      : Array.from({ length: this.cellsWanted }, () => h('span', { class: 'cell idle' }));
    const grid = h('div', { class: 'comb-grid' }, ...rowsOf(list.length, inRow).map((row, r) =>
      h('div', { class: `comb-row${isOdd(r) ? ' odd' : ''}` }, ...row.map((i) => list[i]))));
    return grid;
  }

  drawCell() {
    if (this.picked == null || !this.state) return null;
    const cell = this.state.cells.find((c) => c.i === this.picked);
    if (!cell) return null;
    const a = cell.answer;
    const box = h('div', { class: 'comb-answer' },
      h('b', { text: `${cell.i + 1}. ${cell.title}` }),
      h('span', { class: 'comb-state', text: STATE_RU[cell.state] || cell.state }));
    if (cell.error) box.append(h('div', { class: 'comb-err', text: cell.error }));
    if (a) {
      box.append(h('div', { class: 'comb-line', text: `проверил: ${a.checked} · находок: ${a.found.length} · нет: ${a.missing.length}` }));
      for (const f of a.found.slice(0, 12)) {
        box.append(h('div', { class: 'comb-line' }, h('b', { text: f.key }), h('span', { text: ` ${f.value}` }),
          f.where ? h('span', { class: 'where', text: ` · ${f.where}` }) : null));
      }
      for (const m of a.missing.slice(0, 12)) box.append(h('div', { class: 'comb-line miss', text: `нет: ${m.key} — ${m.why}` }));
      if (a.notes) box.append(h('div', { class: 'comb-note', text: a.notes }));
    }
    return box;
  }

  draw() {
    clear(this.box);
    const st = this.state;
    const head = h('div', { class: 'swarm-head' },
      icon('comb', 'lead'),
      h('div', { class: 'grow' }, h('b', { text: 'Рой агентов' }),
        h('span', { class: 'sub', text: this.busy ? 'рой работает — ответы собираются'
          : st && st.stage === 'done' ? 'рой закончил — соты остались с итогом' : 'рой агентов: проверяют разное одновременно' })),
      h('button', { class: 'icon-btn', title: 'Закрыть', onclick: () => this.close() }, icon('x', 'sm')));
    this.box.append(head);

    if (!this.allowed.on) {
      // текст собирает главный процесс (lib/swarm.js: offText): «доступов нет / обновляются» ≠ «выключено»
      const off = this.allowed.off || 'Рой агентов для вас выключен. Включает администратор: SYSAI → Сеть агентов → Рой агентов.';
      this.box.append(h('p', { class: 'swarm-text', text: `${off} Права у роя только на чтение.` }));
      return;
    }

    if (!st || st.stage === 'idle') {
      const q = h('textarea', { rows: '3', class: 'swarm-q', placeholder: 'Что проверить: например «сверь адреса в спецификации с Э3»',
        spellcheck: 'true' });
      q.value = String(this.questionOf() || '').trim();
      const cells = h('input', { type: 'range', min: '4', max: String(Math.max(4, this.allowed.cells)), step: '1', value: String(this.cellsWanted) });
      const num = h('b', { class: 'comb-num', text: String(this.cellsWanted) });
      cells.addEventListener('input', () => { this.cellsWanted = Number(cells.value); num.textContent = cells.value; });
      this.box.append(
        h('p', { class: 'swarm-text', text: `Разрешение профиля: до ${this.allowed.cells} сот. Каждая сота — отдельный агент со своей частью работы; в конце арбитр сведёт ответы и назовёт спорные.` }),
        h('div', { class: 'swarm-row' }, h('label', { class: 'swarm-label', text: 'Задача' }), q),
        h('div', { class: 'swarm-row' }, h('label', { class: 'swarm-label', text: 'Соты' }), cells, num),
        this.note ? h('div', { class: 'comb-err', text: this.note }) : null,
        h('div', { class: 'swarm-actions' },
          h('button', { class: 'btn primary', text: 'Запустить рой', onclick: async () => {
            const text = q.value.trim();
            if (!text) { this.note = 'нужна задача: что проверять'; this.draw(); return; }
            this.note = '';
            try {
              const state = await this.api.swarm.start({ question: text, cells: this.cellsWanted, minutes: this.minutes,
                project: this.projectOf() || '' });
              this.apply(state);
            } catch (err) {
              this.note = err.message || String(err);
              this.draw();
            }
          } })));
      q.focus();
      return;
    }

    const c = st.counters || { cells: 0, working: 0, done: 0, error: 0, timeout: 0, found: 0, missing: 0 };
    const tail = [
      `${c.cells} сот`,
      c.working ? `${c.working} ищут` : '',
      `${c.done} ответили`,
      c.error ? `${c.error} с ошибкой` : '',
      c.timeout ? `${c.timeout} не успели` : '',
      `${c.found} находок`,
      st.result && st.result.conflicts && st.result.conflicts.length
        ? `${st.result.conflicts.length} спорных` : '',
    ].filter(Boolean).join(' · ');
    this.box.append(
      h('div', { class: 'swarm-q-said', text: st.question }),
      this.drawGrid(),
      h('div', { class: 'comb-tail', text: tail }),
      this.drawCell(),
      h('div', { class: 'swarm-actions' },
        this.busy
          ? h('button', { class: 'btn soft', text: 'Остановить', onclick: () => this.api.swarm.stop() })
          : h('button', { class: 'btn soft', text: 'Новый рой', onclick: () => { this.state = null; this.picked = null; this.draw(); } }),
        st.result && st.result.path
          ? h('button', { class: 'btn soft', text: 'Открыть отчёт', onclick: () => this.api.shell.open(st.result.path) })
          : null,
        st.stage === 'done' ? h('span', { class: 'comb-done', text: 'Соты остаются с итогом, пока вы не запустите новый рой' }) : null));
  }

  dispose() {
    if (this.off) this.off();
  }
}

export { STATE_RU };
