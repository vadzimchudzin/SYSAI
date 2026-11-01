/* SYSAI · interactive organisation mind map: zoom & pan, tidy auto-layout, drag to assign/link,
   inspector panel to edit people and department composition without leaving the map. */
(() => {
  'use strict';
  const canvas = document.getElementById('org-map-canvas');
  const viewport = document.getElementById('org-map-scroll');
  const stage = document.getElementById('org-map-stage');
  const toastEl = document.getElementById('org-toast');
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const ICON_X = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>';
  const ICON_CROWN = '<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M3.2 8.2 7.6 12 12 4.8l4.4 7.2 4.4-3.8-1.9 10.3H5.1Z" fill="currentColor"/></svg>';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const STATE_KEY = 'sysai-org-map-state';
  const VIEW_KEY = 'sysai-org-map-view';
  const ARRANGED_KEY = 'sysai-org-map-arranged-v2';

  /* ---------------- toast ---------------- */
  let toastTimer = 0;
  function toast(message, isError = false) {
    if (!toastEl || !message) return;
    toastEl.textContent = message;
    toastEl.classList.toggle('is-error', !!isError);
    toastEl.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('is-on'), isError ? 6000 : 3800);
  }

  /* ---------------- dialogs (work even when the board is empty) ---------------- */
  const openDialog = id => { const d = document.getElementById(id); if (d && !d.open) d.showModal(); return d; };
  $$('[data-dialog-close], .org-map-dialog-close').forEach(btn => btn.addEventListener('click', event => {
    event.preventDefault(); btn.closest('dialog')?.close();
  }));
  const errorInto = (form, message) => {
    const target = form.querySelector('.org-map-dialog-error');
    if (target) target.textContent = message || 'Не удалось сохранить изменения.';
  };
  const readError = async response => {
    try { const r = await response.json(); return r.detail || r.message || ''; } catch (_) { return ''; }
  };
  async function fetchJson(url, payload) {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) throw new Error(result.detail || 'Не удалось сохранить изменения.');
    return result;
  }
  const removeMembership = (employeeId, departmentId) => {
    const fd = new FormData(); fd.set('view', 'map');
    return postForm(`/departments/${departmentId}/members/${employeeId}/remove`, fd);
  };
  async function postForm(url, formData) {
    const response = await fetch(url, { method: 'POST', body: formData });
    if (!response.ok) throw new Error((await readError(response)) || 'Не удалось сохранить данные.');
    return response;
  }
  function confirmAction(title, text, yesLabel = 'Удалить') {
    const dialog = document.getElementById('org-confirm-dialog');
    if (!dialog) return Promise.resolve(window.confirm(text || title));
    $('#org-confirm-title').textContent = title;
    $('#org-confirm-text').textContent = text || '';
    $('#org-confirm-yes').textContent = yesLabel;
    return new Promise(resolve => {
      const done = value => { dialog.close(); cleanup(); resolve(value); };
      const onClick = event => { const b = event.target.closest('[data-confirm]'); if (b) done(b.dataset.confirm === 'yes'); };
      const onCancel = () => { cleanup(); resolve(false); };
      const cleanup = () => { dialog.removeEventListener('click', onClick); dialog.removeEventListener('cancel', onCancel); };
      dialog.addEventListener('click', onClick); dialog.addEventListener('cancel', onCancel);
      dialog.showModal();
    });
  }
  let currentInspect = null;
  function reloadWith(message, extra = {}) {
    try {
      sessionStorage.setItem(STATE_KEY, JSON.stringify({ toast: message || '', inspect: currentInspect, ...extra }));
    } catch (_) {}
    window.location.replace('/departments?view=map');
  }

  const deptForm = document.getElementById('org-department-create-form');
  $$('[data-create-department]').forEach(btn => btn.addEventListener('click', () => {
    if (!deptForm) return;
    deptForm.reset(); errorInto(deptForm, ''); deptForm.querySelector('.org-map-dialog-error').textContent = '';
    openDialog('org-department-dialog');
    setTimeout(() => deptForm.elements.name?.focus(), 30);
  }));
  deptForm?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await postForm(deptForm.action, new FormData(deptForm));
      reloadWith(`Подразделение «${deptForm.elements.name.value.trim()}» создано`, { focusNewDepartment: true });
    } catch (error) { errorInto(deptForm, error.message); }
  });

  const employeeForm = document.getElementById('org-employee-create-form');
  const employeeDeptSelect = document.getElementById('org-employee-department');
  function openCreateEmployee(departmentId) {
    if (!employeeForm) return;
    employeeForm.reset();
    employeeForm.querySelector('.org-map-dialog-error').textContent = '';
    if (employeeDeptSelect) {
      if (departmentId && [...employeeDeptSelect.options].some(o => Number(o.value) === Number(departmentId))) employeeDeptSelect.value = String(departmentId);
      employeeForm.action = `/departments/${employeeDeptSelect.value || 0}/members/create`;
    }
    openDialog('org-employee-dialog');
    setTimeout(() => employeeForm.elements.name?.focus(), 30);
  }
  employeeDeptSelect?.addEventListener('change', () => { employeeForm.action = `/departments/${employeeDeptSelect.value || 0}/members/create`; });
  document.addEventListener('click', event => {
    const btn = event.target.closest('[data-create-employee-for]');
    if (!btn) return;
    event.preventDefault(); event.stopPropagation();
    openCreateEmployee(Number(btn.dataset.createEmployeeFor || 0));
  });
  employeeForm?.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await postForm(employeeForm.action, new FormData(employeeForm));
      reloadWith(`Сотрудник «${employeeForm.elements.name.value.trim()}» добавлен на карту`, { focusNewEmployee: true, inspect: null });
    } catch (error) { errorInto(employeeForm, error.message); }
  });

  if (!canvas || !viewport) {
    if (toastEl?.dataset.initial) toast(toastEl.dataset.initial);
    return;
  }

  /* ---------------- data ---------------- */
  const svg = canvas.querySelector('.org-map-lines');
  let edges = [];
  let data = { departments: [], employees: [], relations: [], accents: [], layout_version: 0 };
  try { edges = JSON.parse(document.getElementById('org-map-edges')?.textContent || '[]'); } catch (_) { edges = []; }
  try { data = Object.assign(data, JSON.parse(document.getElementById('org-map-data')?.textContent || '{}')); } catch (_) {}
  const ACCENTS = data.accents && data.accents.length ? data.accents : ['#d98e8a', '#8fa8d8'];
  const FREE_ACCENT = '#a9a59a';
  const deptById = new Map(data.departments.map(d => [d.id, d]));
  const empById = new Map(data.employees.map(e => [e.id, e]));
  const nodeByKey = new Map($$('[data-map-node]', canvas).map(n => [n.dataset.nodeKey, n]));
  const accentOf = node => {
    const index = Number(node?.dataset.accent ?? -1);
    return index >= 0 ? ACCENTS[index % ACCENTS.length] : FREE_ACCENT;
  };
  nodeByKey.forEach(node => { if (node.dataset.nodeKind !== 'company') node.style.setProperty('--accent', accentOf(node)); });
  const deptAccent = id => { const d = deptById.get(Number(id)); return d ? ACCENTS[d.accent % ACCENTS.length] : FREE_ACCENT; };
  const initials = name => {
    const parts = String(name || '').replace(/-/g, ' ').split(/\s+/).filter(Boolean);
    if (!parts.length) return 'С';
    return parts.length === 1 ? parts[0].slice(0, 2).toUpperCase() : (parts[0][0] + parts[1][0]).toUpperCase();
  };
  const anchorDept = emp => {
    if (!emp || !emp.memberships.length) return null;
    return (emp.memberships.find(m => m.is_primary) || emp.memberships[0]).department_id;
  };

  // The director is marked with a crown; when they already sit in a department the implicit root line only adds noise.
  edges = edges.filter(e => !(e.kind === 'director' && !e.relation_id
    && (empById.get(Number(String(e.target).split(':')[1]))?.memberships || []).length));
  // Departments without a parent block hang from the company root, so the mind map always has a centre.
  const deptHasParent = new Set(edges.filter(e => e.kind === 'block' && String(e.target).startsWith('department:')).map(e => e.target));
  data.departments.forEach(d => {
    const key = `department:${d.id}`;
    if (!deptHasParent.has(key)) edges.unshift({ source: 'company:1', target: key, kind: 'root', label: 'Подразделение' });
  });

  /* ---------------- geometry ---------------- */
  const box = node => ({ x: parseFloat(node.style.left) || 0, y: parseFloat(node.style.top) || 0, w: node.offsetWidth, h: node.offsetHeight });
  const center = b => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 });
  const root = canvas.querySelector('[data-node-kind="company"]');

  function edgePath(a, b) {
    const ca = center(a), cb = center(b);
    const dx = cb.x - ca.x, dy = cb.y - ca.y;
    const horizontal = Math.abs(dx) > (a.w + b.w) / 2 * 0.72 || Math.abs(dx) > Math.abs(dy) * 1.2;
    let p1, p2, c1, c2;
    if (horizontal) {
      const s = Math.sign(dx) || 1;
      p1 = { x: s > 0 ? a.x + a.w : a.x, y: ca.y };
      p2 = { x: s > 0 ? b.x : b.x + b.w, y: cb.y };
      const k = Math.max(36, Math.abs(p2.x - p1.x) * 0.5);
      c1 = { x: p1.x + s * k, y: p1.y }; c2 = { x: p2.x - s * k, y: p2.y };
    } else {
      const s = Math.sign(dy) || 1;
      p1 = { x: ca.x, y: s > 0 ? a.y + a.h : a.y };
      p2 = { x: cb.x, y: s > 0 ? b.y : b.y + b.h };
      const k = Math.max(30, Math.abs(p2.y - p1.y) * 0.5);
      c1 = { x: p1.x, y: p1.y + s * k }; c2 = { x: p2.x, y: p2.y - s * k };
    }
    const mid = { x: (p1.x + 3 * c1.x + 3 * c2.x + p2.x) / 8, y: (p1.y + 3 * c1.y + 3 * c2.y + p2.y) / 8 };
    return { d: `M ${p1.x.toFixed(1)} ${p1.y.toFixed(1)} C ${c1.x.toFixed(1)} ${c1.y.toFixed(1)}, ${c2.x.toFixed(1)} ${c2.y.toFixed(1)}, ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`, p1, p2, mid };
  }
  const edgeColor = edge => {
    const s = String(edge.source), t = String(edge.target);
    if (t.startsWith('department:')) return s.startsWith('department:') ? '#7fb7a4' : deptAccent(t.split(':')[1]);
    if (s.startsWith('department:')) return deptAccent(s.split(':')[1]);
    if (edge.kind === 'manager') return deptAccent(t.split(':')[1]);
    const src = nodeByKey.get(s);
    return src && src.dataset.nodeKind === 'employee' ? accentOf(src) : '#c9c6bb';
  };

  let frame = 0, focusKey = null;
  function draw() {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(drawNow);
  }
  function drawNow() {
    const lit = focusKey ? new Set([focusKey]) : null;
    let markup = '';
    for (const edge of edges) {
      const source = nodeByKey.get(edge.source), target = nodeByKey.get(edge.target);
      if (!source || !target) continue;
      const kind = ['manager', 'director', 'block', 'reporting', 'root', 'company-member'].includes(edge.kind) ? edge.kind : 'membership';
      const geo = edgePath(box(source), box(target));
      const color = edgeColor(edge);
      const isLit = focusKey && (edge.source === focusKey || edge.target === focusKey);
      if (isLit) { lit.add(edge.source); lit.add(edge.target); }
      const litClass = isLit ? ' is-lit is-flow' : '';
      const title = esc(edge.label || 'Связь');
      let hit = '';
      if (edge.relation_id) hit = `<path class="org-map-edge-hit" data-edge-kind="org" data-edge-id="${edge.relation_id}" d="${geo.d}"><title>${title} — нажмите, чтобы удалить</title></path>`;
      else if (kind === 'membership' || kind === 'manager') {
        const emp = kind === 'manager' ? edge.source : edge.target, dep = kind === 'manager' ? edge.target : edge.source;
        hit = `<path class="org-map-edge-hit" data-edge-kind="membership" data-employee="${esc(emp.split(':')[1])}" data-department="${esc(dep.split(':')[1])}" d="${geo.d}"><title>${title} — нажмите, чтобы убрать из отдела</title></path>`;
      }
      if (kind === 'manager') markup += `<path class="org-map-edge-manager-glow${isLit ? ' is-lit' : ''}" style="--edge:${color}" d="${geo.d}"/>`;
      markup += hit + `<path class="org-map-edge org-map-edge-${kind}${litClass}" style="--edge:${color}" d="${geo.d}"><title>${title}</title></path>`;
      markup += `<circle class="org-map-edge-dot${isLit ? ' is-lit' : ''}" style="--edge:${color}" cx="${geo.p2.x.toFixed(1)}" cy="${geo.p2.y.toFixed(1)}" r="3"/>`;
      if (edge.relation_id && edge.label) markup += `<text class="org-map-edge-label${isLit ? ' is-lit' : ''}" x="${geo.mid.x.toFixed(1)}" y="${(geo.mid.y - 6).toFixed(1)}" text-anchor="middle">${esc(edge.label)}</text>`;
    }
    svg.innerHTML = markup;
    svg.classList.toggle('has-focus', !!focusKey);
    canvas.classList.toggle('has-focus', !!focusKey);
    nodeByKey.forEach((node, key) => node.classList.toggle('is-lit', !!(lit && lit.has(key))));
  }

  /* ---------------- view: zoom & pan ---------------- */
  const view = { x: 0, y: 0, k: 1 };
  const zoomLabel = document.getElementById('org-zoom-value');
  let viewSaveTimer = 0;
  function applyView() {
    canvas.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
    const g = 28 * view.k;
    viewport.style.backgroundSize = `${g}px ${g}px`;
    viewport.style.backgroundPosition = `${view.x}px ${view.y}px`;
    if (zoomLabel) zoomLabel.textContent = `${Math.round(view.k * 100)}%`;
    clearTimeout(viewSaveTimer);
    viewSaveTimer = setTimeout(() => { try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (_) {} }, 250);
  }
  const clampK = k => Math.max(0.2, Math.min(2, k));
  const vpRect = () => viewport.getBoundingClientRect();
  const toWorld = (clientX, clientY) => { const r = vpRect(); return { x: (clientX - r.left - view.x) / view.k, y: (clientY - r.top - view.y) / view.k }; };
  function zoomAt(k, clientX, clientY) {
    const r = vpRect();
    const cx = clientX ?? r.left + r.width / 2, cy = clientY ?? r.top + r.height / 2;
    const w = toWorld(cx, cy);
    view.k = clampK(k);
    view.x = cx - r.left - w.x * view.k; view.y = cy - r.top - w.y * view.k;
    applyView();
  }
  let viewAnim = 0;
  function animateView(target, ms = 480) {
    cancelAnimationFrame(viewAnim);
    const from = { ...view }, t0 = performance.now();
    const step = now => {
      const t = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - t, 3);
      view.x = from.x + (target.x - from.x) * e; view.y = from.y + (target.y - from.y) * e; view.k = from.k + (target.k - from.k) * e;
      applyView();
      if (t < 1) viewAnim = requestAnimationFrame(step);
    };
    viewAnim = requestAnimationFrame(step);
  }
  function contentBounds() {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodeByKey.forEach(node => { const b = box(node); minX = Math.min(minX, b.x); minY = Math.min(minY, b.y); maxX = Math.max(maxX, b.x + b.w); maxY = Math.max(maxY, b.y + b.h); });
    if (!isFinite(minX)) return { x: 0, y: 0, w: 800, h: 600 };
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }
  const inspectorWidth = () => (inspector && !inspector.hidden && window.innerWidth > 980 ? inspector.offsetWidth + 28 : 0);
  function fitView(animate = true) {
    const r = vpRect(), b = contentBounds();
    const padX = 70, padTop = 80, padBottom = 90, availW = Math.max(200, r.width - inspectorWidth());
    const k = clampK(Math.min((availW - padX * 2) / b.w, (r.height - padTop - padBottom) / b.h, 1.05));
    const target = { k, x: (availW - b.w * k) / 2 - b.x * k, y: padTop + (r.height - padTop - padBottom - b.h * k) / 2 - b.y * k };
    if (animate) animateView(target); else { Object.assign(view, target); applyView(); }
  }
  function centerOn(node, k = Math.max(view.k, 0.9)) {
    const r = vpRect(), b = box(node), c = center(b);
    const availW = r.width - inspectorWidth();
    animateView({ k: clampK(k), x: availW / 2 - c.x * clampK(k), y: r.height / 2 - c.y * clampK(k) });
  }

  viewport.addEventListener('wheel', event => {
    event.preventDefault();
    const mouseLike = event.deltaMode === 1 || (event.deltaX === 0 && Math.abs(event.deltaY) >= 50 && Number.isInteger(event.deltaY));
    if (event.ctrlKey || event.metaKey || mouseLike) {
      const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0016) * (event.deltaMode === 1 ? 16 : 1));
      cancelAnimationFrame(viewAnim);
      zoomAt(view.k * factor, event.clientX, event.clientY);
    } else {
      view.x -= event.deltaX; view.y -= event.deltaY; applyView();
    }
  }, { passive: false });

  $$('[data-zoom]').forEach(btn => btn.addEventListener('click', () => {
    const mode = btn.dataset.zoom;
    if (mode === 'fit') return fitView();
    const r = vpRect(), cx = r.left + (r.width - inspectorWidth()) / 2, cy = r.top + r.height / 2;
    const w = toWorld(cx, cy);
    const k = clampK(mode === 'in' ? view.k * 1.25 : mode === 'out' ? view.k / 1.25 : 1);
    animateView({ k, x: cx - r.left - w.x * k, y: cy - r.top - w.y * k }, 260);
  }));

  /* ---------------- hit testing ---------------- */
  function nodeAt(world, except) {
    let found = null;
    nodeByKey.forEach(node => {
      if (node === except || found) return;
      const b = box(node);
      if (world.x >= b.x && world.x <= b.x + b.w && world.y >= b.y && world.y <= b.y + b.h) found = node;
    });
    return found;
  }
  const kindOf = node => node?.dataset.nodeKind;
  function dropIntent(dragged, target) {
    if (!target || target === dragged) return null;
    const a = kindOf(dragged), b = kindOf(target);
    if (a === 'employee' && b === 'department' && target.hasAttribute('data-drop-department')) return 'assign';
    if ((a === 'employee' && (b === 'employee' || b === 'company'))
      || (a === 'department' && (b === 'department' || b === 'company' || b === 'employee'))
      || (a === 'company' && b === 'department')) return 'relation';
    return null;
  }

  /* ---------------- persistence ---------------- */
  function positionPayload(node) {
    const b = box(node);
    const payload = { node_type: kindOf(node), map_x: Math.max(0, Math.round(b.x)), map_y: Math.max(0, Math.round(b.y)) };
    if (payload.node_type === 'department') payload.department_id = Number(node.dataset.departmentId);
    else if (payload.node_type === 'employee') payload.employee_id = Number(node.dataset.employeeId);
    return payload;
  }
  const savePosition = node => fetchJson('/departments/map/position', positionPayload(node));
  // Saves many positions through the regular per-card endpoint, a few requests at a time.
  async function saveLayout(nodes) {
    const queue = [...nodes];
    const worker = async () => { while (queue.length) await savePosition(queue.shift()); };
    await Promise.all(Array.from({ length: Math.min(6, queue.length) }, worker));
  }

  /* ---------------- auto layout: balanced two-sided mind map ---------------- */
  function computeLayout() {
    const R = box(root);
    const DEPT_GAP_X = 150, PERSON_GAP_X = 96, ROW_GAP = 14, GROUP_GAP = 54;
    const groups = [];
    const placed = new Set();
    const deptNodes = $$('.org-map-dept-node', canvas);
    const people = $$('.org-map-person-node', canvas);
    const byAnchor = new Map();
    people.forEach(node => {
      const emp = empById.get(Number(node.dataset.employeeId));
      const anchor = anchorDept(emp);
      const key = anchor && nodeByKey.has(`department:${anchor}`) ? anchor : 0;
      if (!byAnchor.has(key)) byAnchor.set(key, []);
      byAnchor.get(key).push(node);
    });
    const order = node => {
      const emp = empById.get(Number(node.dataset.employeeId));
      const dep = anchorDept(emp);
      const m = emp?.memberships.find(x => x.department_id === dep);
      return (node.classList.contains('is-director') ? 0 : m?.is_primary ? 1 : 2);
    };
    deptNodes.forEach(dept => {
      const members = (byAnchor.get(Number(dept.dataset.departmentId)) || []).sort((a, b) => order(a) - order(b)
        || (a.querySelector('strong')?.textContent || '').localeCompare(b.querySelector('strong')?.textContent || '', 'ru'));
      members.forEach(m => placed.add(m));
      const ph = members.reduce((s, m) => s + m.offsetHeight + ROW_GAP, -ROW_GAP);
      groups.push({ dept, members, h: Math.max(dept.offsetHeight, ph) });
    });
    const free = people.filter(p => !placed.has(p));
    if (free.length) {
      const ph = free.reduce((s, m) => s + m.offsetHeight + ROW_GAP, -ROW_GAP);
      groups.push({ dept: null, members: free, h: ph });
    }
    const sides = [[], []], heights = [0, 0];
    groups.forEach(g => { const side = heights[0] <= heights[1] ? 0 : 1; sides[side].push(g); heights[side] += g.h + GROUP_GAP; });
    // Right side is drawn first in reading order; make it the heavier one for a calm, left-anchored look.
    if (heights[1] > heights[0]) { sides.reverse(); heights.reverse(); }
    const deptW = deptNodes[0]?.offsetWidth || 300, personW = people[0]?.offsetWidth || 268;
    const rootCx = 6000, rootCy = 6000;
    const result = new Map();
    result.set(root, { x: rootCx - R.w / 2, y: rootCy - R.h / 2 });
    sides.forEach((list, sideIndex) => {
      const dir = sideIndex === 0 ? 1 : -1;
      const total = list.reduce((s, g) => s + g.h + GROUP_GAP, -GROUP_GAP);
      let y = rootCy - total / 2;
      const deptX = dir > 0 ? rootCx + R.w / 2 + DEPT_GAP_X : rootCx - R.w / 2 - DEPT_GAP_X - deptW;
      const personX = dir > 0 ? deptX + deptW + PERSON_GAP_X : deptX - PERSON_GAP_X - personW;
      list.forEach(g => {
        const gc = y + g.h / 2;
        if (g.dept) result.set(g.dept, { x: deptX, y: gc - g.dept.offsetHeight / 2 });
        const ph = g.members.reduce((s, m) => s + m.offsetHeight + ROW_GAP, -ROW_GAP);
        let py = gc - ph / 2;
        const px = g.dept ? personX : (dir > 0 ? deptX : deptX + deptW - personW);
        g.members.forEach(m => { result.set(m, { x: px, y: py }); py += m.offsetHeight + ROW_GAP; });
        y += g.h + GROUP_GAP;
      });
    });
    let minX = Infinity, minY = Infinity;
    result.forEach(p => { minX = Math.min(minX, p.x); minY = Math.min(minY, p.y); });
    const shiftX = 160 - minX, shiftY = 140 - minY;
    result.forEach(p => { p.x = Math.round(p.x + shiftX); p.y = Math.round(p.y + shiftY); });
    return result;
  }
  function growCanvas() {
    const b = contentBounds();
    canvas.style.width = `${Math.max(parseFloat(canvas.style.width) || 0, b.x + b.w + 400)}px`;
    canvas.style.height = `${Math.max(parseFloat(canvas.style.height) || 0, b.y + b.h + 400)}px`;
  }
  function animateDraw(ms) {
    const t0 = performance.now();
    const loop = now => { drawNow(); if (now - t0 < ms) requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }
  async function autoLayout({ animate = true, save = true, silent = false } = {}) {
    const layout = computeLayout();
    if (animate) canvas.classList.add('is-layouting');
    layout.forEach((p, node) => { node.style.left = `${p.x}px`; node.style.top = `${p.y}px`; });
    growCanvas();
    if (animate) { animateDraw(760); setTimeout(() => canvas.classList.remove('is-layouting'), 760); } else drawNow();
    fitView(animate);
    if (!save) return;
    try {
      await saveLayout([...layout.keys()]);
      try { localStorage.setItem(ARRANGED_KEY, '1'); } catch (_) {}
      if (!silent) toast('Структура разложена и сохранена');
    } catch (error) { toast(error.message, true); }
  }
  $('[data-auto-layout]')?.addEventListener('click', () => autoLayout());

  function resolveOverlaps() {
    const moved = new Set();
    const items = [...nodeByKey.values()].map(node => ({ node, b: box(node) }));
    for (let pass = 0; pass < 60; pass++) {
      let changed = false;
      items.sort((p, q) => p.b.y - q.b.y || p.b.x - q.b.x);
      for (let i = 0; i < items.length; i++) {
        for (let j = i + 1; j < items.length; j++) {
          const a = items[i].b, c = items[j].b;
          if (!(a.x < c.x + c.w && a.x + a.w > c.x && a.y < c.y + c.h && a.y + a.h > c.y)) continue;
          const lower = items[j].node === root ? items[i] : items[j], upper = lower === items[j] ? items[i] : items[j];
          lower.b.y = Math.round(upper.b.y + upper.b.h + 14);
          lower.node.style.top = `${lower.b.y}px`;
          moved.add(lower.node); changed = true;
        }
      }
      if (!changed) break;
    }
    return moved;
  }

  // Cards added after the last arrangement get a tidy slot next to their department instead of a random spot.
  function placeNewcomers() {
    const fresh = [];
    const R = box(root), rootC = center(R);
    const people = $$('.org-map-person-node', canvas);
    people.forEach(node => {
      const emp = empById.get(Number(node.dataset.employeeId));
      if (!emp || emp.saved) return;
      const dep = anchorDept(emp);
      const deptNode = dep ? nodeByKey.get(`department:${dep}`) : null;
      const siblings = people.filter(p => p !== node && !fresh.includes(p) && empById.get(Number(p.dataset.employeeId))?.saved
        && anchorDept(empById.get(Number(p.dataset.employeeId))) === dep);
      let x, y;
      if (siblings.length) {
        const ref = siblings.reduce((a, b) => (box(b).y > box(a).y ? b : a));
        const rb = box(ref); x = rb.x; y = rb.y + rb.h + 14;
      } else if (deptNode) {
        const db = box(deptNode);
        x = center(db).x >= rootC.x ? db.x + db.w + 96 : db.x - 96 - node.offsetWidth; y = db.y;
      } else { x = R.x; y = R.y + R.h + 60 + fresh.length * 78; }
      node.style.left = `${Math.max(0, Math.round(x))}px`; node.style.top = `${Math.max(0, Math.round(y))}px`;
      fresh.push(node);
    });
    $$('.org-map-dept-node', canvas).forEach(node => {
      const dep = deptById.get(Number(node.dataset.departmentId));
      if (!dep || dep.saved) return;
      const others = $$('.org-map-dept-node', canvas).filter(n => n !== node && deptById.get(Number(n.dataset.departmentId))?.saved);
      let x = R.x + R.w + 150, y = R.y;
      if (others.length) {
        const right = others.filter(n => center(box(n)).x >= rootC.x), left = others.filter(n => center(box(n)).x < rootC.x);
        const side = right.length <= left.length || !left.length ? right : left;
        const list = side.length ? side : others;
        const low = list.reduce((a, b) => (box(b).y + box(b).h > box(a).y + box(a).h ? b : a));
        const lb = box(low);
        const bottom = Math.max(lb.y + lb.h, ...people.filter(p => Math.abs(box(p).x - (lb.x + lb.w + 96)) < 40).map(p => box(p).y + box(p).h));
        x = lb.x; y = bottom + 54;
      }
      node.style.left = `${Math.max(0, Math.round(x))}px`; node.style.top = `${Math.max(0, Math.round(y))}px`;
      fresh.push(node);
    });
    // Cards never sit on top of each other: whatever overlaps slides down its column (newcomers or legacy layouts).
    const moved = new Set(fresh);
    resolveOverlaps().forEach(node => moved.add(node));
    if (moved.size) { growCanvas(); saveLayout([...moved]).catch(() => {}); }
    return fresh;
  }

  /* ---------------- drag, pan, click ---------------- */
  let activeDrag = null, activePan = null, suppressClick = false, dropHover = null;
  const pointers = new Map();
  let pinch = null;
  const setDropHover = node => {
    if (dropHover === node) return;
    dropHover?.classList.remove('is-drop-target');
    dropHover = node; dropHover?.classList.add('is-drop-target');
  };

  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    if (event.target.closest('.org-map-edge-hit')) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2) {
      const [p1, p2] = [...pointers.values()];
      activeDrag = null; activePan = null;
      pinch = { d: Math.hypot(p2.x - p1.x, p2.y - p1.y), k: view.k };
      return;
    }
    const node = event.target.closest('[data-map-node]');
    if (!node) {
      activePan = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: view.x, y: view.y, moved: false };
      viewport.classList.add('is-panning');
      viewport.setPointerCapture(event.pointerId);
      cancelAnimationFrame(viewAnim);
      return;
    }
    if (event.target.closest('button, input, select, textarea')) return;
    const w = toWorld(event.clientX, event.clientY), b = box(node);
    activeDrag = { node, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, offX: w.x - b.x, offY: w.y - b.y, moved: false };
    node.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  viewport.addEventListener('pointermove', event => {
    if (pointers.has(event.pointerId)) pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pinch && pointers.size === 2) {
      const [p1, p2] = [...pointers.values()];
      zoomAt(pinch.k * Math.hypot(p2.x - p1.x, p2.y - p1.y) / Math.max(pinch.d, 1), (p1.x + p2.x) / 2, (p1.y + p2.y) / 2);
      return;
    }
    if (activePan && activePan.pointerId === event.pointerId) {
      const dx = event.clientX - activePan.startX, dy = event.clientY - activePan.startY;
      if (!activePan.moved && Math.hypot(dx, dy) < 3) return;
      activePan.moved = true;
      view.x = activePan.x + dx; view.y = activePan.y + dy; applyView();
      return;
    }
    if (!activeDrag || activeDrag.pointerId !== event.pointerId) return;
    const drag = activeDrag;
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4) return;
    drag.moved = true; event.preventDefault();
    drag.node.classList.add('is-map-dragging');
    const w = toWorld(event.clientX, event.clientY);
    drag.node.style.left = `${Math.max(0, Math.round(w.x - drag.offX))}px`;
    drag.node.style.top = `${Math.max(0, Math.round(w.y - drag.offY))}px`;
    const target = nodeAt(w, drag.node);
    setDropHover(dropIntent(drag.node, target) ? target : null);
    draw();
  });
  const finishPan = event => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!activePan || activePan.pointerId !== event.pointerId) return;
    const pan = activePan; activePan = null;
    viewport.classList.remove('is-panning');
    if (pan.moved) { suppressClick = true; setTimeout(() => { suppressClick = false; }, 300); }
    else if (!event.target.closest?.('[data-map-node]')) closeInspector();
  };
  const finishDrag = async event => {
    if (!activeDrag || activeDrag.pointerId !== event.pointerId) return;
    const drag = activeDrag; activeDrag = null;
    drag.node.classList.remove('is-map-dragging');
    const target = dropHover; setDropHover(null);
    if (!drag.moved) { openInspectorFor(drag.node); return; }
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 300);
    growCanvas(); draw();
    try { await savePosition(drag.node); } catch (error) { toast(error.message, true); return; }
    const intent = dropIntent(drag.node, target);
    if (intent === 'assign') openAssign(drag.node, target);
    else if (intent === 'relation') openRelation(target, drag.node);
  };
  viewport.addEventListener('pointerup', event => { finishPan(event); finishDrag(event); });
  viewport.addEventListener('pointercancel', event => { finishPan(event); finishDrag(event); });
  viewport.addEventListener('dblclick', event => {
    if (event.target.closest('[data-map-node]')) return;
    const r = vpRect(), w = toWorld(event.clientX, event.clientY), k = clampK(view.k * 1.5);
    animateView({ k, x: event.clientX - r.left - w.x * k, y: event.clientY - r.top - w.y * k }, 280);
  });
  canvas.addEventListener('click', event => {
    const edge = event.target.closest('.org-map-edge-hit');
    if (edge) { event.preventDefault(); event.stopPropagation(); onEdgeClick(edge); return; }
    const inspectDept = event.target.closest('[data-inspect-department]');
    if (inspectDept) { event.preventDefault(); event.stopPropagation(); openInspector('department', Number(inspectDept.dataset.inspectDepartment)); return; }
    if (event.target.closest('.org-map-person-node')) event.preventDefault();
    if (suppressClick) { event.preventDefault(); event.stopPropagation(); }
  }, true);
  svg.addEventListener('mouseover', event => { const h = event.target.closest('.org-map-edge-hit'); h?.nextElementSibling?.classList.add('is-hover'); });
  svg.addEventListener('mouseout', event => { const h = event.target.closest('.org-map-edge-hit'); h?.nextElementSibling?.classList.remove('is-hover'); });

  async function onEdgeClick(edge) {
    if (edge.dataset.edgeKind === 'org') {
      if (!await confirmAction('Удалить связь?', 'Линия исчезнет, карточки и назначения в отделах останутся.')) return;
      try { await fetchJson(`/departments/map/relation/${edge.dataset.edgeId}/delete`, {}); reloadWith('Связь удалена'); }
      catch (error) { toast(error.message, true); }
      return;
    }
    const emp = empById.get(Number(edge.dataset.employee)), dep = deptById.get(Number(edge.dataset.department));
    if (!emp || !dep) return;
    if (!await confirmAction('Убрать из подразделения?', `${emp.name} будет исключён(а) из «${dep.name}». Карточка сотрудника и другие назначения сохранятся.`, 'Убрать')) return;
    try { await removeMembership(emp.id, dep.id); reloadWith(`${emp.name} — убран(а) из «${dep.name}»`); }
    catch (error) { toast(error.message, true); }
  }

  /* ---------------- assign / relation dialogs ---------------- */
  const assignForm = document.getElementById('org-assign-form');
  const nodeTitle = node => {
    if (!node) return 'блок';
    if (kindOf(node) === 'company') return root.querySelector('strong')?.textContent || 'Компания';
    if (kindOf(node) === 'department') return node.querySelector(':scope > strong')?.textContent || 'подразделение';
    return node.querySelector('.org-map-person-copy strong')?.textContent || 'сотрудник';
  };
  function openAssign(employeeNode, departmentNode) {
    assignForm.reset(); assignForm.querySelector('.org-map-dialog-error').textContent = '';
    assignForm.elements.employee_id.value = employeeNode.dataset.employeeId;
    assignForm.elements.department_id.value = departmentNode.dataset.departmentId;
    const emp = empById.get(Number(employeeNode.dataset.employeeId));
    const existing = emp?.memberships.find(m => m.department_id === Number(departmentNode.dataset.departmentId));
    if (existing) { assignForm.elements.role.value = existing.role; assignForm.elements.responsibilities.value = existing.responsibilities; assignForm.elements.is_primary.checked = existing.is_primary; }
    $('#org-assign-summary').textContent = `${nodeTitle(employeeNode)} → ${nodeTitle(departmentNode)}. Другие назначения останутся без изменений.`;
    openDialog('org-assign-dialog');
  }
  assignForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const f = assignForm.elements;
    try {
      await fetchJson('/departments/map/assign', {
        employee_id: Number(f.employee_id.value), department_id: Number(f.department_id.value),
        role: f.role.value, responsibilities: f.responsibilities.value, is_primary: f.is_primary.checked,
      });
      const emp = empById.get(Number(f.employee_id.value)), dep = deptById.get(Number(f.department_id.value));
      reloadWith(`${emp?.name || 'Сотрудник'} добавлен(а) в «${dep?.name || 'подразделение'}»`);
    } catch (error) { errorInto(assignForm, error.message); }
  });
  const relationForm = document.getElementById('org-relation-form');
  const nodeIdentity = node => kindOf(node) === 'company' ? { type: 'company', id: 1 }
    : kindOf(node) === 'department' ? { type: 'department', id: Number(node.dataset.departmentId) } : { type: 'employee', id: Number(node.dataset.employeeId) };
  function openRelation(sourceNode, targetNode) {
    relationForm.reset(); relationForm.querySelector('.org-map-dialog-error').textContent = '';
    const s = nodeIdentity(sourceNode), t = nodeIdentity(targetNode);
    relationForm.elements.source_type.value = s.type; relationForm.elements.source_id.value = s.id;
    relationForm.elements.target_type.value = t.type; relationForm.elements.target_id.value = t.id;
    $('#org-relation-summary').textContent = `${nodeTitle(sourceNode)} → ${nodeTitle(targetNode)}`;
    relationForm.elements.relation_type.value = t.type === 'department' && s.type !== 'employee' ? 'linked_to' : 'reports_to';
    openDialog('org-relation-dialog');
  }
  relationForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const f = relationForm.elements;
    try {
      await fetchJson('/departments/map/relation', {
        source_type: f.source_type.value, source_id: Number(f.source_id.value), target_type: f.target_type.value,
        target_id: Number(f.target_id.value), relation_type: f.relation_type.value, label: f.label.value,
      });
      reloadWith('Связь сохранена');
    } catch (error) { errorInto(relationForm, error.message); }
  });

  /* ---------------- inspector ---------------- */
  const inspector = document.getElementById('org-inspector');
  function selectNode(key) {
    nodeByKey.forEach((node, k) => node.classList.toggle('is-selected', k === key));
    focusKey = key; drawNow();
  }
  function closeInspector() {
    if (!inspector || inspector.hidden) { if (focusKey) selectNode(null); return; }
    inspector.hidden = true; inspector.innerHTML = ''; currentInspect = null; selectNode(null);
  }
  function openInspectorFor(node) {
    const kind = kindOf(node);
    if (kind === 'employee') openInspector('employee', Number(node.dataset.employeeId));
    else if (kind === 'department') openInspector('department', Number(node.dataset.departmentId));
    else openInspector('company', 1);
  }
  let views = null;
  const membersOf = depId => data.employees.filter(e => e.memberships.some(m => m.department_id === depId));

  function openInspector(kind, id, { center: doCenter = false } = {}) {
    if (!inspector) return;
    const key = kind === 'company' ? 'company:1' : `${kind}:${id}`;
    const node = nodeByKey.get(key);
    if (!node) return;
    currentInspect = { kind, id };
    inspector.style.setProperty('--accent', kind === 'company' ? '#d98e8a' : accentOf(node));
    views = views || window.SysaiOrgMapViews({ esc, initials, deptById, deptAccent, accentOf, nodeByKey, nodeTitle, membersOf, data, root, ICON_X, ICON_CROWN });
    inspector.innerHTML = kind === 'employee' ? views.employeeHtml(empById.get(id), node) : kind === 'department' ? views.departmentHtml(deptById.get(id)) : views.companyHtml();
    inspector.hidden = false;
    selectNode(key);
    if (kind === 'employee') bindEmployee(empById.get(id));
    else if (kind === 'department') bindDepartment(deptById.get(id));
    else bindCompany();
    $('.org-insp-close', inspector)?.addEventListener('click', closeInspector);
    const r = vpRect(), b = box(node);
    const sx = view.x + b.x * view.k, sw = b.w * view.k;
    if (doCenter || sx + sw > r.width - inspectorWidth() - 20 || sx < 0) centerOn(node, Math.max(view.k, 0.75));
  }

  function bindEmployee(emp) {
    if (!emp) return;
    $$('[data-open-dept]', inspector).forEach(b => b.addEventListener('click', () => openInspector('department', Number(b.dataset.openDept), { center: true })));
    $$('[data-membership]', inspector).forEach(row => {
      const depId = Number(row.dataset.membership);
      const m = emp.memberships.find(x => x.department_id === depId);
      const input = $('[data-role]', row);
      const save = async (patch, message) => {
        try {
          await fetchJson('/departments/map/assign', { employee_id: emp.id, department_id: depId, role: m.role, responsibilities: m.responsibilities, is_primary: m.is_primary, ...patch });
          return message;
        } catch (error) { toast(error.message, true); return null; }
      };
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
      input.addEventListener('change', async () => {
        const msg = await save({ role: input.value.trim() }, 'Роль сохранена');
        if (msg) { m.role = input.value.trim(); toast(msg); }
      });
      $('[data-toggle-head]', row).addEventListener('click', async () => {
        const msg = await save({ is_primary: !m.is_primary, role: input.value.trim() }, m.is_primary ? 'Руководство снято' : `${emp.name} — руководитель «${deptById.get(depId)?.name}»`);
        if (msg) reloadWith(msg);
      });
      $('[data-remove]', row).addEventListener('click', async () => {
        const dep = deptById.get(depId);
        if (!await confirmAction('Убрать из подразделения?', `${emp.name} будет исключён(а) из «${dep?.name}». Карточка сотрудника сохранится.`, 'Убрать')) return;
        try { await removeMembership(emp.id, depId); reloadWith(`${emp.name} — убран(а) из «${dep?.name}»`); }
        catch (error) { toast(error.message, true); }
      });
    });
    $('[data-add-membership]', inspector)?.addEventListener('submit', async event => {
      event.preventDefault();
      const f = event.currentTarget.elements;
      try {
        await fetchJson('/departments/map/assign', { employee_id: emp.id, department_id: Number(f.department_id.value), role: f.role.value, responsibilities: '', is_primary: false });
        reloadWith(`${emp.name} добавлен(а) в «${deptById.get(Number(f.department_id.value))?.name}»`);
      } catch (error) { toast(error.message, true); }
    });
    $('[data-profile]', inspector)?.addEventListener('submit', async event => {
      event.preventDefault();
      const f = event.currentTarget.elements;
      const node = nodeByKey.get(`employee:${emp.id}`);
      // The regular employee card endpoint rewrites memberships, so send them back unchanged and re-mark heads.
      const fd = new FormData();
      fd.set('id', emp.id); fd.set('name', f.name.value); fd.set('aliases', f.aliases.value); fd.set('position', f.position.value);
      fd.set('department', emp.legacy_department || ''); fd.set('competencies', f.competencies.value);
      fd.set('responsibilities', f.responsibilities.value); fd.set('context', f.context.value);
      fd.set('channel', emp.channel || 'telegram'); fd.set('telegram_chat_id', emp.telegram || ''); fd.set('bitrix_user_id', emp.bitrix || '');
      fd.set('active', 'on');
      emp.memberships.forEach(m => {
        fd.append('department_ids', m.department_id);
        fd.set(`department_role_${m.department_id}`, m.role || '');
        fd.set(`department_responsibilities_${m.department_id}`, m.responsibilities || '');
      });
      try {
        if (node) await savePosition(node);
        await postForm('/employees', fd);
        for (const m of emp.memberships.filter(x => x.is_primary)) {
          await fetchJson('/departments/map/assign', { employee_id: emp.id, department_id: m.department_id, role: m.role, responsibilities: m.responsibilities, is_primary: true });
        }
        reloadWith('Профиль сохранён');
      } catch (error) { toast(error.message, true); }
    });
    $$('[data-del-rel]', inspector).forEach(b => b.addEventListener('click', async () => {
      if (!await confirmAction('Удалить связь?', 'Линия исчезнет, карточки останутся.')) return;
      try { await fetchJson(`/departments/map/relation/${b.dataset.delRel}/delete`, {}); reloadWith('Связь удалена'); }
      catch (error) { toast(error.message, true); }
    }));
  }

  function bindDepartment(dep) {
    if (!dep) return;
    $$('[data-open-emp]', inspector).forEach(b => b.addEventListener('click', () => openInspector('employee', Number(b.dataset.openEmp), { center: true })));
    $$('[data-member]', inspector).forEach(row => {
      const emp = empById.get(Number(row.dataset.member));
      const m = emp.memberships.find(x => x.department_id === dep.id);
      const input = $('[data-role]', row);
      const save = patch => fetchJson('/departments/map/assign', { employee_id: emp.id, department_id: dep.id, role: m.role, responsibilities: m.responsibilities, is_primary: m.is_primary, ...patch });
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
      input.addEventListener('change', async () => {
        try { await save({ role: input.value.trim() }); m.role = input.value.trim(); toast('Роль сохранена'); } catch (error) { toast(error.message, true); }
      });
      $('[data-toggle-head]', row).addEventListener('click', async () => {
        try { await save({ is_primary: !m.is_primary, role: input.value.trim() }); reloadWith(m.is_primary ? 'Руководство снято' : `${emp.name} — руководитель «${dep.name}»`); }
        catch (error) { toast(error.message, true); }
      });
      $('[data-remove]', row).addEventListener('click', async () => {
        if (!await confirmAction('Убрать из подразделения?', `${emp.name} будет исключён(а) из «${dep.name}». Карточка сотрудника сохранится.`, 'Убрать')) return;
        try { await removeMembership(emp.id, dep.id); reloadWith(`${emp.name} — убран(а) из «${dep.name}»`); }
        catch (error) { toast(error.message, true); }
      });
    });
    const search = $('[data-pick-search]', inspector), pick = $('[data-pick]', inspector);
    if (search && pick) {
      const all = data.employees.filter(e => !e.memberships.some(m => m.department_id === dep.id));
      const render = q => {
        const needle = q.trim().toLowerCase();
        const list = all.filter(e => !needle || `${e.name} ${e.position} ${e.aliases}`.toLowerCase().includes(needle)).slice(0, 60);
        pick.innerHTML = list.map(e => `<button type="button" data-pick-emp="${e.id}" style="--accent:${accentOf(nodeByKey.get('employee:' + e.id))}"><span class="ava">${esc(initials(e.name))}</span>${esc(e.name)}<small>${esc(e.position)}</small></button>`).join('') || '<div class="org-insp-empty">Никого не нашлось — создайте нового сотрудника.</div>';
      };
      search.addEventListener('input', () => render(search.value));
      pick.addEventListener('click', async event => {
        const b = event.target.closest('[data-pick-emp]'); if (!b) return;
        const emp = empById.get(Number(b.dataset.pickEmp));
        try { await fetchJson('/departments/map/assign', { employee_id: emp.id, department_id: dep.id, role: '', responsibilities: '', is_primary: false }); reloadWith(`${emp.name} добавлен(а) в «${dep.name}»`); }
        catch (error) { toast(error.message, true); }
      });
    }
    const form = $('[data-dept-form]', inspector);
    form?.addEventListener('submit', async event => {
      event.preventDefault();
      const fd = new FormData();
      fd.set('id', dep.id); fd.set('name', form.elements.name.value); fd.set('description', form.elements.description.value);
      fd.set('regulations', form.elements.regulations.value); fd.set('return_view', 'map');
      if (form.elements.active.checked) fd.set('active', 'on');
      try { await postForm('/departments', fd); reloadWith('Подразделение сохранено'); } catch (error) { toast(error.message, true); }
    });
    $('[data-dept-delete]', inspector)?.addEventListener('click', async () => {
      if (!await confirmAction(`Удалить «${dep.name}»?`, 'Подразделение исчезнет с карты. Сотрудники останутся в общем справочнике и в других отделах.')) return;
      const fd = new FormData(); fd.set('view', 'map');
      try { await postForm(`/departments/${dep.id}/delete`, fd); currentInspect = null; reloadWith(`Подразделение «${dep.name}» удалено`); }
      catch (error) { toast(error.message, true); }
    });
  }

  function bindCompany() {
    $$('[data-open-dept]', inspector).forEach(b => b.addEventListener('click', () => openInspector('department', Number(b.dataset.openDept), { center: true })));
    $$('[data-open-emp]', inspector).forEach(b => b.addEventListener('click', () => openInspector('employee', Number(b.dataset.openEmp), { center: true })));
    $('[data-create-department-here]', inspector)?.addEventListener('click', () => $('[data-create-department]')?.click());
  }

  /* ---------------- search ---------------- */
  const searchInput = document.getElementById('org-map-search');
  const searchResults = document.getElementById('org-map-search-results');
  let searchIndex = -1;
  function searchItems(q) {
    const needle = q.trim().toLowerCase();
    if (!needle) return [];
    const out = [];
    data.departments.forEach(d => { if (d.name.toLowerCase().includes(needle)) out.push({ kind: 'department', id: d.id, title: d.name, sub: `${membersOf(d.id).length} в составе`, accent: deptAccent(d.id), ini: initials(d.name) }); });
    data.employees.forEach(e => {
      if (`${e.name} ${e.position} ${e.aliases}`.toLowerCase().includes(needle)) {
        out.push({ kind: 'employee', id: e.id, title: e.name, sub: e.position || e.memberships.map(m => deptById.get(m.department_id)?.name).join(', '), accent: accentOf(nodeByKey.get(`employee:${e.id}`)), ini: initials(e.name) });
      }
    });
    return out.slice(0, 12);
  }
  function renderSearch() {
    const items = searchItems(searchInput.value);
    if (!searchInput.value.trim()) { searchResults.hidden = true; return; }
    searchResults.hidden = false;
    searchIndex = Math.min(searchIndex, items.length - 1);
    searchResults.innerHTML = items.map((it, i) => `<button type="button" class="${i === searchIndex ? 'is-active' : ''}" data-kind="${it.kind}" data-id="${it.id}" style="--accent:${it.accent}"><i class="${it.kind === 'department' ? 'is-dept' : ''}">${esc(it.ini)}</i><span>${esc(it.title)}<small>${esc(it.sub)}</small></span></button>`).join('') || '<div class="empty">Ничего не найдено</div>';
  }
  function chooseSearch(btn) {
    if (!btn) return;
    const kind = btn.dataset.kind, id = Number(btn.dataset.id);
    searchResults.hidden = true; searchInput.value = ''; searchInput.blur();
    const node = nodeByKey.get(`${kind}:${id}`);
    openInspector(kind, id, { center: true });
    node?.classList.remove('is-pulse'); void node?.offsetWidth; node?.classList.add('is-pulse');
  }
  searchInput?.addEventListener('input', () => { searchIndex = 0; renderSearch(); });
  searchInput?.addEventListener('keydown', event => {
    const buttons = $$('button', searchResults);
    if (event.key === 'ArrowDown') { event.preventDefault(); searchIndex = Math.min(buttons.length - 1, searchIndex + 1); renderSearch(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); searchIndex = Math.max(0, searchIndex - 1); renderSearch(); }
    else if (event.key === 'Enter') { event.preventDefault(); chooseSearch(buttons[Math.max(0, searchIndex)]); }
    else if (event.key === 'Escape') { searchInput.value = ''; renderSearch(); searchInput.blur(); }
  });
  searchResults?.addEventListener('mousedown', event => { event.preventDefault(); chooseSearch(event.target.closest('button')); });
  searchInput?.addEventListener('blur', () => setTimeout(() => { searchResults.hidden = true; }, 120));

  document.addEventListener('keydown', event => {
    if (event.target.closest('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]')) return;
    if (event.key === '/') { event.preventDefault(); searchInput?.focus(); }
    else if (event.key === 'Escape') closeInspector();
    else if (event.key === 'f' || event.key === 'F' || event.key === 'а' || event.key === 'А') fitView();
    else if (event.key === '+' || event.key === '=') $('[data-zoom="in"]')?.click();
    else if (event.key === '-' || event.key === '_') $('[data-zoom="out"]')?.click();
    else if (event.key === '0') $('[data-zoom="reset"]')?.click();
  });

  /* ---------------- boot ---------------- */
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(() => draw());
    nodeByKey.forEach(node => observer.observe(node));
  }
  window.addEventListener('resize', draw, { passive: true });

  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(STATE_KEY) || 'null'); sessionStorage.removeItem(STATE_KEY); } catch (_) {}
  const nothingSaved = data.departments.every(d => !d.saved) && data.employees.every(e => !e.saved);
  let fresh = [];
  if (nothingSaved) {
    autoLayout({ animate: false, save: true, silent: true });
  } else {
    fresh = placeNewcomers();
    let restored = false;
    try {
      const v = JSON.parse(localStorage.getItem(VIEW_KEY) || 'null');
      if (v && isFinite(v.x) && isFinite(v.y) && isFinite(v.k)) { Object.assign(view, { x: v.x, y: v.y, k: clampK(v.k) }); applyView(); restored = true; }
    } catch (_) {}
    if (!restored) fitView(false);
    drawNow();
  }
  let arrangedHint = false;
  try { arrangedHint = !nothingSaved && !localStorage.getItem(ARRANGED_KEY) && !saved; } catch (_) {}
  if (saved?.toast) toast(saved.toast);
  else if (toastEl?.dataset.initial) toast(toastEl.dataset.initial);
  else if (arrangedHint) {
    toast('Новая карта структуры: нажмите «Упорядочить», чтобы аккуратно разложить отделы и людей');
    try { localStorage.setItem(ARRANGED_KEY, 'hinted'); } catch (_) {}
  }
  const newest = kind => {
    const list = kind === 'employee' ? data.employees : data.departments;
    return list.length ? list.reduce((a, b) => (b.id > a.id ? b : a)) : null;
  };
  if (saved?.focusNewEmployee) {
    const emp = newest('employee');
    if (emp) setTimeout(() => { openInspector('employee', emp.id, { center: true }); nodeByKey.get(`employee:${emp.id}`)?.classList.add('is-pulse'); }, 80);
  } else if (saved?.focusNewDepartment) {
    const dep = newest('department');
    if (dep) setTimeout(() => { openInspector('department', dep.id, { center: true }); nodeByKey.get(`department:${dep.id}`)?.classList.add('is-pulse'); }, 80);
  } else if (saved?.inspect && nodeByKey.get(saved.inspect.kind === 'company' ? 'company:1' : `${saved.inspect.kind}:${saved.inspect.id}`)) {
    openInspector(saved.inspect.kind, saved.inspect.id);
  } else if (fresh.length) {
    fresh.forEach(node => node.classList.add('is-pulse'));
  }
})();
