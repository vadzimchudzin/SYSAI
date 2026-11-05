/* SYSAI · organisation chart: a top-down tree that lays itself out (company → director → departments →
   people), zoom & pan, collapsible branches, drag a card onto another to assign or link, and an inspector
   panel to edit people and department composition without leaving the map. */
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
  const VIEW_KEY = 'sysai-org-map-view-v3';
  const COLLAPSE_KEY = 'sysai-org-map-collapsed-v1';

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
  let data = { departments: [], employees: [], relations: [], accents: [] };
  let tree = { nodes: [], cross: [] };
  try { data = Object.assign(data, JSON.parse(document.getElementById('org-map-data')?.textContent || '{}')); } catch (_) {}
  try { tree = Object.assign(tree, JSON.parse(document.getElementById('org-map-tree')?.textContent || '{}')); } catch (_) {}
  const ACCENTS = data.accents && data.accents.length ? data.accents : ['#d98e8a', '#8fa8d8'];
  const FREE_ACCENT = '#a9a59a', GOLD = '#e2c27a', SAND = '#d9b98a', TEAL = '#7fb7a4', NEUTRAL = '#8c877b';
  const deptById = new Map(data.departments.map(d => [d.id, d]));
  const empById = new Map(data.employees.map(e => [e.id, e]));
  const nodeByKey = new Map($$('[data-map-node]', canvas).map(n => [n.dataset.nodeKey, n]));
  const root = canvas.querySelector('[data-node-kind="company"]');
  const ROOT_KEY = 'company:1';
  const idOf = key => Number(String(key).split(':')[1]);
  const kindOfKey = key => String(key).split(':')[0];
  const deptAccent = id => { const d = deptById.get(Number(id)); return d ? ACCENTS[d.accent % ACCENTS.length] : FREE_ACCENT; };
  const initials = name => {
    const parts = String(name || '').replace(/-/g, ' ').split(/\s+/).filter(Boolean);
    if (!parts.length) return 'С';
    return parts.length === 1 ? parts[0].slice(0, 2).toUpperCase() : (parts[0][0] + parts[1][0]).toUpperCase();
  };

  /* ---------------- tree model (built on the server, see app/org_tree.py) ---------------- */
  const parentOf = new Map(), linkOf = new Map(), childrenOf = new Map();
  tree.nodes.forEach(n => {
    if (!nodeByKey.has(n.key)) return;
    const up = n.parent && nodeByKey.has(n.parent) ? n.parent : (n.key === ROOT_KEY ? null : ROOT_KEY);
    parentOf.set(n.key, up);
    linkOf.set(n.key, n);
    if (up) { if (!childrenOf.has(up)) childrenOf.set(up, []); childrenOf.get(up).push(n.key); }
  });
  // Any card the tree did not mention still gets a place instead of piling up in the corner.
  nodeByKey.forEach((_, key) => {
    if (parentOf.has(key) || key === ROOT_KEY) return;
    parentOf.set(key, ROOT_KEY); linkOf.set(key, { key, parent: ROOT_KEY, kind: 'company', label: '' });
    if (!childrenOf.has(ROOT_KEY)) childrenOf.set(ROOT_KEY, []);
    childrenOf.get(ROOT_KEY).push(key);
  });
  const kidsOf = key => childrenOf.get(key) || [];
  const descendants = key => kidsOf(key).reduce((n, k) => n + 1 + descendants(k), 0);
  const ancestors = key => { const out = []; let p = parentOf.get(key); while (p) { out.push(p); p = parentOf.get(p); } return out; };

  // Cards take the colour of the department they sit in; the director is gold, the rest neutral.
  const accentByKey = new Map();
  const accentFor = key => {
    if (accentByKey.has(key)) return accentByKey.get(key);
    let value = FREE_ACCENT;
    if (kindOfKey(key) === 'department') value = deptAccent(idOf(key));
    else if (nodeByKey.get(key)?.classList.contains('is-director')) value = GOLD;
    else { const up = parentOf.get(key); if (up && up !== ROOT_KEY && kindOfKey(up) !== 'group') value = accentFor(up); }
    accentByKey.set(key, value);
    return value;
  };
  const accentOf = node => (node ? accentFor(node.dataset.nodeKey) : FREE_ACCENT);
  nodeByKey.forEach((node, key) => { if (key !== ROOT_KEY) node.style.setProperty('--accent', accentFor(key)); });
  const lineColor = parent => {
    if (!parent || parent === ROOT_KEY || kindOfKey(parent) === 'group') return NEUTRAL;
    if (kindOfKey(parent) === 'department') return deptAccent(idOf(parent));
    return nodeByKey.get(parent)?.classList.contains('is-director') ? GOLD : SAND;
  };

  /* ---------------- collapsed branches ---------------- */
  let collapsed = new Set();
  try { (JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]') || []).forEach(k => { if (kidsOf(k).length && k !== ROOT_KEY) collapsed.add(k); }); } catch (_) {}
  const saveCollapsed = () => { try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsed])); } catch (_) {} };
  const visibleKids = key => (collapsed.has(key) ? [] : kidsOf(key));
  const isVisible = key => !ancestors(key).some(a => collapsed.has(a));

  const toggles = new Map();
  childrenOf.forEach((kids, key) => {
    if (!kids.length || key === ROOT_KEY) return;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'org-map-toggle'; button.dataset.toggle = key;
    canvas.appendChild(button);
    toggles.set(key, button);
  });
  const collapseAllBtn = $('[data-collapse-all]');
  function syncToggles() {
    toggles.forEach((button, key) => {
      const shut = collapsed.has(key), count = descendants(key);
      button.classList.toggle('is-collapsed', shut);
      button.innerHTML = shut ? `<span>+${count}</span>` : '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 6h6"/></svg>';
      button.title = shut ? `Развернуть ветку (${count})` : 'Свернуть ветку';
      button.setAttribute('aria-label', button.title);
      button.setAttribute('aria-expanded', shut ? 'false' : 'true');
    });
    const label = collapseAllBtn?.querySelector('[data-collapse-label]');
    if (label) label.textContent = collapsed.size ? 'Развернуть всё' : 'Свернуть всё';
  }
  function setCollapsed(key, shut) {
    if (!toggles.has(key)) return;
    if (shut) collapsed.add(key); else collapsed.delete(key);
    saveCollapsed(); syncToggles();
    autoLayout({ animate: true, keep: key });
  }
  collapseAllBtn?.addEventListener('click', () => {
    if (collapsed.size) collapsed.clear();
    else toggles.forEach((_, key) => {
      // Keep the top of the chart open: company, director and the chain above the departments.
      if (nodeByKey.get(key)?.classList.contains('is-director')) return;
      collapsed.add(key);
    });
    saveCollapsed(); syncToggles();
    autoLayout({ animate: true, fit: true });
  });
  function reveal(key) {
    let changed = false;
    ancestors(key).forEach(a => { if (collapsed.delete(a)) changed = true; });
    if (changed) { saveCollapsed(); syncToggles(); autoLayout({ animate: false }); }
  }

  /* ---------------- layout: tidy top-down tree with stacked lists of people ---------------- */
  const L = { V_GAP: 66, H_GAP: 34, LIST_TOP: 24, SPINE: 20, INDENT: 40, ROW_GAP: 10, COL_GAP: 26, MAX_ROWS: 8, MARGIN: 140 };
  const sizes = new Map();
  const sizeOf = key => {
    if (!sizes.has(key)) { const n = nodeByKey.get(key); sizes.set(key, { w: n.offsetWidth || 240, h: n.offsetHeight || 60 }); }
    return sizes.get(key);
  };
  const isLeaf = key => kindOfKey(key) === 'employee' && !visibleKids(key).length;

  function measureList(leaves) {
    const cols = leaves.length <= L.MAX_ROWS ? 1 : Math.ceil(leaves.length / L.MAX_ROWS);
    const rows = Math.ceil(leaves.length / cols);
    const columns = [];
    let x = 0, h = 0;
    for (let c = 0; c < cols; c++) {
      const items = leaves.slice(c * rows, (c + 1) * rows);
      if (!items.length) break;
      const cw = Math.max(...items.map(k => sizeOf(k).w));
      let y = 0;
      const placed = items.map(k => { const s = sizeOf(k); const it = { key: k, dx: x + L.INDENT, dy: y, h: s.h }; y += s.h + L.ROW_GAP; return it; });
      columns.push({ spine: x + L.SPINE, items: placed });
      h = Math.max(h, y - L.ROW_GAP);
      x += L.INDENT + cw + L.COL_GAP;
    }
    return { columns, w: x - L.COL_GAP, h };
  }
  function measure(key, memo) {
    const s = sizeOf(key), kids = visibleKids(key);
    let info;
    if (!kids.length) info = { mode: 'leaf', nw: s.w, nh: s.h, nodeDx: 0, w: s.w, h: s.h };
    else if (kids.every(isLeaf)) {
      const list = measureList(kids);
      info = { mode: 'list', nw: s.w, nh: s.h, nodeDx: 0, list, w: Math.max(s.w, list.w), h: s.h + L.LIST_TOP + list.h };
    } else {
      const blocks = [];
      const leaves = kids.filter(isLeaf);
      let listAdded = false;
      kids.forEach(k => {
        if (isLeaf(k)) { if (!listAdded) { blocks.push({ type: 'list', list: measureList(leaves) }); listAdded = true; } }
        else blocks.push({ type: 'node', key: k, info: measure(k, memo) });
      });
      let x = 0;
      blocks.forEach(b => {
        b.w = b.type === 'list' ? b.list.w : b.info.w;
        b.h = b.type === 'list' ? b.list.h : b.info.h;
        b.dx = x;
        b.anchors = b.type === 'list' ? b.list.columns.map(c => x + c.spine) : [x + b.info.nodeDx + b.info.nw / 2];
        b.group = b.type === 'node' && kindOfKey(b.key) === 'group';
        x += b.w + L.H_GAP;
      });
      // The parent sits above the middle of its children; the «no department» pile does not pull it aside.
      const core = blocks.filter(b => !b.group).length ? blocks.filter(b => !b.group) : blocks;
      const first = core[0].anchors[0], last = core[core.length - 1].anchors[core[core.length - 1].anchors.length - 1];
      let nodeDx = (first + last) / 2 - s.w / 2;
      const minX = Math.min(0, nodeDx), maxX = Math.max(x - L.H_GAP, nodeDx + s.w);
      nodeDx -= minX;
      blocks.forEach(b => { b.dx -= minX; b.anchors = b.anchors.map(a => a - minX); });
      info = { mode: 'row', nw: s.w, nh: s.h, nodeDx, blocks, rowDy: s.h + L.V_GAP, w: maxX - minX, h: s.h + L.V_GAP + Math.max(...blocks.map(b => b.h)) };
    }
    memo.set(key, info);
    return info;
  }
  function placeList(list, bx, by, from, parentKey, out) {
    const sx0 = bx + list.columns[0].spine;
    list.columns.forEach((col, c) => {
      const sx = bx + col.spine;
      col.items.forEach(it => {
        const lx = bx + it.dx, ly = by + it.dy, cy = ly + it.h / 2;
        out.pos.set(it.key, { x: lx, y: ly });
        const pts = from.mode === 'list'
          ? (c === 0 ? [[sx0, from.bottom], [sx0, cy], [lx, cy]] : [[sx0, from.bottom], [sx0, from.bus], [sx, from.bus], [sx, cy], [lx, cy]])
          : [[from.px, from.bottom], [from.px, from.bus], [sx, from.bus], [sx, cy], [lx, cy]];
        out.links.push({ parent: parentKey, child: it.key, pts, hit: [[sx, cy], [lx, cy]] });
      });
    });
  }
  function place(key, x0, y0, memo, out) {
    const info = memo.get(key);
    const nx = x0 + info.nodeDx, ny = y0, bottom = ny + info.nh;
    out.pos.set(key, { x: nx, y: ny });
    if (info.mode === 'list') {
      placeList(info.list, nx, bottom + L.LIST_TOP, { mode: 'list', bottom, bus: bottom + L.LIST_TOP / 2 }, key, out);
    } else if (info.mode === 'row') {
      const px = nx + info.nw / 2, bus = bottom + L.V_GAP / 2;
      info.blocks.forEach(b => {
        const bx = x0 + b.dx, by = y0 + info.rowDy;
        if (b.type === 'list') { placeList(b.list, bx, by, { mode: 'row', px, bottom, bus }, key, out); return; }
        place(b.key, bx, by, memo, out);
        const c = out.pos.get(b.key), cx = c.x + b.info.nw / 2;
        out.links.push({ parent: key, child: b.key, pts: [[px, bottom], [px, bus], [cx, bus], [cx, c.y]], hit: [[cx, bus], [cx, c.y]], label: [cx, (bus + c.y) / 2] });
      });
    }
  }
  function computeLayout() {
    // Long teams are folded into columns; the column height is chosen so the whole chart fits the
    // screen as large as possible (a wide company gets taller columns, a narrow one shorter).
    const r = vpRect(), availW = Math.max(320, r.width - 120), availH = Math.max(260, r.height - 170);
    let best = null;
    for (const rows of [8, 10, 12, 6, 16]) {
      L.MAX_ROWS = rows;
      const memo = new Map(), info = measure(ROOT_KEY, memo);
      const k = Math.min(availW / info.w, availH / info.h, 1);
      if (!best || k > best.k * 1.04) best = { rows, k, memo, info };
    }
    L.MAX_ROWS = best.rows;
    const out = { pos: new Map(), links: [] };
    place(ROOT_KEY, L.MARGIN, L.MARGIN, best.memo, out);
    return { ...out, w: best.info.w + L.MARGIN * 2, h: best.info.h + L.MARGIN * 2 };
  }

  /* ---------------- drawing ---------------- */
  const f1 = v => (Math.round(v * 10) / 10).toString();
  function roundedPath(points, radius = 11) {
    const pts = [];
    points.forEach(p => { const z = pts[pts.length - 1]; if (!z || Math.abs(z[0] - p[0]) > 0.4 || Math.abs(z[1] - p[1]) > 0.4) pts.push(p); });
    for (let i = pts.length - 2; i > 0; i--) {
      const a = pts[i - 1], b = pts[i], c = pts[i + 1];
      if ((Math.abs(a[0] - b[0]) < 0.4 && Math.abs(b[0] - c[0]) < 0.4) || (Math.abs(a[1] - b[1]) < 0.4 && Math.abs(b[1] - c[1]) < 0.4)) pts.splice(i, 1);
    }
    if (pts.length < 2) return '';
    let d = `M ${f1(pts[0][0])} ${f1(pts[0][1])}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const a = pts[i - 1], b = pts[i], c = pts[i + 1];
      const l1 = Math.hypot(b[0] - a[0], b[1] - a[1]), l2 = Math.hypot(c[0] - b[0], c[1] - b[1]);
      const r = Math.min(radius, l1 / 2, l2 / 2);
      d += ` L ${f1(b[0] + (a[0] - b[0]) * r / l1)} ${f1(b[1] + (a[1] - b[1]) * r / l1)} Q ${f1(b[0])} ${f1(b[1])} ${f1(b[0] + (c[0] - b[0]) * r / l2)} ${f1(b[1] + (c[1] - b[1]) * r / l2)}`;
    }
    const z = pts[pts.length - 1];
    return `${d} L ${f1(z[0])} ${f1(z[1])}`;
  }
  const box = node => ({ x: parseFloat(node.style.left) || 0, y: parseFloat(node.style.top) || 0, w: node.offsetWidth, h: node.offsetHeight });
  const center = b => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 });
  function curve(a, b) {
    const ca = center(a), cb = center(b), dx = cb.x - ca.x, dy = cb.y - ca.y;
    let p1, p2, c1, c2;
    if (Math.abs(dy) > (a.h + b.h) / 2 + 24) {
      const s = Math.sign(dy) || 1;
      p1 = { x: ca.x, y: s > 0 ? a.y + a.h : a.y }; p2 = { x: cb.x, y: s > 0 ? b.y : b.y + b.h };
      const k = Math.max(40, Math.abs(p2.y - p1.y) * 0.45);
      c1 = { x: p1.x, y: p1.y + s * k }; c2 = { x: p2.x, y: p2.y - s * k };
    } else {
      const s = Math.sign(dx) || 1;
      p1 = { x: s > 0 ? a.x + a.w : a.x, y: ca.y }; p2 = { x: s > 0 ? b.x : b.x + b.w, y: cb.y };
      const k = Math.max(40, Math.abs(p2.x - p1.x) * 0.45);
      c1 = { x: p1.x + s * k, y: p1.y }; c2 = { x: p2.x - s * k, y: p2.y };
    }
    const mid = { x: (p1.x + 3 * c1.x + 3 * c2.x + p2.x) / 8, y: (p1.y + 3 * c1.y + 3 * c2.y + p2.y) / 8 };
    return { d: `M ${f1(p1.x)} ${f1(p1.y)} C ${f1(c1.x)} ${f1(c1.y)}, ${f1(c2.x)} ${f1(c2.y)}, ${f1(p2.x)} ${f1(p2.y)}`, mid, end: p2 };
  }
  const DEFAULT_LABELS = new Set(['', 'Подчинение', 'Связь отделов', 'Подразделение', 'Директор', 'Руководит', 'reports_to', 'linked_to']);
  const hitAttrs = link => {
    if (link.relation_id) return `data-edge-kind="org" data-edge-id="${link.relation_id}"`;
    if (link.employee_id && link.department_id) return `data-edge-kind="membership" data-employee="${link.employee_id}" data-department="${link.department_id}"`;
    return '';
  };
  const hitTitle = link => (link.relation_id ? 'нажмите, чтобы удалить связь' : 'нажмите, чтобы убрать из отдела');

  let layout = { pos: new Map(), links: [], w: 0, h: 0 };
  let drawnLinks = new Map();
  let focusKey = null, frame = 0;
  function litSets() {
    if (!focusKey) return null;
    const nodes = new Set([focusKey, ...ancestors(focusKey), ...visibleKids(focusKey)]);
    const links = new Set([focusKey, ...ancestors(focusKey).filter(k => k !== ROOT_KEY), ...visibleKids(focusKey)]);
    tree.cross.forEach(c => { if (c.source === focusKey) nodes.add(c.target); if (c.target === focusKey) nodes.add(c.source); });
    return { nodes, links };
  }
  function draw() { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => drawNow()); }
  function drawNow(linkPts = null) {
    const lit = litSets();
    // Shared trunks overlap: each layer is composited as a whole so overlaps never look brighter.
    let base = '', cross = '', top = '', hits = '', labels = '';
    for (const l of layout.links) {
      const link = linkOf.get(l.child) || {};
      const pts = linkPts?.get(l.child) || l.pts;
      const isLit = !!(lit && lit.links.has(l.child));
      const path = `<path class="org-map-edge org-map-edge-${esc(link.kind || 'membership')}" style="--edge:${lineColor(l.parent)}" d="${roundedPath(pts)}"/>`;
      if (isLit) top += path; else base += path;
      if (linkPts) continue;
      const attrs = hitAttrs(link);
      if (attrs) hits += `<path class="org-map-edge-hit" ${attrs} d="M ${f1(l.hit[0][0])} ${f1(l.hit[0][1])} L ${f1(l.hit[1][0])} ${f1(l.hit[1][1])}"><title>${esc(link.label || 'Связь')} — ${hitTitle(link)}</title></path>`;
      // People carry the caption of their line on the card; captions on the chart are for departments.
      if (l.label && link.relation_id && kindOfKey(l.child) !== 'employee' && !DEFAULT_LABELS.has(link.label)) {
        labels += `<text class="org-map-edge-label${isLit ? ' is-lit' : ''}" x="${f1(l.label[0] + 9)}" y="${f1(l.label[1] + 3.5)}">${esc(link.label)}</text>`;
      }
    }
    if (!linkPts) {
      for (const c of tree.cross) {
        const a = nodeByKey.get(c.source), b = nodeByKey.get(c.target);
        if (!a || !b || a.classList.contains('is-hidden') || b.classList.contains('is-hidden')) continue;
        const geo = curve(box(a), box(b));
        const isLit = !!(lit && (c.source === focusKey || c.target === focusKey));
        // «Also in another department» lines would criss-cross the chart: they appear for the selected card only.
        if (!isLit && (c.kind === 'membership' || c.kind === 'manager')) continue;
        const color = c.kind === 'reporting' ? SAND : c.kind === 'block' ? TEAL : deptAccent(idOf(c.source));
        const markup = `<path class="org-map-cross org-map-cross-${esc(c.kind)}" style="--edge:${color}" d="${geo.d}"/>`
          + `<circle class="org-map-edge-dot" style="--edge:${color}" cx="${f1(geo.end.x)}" cy="${f1(geo.end.y)}" r="2.6"/>`;
        if (isLit) top += markup; else cross += markup;
        const attrs = hitAttrs(c);
        if (attrs) hits += `<path class="org-map-edge-hit is-cross" ${attrs} d="${geo.d}"><title>${esc(c.label || 'Связь')} — ${hitTitle(c)}</title></path>`;
        if (isLit && c.label) labels += `<text class="org-map-edge-label is-lit" x="${f1(geo.mid.x)}" y="${f1(geo.mid.y - 6)}" text-anchor="middle">${esc(c.label)}</text>`;
      }
    }
    svg.innerHTML = `<g class="org-map-layer-base">${base}</g><g class="org-map-layer-cross">${cross}</g><g class="org-map-layer-lit">${top}</g><g class="org-map-layer-hits">${hits}</g><g class="org-map-layer-labels">${labels}</g>`;
    svg.classList.toggle('has-focus', !!focusKey);
    canvas.classList.toggle('has-focus', !!focusKey);
    nodeByKey.forEach((node, key) => node.classList.toggle('is-lit', !!(lit && lit.nodes.has(key))));
  }

  /* ---------------- applying the layout (with a short, calm transition) ---------------- */
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  let layoutAnim = 0;
  function positionToggles(pos) {
    toggles.forEach((button, key) => {
      const p = pos.get(key);
      button.hidden = !p;
      if (!p) return;
      const s = sizeOf(key);
      button.style.left = `${Math.round(p.x + s.w / 2)}px`;
      button.style.top = `${Math.round(p.y + s.h - 11)}px`;
    });
  }
  function autoLayout({ animate = false, fit = false, keep = null } = {}) {
    sizes.clear();
    nodeByKey.forEach((node, key) => node.classList.toggle('is-hidden', !isVisible(key)));
    const before = new Map(layout.pos), beforeLinks = new Map(layout.links.map(l => [l.child, l.pts]));
    const anchorBefore = keep ? before.get(keep) : null;
    layout = computeLayout();
    canvas.style.width = `${Math.ceil(layout.w)}px`;
    canvas.style.height = `${Math.ceil(layout.h)}px`;
    // Keep the branch the user clicked under the pointer: shift the view by how far it moved.
    if (anchorBefore && layout.pos.get(keep)) {
      const after = layout.pos.get(keep);
      view.x -= (after.x - anchorBefore.x) * view.k; view.y -= (after.y - anchorBefore.y) * view.k;
      applyView();
    }
    cancelAnimationFrame(layoutAnim);
    const finish = () => {
      layout.pos.forEach((p, key) => { const n = nodeByKey.get(key); n.style.left = `${Math.round(p.x)}px`; n.style.top = `${Math.round(p.y)}px`; n.classList.remove('is-entering'); });
      positionToggles(layout.pos);
      drawNow();
    };
    if (!animate || reduceMotion || !before.size) { finish(); if (fit) fitView(false); canvas.classList.add('is-ready'); return; }
    const from = new Map();
    layout.pos.forEach((p, key) => {
      let start = before.get(key);
      if (!start) {
        const up = ancestors(key).find(a => before.has(a));
        const anchor = up ? before.get(up) : p;
        start = { x: anchor.x, y: anchor.y };
        nodeByKey.get(key).classList.add('is-entering');
      }
      from.set(key, start);
    });
    const fromLinks = new Map();
    layout.links.forEach(l => {
      const prev = beforeLinks.get(l.child);
      fromLinks.set(l.child, prev && prev.length === l.pts.length ? prev : l.pts.map(() => l.pts[0]));
    });
    positionToggles(new Map());
    if (fit) fitView(true);
    const t0 = performance.now(), ms = 360;
    const step = now => {
      const t = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - t, 3);
      layout.pos.forEach((p, key) => {
        const s = from.get(key), n = nodeByKey.get(key);
        n.style.left = `${Math.round(s.x + (p.x - s.x) * e)}px`; n.style.top = `${Math.round(s.y + (p.y - s.y) * e)}px`;
        if (n.classList.contains('is-entering')) n.style.opacity = String(e);
      });
      const pts = new Map();
      layout.links.forEach(l => { const a = fromLinks.get(l.child); pts.set(l.child, l.pts.map((q, i) => [a[i][0] + (q[0] - a[i][0]) * e, a[i][1] + (q[1] - a[i][1]) * e])); });
      if (t < 1) { drawNow(pts); layoutAnim = requestAnimationFrame(step); }
      else { nodeByKey.forEach(n => { n.style.opacity = ''; }); finish(); }
    };
    layoutAnim = requestAnimationFrame(step);
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
  const clampK = k => Math.max(0.15, Math.min(2, k));
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
  function animateView(target, ms = 420) {
    cancelAnimationFrame(viewAnim);
    if (reduceMotion) { Object.assign(view, target); applyView(); return; }
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
    layout.pos.forEach((p, key) => { const s = sizeOf(key); minX = Math.min(minX, p.x); minY = Math.min(minY, p.y); maxX = Math.max(maxX, p.x + s.w); maxY = Math.max(maxY, p.y + s.h); });
    if (!isFinite(minX)) return { x: 0, y: 0, w: 800, h: 600 };
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }
  const inspector = document.getElementById('org-inspector');
  const inspectorWidth = () => (inspector && !inspector.hidden && window.innerWidth > 980 ? inspector.offsetWidth + 28 : 0);
  const READABLE_K = 0.45, START_K = 0.6;
  function fitView(animate = true, { readable = false } = {}) {
    const r = vpRect(), b = contentBounds();
    const padX = 60, padTop = 78, padBottom = 92, availW = Math.max(200, r.width - inspectorWidth());
    let k = clampK(Math.min((availW - padX * 2) / b.w, (r.height - padTop - padBottom) / b.h, 1));
    let x = (availW - b.w * k) / 2 - b.x * k;
    const top = layout.pos.get(ROOT_KEY);
    if (readable && k < READABLE_K && top) {
      // On a small screen the whole company would be too small to read: open at a readable scale
      // with the company and the director in the middle; «Вписать» still shows everything.
      k = clampK(START_K);
      x = availW / 2 - (top.x + sizeOf(ROOT_KEY).w / 2) * k;
    }
    // A tall chart starts from the top (company and director in view) rather than from its middle.
    const y = b.h * k > r.height - padTop - padBottom ? padTop - b.y * k : padTop + (r.height - padTop - padBottom - b.h * k) / 2 - b.y * k;
    const target = { k, x, y };
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
      if (node === except || found || node.classList.contains('is-hidden')) return;
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

  /* ---------------- drag, pan, click ---------------- */
  let activeDrag = null, activePan = null, suppressClick = false, dropHover = null;
  const pointers = new Map();
  let pinch = null;
  const dropHint = document.createElement('div');
  dropHint.className = 'org-map-drop-hint'; dropHint.hidden = true;
  stage?.appendChild(dropHint);
  const setDropHover = node => {
    if (dropHover === node) return;
    dropHover?.classList.remove('is-drop-target');
    dropHover = node; dropHover?.classList.add('is-drop-target');
  };
  function showHint(drag, target, event) {
    const intent = dropIntent(drag.node, target);
    if (!intent) { dropHint.hidden = true; return; }
    dropHint.textContent = intent === 'assign' ? `＋ В состав «${nodeTitle(target)}»` : `↳ Связать с «${nodeTitle(target)}»`;
    const r = stage.getBoundingClientRect();
    dropHint.style.left = `${event.clientX - r.left + 16}px`; dropHint.style.top = `${event.clientY - r.top + 18}px`;
    dropHint.hidden = false;
  }

  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    if (event.target.closest('.org-map-edge-hit, .org-map-toggle')) return;
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
    activeDrag = { node, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, offX: w.x - b.x, offY: w.y - b.y, home: { x: b.x, y: b.y }, moved: false };
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
    if (kindOf(drag.node) === 'group') return;
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 5) return;
    drag.moved = true; event.preventDefault();
    drag.node.classList.add('is-map-dragging');
    canvas.classList.add('is-dragging');
    const w = toWorld(event.clientX, event.clientY);
    drag.node.style.left = `${Math.round(w.x - drag.offX)}px`;
    drag.node.style.top = `${Math.round(w.y - drag.offY)}px`;
    const target = nodeAt(w, drag.node);
    setDropHover(dropIntent(drag.node, target) ? target : null);
    showHint(drag, target, event);
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
  function snapBack(node, home) {
    if (reduceMotion) { node.style.left = `${home.x}px`; node.style.top = `${home.y}px`; return; }
    const from = box(node), t0 = performance.now(), ms = 260;
    const step = now => {
      const t = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - t, 3);
      node.style.left = `${Math.round(from.x + (home.x - from.x) * e)}px`; node.style.top = `${Math.round(from.y + (home.y - from.y) * e)}px`;
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  const finishDrag = event => {
    if (!activeDrag || activeDrag.pointerId !== event.pointerId) return;
    const drag = activeDrag; activeDrag = null;
    drag.node.classList.remove('is-map-dragging');
    canvas.classList.remove('is-dragging');
    dropHint.hidden = true;
    const target = dropHover; setDropHover(null);
    if (!drag.moved) { openInspectorFor(drag.node); return; }
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 300);
    snapBack(drag.node, drag.home);
    const intent = event.type === 'pointerup' ? dropIntent(drag.node, target) : null;
    if (intent === 'assign') openAssign(drag.node, target);
    else if (intent === 'relation') openRelation(target, drag.node);
  };
  viewport.addEventListener('pointerup', event => { finishPan(event); finishDrag(event); });
  viewport.addEventListener('pointercancel', event => { finishPan(event); finishDrag(event); });
  viewport.addEventListener('dblclick', event => {
    if (event.target.closest('[data-map-node], .org-map-toggle')) return;
    const r = vpRect(), w = toWorld(event.clientX, event.clientY), k = clampK(view.k * 1.5);
    animateView({ k, x: event.clientX - r.left - w.x * k, y: event.clientY - r.top - w.y * k }, 280);
  });
  canvas.addEventListener('click', event => {
    const toggle = event.target.closest('.org-map-toggle');
    if (toggle) { event.preventDefault(); event.stopPropagation(); setCollapsed(toggle.dataset.toggle, !collapsed.has(toggle.dataset.toggle)); return; }
    const edge = event.target.closest('.org-map-edge-hit');
    if (edge) { event.preventDefault(); event.stopPropagation(); onEdgeClick(edge); return; }
    const inspectDept = event.target.closest('[data-inspect-department]');
    if (inspectDept) { event.preventDefault(); event.stopPropagation(); openInspector('department', Number(inspectDept.dataset.inspectDepartment)); return; }
    if (event.target.closest('.org-map-person-node')) event.preventDefault();
    if (suppressClick) { event.preventDefault(); event.stopPropagation(); }
  }, true);

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
    if (kindOf(node) === 'group') return 'Без подразделения';
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
    else openInspector('company', 1, { select: node.dataset.nodeKey });
  }
  let views = null;
  const membersOf = depId => data.employees.filter(e => e.memberships.some(m => m.department_id === depId));

  function openInspector(kind, id, { center: doCenter = false, select = null } = {}) {
    if (!inspector) return;
    const key = kind === 'company' ? 'company:1' : `${kind}:${id}`;
    reveal(key);
    const node = nodeByKey.get(select || key);
    if (!node) return;
    currentInspect = { kind, id };
    inspector.style.setProperty('--accent', kind === 'company' ? '#d98e8a' : accentOf(node));
    views = views || window.SysaiOrgMapViews({ esc, initials, deptById, deptAccent, accentOf, nodeByKey, nodeTitle, membersOf, data, root, ICON_X, ICON_CROWN });
    inspector.innerHTML = kind === 'employee' ? views.employeeHtml(empById.get(id), node) : kind === 'department' ? views.departmentHtml(deptById.get(id)) : views.companyHtml();
    inspector.hidden = false;
    selectNode(select || key);
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
    openInspector(kind, id, { center: true });
    const node = nodeByKey.get(`${kind}:${id}`);
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
    if (event.target.closest?.('input, textarea, select, [contenteditable]') || document.querySelector('dialog[open]')) return;
    if (event.key === '/') { event.preventDefault(); searchInput?.focus(); }
    else if (event.key === 'Escape') closeInspector();
    else if (event.key === 'f' || event.key === 'F' || event.key === 'а' || event.key === 'А') fitView();
    else if (event.key === '+' || event.key === '=') $('[data-zoom="in"]')?.click();
    else if (event.key === '-' || event.key === '_') $('[data-zoom="out"]')?.click();
    else if (event.key === '0') $('[data-zoom="reset"]')?.click();
  });

  /* ---------------- boot ---------------- */
  syncToggles();
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(STATE_KEY) || 'null'); sessionStorage.removeItem(STATE_KEY); } catch (_) {}
  autoLayout();
  let restored = false;
  if (saved) {
    // Back from an edit made on the map: keep the user's place instead of jumping to the overview.
    try {
      const v = JSON.parse(localStorage.getItem(VIEW_KEY) || 'null');
      if (v && isFinite(v.x) && isFinite(v.y) && isFinite(v.k)) { Object.assign(view, { x: v.x, y: v.y, k: clampK(v.k) }); applyView(); restored = true; }
    } catch (_) {}
  }
  if (!restored) fitView(false, { readable: true });

  // Card sizes settle once web fonts arrive (or a name is edited): re-flow quietly when they change.
  let relayoutTimer = 0;
  const signatureNow = () => [...sizes.keys()].map(k => { const n = nodeByKey.get(k); return `${k}:${n.offsetWidth}x${n.offsetHeight}`; }).join('|');
  const signatureMeasured = () => [...sizes].map(([k, s]) => `${k}:${s.w}x${s.h}`).join('|');
  const relayoutIfNeeded = () => {
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(() => {
      if (activeDrag) return;
      if (signatureNow() !== signatureMeasured()) autoLayout();
    }, 80);
  };
  document.fonts?.ready?.then(relayoutIfNeeded);
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(relayoutIfNeeded);
    nodeByKey.forEach(node => observer.observe(node));
  }

  if (saved?.toast) toast(saved.toast);
  else if (toastEl?.dataset.initial) toast(toastEl.dataset.initial);
  const newest = kind => {
    const list = kind === 'employee' ? data.employees : data.departments;
    return list.length ? list.reduce((a, b) => (b.id > a.id ? b : a)) : null;
  };
  const pulse = key => { const node = nodeByKey.get(key); node?.classList.remove('is-pulse'); void node?.offsetWidth; node?.classList.add('is-pulse'); };
  if (saved?.focusNewEmployee) {
    const emp = newest('employee');
    if (emp) setTimeout(() => { openInspector('employee', emp.id, { center: true }); pulse(`employee:${emp.id}`); }, 80);
  } else if (saved?.focusNewDepartment) {
    const dep = newest('department');
    if (dep) setTimeout(() => { openInspector('department', dep.id, { center: true }); pulse(`department:${dep.id}`); }, 80);
  } else if (saved?.inspect && nodeByKey.get(saved.inspect.kind === 'company' ? 'company:1' : `${saved.inspect.kind}:${saved.inspect.id}`)) {
    openInspector(saved.inspect.kind, saved.inspect.id);
  }
})();
