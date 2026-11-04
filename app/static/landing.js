/* SYSAI landing: the live voice under the wordmark (pixel wave like the sign-in page, with speech bubbles) and the
   record-to-tasks scheme. Lines are measured from the real blocks, so they follow any layout; signals run along them
   and every block they reach changes its state. Vanilla JS, no dependencies. */
(() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v)), fract = v => v - Math.floor(v);
  const smooth = x => x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);
  const inOut = x => x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
  const watch = (el, cb, threshold = .1) => { if ('IntersectionObserver' in window) new IntersectionObserver(([en]) => cb(en.isIntersecting), { threshold }).observe(el); else cb(true); };

  /* ---------- 1. the voice: a timeline of two speakers scrolls through a grid of pixels ---------- */
  const vx = $('#vx');
  if (vx) (() => {
    const cv = $('.vx-cv', vx), ctx = cv && cv.getContext('2d', { alpha: true }); if (!ctx) return;
    const lanes = { d: $('.vx-top', vx), i: $('.vx-bot', vx) }, timeEl = $('#vx-time');
    const COL = { d: [248, 120, 138], i: [115, 167, 255] }, NAME = { d: 'Директор', i: 'Иван' }, QUIET = [150, 146, 138];
    const SCRIPT = [['d', 'Иван, ошибки оплаты — до пятницы', 2.7], ['i', 'Успею к четвергу', 1.5], ['d', 'Тогда в четверг покажем клиенту', 2.4],
      ['i', 'Подготовлю демо-стенд', 1.8], ['d', 'Риски по срокам — в протокол', 2.3], ['i', 'Принято', .9]];
    const PH = []; let T = .6;
    SCRIPT.forEach(([who, text, dur], k) => { PH.push({ who, text, words: text.split(' '), a: T, b: T + dur, k }); T += dur + (k % 2 ? .8 : .55); });
    const LOOP = T + .3, V = 44; /* seconds in the loop, pixels per second */
    const wrap = tau => ((tau % LOOP) + LOOP) % LOOP;
    function phraseAt(tau) { tau = wrap(tau); for (const p of PH) if (tau >= p.a && tau < p.b) return [p, tau - p.a]; return [null, 0]; }
    function envAt(p, u) {
      const d = p.b - p.a, pos = u / d, L = p.text.length; let acc = 0, dip = 1;
      for (let w = 0; w < p.words.length - 1; w++) { acc += (p.words[w].length + 1) / L; const g = Math.abs(pos - acc) * d; if (g < .06) dip = Math.min(dip, .2 + 13 * g); }
      const syl = Math.pow(Math.abs(Math.sin(u * 14.5 + p.k * 1.7)), .55) * (.74 + .26 * Math.sin(u * 5.3 + p.k));
      return clamp(smooth(u / .12) * smooth((d - u) / .16) * dip * (.2 + .8 * syl) * (.78 + .22 * Math.sin(u * 1.9 + p.k * 2.3)), 0, 1);
    }

    let W = 0, H = 0, cell = 4, P = 0, clock = 0;
    const ptr = { x: -999, tx: -999, y: 0, e: 0, inside: false }, sparks = [];
    function resize() {
      const r = cv.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2); W = Math.max(1, r.width); H = Math.max(1, r.height);
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cell = W < 420 ? 3.6 : 4; P = Math.round(W * .62); draw(clock, 0);
    }
    function draw(t, dt) {
      ctx.clearRect(0, 0, W, H);
      ptr.x += (ptr.tx - ptr.x) * .18; ptr.e += ((ptr.inside ? 1 : 0) - ptr.e) * .07;
      const cols = Math.ceil(W / cell), rows = Math.floor(H / cell), oy = (H - rows * cell) / 2, mid = H / 2, [cur] = phraseAt(t);
      /* soft light behind the playhead in the color of the speaker */
      const lc = cur ? COL[cur.who] : QUIET, oval = (x, r, k, stops) => { ctx.save(); ctx.translate(x, mid); ctx.scale(1, k); const g = ctx.createRadialGradient(0, 0, 0, 0, 0, r);
        stops.forEach(([o, c]) => g.addColorStop(o, c)); ctx.fillStyle = g; ctx.fillRect(-r, -r, 2 * r, 2 * r); ctx.restore(); };
      oval(P, 130, .2, [[0, `rgba(${lc[0]},${lc[1]},${lc[2]},${cur ? .22 : .07})`], [1, `rgba(${lc[0]},${lc[1]},${lc[2]},0)`]]);
      if (ptr.e > .01) oval(ptr.x, 96, .26, [[0, `rgba(240,112,131,${(.18 * ptr.e).toFixed(3)})`], [.6, `rgba(120,170,244,${(.07 * ptr.e).toFixed(3)})`], [1, 'rgba(120,170,244,0)']]);
      for (let c = 0; c < cols; c++) {
        const x = c * cell + cell / 2, tau = t + (x - P) / V, [p, u] = phraseAt(tau), played = x <= P;
        const near = Math.exp(-Math.pow((x - P) / 44, 2)), boost = ptr.e * Math.exp(-Math.pow((x - ptr.x) / 54, 2));
        let env = p ? Math.min(1, 1.06 * Math.pow(envAt(p, u), .72)) : 0;
        env = clamp(env * (1 + near * (.16 * Math.sin(t * 11 + c * .9) + .1 * Math.sin(t * 17.3 - c * 1.7))) + boost * (p ? .3 : .12), 0, 1);
        const idx = Math.floor(tau * V / cell), jit = p ? .84 + .3 * fract(Math.sin(idx * 91.7 + 3.1) * 43758.5453) : 1;
        const amp = Math.max(cell * .62, Math.min(1, env * jit) * (H * .5 - 1)), rgb = p ? COL[p.who] : QUIET;
        const age = played ? .45 + .55 * smooth(x / (W * .32)) : .26;
        for (let r = 0; r < rows; r++) {
          const y = oy + r * cell + cell / 2, rel = Math.abs(y - mid) / amp, n = .08 * Math.sin(c * .67 + r * .19 + t * 1.45);
          if (rel > 1 + n) continue;
          const edge = clamp(1 - rel, 0, 1), h = fract(Math.sin(idx * 127.1 + r * 311.7) * 43758.5453);
          if (p && h < .05 + .17 * (1 - edge)) continue;
          const sh = .5 + .5 * Math.sin(t * 1.25 - c * .09 + Math.sin(r * .5 + t * .3) * .6);
          const a = clamp((.16 + .6 * edge + .24 * sh + near * .25 * (played ? 1 : 0) + boost * .3) * (p ? 1 : .4) * age, .03, 1);
          const s = cell * (.5 + .32 * edge + boost * .08);
          ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a.toFixed(3)})`; ctx.fillRect(x - s / 2, y - s / 2, s, s);
        }
      }
      /* sparkles leave the wave at the playhead and float away with it */
      if (cur && dt > 0 && Math.random() < dt * 14) sparks.push({ x: P - Math.random() * 6, y: mid + (Math.random() - .5) * H * .7, vy: (Math.random() - .5) * 26, life: 0, max: .6 + Math.random() * .6, rgb: COL[cur.who] });
      for (let i = sparks.length - 1; i >= 0; i--) { const s = sparks[i]; s.life += dt; s.x -= V * dt; s.y += s.vy * dt; if (s.life > s.max) { sparks.splice(i, 1); continue; }
        const f = 1 - s.life / s.max; ctx.fillStyle = `rgba(${s.rgb[0]},${s.rgb[1]},${s.rgb[2]},${(.9 * f).toFixed(3)})`; ctx.fillRect(s.x - 1, s.y - 1, 2, 2); }
      const ph = ctx.createLinearGradient(0, 0, 0, H); ph.addColorStop(0, 'rgba(240,238,230,0)'); ph.addColorStop(.5, 'rgba(240,238,230,.85)'); ph.addColorStop(1, 'rgba(240,238,230,0)');
      ctx.fillStyle = ph; ctx.fillRect(P - .5, 0, 1, H);
    }

    /* speech bubbles: one per lane, the tail points at the start of the phrase and drifts with the wave */
    const bub = { d: null, i: null }; let lastK = -1;
    function drop(o) { if (!o) return; o.el.classList.remove('on'); setTimeout(() => o.el.remove(), 450); }
    function spawn(p, t0, all) {
      const lane = lanes[p.who]; if (!lane) return; drop(bub[p.who]);
      const el = document.createElement('div'); el.className = 'vx-say ' + p.who; el.innerHTML = `<i></i><b>${NAME[p.who]}</b><span></span>`;
      lane.appendChild(el); const o = bub[p.who] = { el, span: el.lastChild, p, t0, n: 0, w: 0 };
      if (all) { o.n = p.words.length; o.span.textContent = p.text; o.w = el.offsetWidth; }
      requestAnimationFrame(() => el.classList.add('on')); return o;
    }
    function place(o, t) {
      const d = o.p.b - o.p.a, k = t - o.t0, n = clamp(Math.ceil(o.p.words.length * (k + .15) / (d * .9)), 1, o.p.words.length);
      if (n !== o.n) { o.n = n; o.span.textContent = o.p.words.slice(0, n).join(' '); o.w = o.el.offsetWidth; }
      const xa = P - k * V, left = clamp(xa - 14, 0, Math.max(0, W - o.w)), tx = clamp(xa - left, 9, o.w - 9);
      o.el.style.transform = `translateX(${left.toFixed(1)}px)`; o.el.style.setProperty('--tx', tx.toFixed(1) + 'px');
      if (k > d + 1.5 && !o.gone) { o.gone = true; drop(o); if (bub[o.p.who] === o) bub[o.p.who] = null; }
    }
    function talk(t) {
      const tau = wrap(t), k = PH.findIndex(p => tau >= p.a && tau < p.b);
      if (k >= 0 && k !== lastK && tau - PH[k].a < .4) { lastK = k; spawn(PH[k], t - (tau - PH[k].a)); }
      if (k < 0 && tau < PH[0].a) lastK = -1;
      for (const w in bub) if (bub[w]) place(bub[w], t);
    }
    const mmss = s => `${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(Math.floor(s) % 60).padStart(2, '0')}`;

    vx.addEventListener('pointermove', e => { const r = cv.getBoundingClientRect(); ptr.tx = e.clientX - r.left; if (!ptr.inside) ptr.x = ptr.tx; ptr.inside = true; }, { passive: true });
    vx.addEventListener('pointerleave', () => { ptr.inside = false; }, { passive: true });
    if ('ResizeObserver' in window) new ResizeObserver(resize).observe(cv); else addEventListener('resize', resize, { passive: true });
    resize();
    if (reduced) { clock = PH[0].b - .1; draw(clock, 0); const o = spawn(PH[0], PH[0].a, true); place(o, clock); const q = spawn(PH[1], PH[1].a, true); q.el.style.transform = `translateX(${Math.round(W * .3)}px)`; return; }
    let raf = 0, last = 0, acc = 0, on = false, sec = -1;
    function frame() {
      const ts = performance.now(), dt = Math.min(.1, (ts - last) / 1000 || 0); last = ts; acc += dt;
      if (acc >= .024) { clock += acc; draw(clock, acc); talk(clock); acc = 0; const s = Math.floor(clock); if (s !== sec && timeEl) { sec = s; timeEl.textContent = mmss(s); } }
      raf = on ? requestAnimationFrame(frame) : 0;
    }
    const run = v => { on = v && !document.hidden; if (on && !raf) { last = performance.now(); raf = requestAnimationFrame(frame); } };
    watch(vx, v => { vx._vis = v; run(v); }, 0);
    document.addEventListener('visibilitychange', () => run(vx._vis));
  })();

  /* ---------- 2. the scheme: a request comes in at the top and flows down ---------- */
  const lx = $('#lx'), core = $('#lx-core');
  if (lx && core) (() => {
    const svg = $('#lx-svg'), stageEl = $('#lx-stage');
    const defs = mk('defs', {}, svg);
    mk('feGaussianBlur', { stdDeviation: 2.6 }, mk('filter', { id: 'lx-blur', x: '-50%', y: '-50%', width: '200%', height: '200%' }, defs));
    mk('feGaussianBlur', { stdDeviation: .8 }, mk('filter', { id: 'lx-blur2', x: '-50%', y: '-50%', width: '200%', height: '200%' }, defs));
    const base = mk('g', {}, svg), sigLayer = mk('g', {}, svg);
    const lines = {}, knots = {}, routes = {}, seen = new Set(), lit = new Set();
    const rec = $('#lx-rec'), recSt = $('.lx-st', rec), srcs = $$('.lx-src', lx), mem = $('#lx-mem'), modelsEl = $('#lx-models'), outCol = $('.lx-out', lx);
    const hcard = $('#lx-herm .lx-hcard'), hrows = $$('#lx-herm .lx-hrow').map(el => ({ el, k: el.dataset.k, st: $('.lx-st', el) }));
    const H = Object.fromEntries(hrows.map(r => [r.k, r]));
    const models = Object.fromEntries($$('.lx-m', core).map(m => [m.dataset.m, m]));
    const agents = $$('.lx-ag', core).map(el => ({ el, k: el.dataset.a, st: $('.lx-st', el), uses: (el.dataset.uses || '').split(' ').filter(Boolean) }));
    const A = Object.fromEntries(agents.map(a => [a.k, a]));
    const MEM = ['who', 'sec', 'ast'];
    const branches = $$('.lx-br', lx).map(el => ({ el, k: el.dataset.k, node: $('.lx-node', el), st: $('.lx-node .lx-st', el), subs: $$('.lx-sub', el), row: 0,
      tone: el.dataset.tone === 'b' ? 'var(--lx-blue)' : 'var(--lx-coral)' }));
    const B = Object.fromEntries(branches.map(b => [b.k, b]));
    const R = 10, VIOLET = 'var(--lx-violet)', BLUE = 'var(--lx-blue)', CORAL = 'var(--lx-coral)', GREEN = 'var(--lx-green)', AMBER = 'var(--lx-amber)';
    const box = (el, host) => { const r = el.getBoundingClientRect(), p = host.getBoundingClientRect(); const l = r.left - p.left, t = r.top - p.top; return { l, t, r: l + r.width, b: t + r.height, w: r.width, h: r.height, cx: l + r.width / 2, cy: t + r.height / 2 }; };
    const f = v => v.toFixed(1);
    const glow = m => (models[m] && getComputedStyle(models[m]).getPropertyValue('--glow').trim()) || '#f0eee6';

    function line(key, d, cls, tone) {
      let l = lines[key]; if (!l) { l = lines[key] = mk('path', { class: 'lx-ln ' + (cls || '') }, base); if (tone) l.style.setProperty('--tone', tone); }
      l.setAttribute('d', d); seen.add('l:' + key); return l;
    }
    /* a knot is lit together with the line of the same key; n tells apart several knots of one line */
    function knot(key, x, y, tone, r = 2.6, n = 0) {
      const id = key + '#' + n; let k = knots[id]; if (!k) { k = knots[id] = mk('circle', { class: 'lx-knot' }, base); k._key = key; if (tone) k.style.setProperty('--tone', tone); }
      k.setAttribute('cx', f(x)); k.setAttribute('cy', f(y)); k.setAttribute('r', r); seen.add('k:' + id); return k;
    }
    function sweep() {
      for (const k in lines) if (!seen.has('l:' + k)) { lines[k].remove(); delete lines[k]; }
      for (const k in knots) if (!seen.has('k:' + k)) { knots[k].remove(); delete knots[k]; }
    }
    function light(key, on = true) { if (on) lit.add(key); else lit.delete(key); if (lines[key]) lines[key].classList.toggle('lit', on); for (const id in knots) if (knots[id]._key === key) knots[id].classList.toggle('lit', on); }
    function relight() { for (const k in lines) lines[k].classList.toggle('lit', lit.has(k)); for (const id in knots) knots[id].classList.toggle('lit', lit.has(knots[id]._key)); }

    /* down from (x0,y0) to the level yb, across to x1, down to y1, with rounded corners */
    function vhv(x0, y0, yb, x1, y1) {
      const dx = x1 - x0; if (Math.abs(dx) < 1) return `M${f(x0)},${f(y0)} V${f(y1)}`;
      const s = Math.sign(dx), r = Math.max(1, Math.min(R, Math.abs(dx) / 2, yb - y0, y1 - yb));
      return `M${f(x0)},${f(y0)} V${f(yb - r)} Q${f(x0)},${f(yb)} ${f(x0 + s * r)},${f(yb)} H${f(x1 - s * r)} Q${f(x1)},${f(yb)} ${f(x1)},${f(yb + r)} V${f(y1)}`;
    }
    /* a drop from a bus at the level yb into the top of a block; s is the side the bus comes from, 0 for a straight drop */
    const drop = (x, yb, y1, s) => s ? `M${f(x + s * R)},${f(yb)} Q${f(x)},${f(yb)} ${f(x)},${f(yb + R)} V${f(y1)}` : `M${f(x)},${f(yb)} V${f(y1)}`;
    /* an elbow from a rail at x that runs down into a block edge at (x1,y1) */
    const elbowIn = (x, y1, x1) => `M${f(x)},${f(y1 - R)} Q${f(x)},${f(y1)} ${f(x + R)},${f(y1)} H${f(x1)}`;
    const railTo = (x, y0, y1, x1) => `M${f(x)},${f(y0)} V${f(y1 - R)} Q${f(x)},${f(y1)} ${f(x + R)},${f(y1)} H${f(x1)}`;
    const hcurve = (x0, y0, x1, y1) => { const dx = (x1 - x0) * .55; return `M${f(x0)},${f(y0)} C${f(x0 + dx)},${f(y0)} ${f(x1 - dx)},${f(y1)} ${f(x1)},${f(y1)}`; };
    const chevronDown = (x, y) => `M${f(x - 3.5)},${f(y - 6)} L${f(x)},${f(y - 1.5)} L${f(x + 3.5)},${f(y - 6)}`;
    const chevronRight = (x, y) => `M${f(x - 6)},${f(y - 3.5)} L${f(x - 1.5)},${f(y)} L${f(x - 6)},${f(y + 3.5)}`;

    let mode = '';
    function layout() {
      svg.setAttribute('viewBox', `0 0 ${lx.clientWidth} ${lx.clientHeight}`); seen.clear(); for (const k in routes) delete routes[k];
      const wide = getComputedStyle(modelsEl).position === 'absolute';
      const c = box(core, lx), rb = box(rec, lx), ag = agents.map(a => box(a.el, lx)), nodes = branches.map(b => box(b.node, lx));
      const narrow = !wide && ag.length > 1 && Math.abs(ag[1].cy - ag[0].cy) > 4;
      mode = wide ? 'wide' : narrow ? 'narrow' : 'mid';
      const cx = rb.cx, a0 = ag[0], z = ag[ag.length - 1], ob = box(outCol, lx);

      /* sources: five drops merge into one line down to the record */
      if (wide) {
        const sb = srcs.map(s => box(s, lx)), yb = sb[0].b + Math.min(22, (rb.t - sb[0].b) / 2), xs = sb.map(s => s.cx);
        sb.forEach((s, i) => { const side = Math.abs(s.cx - cx) < 1 ? 0 : Math.sign(s.cx - cx);
          line('f-' + i, side ? `M${f(s.cx)},${f(s.b)} V${f(yb - R)} Q${f(s.cx)},${f(yb)} ${f(s.cx - side * R)},${f(yb)}` : `M${f(s.cx)},${f(s.b)} V${f(yb)}`, 'dash', BLUE);
          knot('f-' + i, s.cx, s.b, BLUE, 2.4); routes['src-' + i] = vhv(s.cx, s.b, yb, cx, rb.t); });
        line('bus', `M${f(Math.min(...xs) + R)},${f(yb)} H${f(Math.max(...xs) - R)}`, 'dash', BLUE);
        line('drop', `M${f(cx)},${f(yb)} V${f(rb.t)}`, 'dash', BLUE); knot('drop', cx, rb.t, BLUE, 2.4);
      } else {
        const ib = box($('.lx-in', lx), lx), d = `M${f(cx)},${f(ib.b + 4)} V${f(rb.t)}`;
        line('drop', d, 'dash', BLUE); knot('drop', cx, ib.b + 4, BLUE, 2.2); knot('drop', cx, rb.t, BLUE, 2.4, 1); srcs.forEach((s, i) => { routes['src-' + i] = d; });
      }
      /* the record goes down the spine into the first agent */
      const yIn = wide ? a0.t : c.t;
      routes.rec = `M${f(cx)},${f(rb.b)} V${f(yIn)}`; line('entry', routes.rec, '', CORAL);
      knot('entry', cx, rb.b, CORAL, 2.4); knot('entry', cx, c.t, CORAL, wide ? 2.6 : 3, 1); if (wide) knot('entry', cx, a0.t, CORAL, 3, 2);
      /* the chain of agents */
      agents.forEach((a, i) => { if (!i) return; const p = ag[i - 1], q = ag[i], key = 'chain-' + (i - 1); let d, arrow;
        if (wide) { d = `M${f(cx)},${f(p.b)} V${f(q.t)}`; arrow = chevronDown(cx, q.t); }
        else if (narrow) { const x = p.l + 26; d = `M${f(x)},${f(p.b)} V${f(q.t)}`; arrow = chevronDown(x, q.t); }
        else { d = `M${f(p.r)},${f(p.cy)} H${f(q.l)}`; arrow = chevronRight(q.l, q.cy); }
        line(key, d, '', VIOLET); routes[key] = d; line(key + 'v', arrow, 'arrow', VIOLET); });
      /* the exit and the tree of outputs */
      if (wide) {
        routes.exit = `M${f(cx)},${f(z.b)} V${f(c.b)}`; line('exit', routes.exit, '', AMBER); knot('exit', cx, z.b, AMBER, 2.6); knot('exit', cx, c.b, AMBER, 3, 1);
        const rows = []; nodes.forEach((n, i) => { const row = rows.find(r => Math.abs(r.t - n.t) < 6); if (row) row.items.push(i); else rows.push({ t: n.t, items: [i] }); });
        rows.sort((p, q) => p.t - q.t);
        const capB = box($('.lx-cap', outCol), lx).b; let above = capB, yPrev = c.b;
        rows.forEach((row, ri) => {
          const yb = (above + row.t) / 2, xs = row.items.map(i => nodes[i].cx), x0 = Math.min(...xs), x1 = Math.max(...xs);
          line(ri ? 'trunk' + ri : 'trunk', `M${f(cx)},${f(yPrev)} V${f(yb)}`, 'trunk'); knot(ri ? 'trunk' + ri : 'trunk', cx, yb, null, 2.2);
          if (x1 - x0 > 2 * R) line('bus-' + ri, `M${f(Math.min(x0 + R, cx))},${f(yb)} H${f(Math.max(x1 - R, cx))}`, 'trunk');
          row.items.forEach(i => { const n = nodes[i], b = branches[i], outer = n.cx === x0 || n.cx === x1, side = Math.abs(n.cx - cx) < 1 || !outer ? 0 : Math.sign(n.cx - cx);
            b.row = ri; line('br-' + b.k, drop(n.cx, yb, n.t, side ? -side : 0), '', b.tone); knot('br-' + b.k, n.cx, n.t, b.tone, 2.4);
            routes['br-' + b.k] = vhv(cx, c.b, yb, n.cx, n.t); });
          yPrev = yb; above = Math.max(...row.items.map(i => box(branches[i].el, lx).b)); });
      } else {
        const tx = ob.l + 14, y0 = c.b; let bot = y0;
        knot('exit', tx, y0, null, 3);
        branches.forEach((b, i) => { const n = nodes[i]; b.row = 0; line('br-' + b.k, elbowIn(tx, n.cy, n.l), '', b.tone); knot('br-' + b.k, n.l, n.cy, b.tone, 2.4);
          bot = Math.max(bot, n.cy - R); routes['br-' + b.k] = railTo(tx, y0, n.cy, n.l); });
        if (hcard) { const hb = box(hcard, lx), y1 = box($('.lx-hh', hcard), lx).cy;
          line('herm', elbowIn(tx, y1, hb.l), '', VIOLET); knot('herm', hb.l, y1, VIOLET, 2.4); bot = Math.max(bot, y1 - R); routes.herm = railTo(tx, y0, y1, hb.l); }
        line('trunk', `M${f(tx)},${f(y0)} V${f(bot)}`, 'trunk');
      }
      /* small trees under the blocks */
      branches.forEach((b, i) => { if (!b.subs.length) return; const n = nodes[i], sx = n.l + 17; let y = n.b, prev = null;
        b.subs.forEach(v => { const s = box(v.firstElementChild || v, lx), key = `sub-${b.k}-${v.dataset.k}`;
          if (prev && Math.abs(prev.cy - s.cy) < 3) line(key, `M${f(prev.r)},${f(s.cy)} H${f(s.l)}`, 'dash', b.tone);
          else { line(key, railTo(sx, y, s.cy, s.l), 'dash', b.tone); y = s.cy - R; }
          routes[key] = railTo(sx, n.b, s.cy, s.l); prev = s; }); });
      /* Hermes: a link from the assistant that passes under the memory rail, then a small tree of the agents on the computers */
      if (hcard) {
        if (wide) { const hb = box(hcard, lx), mb = box(mem, lx), y0 = z.cy + 8, y1 = box($('.lx-hh', hcard), lx).cy,
            xr = (c.r + mb.l) / 2, xh = Math.min(xr + 14, hb.l - R - 2), r = Math.max(1, Math.min(R, Math.abs(y1 - y0) / 2)), s = Math.sign(y1 - y0) || 1,
            d = `M${f(z.r)},${f(y0)} H${f(xh - r)} Q${f(xh)},${f(y0)} ${f(xh)},${f(y0 + s * r)} V${f(y1 - s * r)} Q${f(xh)},${f(y1)} ${f(xh + r)},${f(y1)} H${f(hb.l)}`;
          line('herm', d, '', VIOLET); knot('herm', z.r, y0, VIOLET, 2.2); knot('herm', hb.l, y1, VIOLET, 2.6, 1); routes.herm = d; }
        const ic = box($('.lx-hh .lx-ic', hcard), lx); let y = ic.b;
        hrows.forEach(r => { const q = box(r.el, lx), key = 'hr-' + r.k; line(key, railTo(ic.cx, y, q.cy, q.l), 'dash', VIOLET); routes[key] = railTo(ic.cx, ic.b, q.cy, q.l); y = q.cy - R; });
      }
      /* models into the agents that use them */
      if (!narrow) agents.forEach((a, i) => { const q = ag[i], n = a.uses.length;
        a.uses.forEach((m, j) => { if (!models[m]) return; const mb = box(models[m], lx), key = `web-${a.k}-${m}`, tone = glow(m); let d, x1, y1;
          if (wide) { x1 = q.l; y1 = q.cy + (j - (n - 1) / 2) * 7; d = hcurve(mb.r, mb.cy, x1, y1); knot('m-' + m, mb.r, mb.cy, tone, 2.2); }
          else { const x0 = mb.cx, y0 = mb.b + 3; x1 = q.cx + (j - (n - 1) / 2) * 12; y1 = q.t; const dy = y1 - y0; d = `M${f(x0)},${f(y0)} C${f(x0)},${f(y0 + dy * .55)} ${f(x1)},${f(y1 - dy * .55)} ${f(x1)},${f(y1)}`; }
          line(key, d, 'web', `color-mix(in srgb,${tone} 85%,#fff)`); routes[key] = d; knot(key, x1, y1, tone, 1.8); }); });
      /* memory: a rail on the right of the chain (wide) or short lines up from the bar (stacked) */
      const mb = box(mem, lx);
      if (wide) { const xr = (c.r + mb.l) / 2, yTop = mb.cy, yBot = Math.max(...MEM.map(k => ag[agents.indexOf(A[k])].cy));
        line('mem-rail', `M${f(xr)},${f(yBot - R)} V${f(yTop + R)} Q${f(xr)},${f(yTop)} ${f(xr + R)},${f(yTop)} H${f(mb.l)}`, 'dash', GREEN); knot('mem-rail', mb.l, yTop, GREEN, 2.4);
        MEM.forEach(k => { const q = ag[agents.indexOf(A[k])], key = 'mem-' + k;
          line(key, `M${f(q.r)},${f(q.cy)} H${f(xr - R)} Q${f(xr)},${f(q.cy)} ${f(xr)},${f(q.cy - R)}`, 'dash', GREEN); knot(key, q.r, q.cy, GREEN, 2.2);
          routes[key] = `M${f(mb.l)},${f(yTop)} H${f(xr + R)} Q${f(xr)},${f(yTop)} ${f(xr)},${f(yTop + R)} V${f(q.cy - R)} Q${f(xr)},${f(q.cy)} ${f(xr - R)},${f(q.cy)} H${f(q.r)}`; });
      } else if (!narrow) MEM.forEach(k => { const q = ag[agents.indexOf(A[k])], key = 'mem-' + k, d = `M${f(q.cx)},${f(mb.t)} V${f(q.b)}`;
        line(key, d, 'dash', GREEN); routes[key] = d; knot(key, q.cx, mb.t, GREEN, 2.2); });
      sweep(); relight();
    }

    /* signals: a short bright tail with a soft glow running along a route */
    const sigs = []; let now = 0;
    function signal(key, tone, ms = 900, done, reverse = false) {
      const d = routes[key]; if (!d) { if (done) done(); return; }
      const g = mk('g', {}, sigLayer), gl = mk('path', { d, class: 'lx-sig-glow' }, g), tail = mk('path', { d, class: 'lx-sig-tail' }, g), head = mk('circle', { r: 2.4, class: 'lx-sig-head' }, g), dot = mk('circle', { r: 1.6 }, g);
      gl.style.stroke = tail.style.stroke = head.style.fill = tone; dot.style.fill = '#fff';
      sigs.push({ g, glow: gl, tail, head, dot, key, d, len: tail.getTotalLength(), t0: now, ms, done, reverse, fired: false });
    }
    function drawSigs() {
      for (let i = sigs.length - 1; i >= 0; i--) {
        const s = sigs[i], d = routes[s.key];
        if (d && d !== s.d) { s.d = d; s.glow.setAttribute('d', d); s.tail.setAttribute('d', d); s.len = s.tail.getTotalLength(); }
        const raw = (now - s.t0) / s.ms, e = inOut(clamp(raw, 0, 1)), pos = s.reverse ? s.len * (1 - e) : s.len * e, trail = Math.min(64, s.len * .45);
        const a = s.reverse ? pos : Math.max(0, pos - trail), b = s.reverse ? Math.min(s.len, pos + trail) : pos;
        for (const p of [s.glow, s.tail]) { p.style.strokeDasharray = `${Math.max(.01, b - a)} ${s.len + 10}`; p.style.strokeDashoffset = -a; }
        const pt = s.tail.getPointAtLength(pos); for (const c of [s.head, s.dot]) { c.setAttribute('cx', pt.x); c.setAttribute('cy', pt.y); }
        if (raw >= 1 && !s.fired) { s.fired = true; if (s.done) s.done(); }
        if (raw >= 1) { const k = 1 - clamp((raw - 1) * s.ms / 300, 0, 1); s.g.style.opacity = k; if (k <= 0) { s.g.remove(); sigs.splice(i, 1); } }
      }
    }

    /* states */
    const say = (el, text) => { const b = el && (el.querySelector('b') || el); if (!b || b.textContent === text) return; b.style.opacity = 0; setTimeout(() => { b.textContent = text; b.style.opacity = 1; }, 170); };
    const status = (o, s, text) => { const st = o.st || o; st.dataset.s = s; say(st, text); };
    const stage = (text, s = 'run') => { stageEl.className = 'lx-stage ' + s; say(stageEl, text); };
    const hit = (el, ms = 1500) => { if (!el) return; el.classList.add('hit'); clearTimeout(el._h); el._h = setTimeout(() => el.classList.remove('hit'), ms); };
    const model = (keys, on = true) => keys.forEach(k => { if (models[k]) models[k].classList.toggle('on', on); light('m-' + k, on); });
    function agentOn(a, text) {
      a.el.classList.remove('ok'); a.el.classList.add('run'); status(a, 'run', text); model(a.uses);
      a.uses.forEach((m, j) => { light(`web-${a.k}-${m}`); setTimeout(() => signal(`web-${a.k}-${m}`, glow(m), 700), j * 110); });
      if (MEM.includes(a.k)) { mem.classList.add('on'); light('mem-' + a.k); light('mem-rail'); setTimeout(() => signal('mem-' + a.k, GREEN, mode === 'wide' ? 900 : 600), 250); }
    }
    function agentOk(a, text) { a.el.classList.remove('run'); a.el.classList.add('ok'); status(a, 'ok', text); model(a.uses, false);
      a.uses.forEach(m => light(`web-${a.k}-${m}`, false)); light('mem-' + a.k, false); light('mem-rail', false); }
    function to(k, s, text, ms = 900, tone) { const b = B[k]; if (!b) return; light('exit'); light('trunk'); if (b.row) light('trunk' + b.row); light('bus-' + b.row);
      signal('br-' + k, tone || b.tone, ms, () => { light('br-' + k); hit(b.node); status(b, s, text); }); }
    function sub(k, key, tone, reverse, done) { const b = B[k], v = b && b.subs.find(x => x.dataset.k === key); if (!v) return; light(`sub-${k}-${key}`);
      const fin = () => { v.classList.add('on'); if (done) done(); };
      if (reverse) { v.classList.add('on'); signal(`sub-${k}-${key}`, tone || b.tone, 700, done, true); } else signal(`sub-${k}-${key}`, tone || b.tone, 600, fin); }
    /* a task goes to the agent of a person on his computer */
    function agentPc(k, s, text) { const r = H[k]; if (!r) return; light('herm');
      signal('herm', VIOLET, 900, () => { hit(hcard); light('hr-' + k); signal('hr-' + k, VIOLET, 500, () => { r.el.classList.add('on'); status(r, s, text); }); }); }

    /* the recording: two voices, coral is the director, blue is Ivan */
    const barsEl = $('#lx-bars'), bars = [];
    if (barsEl) { let seed = 11; const rnd = () => (seed = seed * 16807 % 2147483647) / 2147483647;
      for (let i = 0; i < 44; i++) { const x = i / 44, e = document.createElement('i'), blue = (x > .22 && x < .36) || (x > .58 && x < .72) || x > .9; if (blue) e.className = 'b';
        barsEl.appendChild(e); bars.push({ e, a: .25 + .75 * Math.pow(rnd(), .7), p: rnd() * 6.3, v: 2 + rnd() * 3 }); } }
    let live = true, loop = 0;

    const STEPS = [
      [0, () => { live = true; status(recSt, 'run', 'запись'); stage('слушает планёрку'); }],
      [400, () => { const i = loop % srcs.length, s = srcs[i]; if (!s) return; hit(s, 1700); light('f-' + i); light('bus'); light('drop'); signal('src-' + i, BLUE, 1300); }],
      [2000, () => { live = false; status(recSt, 'ok', 'записано'); hit(rec, 1300); light('entry');
        signal('rec', CORAL, mode === 'wide' ? 800 : 600, () => { core.classList.add('on'); agentOn(A.ear, 'слушает'); stage('Слух: два движка сверяют текст'); }); }],
      [4900, () => { agentOk(A.ear, 'текст'); light('chain-0'); light('chain-0v'); signal('chain-0', VIOLET, 450, () => { agentOn(A.who, 'узнаёт'); stage('Кто говорит: голоса из памяти'); }); }],
      [6500, () => { agentOk(A.who, '2 голоса'); light('chain-1'); light('chain-1v'); signal('chain-1', VIOLET, 450, () => { agentOn(A.sec, 'пишет'); stage('Секретарь: протокол и задачи'); }); }],
      [8100, () => { agentOk(A.sec, 'протокол'); light('chain-2'); light('chain-2v'); signal('chain-2', VIOLET, 450, () => { agentOn(A.ast, 'в работе'); stage('Ассистент: руководителю на проверку'); }); }],
      [8900, () => { light('exit'); signal('exit', AMBER, 320, () => to('boss', 'run', 'на проверке', 1000)); }],
      [10300, () => sub('boss', 'dec')], [10550, () => sub('boss', 'tasks')], [10800, () => sub('boss', 'risk')],
      [11500, () => { status(B.boss, 'ok', 'одобрен'); hit(B.boss.node); stage('Ассистент: рассылает задачи'); }],
      [11800, () => { [['ivan', 'sent', 'доставлено'], ['maria', 'run', 'уточняет'], ['b24', 'sent', 'создано'], ['chat', 'sent', 'отправлено'], ['cal', 'sent', 'в календаре'], ['drive', 'sent', 'сохранено']]
          .forEach(([k, s, t], i) => setTimeout(() => to(k, s, t, 1000), i * 170)); }],
      [12600, () => { agentPc('ivan', 'run', 'взял задачу'); stage('Ассистент: задача — агенту Ивана'); }],
      [13900, () => sub('maria', 'ask', BLUE)],
      [15200, () => sub('maria', 'reply', CORAL, true, () => { status(B.maria, 'ok', 'срок пт'); hit(B.maria.node); })],
      [15700, () => { agentPc('maria', 'sent', 'срок в плане'); stage('Ассистент: срок в Битрикс24'); }],
      [16000, () => to('b24', 'sent', 'срок обновлён', 900, BLUE)],
      [16800, () => { sub('ivan', 'remind', CORAL); status(H.ivan, 'run', 'в работе'); }],
      [17700, () => { agentOk(A.ast, 'готово'); mem.classList.remove('on'); stage('готово · 7 получателей · 2 агента', 'ok'); }]
    ];
    const LOOP = 20500;
    function reset() {
      sigs.splice(0).forEach(s => s.g.remove()); lit.clear(); relight(); core.classList.remove('on'); mem.classList.remove('on'); model(Object.keys(models), false);
      agents.forEach(a => { a.el.classList.remove('run', 'ok'); status(a, 'wait', 'ждёт'); });
      branches.forEach(b => { status(b, 'wait', b.k === 'boss' ? 'готовится' : 'ждёт'); b.node.classList.remove('hit'); b.subs.forEach(v => v.classList.remove('on')); });
      hrows.forEach(r => { r.el.classList.remove('on'); status(r, 'wait', 'ждёт'); }); if (hcard) hcard.classList.remove('hit');
      status(recSt, 'run', 'запись');
    }
    let t0 = 0, next = 0, started = false;
    function tick() {
      const t = now - t0;
      while (next < STEPS.length && STEPS[next][0] <= t) STEPS[next++][1]();
      if (t > LOOP) { reset(); loop++; t0 = now; next = 0; }
      const s = now / 1000; bars.forEach(b => { const h = live ? b.a * (.3 + .7 * Math.abs(Math.sin(s * b.v + b.p))) : b.a * .72; b.e.style.transform = `scaleY(${Math.max(.08, h).toFixed(3)})`; b.e.style.opacity = live ? .95 : .55; });
    }

    let pending = 0; const relayout = () => { if (!pending) pending = requestAnimationFrame(() => { pending = 0; layout(); }); };
    layout(); if ('ResizeObserver' in window) { const ro = new ResizeObserver(relayout); [lx, core, outCol, hcard].forEach(el => el && ro.observe(el)); } else addEventListener('resize', relayout);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
    if (reduced) {
      agents.forEach(a => { a.el.classList.add('ok'); status(a, 'ok', 'готово'); }); branches.forEach(b => { status(b, b.k === 'boss' ? 'ok' : 'sent', b.k === 'boss' ? 'одобрен' : 'готово'); b.subs.forEach(v => v.classList.add('on')); });
      hrows.forEach(r => { r.el.classList.add('on'); status(r, 'ok', r.k === 'ivan' ? 'в работе' : 'срок в плане'); });
      status(recSt, 'ok', 'записано'); stage('готово · 7 получателей · 2 агента', 'ok'); bars.forEach(b => { b.e.style.transform = `scaleY(${(b.a * .72).toFixed(3)})`; });
      for (const k in lines) light(k); for (const id in knots) knots[id].classList.add('lit'); return;
    }
    let raf = 0, vis = false, last = 0;
    function frame() { const ts = performance.now(); now += Math.min(64, ts - last); last = ts; tick(); drawSigs(); raf = vis && !document.hidden ? requestAnimationFrame(frame) : 0; }
    const run = () => { if (vis && !document.hidden && !raf) { last = performance.now(); raf = requestAnimationFrame(frame); } };
    watch(lx, v => { vis = v; if (v && !started) { started = true; t0 = now + 300; } run(); }, .15);
    document.addEventListener('visibilitychange', run);
  })();

  /* ---------- 3. the steps: the small trees light up one branch after another ---------- */
  const stepsEl = $('#lx-steps');
  if (stepsEl) { const items = $$('.tree li', stepsEl);
    if (reduced) items.forEach(li => li.classList.add('on'));
    else { let k = 0, iv = 0; const run = () => { items.forEach((li, i) => li.classList.toggle('on', i < k)); k = k >= items.length + 3 ? 0 : k + 1; };
      watch(stepsEl, v => { clearInterval(iv); if (v) iv = setInterval(run, 520); }, .3); } }
})();
