/* SYSAI landing: the live voice under the wordmark, the pixel waveform of the record, and the scheme from a record to tasks.
   The scheme lines are bezier bundles measured from the real blocks; they are drawn while the page scrolls, and every signal
   is a small pixel sound wave running along a line, in the same pixel style as the voice. Vanilla JS, no dependencies. */
(() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v)), fract = v => v - Math.floor(v);
  const smooth = x => x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);
  const inOut = x => x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
  const watch = (el, cb, threshold = .1) => { if ('IntersectionObserver' in window) new IntersectionObserver(([en]) => cb(en.isIntersecting), { threshold }).observe(el); else cb(true); };
  const bg = $('#l-bg');

  /* ---------- 1. the voice under the wordmark: a timeline of two speakers scrolls through a grid of pixels ---------- */
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
      if (bg) { const w = cur ? cur.who : ''; if ((bg.dataset.spk || '') !== w) bg.dataset.spk = w; } /* the stage light follows the speaker */
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

  /* ---------- 2. the record: a matrix of pixels fills while the meeting is recorded, coral is the director, blue is Ivan ---------- */
  function recWave(cv) {
    const ctx = cv && cv.getContext('2d'); if (!ctx) return null;
    const COL = { d: [248, 120, 138], i: [115, 167, 255] }, SEG = [[0, 'd'], [.21, 'i'], [.35, 'd'], [.57, 'i'], [.71, 'd'], [.89, 'i']];
    const spk = x => { let w = 'd'; for (const [a, k] of SEG) if (x >= a) w = k; return w; };
    let W = 0, H = 0, cell = 3, cols = 0, rows = 0, oy = 0, amp = [], who = [];
    function build() {
      const r = cv.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2); W = r.width; H = r.height; if (!W || !H) return;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cols = Math.floor(W / cell); rows = Math.floor(H / cell); oy = (H - rows * cell) / 2; amp = []; who = [];
      let seed = 7; const rnd = () => (seed = seed * 16807 % 2147483647) / 2147483647;
      for (let c = 0; c < cols;) { /* words of a few columns with short pauses between them */
        const n = 3 + Math.floor(rnd() * 7), peak = .35 + .65 * rnd();
        for (let j = 0; j < n && c < cols; j++, c++) { amp[c] = peak * (.45 + .55 * Math.sin(Math.PI * (j + .5) / n)) * (.7 + .3 * rnd()); who[c] = spk(c / cols); }
        for (let j = rnd() < .3 ? 2 : 1; j > 0 && c < cols; j--, c++) { amp[c] = 0; who[c] = spk(c / cols); }
      }
      SEG.forEach(([a]) => { const c0 = Math.round(a * cols); for (let k = -2; k <= 1; k++) if (amp[c0 + k] != null) amp[c0 + k] = 0; });
    }
    function draw(fill, live, scan, t) {
      if (!W) return; ctx.clearRect(0, 0, W, H);
      const mid = H / 2, head = Math.floor(fill * cols);
      for (let c = 0; c < cols; c++) {
        const x = c * cell + cell / 2;
        if (c >= head || amp[c] <= 0) { if (c % 2 === 0) { ctx.fillStyle = c >= head ? 'rgba(240,238,230,.13)' : 'rgba(240,238,230,.08)'; ctx.fillRect(x - .6, mid - .6, 1.2, 1.2); } continue; }
        const rgb = COL[who[c]], near = live ? Math.exp(-Math.pow((head - c) / 6, 2)) : 0, sc = scan >= 0 ? Math.exp(-Math.pow((c - scan * cols) / 7, 2)) : 0;
        let a0 = amp[c]; if (near > .03) a0 = clamp(a0 * (.65 + .6 * Math.abs(Math.sin(t * 13 + c * 1.3))), 0, 1);
        const A = Math.max(cell * .6, a0 * (H / 2 - 1));
        for (let r = 0; r < rows; r++) {
          const y = oy + r * cell + cell / 2, rel = Math.abs(y - mid) / A; if (rel > 1) continue;
          const edge = 1 - rel, a = clamp(.26 + .5 * edge + .35 * near + .5 * sc, .05, 1), s = cell * (.56 + .24 * edge);
          ctx.fillStyle = `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a.toFixed(3)})`; ctx.fillRect(x - s / 2, y - s / 2, s, s);
        }
      }
      if (live && head < cols) { ctx.fillStyle = 'rgba(240,238,230,.75)'; ctx.fillRect(head * cell, 3, 1, H - 6); }
    }
    return { build, draw };
  }

  /* ---------- 3. the scheme: a request comes in at the top and flows down ---------- */
  const lx = $('#lx'), core = $('#lx-core');
  if (lx && core) (() => {
    const svg = $('#lx-svg'), stageEl = $('#lx-stage'), defs = mk('defs', {}, svg);
    const gBase = mk('g', {}, svg), gOv = mk('g', {}, svg), gKn = mk('g', {}, svg), gSig = mk('g', {}, svg);
    const CORAL = 'var(--lx-coral)', BLUE = 'var(--lx-blue)', VIOLET = 'var(--lx-violet)', AMBER = 'var(--lx-amber)', GREEN = 'var(--lx-green)';
    const GLOW = { gemini: '#6f9bff', openai: '#e9e7e0', claude: '#d97757', mistral: '#ff8a1f', eleven: '#f0eee6' };
    const rec = $('#lx-rec'), recSt = $('.lx-st', rec), srcs = $$('.lx-src', lx), mem = $('#lx-mem'), herm = $('#lx-herm'), hh = $('.lx-hh', herm);
    const hrows = $$('.lx-hrow', herm).map(el => ({ el, k: el.dataset.k, st: $('.lx-st', el) })), HR = Object.fromEntries(hrows.map(r => [r.k, r]));
    const models = {}; $$('[data-m]', lx).forEach(el => (models[el.dataset.m] = models[el.dataset.m] || []).push(el));
    const modelRow = Object.fromEntries($$('.lx-m', lx).map(m => [m.dataset.m, m]));
    const agents = $$('.lx-ag', core).map(el => ({ el, k: el.dataset.a, st: $('.lx-st', el), ic: $('.lx-ic', el), uses: (el.dataset.uses || '').split(' ').filter(Boolean) }));
    const A = Object.fromEntries(agents.map(a => [a.k, a])), MEM = ['who', 'sec', 'ast'];
    const branches = $$('.lx-br', lx).map(el => ({ el, k: el.dataset.k, node: $('.lx-node', el), ic: $('.lx-node .lx-ic', el), st: $('.lx-node .lx-st', el),
      subs: $$('.lx-sub', el), side: el.closest('.lx-out').dataset.side, tone: el.dataset.tone === 'b' ? BLUE : CORAL }));
    const B = Object.fromEntries(branches.map(b => [b.k, b]));
    const RIBBON = ['boss', 'ivan', 'maria', 'drive', 'cal', 'chat', 'b24']; /* left to right: the top rows peel off first, so no strand crosses another */
    const wave = recWave($('#lx-wave'));
    const f = v => v.toFixed(1);
    /* boxes from offsets: they ignore the transforms of the reveal animation */
    const box = el => { let l = 0, t = 0, e = el; while (e && e !== lx) { l += e.offsetLeft; t += e.offsetTop; const p = e.offsetParent; if (p && p !== lx) { l += p.clientLeft; t += p.clientTop; } e = p; }
      const w = el.offsetWidth, h = el.offsetHeight; return { l, t, r: l + w, b: t + h, w, h, cx: l + w / 2, cy: t + h / 2 }; };

    /* lines: a hairline base drawn on scroll, an overlay colored by the signals, a pixel knot on every end */
    const L = {}, alias = {}, seen = new Set();
    const get = key => L[alias[key] || key];
    function route(key, d, tone, o = {}) {
      let l = L[key];
      if (!l) { l = L[key] = { key, prog: 0, ov: null, base: mk('path', { class: 'lx-ln' }, gBase), k0: mk('rect', { class: 'lx-kn', width: 3, height: 3 }, gKn), k1: mk('rect', { class: 'lx-kn', width: 3, height: 3 }, gKn) }; }
      l.tone = tone; l.hidden = !!o.hidden; l.base.style.visibility = o.hidden ? 'hidden' : '';
      for (const k of [l.k0, l.k1]) k.style.setProperty('--tone', tone);
      l.k0.style.display = o.hidden || o.k0 === false ? 'none' : ''; l.k1.style.display = o.hidden || o.k1 === false ? 'none' : '';
      if (l.d !== d) {
        l.d = d; l.base.setAttribute('d', d); if (l.ov) l.ov.setAttribute('d', d); l.lut = null;
        l.len = l.base.getTotalLength(); const bb = l.base.getBBox(); l.y0 = bb.y; l.y1 = bb.y + bb.height;
        const p0 = l.base.getPointAtLength(0), p1 = l.base.getPointAtLength(l.len);
        l.k0.setAttribute('x', f(p0.x - 1.5)); l.k0.setAttribute('y', f(p0.y - 1.5)); l.k1.setAttribute('x', f(p1.x - 1.5)); l.k1.setAttribute('y', f(p1.y - 1.5));
        paint(l); if (l.lit) lit(l, l.lit[0], l.lit[1]);
      }
      seen.add(key); return l;
    }
    function paint(l) {
      const p = l.prog;
      if (p >= .999) { l.base.style.strokeDasharray = ''; l.base.style.strokeDashoffset = ''; }
      else { l.base.style.strokeDasharray = `${f(l.len)} ${f(l.len + 4)}`; l.base.style.strokeDashoffset = f(l.len * (1 - p)); }
      l.k0.classList.toggle('in', p > .01); l.k1.classList.toggle('in', p > .985);
    }
    const grow = (l, p) => { if (p > l.prog + 1e-4) { l.prog = Math.min(1, p); if (!l.hidden) paint(l); } };
    function lit(l, a, b, tone) { /* the colored part between the lengths a and b */
      if (!l.ov) l.ov = mk('path', { class: 'lx-ov off', d: l.d }, gOv);
      if (tone) l.ov.style.setProperty('--tone', tone); else if (!l.ov.style.getPropertyValue('--tone')) l.ov.style.setProperty('--tone', l.tone);
      a = clamp(a, 0, l.len); b = clamp(b, a, l.len); l.lit = [a, b];
      l.ov.style.strokeDasharray = `0 ${f(a)} ${f(b - a)} ${f(l.len + 4)}`; l.ov.classList.remove('off');
    }
    function unlit(l) { if (l && l.ov) { l.ov.classList.add('off'); l.lit = null; } if (l) { l.k0.classList.remove('lit'); l.k1.classList.remove('lit'); } }
    function sweep() {
      for (const k in L) if (!seen.has(k)) { const l = L[k]; l.base.remove(); if (l.ov) l.ov.remove(); l.k0.remove(); l.k1.remove(); delete L[k]; }
      seen.clear();
    }

    const vS = (x0, y0, x1, y1, k = .55) => { const dy = y1 - y0; return `M${f(x0)},${f(y0)} C${f(x0)},${f(y0 + dy * k)} ${f(x1)},${f(y1 - dy * k)} ${f(x1)},${f(y1)}`; };
    const hS = (x0, y0, x1, y1, k = .5) => { const dx = x1 - x0; return `M${f(x0)},${f(y0)} C${f(x0 + dx * k)},${f(y0)} ${f(x1 - dx * k)},${f(y1)} ${f(x1)},${f(y1)}`; };
    /* a strand of the ribbon: straight down, then a long soft turn into the side of a block */
    const peel = (x, y0, x1, y1, D) => { D = clamp(D, 8, y1 - y0); return `M${f(x)},${f(y0)} V${f(y1 - D)} C${f(x)},${f(y1 - D * .36)} ${f(x + (x1 - x) * .4)},${f(y1)} ${f(x1)},${f(y1)}`; };
    const rail = (x, y0, x1, y1, r = 10) => `M${f(x)},${f(y0)} V${f(y1 - r)} Q${f(x)},${f(y1)} ${f(x + r)},${f(y1)} H${f(x1)}`;

    let mode = '';
    function trees() {
      branches.forEach(b => { if (!b.subs.length) return; const ib = box(b.ic); let prev = null, y = ib.b + 1;
        b.subs.forEach(v => { const s = box(v), key = `sub-${b.k}-${v.dataset.k}`;
          if (prev && Math.abs(prev.cy - s.cy) < 3) route(key, `M${f(prev.r)},${f(s.cy)} H${f(s.l)}`, b.tone, { k0: false });
          else { route(key, rail(ib.cx, y, s.l, s.cy, 8), b.tone, { k0: false }); y = s.cy - 8; }
          prev = s; }); });
      const ic = box($('.lx-ic', hh)); let y = ic.b + 1;
      hrows.forEach(r => { const q = box(r.el); route('hr-' + r.k, rail(ic.cx, y, q.l, q.cy, 8), VIOLET, { k0: false }); y = q.cy - 8; });
    }
    function layout() {
      svg.setAttribute('viewBox', `0 0 ${lx.clientWidth} ${lx.clientHeight}`);
      for (const k in alias) delete alias[k];
      mode = getComputedStyle(lx).display === 'grid' ? 'wide' : 'narrow';
      const rb = box(rec), cb = box(core), cx = Math.round(rb.cx) + .5, ag = agents.map(a => box(a.el)), ic = agents.map(a => box(a.ic));
      /* five sources bundle into the record */
      srcs.forEach((s, i) => { const b = box(s); route('src-' + i, vS(b.cx, b.b + (mode === 'wide' ? 0 : 4), cx + (i - 2) * 5, rb.t), BLUE); });
      /* two voices go down into the core */
      route('rec-d', `M${f(cx - 3)},${f(rb.b)} V${f(cb.t)}`, CORAL); route('rec-i', `M${f(cx + 3)},${f(rb.b)} V${f(cb.t)}`, BLUE);
      /* the chain of agents inside the core */
      for (let i = 1; i < ic.length; i++) route('chain-' + (i - 1), `M${f(ic[i - 1].cx)},${f(ic[i - 1].b + 4)} V${f(ic[i].t - 4)}`, VIOLET, { k0: false, k1: false });
      if (mode === 'wide') {
        /* models cross over to the agents that use them */
        agents.forEach((a, i) => { const n = a.uses.length; a.uses.forEach((m, j) => { const mb = box(modelRow[m]);
          route(`web-${a.k}-${m}`, hS(mb.r, mb.cy, ag[i].l, ag[i].cy + (j - (n - 1) / 2) * 5), GLOW[m]); }); });
        /* memory and Hermes on the right */
        const mi = box($('.lx-ic', mem)), mb = box(mem), hb = box(herm), hc = box($('.lx-ic', hh));
        MEM.forEach((k, j) => { const q = ag[agents.indexOf(A[k])]; route('mem-' + k, hS(mb.l, mi.cy + (j - 1) * 5, q.r, q.cy + (k === 'ast' ? -4 : 0)), GREEN); });
        const q = ag[agents.length - 1]; route('herm', hS(q.r, q.cy + 4, hb.l, hc.cy), VIOLET);
        /* the ribbon: seven strands leave the core and peel off to the rows on both sides */
        RIBBON.forEach((k, idx) => { const b = B[k], n = box(b.node), x = cx + (idx - 3) * 6;
          route('br-' + k, peel(x, cb.b, b.side === 'l' ? n.r : n.l, n.cy, 92), b.tone); });
      } else {
        /* one rail on the left: memory, Hermes and every output hang on it */
        const rx = 14.5, y0 = cb.b, items = [['mem', mem, box($('.lx-ic', mem)).cy, GREEN], ['herm', herm, box(hh).cy, VIOLET], ...branches.map(b => ['br-' + b.k, b.node, box(b.node).cy, b.tone])];
        let bot = y0;
        items.forEach(([key, el, ay, tone]) => { const e = box(el); route(key, rail(rx, y0, e.l, ay, 12), tone, { hidden: true }); route('stub-' + key, rail(rx, ay - 24, e.l, ay, 12), tone, { k0: false }); bot = Math.max(bot, ay - 24); });
        route('rail', `M${f(rx)},${f(y0)} V${f(bot)}`, 'rgba(240,238,230,.5)', { k1: false });
        MEM.forEach(k => { alias['mem-' + k] = 'mem'; });
      }
      trees(); sweep(); measure(); reveal(true);
    }

    /* scroll: the front runs a bit above the bottom of the screen, lines grow behind it and never shrink back */
    const rv = $$('.lx-rv'), rvTop = new Map(); let lxTop = 0, F = -1e9;
    function measure() { const sy = scrollY; lxTop = lx.getBoundingClientRect().top + sy; rv.forEach(el => rvTop.set(el, el.getBoundingClientRect().top + sy)); }
    function reveal(instant) {
      const fr = scrollY + innerHeight * .86; if (fr > F) F = fr; const fl = F - lxTop;
      for (const k in L) { const l = L[k]; if (l.prog < 1) grow(l, clamp((fl - l.y0) / Math.max(l.y1 - l.y0, 150), 0, 1)); }
      rv.forEach(el => { if (!el.classList.contains('in') && F > rvTop.get(el) + 24) { el.classList.add('in'); if (instant) el.classList.add('now'); } });
    }

    /* signals: a short pixel sound wave runs along a line; the line takes its color behind it */
    const grads = {};
    function gradFor(tone) { if (grads[tone]) return grads[tone]; const id = 'lxg' + Object.keys(grads).length, g = mk('radialGradient', { id }, defs);
      const s0 = mk('stop', { offset: 0 }, g), s1 = mk('stop', { offset: 1 }, g); s0.style.stopColor = s1.style.stopColor = tone; s0.style.stopOpacity = .45; s1.style.stopOpacity = 0; return (grads[tone] = id); }
    const sigs = []; let now = 0;
    function signal(key, tone, ms = 900, done, reverse = false) {
      const l = get(key); if (!l || !l.len) { if (done) done(); return; }
      const g = mk('g', { class: 'lx-sig' }, gSig); g.style.color = tone;
      const glow = mk('circle', { r: 15, fill: `url(#${gradFor(tone)})` }, g);
      sigs.push({ l, g, glow, p3: mk('path', { class: 'p3' }, g), p2: mk('path', { class: 'p2' }, g), p1: mk('path', { class: 'p1' }, g), tone, t0: now, ms, done, reverse, fired: false, seed: Math.random() * 6.3 });
    }
    function lutOf(l) { if (l.lut) return l.lut; const n = Math.max(2, Math.ceil(l.len / 2)), a = new Float32Array((n + 1) * 2);
      for (let i = 0; i <= n; i++) { const p = l.base.getPointAtLength(l.len * i / n); a[2 * i] = p.x; a[2 * i + 1] = p.y; } return (l.lut = { n, a }); }
    function at(l, s) { const { n, a } = lutOf(l), u = clamp(s / l.len, 0, 1) * n, i = Math.min(n - 1, Math.floor(u)), k = u - i;
      const x0 = a[2 * i], y0 = a[2 * i + 1], x1 = a[2 * i + 2], y1 = a[2 * i + 3], m = Math.hypot(x1 - x0, y1 - y0) || 1;
      return [x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, -(y1 - y0) / m, (x1 - x0) / m]; }
    const CELL = 3, BODY = 27, TAIL = 30, AMP = 7.4;
    const sq = (x, y, s) => `M${f(x - s / 2)} ${f(y - s / 2)}h${s}v${s}h-${s}z`;
    function drawSigs() {
      const ts = now / 1000;
      for (let i = sigs.length - 1; i >= 0; i--) {
        const s = sigs[i], l = s.l;
        if (L[l.key] !== l) { s.g.remove(); sigs.splice(i, 1); if (!s.fired && s.done) s.done(); continue; }
        const raw = (now - s.t0) / s.ms, e = inOut(clamp(raw, 0, 1)), dir = s.reverse ? -1 : 1, pos = s.reverse ? l.len * (1 - e) : l.len * e;
        /* a signal never runs ahead of the scroll: past the drawn part of its line it fades out */
        const lim = l.prog * l.len, vis = clamp(1 - (pos - lim) / 24, 0, 1), tf = raw > 1 ? 1 - clamp((raw - 1) * s.ms / 260, 0, 1) : 1, fade = tf * vis;
        if (raw <= 1) { if (s.reverse) { if (pos < lim) lit(l, pos, lim, s.tone); } else if (lim > 0) lit(l, 0, Math.min(pos, lim), s.tone); }
        const cells = new Map(), put = (x, y, lv) => { const id = (Math.round(y / CELL) + 64) * 8192 + Math.round(x / CELL) + 512; if ((cells.get(id) || 0) < lv) cells.set(id, lv); };
        for (let u = 0; u <= BODY; u += 2) {
          const a = pos - dir * u; if (a < 0 || a > lim) continue;
          const [x, y, nx, ny] = at(l, a), env = Math.pow(Math.sin(Math.PI * u / BODY), 1.3);
          const osc = .3 + .7 * Math.abs(Math.sin(a * .36 + ts * 11 + s.seed)) * (.75 + .25 * Math.sin(a * .11 - ts * 4));
          const K = Math.floor(env * osc * AMP * fade / CELL + .45), core = Math.max(1, K * .5);
          for (let k = -K; k <= K; k++) put(x + nx * k * CELL, y + ny * k * CELL, Math.abs(k) < core ? 3 : 2);
        }
        for (let u = BODY; u <= BODY + TAIL; u += CELL) { const a = pos - dir * u; if (a < 0 || a > lim) continue;
          if (fract(Math.sin(Math.floor(a / CELL) * 12.9898 + Math.floor(ts * 14) * 78.233) * 43758.5453) < (1 - (u - BODY) / TAIL) * fade) { const [x, y] = at(l, a); put(x, y, 1); } }
        let d1 = '', d2 = '', d3 = '';
        for (const [id, lv] of cells) { const gx = id % 8192 - 512, gy = Math.floor(id / 8192) - 64, x = gx * CELL, y = gy * CELL;
          if (lv === 3) d1 += sq(x, y, 2.4); else if (lv === 2) d2 += sq(x, y, 2.1); else d3 += sq(x, y, 1.8); }
        s.p1.setAttribute('d', d1); s.p2.setAttribute('d', d2); s.p3.setAttribute('d', d3);
        const [hx, hy] = at(l, pos); s.glow.setAttribute('cx', f(hx)); s.glow.setAttribute('cy', f(hy)); s.glow.setAttribute('opacity', fade.toFixed(2));
        if (raw >= 1 && !s.fired) { s.fired = true; if (vis > 0) (s.reverse ? l.k0 : l.k1).classList.add('lit'); if (s.done) s.done(); }
        if (tf <= 0) { s.g.remove(); sigs.splice(i, 1); }
      }
    }

    /* states */
    const say = (el, text) => { const b = el && (el.querySelector('b') || el); if (!b || b.textContent === text) return; b.style.opacity = 0; setTimeout(() => { b.textContent = text; b.style.opacity = 1; }, 170); };
    const status = (o, s, text) => { const st = o.st || o; st.dataset.s = s; say(st, text); };
    const stage = (text, s = 'run') => { stageEl.className = 'lx-stage ' + s; say(stageEl, text); };
    const hit = (el, ms = 1500) => { if (!el) return; el.classList.add('hit'); clearTimeout(el._h); el._h = setTimeout(() => el.classList.remove('hit'), ms); };
    const model = (keys, on = true) => keys.forEach(k => (models[k] || []).forEach(el => el.classList.toggle('on', on)));
    function agentOn(a, text) {
      a.el.classList.remove('ok'); a.el.classList.add('run'); status(a, 'run', text); model(a.uses);
      a.uses.forEach((m, j) => setTimeout(() => signal(`web-${a.k}-${m}`, GLOW[m], 750), j * 120));
      if (MEM.includes(a.k)) { mem.classList.add('on'); setTimeout(() => signal('mem-' + a.k, GREEN, mode === 'wide' ? 850 : 650, null, mode === 'wide'), 260); }
    }
    function agentOk(a, text) { a.el.classList.remove('run'); a.el.classList.add('ok'); status(a, 'ok', text); model(a.uses, false);
      a.uses.forEach(m => unlit(get(`web-${a.k}-${m}`))); if (mode === 'wide') unlit(get('mem-' + a.k)); }
    function to(k, s, text, ms = 1200, tone) { const b = B[k]; if (!b) return; const go = () => { hit(b.node); status(b, s, text); };
      signal('br-' + k, tone || b.tone, ms, go); }
    function sub(k, key, tone, reverse, done) { const b = B[k], v = b && b.subs.find(x => x.dataset.k === key); if (!v) return;
      if (reverse) { v.classList.add('on'); signal(`sub-${k}-${key}`, tone || b.tone, 700, done, true); } else signal(`sub-${k}-${key}`, tone || b.tone, 600, () => { v.classList.add('on'); if (done) done(); }); }
    function agentPc(k, s, text) { const r = HR[k]; if (!r) return;
      signal('herm', VIOLET, 900, () => { hit(herm); signal('hr-' + k, VIOLET, 500, () => { r.el.classList.add('on'); status(r, s, text); }); }); }

    let live = false, scan = -1, scanT0 = 0, loop = 0, recT0 = 0;
    const STEPS = [
      [0, () => { live = true; recT0 = now; status(recSt, 'run', 'запись'); stage('слушает планёрку'); }],
      [300, () => { const i = loop % srcs.length, s = srcs[i]; if (!s) return; hit(s, 1900); signal('src-' + i, BLUE, 1500, () => hit(rec, 1200)); }],
      [2100, () => { live = false; scan = 0; scanT0 = now; status(recSt, 'ok', 'записано'); signal('rec-d', CORAL, 900);
        signal('rec-i', BLUE, 900, () => { core.classList.add('on'); agentOn(A.ear, 'слушает'); stage('Слух: сверяет два движка'); }); }],
      [4800, () => { agentOk(A.ear, 'текст'); signal('chain-0', VIOLET, 520, () => { agentOn(A.who, 'узнаёт'); stage('Кто говорит: голоса из памяти'); }); }],
      [6400, () => { agentOk(A.who, '2 голоса'); signal('chain-1', VIOLET, 520, () => { agentOn(A.sec, 'пишет'); stage('Секретарь: протокол и задачи'); }); }],
      [8000, () => { agentOk(A.sec, 'протокол'); signal('chain-2', VIOLET, 520, () => { agentOn(A.ast, 'в работе'); stage('Ассистент: протокол на проверку'); }); }],
      [8800, () => to('boss', 'run', 'на проверке', 1300)],
      [10400, () => sub('boss', 'dec')], [10650, () => sub('boss', 'tasks')], [10900, () => sub('boss', 'risk')],
      [11600, () => { status(B.boss, 'ok', 'одобрен'); hit(B.boss.node); stage('Ассистент: рассылает задачи'); }],
      [11900, () => { [['ivan', 'sent', 'доставлено'], ['maria', 'run', 'уточняет'], ['b24', 'sent', 'создано'], ['chat', 'sent', 'отправлено'], ['cal', 'sent', 'в календаре'], ['drive', 'sent', 'сохранено']]
        .forEach(([k, s, t], i) => setTimeout(() => to(k, s, t, 1250), i * 170)); }],
      [12900, () => { agentPc('ivan', 'run', 'взял задачу'); stage('Ассистент: задача агенту Ивана'); }],
      [14100, () => sub('maria', 'ask', BLUE)],
      [15400, () => sub('maria', 'reply', CORAL, true, () => { status(B.maria, 'ok', 'срок пт'); hit(B.maria.node); })],
      [15900, () => { agentPc('maria', 'sent', 'срок в плане'); stage('Ассистент: срок в Битрикс24'); }],
      [16200, () => to('b24', 'sent', 'срок обновлён', 1100, BLUE)],
      [17000, () => { sub('ivan', 'remind', CORAL); status(HR.ivan, 'run', 'в работе'); }],
      [17900, () => { agentOk(A.ast, 'готово'); mem.classList.remove('on'); stage('готово · 7 получателей · 2 агента', 'ok'); }]
    ];
    const LOOP = 22100; /* the full picture holds for about four seconds, then the story starts again */
    function reset() {
      sigs.splice(0).forEach(s => s.g.remove()); for (const k in L) unlit(L[k]);
      core.classList.remove('on'); mem.classList.remove('on'); model(Object.keys(models), false);
      agents.forEach(a => { a.el.classList.remove('run', 'ok'); status(a, 'wait', 'ждёт'); });
      branches.forEach(b => { status(b, 'wait', b.k === 'boss' ? 'готовится' : 'ждёт'); b.node.classList.remove('hit'); b.subs.forEach(v => v.classList.remove('on')); });
      hrows.forEach(r => { r.el.classList.remove('on'); status(r, 'wait', 'ждёт'); }); herm.classList.remove('hit');
      status(recSt, 'run', 'запись'); stage('ждёт запись', '');
    }
    let t0 = 0, next = 0, started = false, waveDirty = true;
    function tick() {
      const t = now - t0; if (t < 0) return;
      while (next < STEPS.length && STEPS[next][0] <= t) STEPS[next++][1]();
      if (wave) {
        if (scan >= 0) { scan = (now - scanT0) / 900; if (scan > 1.2) { scan = -1; waveDirty = true; } }
        if (live || scan >= 0 || waveDirty) { wave.draw(live ? clamp((now - recT0) / 2000, 0, 1) : 1, live, scan, now / 1000); waveDirty = false; }
      }
      if (t > LOOP) { reset(); loop++; t0 = now; next = 0; }
    }

    let pending = 0; const relayout = () => { if (!pending) pending = requestAnimationFrame(() => { pending = 0; layout(); if (wave) { wave.build(); waveDirty = true; if (reduced || !started) wave.draw(started || reduced ? 1 : 0, false, -1, 0); } }); };
    if (wave) { wave.build(); wave.draw(reduced ? 1 : 0, false, -1, 0); }
    layout();
    if (!reduced) document.documentElement.classList.add('lx-armed');
    let sp = 0; addEventListener('scroll', () => { if (!sp) sp = requestAnimationFrame(() => { sp = 0; reveal(false); }); }, { passive: true });
    if ('ResizeObserver' in window) { const ro = new ResizeObserver(relayout); [lx, core].forEach(el => ro.observe(el)); } else addEventListener('resize', relayout);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
    addEventListener('load', relayout);
    if (reduced) {
      for (const k in L) { grow(L[k], 1); lit(L[k], 0, L[k].len); L[k].k1.classList.add('lit'); }
      agents.forEach(a => { a.el.classList.add('ok'); status(a, 'ok', 'готово'); });
      branches.forEach(b => { status(b, b.k === 'boss' ? 'ok' : 'sent', b.k === 'boss' ? 'одобрен' : 'готово'); b.subs.forEach(v => v.classList.add('on')); });
      hrows.forEach(r => { r.el.classList.add('on'); status(r, 'ok', r.k === 'ivan' ? 'в работе' : 'срок в плане'); });
      status(recSt, 'ok', 'записано'); stage('готово · 7 получателей · 2 агента', 'ok'); return;
    }
    let raf = 0, vis = false, last = 0;
    function frame() { const ts = performance.now(); now += Math.min(64, ts - last); last = ts; tick(); drawSigs(); raf = vis && !document.hidden ? requestAnimationFrame(frame) : 0; }
    const run = () => { if (vis && !document.hidden && !raf) { last = performance.now(); raf = requestAnimationFrame(frame); } };
    watch(lx, v => { vis = v; if (v && !started) { started = true; t0 = now + 250; } run(); }, .12);
    document.addEventListener('visibilitychange', run);
  })();

  /* ---------- 4. the steps: the lists light up one line after another ---------- */
  const stepsEl = $('#lx-steps');
  if (stepsEl) { const items = $$('.l-step li', stepsEl);
    if (reduced) items.forEach(li => li.classList.add('on'));
    else { let k = 0, iv = 0; const run = () => { items.forEach((li, i) => li.classList.toggle('on', i < k)); k = k >= items.length + 3 ? 0 : k + 1; };
      watch(stepsEl, v => { clearInterval(iv); if (v) iv = setInterval(run, 520); }, .3); } }
})();
