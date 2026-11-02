// Employee directory: search and filters over the list, the side card loads and saves without a page reload.
// Without JavaScript the same links and forms work as ordinary page navigation.
(() => {
  const root = document.querySelector('[data-emp-root]');
  if (!root) return;
  const panel = document.getElementById('employee-panel');
  const toolbar = root.querySelector('.emp-toolbar');
  const search = document.getElementById('employee-search');
  const deptFilter = document.getElementById('employee-dept-filter');
  const summary = document.getElementById('employee-summary');
  const toast = document.getElementById('employee-toast');
  const FETCH_HEADERS = { 'X-Requested-With': 'fetch' };
  const STORE_KEY = 'sysai-employees-filter';

  root.classList.add('emp-js');
  if (toolbar) toolbar.hidden = false;

  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || '{}'); } catch (_) { saved = {}; }
  let chipFilter = ['active', 'no-tg', 'voice', 'inactive'].includes(saved.chip) ? saved.chip : 'active';
  if (search && saved.q) search.value = saved.q;
  if (deptFilter && saved.dept && [...deptFilter.options].some(o => o.value === saved.dept)) deptFilter.value = saved.dept;

  let dirty = false;
  let currentTab = 'profile';
  let toastTimer = null;

  const norm = value => String(value || '').toLowerCase().replace(/ё/g, 'е').trim();
  const rows = () => [...root.querySelectorAll('.emp-row')];

  function remember() {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ chip: chipFilter, q: search ? search.value : '', dept: deptFilter ? deptFilter.value : '' }));
    } catch (_) { /* private mode */ }
  }

  function chipMatches(row, chip) {
    const active = row.dataset.active === '1';
    if (chip === 'inactive') return !active;
    if (!active) return false;
    if (chip === 'no-tg') return row.dataset.telegram !== '1';
    if (chip === 'voice') return row.dataset.voice !== 'ready';
    return true;
  }

  function deptMatches(row, dept) {
    return !dept || (row.dataset.departments || '').split(',').includes(dept);
  }

  function applyFilters() {
    const terms = norm(search && search.value).split(/\s+/).filter(Boolean);
    const dept = deptFilter ? deptFilter.value : '';
    const all = rows();
    let shown = 0;
    all.forEach(row => {
      const text = row.dataset.search || '';
      const visible = chipMatches(row, chipFilter) && deptMatches(row, dept) && terms.every(t => text.includes(t));
      row.hidden = !visible;
      if (visible) shown += 1;
    });
    root.querySelectorAll('.emp-group').forEach(group => {
      const count = group.querySelectorAll('.emp-row:not([hidden])').length;
      group.hidden = count === 0;
      const badge = group.querySelector('.emp-group-count');
      if (badge) badge.textContent = count;
    });
    const empty = document.getElementById('employee-no-results');
    if (empty) empty.hidden = shown > 0 || all.length === 0;
    if (toolbar) {
      toolbar.querySelectorAll('[data-chip]').forEach(chip => {
        const on = chip.dataset.chip === chipFilter;
        chip.classList.toggle('on', on);
        chip.setAttribute('aria-pressed', on ? 'true' : 'false');
        const counter = chip.querySelector('.emp-chip-count');
        if (counter) counter.textContent = all.filter(r => chipMatches(r, chip.dataset.chip) && deptMatches(r, dept)).length;
      });
    }
    remember();
  }

  function showToast(message, isError) {
    if (!toast || !message) return;
    toast.textContent = message;
    toast.classList.toggle('err', Boolean(isError));
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, isError ? 6000 : 3500);
  }

  function selectRow(id) {
    rows().forEach(row => {
      const on = id != null && row.dataset.id === String(id);
      row.classList.toggle('selected', on);
      const link = row.querySelector('.emp-row-link');
      if (link) {
        if (on) link.setAttribute('aria-current', 'true');
        else link.removeAttribute('aria-current');
      }
    });
  }

  function revealRow(id) {
    const row = document.getElementById(`employee-${id}`);
    if (!row) return;
    if (row.hidden) {
      // A saved or opened person must stay visible even if the current filter would hide them.
      chipFilter = row.dataset.active === '1' ? 'active' : 'inactive';
      if (search) search.value = '';
      if (deptFilter) deptFilter.value = '';
      applyFilters();
    }
    const box = row.getBoundingClientRect();
    if (box.top < 0 || box.bottom > window.innerHeight) row.scrollIntoView({ block: 'nearest' });
  }

  function flashRow(id) {
    const row = document.getElementById(`employee-${id}`);
    if (!row) return;
    row.classList.remove('emp-row-flash');
    void row.offsetWidth;
    row.classList.add('emp-row-flash');
  }

  const narrow = window.matchMedia('(max-width: 1100px)');

  function setPanelOpen(open) {
    root.classList.toggle('panel-open', open);
    document.body.classList.toggle('emp-panel-modal', open && narrow.matches);
    fitPanel();
  }

  // On wide screens the sticky card must end above the bottom edge, so its footer buttons stay visible
  // even before the page is scrolled.
  let fitQueued = false;
  function fitPanel() {
    if (narrow.matches) { panel.style.removeProperty('--emp-panel-max'); return; }
    const top = Math.max(16, panel.getBoundingClientRect().top);
    panel.style.setProperty('--emp-panel-max', `${Math.max(320, Math.round(window.innerHeight - top - 16))}px`);
  }
  function queueFit() {
    if (fitQueued) return;
    fitQueued = true;
    requestAnimationFrame(() => { fitQueued = false; fitPanel(); });
  }
  window.addEventListener('scroll', queueFit, { passive: true });
  window.addEventListener('resize', queueFit);
  narrow.addEventListener('change', () => setPanelOpen(root.classList.contains('panel-open')));

  function activateTab(name) {
    const tabs = [...panel.querySelectorAll('[data-tab]')];
    if (!tabs.length) return;
    if (!tabs.some(t => t.dataset.tab === name)) name = tabs[0].dataset.tab;
    currentTab = name;
    tabs.forEach(tab => {
      const on = tab.dataset.tab === name;
      tab.classList.toggle('on', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
    });
    panel.querySelectorAll('[data-pane]').forEach(pane => { pane.hidden = pane.dataset.pane !== name; });
    const field = panel.querySelector('[data-emp-tab-field]');
    if (field) field.value = name;
  }

  function setError(message) {
    const box = panel.querySelector('.emp-form-error');
    if (!box) { if (message) showToast(message, true); return; }
    box.textContent = message || '';
    box.hidden = !message;
    if (!message) return;
    if (box.offsetParent === null) showToast(message, true);
    else box.scrollIntoView({ block: 'nearest' });
  }

  function initPanel(tab) {
    dirty = false;
    const card = panel.querySelector('.emp-card');
    setPanelOpen(Boolean(card));
    if (!card) return;
    activateTab(tab || card.dataset.activeTab || 'profile');
    const form = panel.querySelector('#employee-form');
    if (form) {
      form.noValidate = true;  // validated in onSubmit, so fields on hidden tabs can be revealed first
      form.addEventListener('input', () => { dirty = true; });
      form.addEventListener('change', () => { dirty = true; });
      form.addEventListener('submit', onSubmit);
    }
  }

  function confirmLeave() {
    return !dirty || window.confirm('В карточке есть несохранённые изменения. Закрыть без сохранения?');
  }

  function panelUrlFor(pageUrl) {
    const params = new URLSearchParams(pageUrl.search);
    const query = new URLSearchParams();
    ['edit', 'add', 'tab'].forEach(key => { if (params.get(key)) query.set(key, params.get(key)); });
    const qs = query.toString();
    return `/employees/panel${qs ? `?${qs}` : ''}`;
  }

  async function open(pageUrl, { push = true } = {}) {
    if (!confirmLeave()) return;
    const params = new URLSearchParams(pageUrl.search);
    const editId = params.get('edit');
    panel.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(panelUrlFor(pageUrl), { headers: FETCH_HEADERS, credentials: 'same-origin' });
      if (!response.ok) throw new Error(response.status === 404 ? 'Сотрудник не найден — возможно, его уже удалили.' : 'Не удалось открыть карточку.');
      panel.innerHTML = await response.text();
    } catch (error) {
      showToast(error.message || 'Не удалось открыть карточку.', true);
      return;
    } finally {
      panel.removeAttribute('aria-busy');
    }
    const clean = new URL(pageUrl.href);
    clean.searchParams.delete('voice_msg');
    clean.searchParams.delete('saved');
    clean.hash = '';
    if (push) history.pushState({ employees: true }, '', clean.pathname + clean.search);
    initPanel(params.get('tab') || undefined);
    selectRow(editId);
    if (editId) revealRow(editId);
    panel.scrollTop = 0;
    if (params.get('add')) {
      const name = panel.querySelector('input[name="name"]');
      if (name) name.focus();
    }
  }

  function swapList(html) {
    const region = document.getElementById('employee-list-region');
    if (!region || !html) return;
    const holder = document.createElement('div');
    holder.innerHTML = html.trim();
    const fresh = holder.querySelector('#employee-list-region');
    if (fresh) region.replaceWith(fresh);
    applyFilters();
  }

  async function onSubmit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.checkValidity()) {
      const invalid = form.querySelector(':invalid');
      if (invalid) {
        const pane = invalid.closest('[data-pane]');
        if (pane) activateTab(pane.dataset.pane);
        invalid.reportValidity();
      }
      return;
    }
    const data = new FormData(form);
    const submitter = event.submitter;
    if (submitter && submitter.name) data.set(submitter.name, submitter.value);
    const buttons = [...panel.querySelectorAll('button[form="employee-form"]')];
    buttons.forEach(button => { button.disabled = true; });
    setError('');
    let payload = null;
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: data, credentials: 'same-origin',
        headers: { ...FETCH_HEADERS, Accept: 'application/json' },
      });
      payload = await response.json().catch(() => null);
      if (!response.ok || !payload || !payload.ok) {
        const detail = payload && (payload.error || (typeof payload.detail === 'string' ? payload.detail : ''));
        throw new Error(detail || 'Не удалось сохранить карточку. Проверьте поля и попробуйте ещё раз.');
      }
    } catch (error) {
      buttons.forEach(button => { button.disabled = false; });
      setError(error.message);
      return;
    }
    dirty = false;
    const keepTab = currentTab;
    swapList(payload.list_html);
    if (summary && payload.summary) summary.textContent = payload.summary;
    panel.innerHTML = payload.panel_html;
    if (payload.next === 'add') {
      history.replaceState({ employees: true }, '', '/employees?add=1');
      initPanel('profile');
      selectRow(null);
      const name = panel.querySelector('input[name="name"]');
      if (name) name.focus();
    } else {
      history.replaceState({ employees: true }, '', `/employees?edit=${payload.id}`);
      initPanel(keepTab);
      selectRow(payload.id);
    }
    revealRow(payload.id);
    flashRow(payload.id);
    showToast(payload.message);
  }

  root.addEventListener('click', event => {
    const tab = event.target.closest('[data-tab]');
    if (tab && panel.contains(tab)) { activateTab(tab.dataset.tab); return; }
    const chip = event.target.closest('[data-chip]');
    if (chip) { chipFilter = chip.dataset.chip; applyFilters(); return; }
    const link = event.target.closest('a[data-emp-open]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    open(new URL(link.href, location.href));
  });

  // The page header "Add" button lives outside the layout root.
  document.querySelectorAll('.employees-page-head a[data-emp-open]').forEach(link => {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      open(new URL(link.href, location.href));
    });
  });

  panel.addEventListener('keydown', event => {
    const tab = event.target.closest('[data-tab]');
    if (!tab || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const tabs = [...panel.querySelectorAll('[data-tab]')];
    const next = tabs[(tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    activateTab(next.dataset.tab);
    next.focus();
  });

  if (search) {
    let timer = null;
    search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(applyFilters, 80); });
    search.addEventListener('keydown', event => {
      if (event.key === 'Escape' && search.value) { search.value = ''; applyFilters(); }
    });
  }
  if (deptFilter) deptFilter.addEventListener('change', applyFilters);

  document.addEventListener('keydown', event => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    if (event.key === '/' && !typing && search) { event.preventDefault(); search.focus(); }
    if (event.key === 'Escape' && !typing && root.classList.contains('panel-open') && panel.querySelector('.emp-card')) {
      open(new URL('/employees', location.href));
    }
  });

  window.addEventListener('popstate', () => { open(new URL(location.href), { push: false }); });
  window.addEventListener('beforeunload', event => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
  // Voice actions are ordinary form posts that reload the page; leaving the card for them is intended.
  panel.addEventListener('submit', event => {
    if (event.target.id !== 'employee-form') dirty = false;
  }, true);

  const initial = new URLSearchParams(location.search);
  initPanel(location.hash === '#voice' ? 'voice' : (initial.get('tab') || undefined));
  if (initial.get('edit')) {
    const row = document.getElementById(`employee-${initial.get('edit')}`);
    if (row && row.dataset.active !== '1') chipFilter = 'inactive';
  }
  applyFilters();
  if (initial.get('edit')) revealRow(initial.get('edit'));
  if (initial.get('voice_msg') || initial.get('saved')) {
    const clean = new URL(location.href);
    clean.searchParams.delete('voice_msg');
    clean.searchParams.delete('saved');
    history.replaceState({ employees: true }, '', clean.pathname + clean.search + clean.hash);
  }
})();
