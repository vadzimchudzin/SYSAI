// The Skills page: search and filters over the built-in skills, the unsaved-changes bar, long company skills folded.
(() => {
  const root = document.querySelector('[data-sk-root]');
  if (!root) return;
  const form = root.querySelector('[data-sk-form]');
  const rows = [...root.querySelectorAll('[data-sk-row]')];
  const groups = [...root.querySelectorAll('[data-sk-group]')];
  const tools = root.querySelector('[data-sk-tools]');
  const search = root.querySelector('[data-sk-search]');
  const chips = [...root.querySelectorAll('[data-sk-filter]')];
  const empty = root.querySelector('[data-sk-empty]');
  const cols = root.querySelector('.sk-cols');
  const bar = root.querySelector('[data-sk-save]');
  const msg = root.querySelector('[data-sk-msg]');
  const undo = root.querySelector('[data-sk-undo]');
  const calm = msg ? msg.textContent : '';
  let filter = 'all';
  let leaving = false;

  const isOn = (row) => row.querySelector('.sk-switch input').checked;
  const mode = (row) => row.querySelector('select[name^="mode_"]');
  const tests = {
    all: () => true,
    off: (row) => !isOn(row),
    guarded: (row) => !!mode(row) && mode(row).value !== 'auto',
    errors: (row) => Number(row.dataset.errors) > 0,
    unused: (row) => Number(row.dataset.total) === 0,
  };

  function sync() {
    rows.forEach((row) => {
      row.classList.toggle('is-off', !isOn(row));
      const select = mode(row);
      if (select) select.closest('.sk-mode').dataset.mode = select.value;
      const who = row.querySelector('.sk-who');
      const limit = who && who.querySelector('select');
      if (limit) who.classList.toggle('is-limited', limit.value !== '');
    });
    groups.forEach((group) => {
      const own = [...group.querySelectorAll('[data-sk-row]')];
      const count = group.querySelector('[data-sk-count]');
      if (count) count.textContent = `${own.filter(isOn).length} из ${own.length} включено`;
    });
    chips.forEach((chip) => {
      const n = chip.querySelector('[data-sk-n]');
      if (n) n.textContent = rows.filter(tests[chip.dataset.skFilter]).length;
    });
  }

  function show() {
    const q = (search ? search.value : '').trim().toLowerCase();
    let shown = 0;
    rows.forEach((row) => {
      const fits = tests[filter](row) && (!q || row.dataset.text.includes(q));
      row.hidden = !fits;
      if (fits) shown += 1;
    });
    groups.forEach((group) => { group.hidden = !group.querySelector('[data-sk-row]:not([hidden])'); });
    if (empty) empty.hidden = shown > 0;
    if (cols) cols.hidden = shown === 0;
  }

  function pick(name) {
    filter = tests[name] ? name : 'all';
    chips.forEach((chip) => chip.setAttribute('aria-pressed', String(chip.dataset.skFilter === filter)));
    show();
  }

  const state = () => (form ? new URLSearchParams(new FormData(form)).toString() : '');
  const start = state();
  function dirty() {
    if (!bar) return;
    const changed = state() !== start;
    bar.classList.toggle('is-dirty', changed);
    const narrow = window.matchMedia('(max-width: 600px)').matches;
    if (msg) msg.textContent = changed ? (narrow ? 'Не сохранено' : 'Есть несохранённые изменения') : calm;
    if (undo) undo.hidden = !changed;
  }

  if (tools) tools.hidden = false;
  root.querySelectorAll('[data-sk-go]').forEach((link) => {
    link.hidden = false;
    link.addEventListener('click', () => {
      if (search) search.value = '';
      pick(link.dataset.skGo);
      const board = root.querySelector('#agent-skills');
      if (board) board.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
    });
  });
  chips.forEach((chip) => chip.addEventListener('click', () => pick(chip.dataset.skFilter)));
  if (search) {
    search.addEventListener('input', show);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && search.value) { search.value = ''; show(); }
      if (event.key === 'Enter') event.preventDefault(); // Enter in the search must not send the form
    });
  }
  const reset = root.querySelector('[data-sk-reset]');
  if (reset) reset.addEventListener('click', () => { if (search) search.value = ''; pick('all'); });

  if (form) {
    form.addEventListener('change', () => { sync(); dirty(); });
    form.addEventListener('submit', () => { leaving = true; });
    if (undo) undo.addEventListener('click', () => { form.reset(); sync(); show(); dirty(); });
  }
  window.addEventListener('beforeunload', (event) => {
    if (leaving || !bar || !bar.classList.contains('is-dirty')) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // long texts of the company skills: five lines and «Показать полностью»
  root.querySelectorAll('[data-sk-clamp]').forEach((body) => {
    body.classList.add('is-clamped');
    const more = body.closest('.sk-card') && body.closest('.sk-card').querySelector('[data-sk-more]');
    if (!more) return;
    if (body.scrollHeight - body.clientHeight < 4) { body.classList.remove('is-clamped'); return; }
    more.hidden = false;
    more.setAttribute('aria-expanded', 'false');
    more.addEventListener('click', () => {
      const open = body.classList.toggle('is-clamped') === false;
      more.textContent = open ? 'Свернуть' : 'Показать полностью';
      more.setAttribute('aria-expanded', String(open));
    });
  });

  sync();
  show();
})();
