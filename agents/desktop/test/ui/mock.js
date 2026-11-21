// window.sysai for the window in a browser (serve.js): Hermes is a script, the projects are real (lib/projects.js over
// /rpc), the rest answers with something plausible. ?scene=work (a conversation in the middle of a job, the pane with
// the project) | chat (the conversation alone) | empty (a new conversation) | boot (Hermes does not start).
(() => {
  const scene = new URLSearchParams(location.search).get('scene') || 'work';
  // ?theme=light — посмотреть светлую тему на стенде: app.js читает этот ключ при загрузке
  const theme = new URLSearchParams(location.search).get('theme');
  if (theme) localStorage.setItem('sysai.theme', theme);
  const listeners = {};
  const on = (ch) => (fn) => { (listeners[ch] = listeners[ch] || []).push(fn); return () => {}; };
  const emit = (ch, payload) => (listeners[ch] || []).forEach((fn) => fn(payload));
  const rpc = async (channel, ...args) => {
    const r = await fetch('/rpc', { method: 'POST', body: JSON.stringify({ channel, args }) }).then((x) => x.json());
    if (!r.ok) throw new Error(r.error);
    return r.value;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const PROJECT = '/tmp/sysai-ui-demo/Элеватор Благовещенск';
  const ago = (m) => new Date(Date.now() - m * 60000).toISOString();
  // разговоры, которые окно держит само: одного нет в списке Hermes — он открывается из своей копии
  const STORED = [
    { id: 's-2', title: 'Разобрать пример проекта 2024 года', project: 'Элеватор Благовещенск', cwd: PROJECT, updated: ago(90), messages: 24 },
    { id: 's-kept', title: 'Сверка сигналов, которой агент уже не помнит', project: '', cwd: '', updated: ago(60 * 30), messages: 12 },
  ];
  const SESSIONS = [
    { sessionId: 's-work', title: 'Собрать проект элеватора по ТЗ и Э3', updatedAt: ago(2), cwd: PROJECT },
    { sessionId: 's-2', title: 'Разобрать пример проекта 2024 года', updatedAt: ago(90), cwd: PROJECT },
    { sessionId: 's-3', title: 'Почему не открывается окно 12 на панели', updatedAt: ago(60 * 26) },
    { sessionId: 's-4', title: 'Сверить перечень сигналов с Э3 лист 7', updatedAt: ago(60 * 24 * 3) },
    { sessionId: 's-5', title: 'Экспорт тегов OPC UA для панели', updatedAt: ago(60 * 24 * 9) },
  ];
  const text = (t) => ({ type: 'text', text: t });
  const out = (t) => [{ type: 'content', content: text(t) }];
  const MODEL = [{ id: 'model', name: 'Модель', type: 'select', currentValue: 'strong', options: [
    { value: 'strong', name: 'Claude Sonnet — сильная' }, { value: 'mid', name: 'GPT — средняя' }, { value: 'cheap', name: 'Kimi — быстрая' }] }];

  function script(sid) {
    const u = (update) => emit('acp:notification', { method: 'session/update', params: { sessionId: sid, update } });
    const tool = (id, title, rawInput, status, content, kind = 'other') => u({ sessionUpdate: 'tool_call', toolCallId: id, title, kind,
      status, rawInput, content: content ? out(content) : undefined });
    u({ sessionUpdate: 'user_message_chunk', content: text('Собери проект элеватора: в материалах ТЗ, схема Э3 и перечень механизмов. Контроллер M241, панель cMT. Иди по этапам.') });
    u({ sessionUpdate: 'agent_thought_chunk', content: text('Проект уже начат: материалы и решения закрыты, программа написана. Следующий этап — компиляция. Сначала прочитаю ход в pipeline.json.') });
    u({ sessionUpdate: 'plan', entries: [
      { content: 'Прочитать ход проекта и спецификацию', status: 'completed', priority: 'high' },
      { content: 'Собрать проект Machine Expert и скомпилировать', status: 'in_progress', priority: 'high' },
      { content: 'Проверить логику в симуляции', status: 'pending', priority: 'medium' },
      { content: 'Собрать экраны панели cMT3072X', status: 'pending', priority: 'medium' },
    ] });
    tool('t1', 'mcp__plc_kit__plc_stage_update: ' + PROJECT, { project_dir: PROJECT }, 'completed', '{"next": "build", "done": 3}');
    tool('t2', 'read_file: _work/spec/project.spec.json', { path: '_work/spec/project.spec.json' }, 'completed', '12 механизмов, 96 сигналов', 'read');
    tool('t3', 'mcp__plc_kit__plc_stage_update: ' + PROJECT, { project_dir: PROJECT, stage: 'build', state: 'work', note: 'Компилирую в Machine Expert' }, 'completed', 'ok');
    tool('t4', 'tool_call: mcp__plc_kit__plc_project_build', { name: 'mcp__plc_kit__plc_project_build', arguments: { project_dir: PROJECT } }, 'completed',
      'ОШИБКА (plc-kit/plc_project_build): канал DI.I7 привязан к двум сигналам (N3.belt, N4.speed)\n[SYSAI: сервер plc-kit работает и ответил — это ответ на ЭТОТ вызов, а не обрыв связи.]');
    u({ sessionUpdate: 'agent_message_chunk', content: text('Канал **DI.I7** оказался у двух сигналов — по Э3 лист 7 это датчик схода ленты нории N3, у N4 скорость на **DI.I8**. Исправил спецификацию и собираю снова.\n\n') });
    tool('t5', 'patch: _work/spec/project.spec.json', { path: '_work/spec/project.spec.json' }, 'completed', '', 'edit');
    tool('t6', 'mcp__plc_kit__plc_project_build: ' + PROJECT, { project_dir: PROJECT }, 'completed', 'Elevator.project собран: 12 FB, 96 каналов, OPC UA');
    tool('t7', 'mcp__machine_expert__me_build: plc/Elevator.project', { file: 'plc/Elevator.project' }, scene === 'work' ? 'in_progress' : 'completed',
      scene === 'work' ? null : '0 ошибок, 2 предупреждения');
    if (scene !== 'work') {
      u({ sessionUpdate: 'agent_message_chunk', content: text('Компиляция прошла: **0 ошибок**, 2 предупреждения (неиспользуемые переменные `GVL.tmp1`, `GVL.tmp2`).\n\n| Этап | Состояние |\n|---|---|\n| Программа ПЛК | готово |\n| Компиляция | готово |\n| Экраны | следующий |\n\nДальше — проверка в симуляции и экраны.') });
    }
    u({ sessionUpdate: 'usage_update', used: 48200, size: 200000 });
  }

  let gen = 0;
  window.sysai = {
    info: async () => ({ version: '0.10.1', electron: '33.2.0', agent: 'opo-ivanov', person: 'Иванов Пётр Сергеевич', connected: true,
      server: 'https://hostless.cloud/apps/sysai', home: 'C:\\Users\\ivanov\\SYSAI', profile: 'sysai' }),
    ready: () => {},
    acp: {
      state: async () => (scene === 'boot' ? { state: 'error', message: 'Hermes завершился с кодом 1', stderr: 'ModuleNotFoundError: No module named acp' }
        : { state: 'ready', generation: ++gen, agentInfo: { version: '0.23.1' } }),
      restart: async () => {},
      request: async (method, params) => {
        if (method === 'session/list') return { sessions: scene === 'empty' ? SESSIONS.slice(1) : SESSIONS };
        if (method === 'session/new') return { sessionId: `s-new-${Date.now()}`, configOptions: MODEL };
        if (method === 'session/load') {
          // ?scene=kept — разговор, которого агент уже не помнит: окно показывает свою копию
          if (scene === 'kept') throw new Error('session not found: профиль агента создан заново');
          setTimeout(() => script(params.sessionId), 30); await sleep(80); return { configOptions: MODEL };
        }
        if (method === 'session/prompt') { await sleep(1500); return { stopReason: 'end_turn' }; }
        return {};
      },
      notify: async () => {}, respond: async () => {},
      onState: on('acp:state'), onNotification: on('acp:notification'), onRequest: on('acp:request'), onRequestGone: on('acp:request-gone'),
    },
    auto: { get: async () => ({ on: true, count: 3, project: 'Элеватор Благовещенск' }), set: async (v) => ({ on: v, count: 3 }),
      recent: async () => [], undo: async () => ({}), onState: on('auto:state'), onToggle: on('auto:toggle') },
    fs: {
      places: async () => [], drives: async () => [], list: async () => ({ path: 'C:\\', items: [] }),
      info: async (p) => ({ exists: true, path: p, dir: false }), text: async () => '', bytes: async () => new Uint8Array(),
      url: async (p) => `file://${p}`, attachments: async () => ({ items: [], text: '' }), paste: async () => '',
    },
    shell: { open: async () => {}, reveal: async () => {}, run: async () => {}, external: async () => {} },
    // «Проверить обновление»: в стенде ?upd=1 — есть новая версия, ?upd=bad — компьютер не связан с SYSAI
    // рой: стенд показывает живые соты без настоящих агентов
    swarm: (() => {
      let state = null;
      const subs = new Set();
      const count = (s) => {
        const list = s.cells || [];
        s.counters = {
          cells: list.length,
          working: list.filter((x) => x.state === 'working').length,
          done: list.filter((x) => x.state === 'done').length,
          error: list.filter((x) => x.state === 'error').length,
          timeout: list.filter((x) => x.state === 'timeout').length,
          found: list.reduce((n, x) => n + ((x.answer && x.answer.found) || []).length, 0),
          missing: list.reduce((n, x) => n + ((x.answer && x.answer.missing) || []).length, 0),
        };
      };
      const say = (s) => { count(s); state = s; for (const fn of subs) fn(JSON.parse(JSON.stringify(s))); };
      const cell = (i, n) => ({
        i, title: ['Нория', 'Шнек', 'Триер', 'МУЗ', 'Весы', 'Заслонка', 'Вентилятор', 'Датчик'][i % 8] + ` ${i + 1}`,
        state: 'idle', startedAt: 0, endedAt: 0, error: '', raw: '', answer: null,
      });
      return {
        allowed: async () => ({ on: true, cells: 16, why: '' }),
        state: async () => state,
        start: async ({ question, cells }) => {
          const n = Math.max(4, Math.min(16, cells || 8));
          const fresh = { id: 'swarm-demo', question, stage: 'work', startedAt: Date.now(), endedAt: 0,
            cells: Array.from({ length: n }, (_, i) => cell(i, n)),
            counters: { cells: n, working: 0, done: 0, error: 0, timeout: 0, found: 0, missing: 0 },
            result: { markdown: '', conflicts: [], path: '' }, error: '' };
          say(fresh);
          // три волны: ищут → ответили / один с ошибкой / один не успел
          setTimeout(() => { for (const c of state.cells.slice(0, Math.ceil(n / 3))) c.state = 'working'; say(state); }, 500);
          setTimeout(() => {
            for (const c of state.cells) {
              if (c.i % 7 === 3) { c.state = 'error'; c.error = 'модель отказала (демо)'; }
              else if (c.i % 11 === 9) { c.state = 'timeout'; }
              else if (c.i < Math.ceil(n * 0.7)) {
                c.state = 'done';
                c.answer = { checked: 12 + c.i, found: [{ key: `Q${c.i + 1}`, value: '1', where: 'Э3:лист ' + (c.i + 1) }], missing: [], notes: '', parsed: true };
              }
            }
            say(state);
          }, 1600);
          setTimeout(() => {
            for (const c of state.cells) if (c.state === 'working') {
              c.state = 'done';
              c.answer = { checked: 4, found: [], missing: [{ key: `Q${c.i + 1}`, why: 'нет в схеме' }], notes: '', parsed: true };
            }
            state.stage = 'done';
            state.result = { markdown: '# Демо', path: 'C:/проект/.sysai/swarm/swarm-demo/result.md',
              conflicts: [{ key: 'q1', values: [{ value: '1', cells: [{ cell: 0, where: 'Э3' }] }, { value: '2', cells: [{ cell: 2, where: 'Э3' }] }] }] };
            say(state);
          }, 3000);
          return state;
        },
        stop: async () => true,
        onState: (fn) => { subs.add(fn); return () => subs.delete(fn); },
      };
    })(),
    updates: {
      check: async () => (new URLSearchParams(location.search).get('upd') === 'bad'
        ? { ok: false, status: 401, local: { version: '0.4.11' }, log: ['2026-11-20 09:12:04 WARNING SYSAI не принял токен агента'],
          error: 'SYSAI не принял токен агента: агента отключили или подключили на другом компьютере. Получите новый код на странице агента и запустите установщик снова' }
        : { ok: true, update: new URLSearchParams(location.search).get('upd') === '1',
          local: { version: '0.4.11' }, offered: { version: '0.4.12', build: 'f'.repeat(64) }, autoUpdate: true,
          why: 'SYSAI даёт 0.4.12, на компьютере 0.4.11', log: [] }),
      now: async () => ({ asking: true }),
      // «что с обновлением сейчас»: стенд делает вид, что синхронизатор уже раскладывает файлы
      status: async () => ({ running: true, asked: false, version: '0.4.11', updateTo: '0.4.12',
        updateAt: new Date().toISOString(), desktopTo: '', error: '', errorAt: '', syncedAt: new Date().toISOString(),
        logAt: new Date().toISOString(), log: ['2026-11-20 12:20:01 INFO скачано, ставлю 0.4.12'] }),
    },
    // «Открыть в программе»: в стенде все четыре формата уже «есть», чтобы кнопки было видно
    apps: {
      detect: async () => [
        { id: 'oni', title: 'ONI Visual Studio', kind: 'hmi', open: 'file', file: PROJECT + '/hmi', vendor: 'ONI (Flexem)', serves: 'панели ONI/Flexem — экраны и мнемосхемы' },
        { id: 'easybuilder', title: 'EasyBuilder Pro', kind: 'hmi', open: 'file', file: PROJECT + '/hmi/Line.cmtp', vendor: 'Weintek', serves: 'панели cMT/eMT — экраны и мнемосхемы' },
        { id: 'me', title: 'EcoStruxure Machine Expert', kind: 'plc', open: 'file', file: PROJECT + '/plc/Elevator.project', vendor: 'Schneider Electric', serves: 'контроллеры M2xx/M5xx (TM241, TM251, TM262…)' },
        { id: 'hawell', title: 'Haiwell Happy', kind: 'plc', open: 'paste', file: PROJECT + '/hawell/program.il', vendor: 'Haiwell', serves: 'контроллеры Haiwell — программа списком инструкций' },
      ],
      open: async (id) => ({ id, exe: '', file: '', clipboard: id === 'hawell', auto: false, len: 900 }),

      // «где и что будет»: в стенде железо проекта — M241 и панель cMT3072X
      plan: async () => {
        const openers = await window.sysai.apps.detect();
        const side = (kind, fact) => {
          const list = openers.filter((o) => o.kind === kind);
          const want = kind === 'hmi' ? (/c?mt|weintek/i.test(fact) ? 'easybuilder' : 'oni') : (/haiwell/i.test(fact) ? 'hawell' : 'me');
          const hit = list.find((o) => o.id === want) || list[0] || {};
          const out = { what: kind === 'plc' ? 'ПЛК' : 'Экраны', id: hit.id || '', fact, program: hit.title || '', vendor: hit.vendor || '',
            serves: hit.serves || '', file: hit.file || '', ready: Boolean(hit.title) };
          if (hit.id === 'me') Object.assign(out, { languages: ['st', 'ld'], writes: ['st'], languageNote: 'LD для Machine Expert пока не генерируется — программа идёт на ST' });
          return out;
        };
        const scoped = scene === 'scope';
        return { openers, environment: { plc: side('plc', scoped ? '' : 'TM241CE40R'), hmi: side('hmi', 'cMT3072X'), resolution: '1024×600',
          link: scoped ? 'modbus_tcp' : 'opcua', linkWhy: scoped ? 'Modbus TCP по Ethernet: адреса %M в программе и на экранах'
            : 'оба умеют OPC UA: переменные по именам, адреса %M не нужны' } };
      },
    },
    dialog: { files: async () => [] }, conv: { export: async () => '' }, clipboard: { write: async () => {} },
    storage: { list: async () => ({ items: [] }), fetch: async () => '', upload: async () => ({}) },
    projects: {
      list: () => rpc('projects:list'), create: (n, s) => rpc('projects:create', n, s), info: (n) => rpc('projects:info', n),
      add: async () => ({}), tidy: async () => [], dialog: (n, d) => rpc('projects:dialog', n, d), role: async () => ({}), attach: async () => ({}),
      delete: async (n, mode) => ({ name: n, mode, dialogs: 0, path: `/проекты/.trash/${n}` }),
    },
    // разговоры: на стенде живут в памяти страницы — как будто окно их уже записало
    chats: {
      list: async () => ({ dialogs: STORED, deleted: [] }),
      read: async (id) => ({ id, title: 'Сверка сигналов, которой агент уже не помнит', cwd: '',
        messages: [
          { t: 'user', text: 'Сверь перечень сигналов с Э3 лист 7 — каналы DI и DO.' },
          { t: 'agent', text: 'Сверил: **96 сигналов**, расхождений два.\n\n- `DI.I7` — датчик схода ленты нории N3 (по Э3 лист 7), в перечне он был у N4.\n- `DO.Q5` — пускатель шнека N2, в перечне отсутствует.\n\nСпецификацию поправил, перечень обновлён.' },
          { t: 'tool', id: 'k1', title: 'read_file: materials/Э3 лист 7.pdf', kind: 'read', status: 'completed' },
          { t: 'notice', text: 'Разговор восстановлен с этого компьютера: агент его уже не помнит.', kind: '' },
        ] }),
      write: async (doc) => ({ id: doc.id, title: doc.title || '', updated: new Date().toISOString(), messages: (doc.messages || []).length }),
      delete: async (id) => ({ id }),
    },
    // вопрос системным окном: на стенде — сразу «Отмена»
    ui: { confirm: async (opts) => ({ response: 0, buttons: (opts && opts.buttons) || [] }) },
    canvas: { state: async () => ({ open: false }), open: async () => {}, pick: async () => '', window: async () => {}, edit: async () => ({}),
      undo: async () => ({}), reload: async () => {}, close: async () => {}, easybuilder: async () => {}, pointer: () => {},
      onState: on('canvas:state'), onCursors: on('canvas:cursors'), onEdited: on('canvas:edited'), onFocus: on('canvas:focus') },
    win: { overlay: async () => {}, logs: async () => '', onShow: on('ui:show'), onNotice: on('ui:notice') },
    pathForFile: () => '',
  };
  localStorage.setItem('sysai.session.last', scene === 'empty' ? '' : scene === 'kept' ? 's-kept' : 's-work');
  localStorage.setItem('sysai.project', scene === 'scope' ? 'Шкаф СП-2 экраны' : 'Элеватор Благовещенск');
  localStorage.removeItem('sysai.pane.open');
  if (scene === 'work' || scene === 'scope') addEventListener('load', () => setTimeout(() => document.getElementById('nav-projects').click(), 200));
  // ?scene=new — «Новый проект»: имя и выбор объёма (полностью / только ПЛК / только экраны)
  if (scene === 'new') {
    localStorage.removeItem('sysai.project');
    addEventListener('load', () => setTimeout(() => {
      document.getElementById('nav-projects').click();
      setTimeout(() => { const b = [...document.querySelectorAll('.btn')].find((x) => /Новый проект/.test(x.textContent || '')); if (b) b.click(); }, 500);
    }, 200));
  }
  // ?scene=job&upd=1 — нажать «Установить» и посмотреть полосу обновления
  if (scene === 'job') addEventListener('load', () => setTimeout(() => {
    const b = [...document.querySelectorAll('.upd-bar .btn')].find((x) => /Установить/.test(x.textContent || ''));
    if (b) b.click();
  }, 900));
  if (scene === 'format') {   // «в каком формате сохранить» — варианты с маленькими лого
    setTimeout(() => emit('acp:request', { id: 9001, method: 'elicitation/create', params: {
      sessionId: 's-work',
      message: 'В каком формате сохранить программу ПЛК?',
      requestedSchema: { type: 'object', properties: { answer: { type: 'string',
        enum: ['Machine Expert', 'Haiwell Happy'],
        'x-icons': { 'Machine Expert': 'img/format-me.svg', 'Haiwell Happy': 'img/format-hawell.svg' } } } },
    } }), 900);
  }
  if (scene === 'swarm') {
    setTimeout(() => { const b = document.querySelector('.comb-btn'); if (b) b.click(); }, 700);
    setTimeout(() => { const q = document.querySelector('.swarm-q'); if (q) { q.value = 'сверь адреса в спецификации с Э3'; q.dispatchEvent(new Event('input', { bubbles: true })); } }, 900);
    setTimeout(() => { const b = [...document.querySelectorAll('.swarm-card .btn')].find((x) => /Запустить/.test(x.textContent)); if (b) b.click(); }, 1100);
    setTimeout(() => { const c = document.querySelector('.comb-grid .cell.done'); if (c) c.click(); }, 4200);
  }
  if (scene === 'panel') {    // экраны: сначала формат панели, потом разрешение — панели разные
    setTimeout(() => emit('acp:request', { id: 9002, method: 'elicitation/create', params: {
      sessionId: 's-work',
      message: 'В каком формате рисовать экраны?',
      requestedSchema: { type: 'object', properties: { answer: { type: 'string',
        enum: ['ONI', 'EasyBuilder'],
        'x-icons': { 'ONI': 'img/format-oni.svg', 'EasyBuilder': 'img/format-easybuilder.svg' } } } },
    } }), 600);
    setTimeout(() => emit('acp:request', { id: 9003, method: 'elicitation/create', params: {
      sessionId: 's-work',
      message: 'Под какое разрешение рисовать экраны? Панели разные — экран под 1024×768 на 800×480 не влезает.',
      requestedSchema: { type: 'object', properties: { answer: { type: 'string',
        enum: ['800×480 — 7" панель', '1024×600 — 7" широкая', '1024×768 — 10,4"/12,1"',
               '1280×800 — 10,1"', '1366×768 — 15"', '1920×1080 — 15"/21,5"'] } } },
    } }), 1400);
  }
})();
