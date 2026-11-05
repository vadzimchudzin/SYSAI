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

  /* ---------- 3. the scheme: a record comes in at the top, runs down the spine of the agents and fans out to people ---------- */
  const lx = $('#lx'), core = $('#lx-core');
  if (lx && core) (() => {
    const svg = $('#lx-svg'), stageEl = $('#lx-stage'), defs = mk('defs', {}, svg);
    const gBase = mk('g', {}, svg), gOv = mk('g', {}, svg), gKn = mk('g', {}, svg), gSig = mk('g', {}, svg);
    const CORAL = 'var(--lx-coral)', BLUE = 'var(--lx-blue)', VIOLET = 'var(--lx-violet)', AMBER = 'var(--lx-amber)', GREEN = 'var(--lx-green)';
    const GLOW = { eleven: '#e9e6de', gemini: '#86a9f2', mistral: '#e9b872', claude: '#e58a68', openai: '#b9b5aa' };
    const words = v => (v || '').split(' ').filter(Boolean);
    const rec = $('#lx-rec'), recSt = $('.lx-st', rec), srcList = $('.lx-srcs', lx), srcs = $$('.lx-src', lx), prep = $$('#lx-prep li');
    const memEl = $('#lx-mem'), MR = Object.fromEntries($$('li[data-k]', memEl).map(el => [el.dataset.k, el]));
    const herm = $('#lx-herm'), hcap = $('.lx-cap', herm);
    const hrows = $$('.lx-hrow', herm).map(el => ({ el, k: el.dataset.k, sq: $('.lx-sq', el), st: $('.lx-st', el) })), HR = Object.fromEntries(hrows.map(r => [r.k, r]));
    const models = {}; $$('[data-m]', lx).forEach(el => (models[el.dataset.m] = models[el.dataset.m] || []).push(el));
    const modelRow = Object.fromEntries($$('.lx-m', lx).map(m => [m.dataset.m, m]));
    const agents = $$('.lx-ag', core).map(el => ({ el, k: el.dataset.a, sq: $('.lx-ah .lx-sq', el), st: $('.lx-ah .lx-st', el), tone: el.style.getPropertyValue('--tone').trim() || BLUE,
      steps: $$('.lx-ss li', el).map(li => ({ el: li, k: li.dataset.k, uses: words(li.dataset.uses), mem: words(li.dataset.mem), y: 0 })) }));
    const A = Object.fromEntries(agents.map(a => [a.k, a])), STEP = {};
    agents.forEach(a => a.steps.forEach(s => { s.a = a; STEP[s.k] = s; }));
    const branches = $$('.lx-br', lx).map(el => ({ el, k: el.dataset.k, node: $('.lx-node', el), sq: $('.lx-node .lx-sq', el), st: $('.lx-node .lx-st', el),
      subs: $$('.lx-sub', el), side: el.closest('.lx-out').dataset.side, tone: el.dataset.tone === 'b' ? BLUE : CORAL, from: words(el.dataset.from) }));
    const B = Object.fromEntries(branches.map(b => [b.k, b]));
    const RIBBON = ['boss', 'ivan', 'maria', 'drive', 'cal', 'chat', 'b24']; /* left to right: the top rows peel off first, so no strand crosses another */
    const wave = recWave($('#lx-wave'));
    const f = v => v.toFixed(1);
    /* boxes from offsets: they ignore the transforms of the reveal animation */
    const box = el => { let l = 0, t = 0, e = el; while (e && e !== lx) { l += e.offsetLeft; t += e.offsetTop; const p = e.offsetParent; if (p && p !== lx) { l += p.clientLeft; t += p.clientTop; } e = p; }
      const w = el.offsetWidth, h = el.offsetHeight; return { l, t, r: l + w, b: t + h, w, h, cx: l + w / 2, cy: t + h / 2 }; };

    /* lines: a hairline base drawn on scroll, an overlay colored by the signals, a pixel knot on the ends */
    const L = {}, alias = {}, seen = new Set();
    const get = key => L[alias[key] || key];
    function route(key, d, tone, o = {}) {
      let l = L[key];
      if (!l) { l = L[key] = { key, prog: 0, ov: null, base: mk('path', { class: 'lx-ln' }, gBase), k0: mk('rect', { class: 'lx-kn', width: 3, height: 3 }, gKn), k1: mk('rect', { class: 'lx-kn', width: 3, height: 3 }, gKn) }; }
      l.tone = tone; l.hidden = !!o.hidden; l.ends = (o.ends || []).filter(Boolean);
      l.base.setAttribute('class', 'lx-ln' + (o.cls ? ' ' + o.cls : '') + (l.base.classList.contains('hl') ? ' hl' : ''));
      l.base.style.visibility = o.hidden ? 'hidden' : ''; l.base.style.setProperty('--tone', tone);
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
      if (!l) return;
      if (!l.ov) l.ov = mk('path', { class: 'lx-ov off', d: l.d }, gOv);
      if (tone) l.ov.style.setProperty('--tone', tone); else if (!l.ov.style.getPropertyValue('--tone')) l.ov.style.setProperty('--tone', l.tone);
      a = clamp(a, 0, l.len); b = clamp(b, a, l.len); l.lit = [a, b];
      l.ov.style.strokeDasharray = `0 ${f(a)} ${f(b - a)} ${f(l.len + 4)}`; l.ov.classList.remove('off');
    }
    const litAll = (key, tone) => { const l = get(key); if (l && l.prog > .99) { lit(l, 0, l.len, tone); l.k1.classList.add('lit'); } };
    function unlit(l) { if (!l) return; sigs.forEach(s => { if (s.l === l) s.dead = true; }); if (l.ov) { l.ov.classList.add('off'); l.lit = null; } l.k0.classList.remove('lit'); l.k1.classList.remove('lit'); }
    function sweep() {
      for (const k in L) if (!seen.has(k)) { const l = L[k]; l.base.remove(); if (l.ov) l.ov.remove(); l.k0.remove(); l.k1.remove(); delete L[k]; }
      seen.clear();
    }

    const vS = (x0, y0, x1, y1, k = .55) => { const dy = y1 - y0; return `M${f(x0)},${f(y0)} C${f(x0)},${f(y0 + dy * k)} ${f(x1)},${f(y1 - dy * k)} ${f(x1)},${f(y1)}`; };
    const hS = (x0, y0, x1, y1, k = .5) => { const dx = x1 - x0; return `M${f(x0)},${f(y0)} C${f(x0 + dx * k)},${f(y0)} ${f(x1 - dx * k)},${f(y1)} ${f(x1)},${f(y1)}`; };
    /* a strand of the ribbon: straight down, then a long soft turn into the tag of a row */
    const peel = (x, y0, x1, y1, D) => { D = clamp(D, 8, y1 - y0); return `M${f(x)},${f(y0)} V${f(y1 - D)} C${f(x)},${f(y1 - D * .3)} ${f(x + (x1 - x) * .42)},${f(y1)} ${f(x1)},${f(y1)}`; };
    const rail = (x, y0, x1, y1, r = 10) => `M${f(x)},${f(y0)} V${f(y1 - r)} Q${f(x)},${f(y1)} ${f(x + r)},${f(y1)} H${f(x1)}`;

    let mode = '';
    function trees() { /* short rails from a row to what hangs under it */
      branches.forEach(b => { if (!b.subs.length) return; const n = box(b.node), x = (b.side === 'r' && mode === 'wide' ? box(b.sq).r + 14 : n.l) + 3.5; let prev = null, y = n.b - 9;
        b.subs.forEach(v => { const s = box(v), key = `sub-${b.k}-${v.dataset.k}`;
          if (prev && Math.abs(prev.cy - s.cy) < 3) route(key, `M${f(prev.r + 4)},${f(s.cy)} H${f(s.l - 4)}`, b.tone, { k0: false });
          else { route(key, rail(x, y, s.l - 4, s.cy, 6), b.tone, { k0: false }); y = s.cy - 6; }
          prev = s; }); });
    }
    function layout() {
      svg.setAttribute('viewBox', `0 0 ${lx.clientWidth} ${lx.clientHeight}`);
      for (const k in alias) delete alias[k];
      mode = getComputedStyle(lx).display === 'grid' ? 'wide' : 'narrow';
      const rb = box(rec), cb = box(core), cx = Math.round(rb.cx) + .5;
      /* 1. sources: three fibres from the port of every source fan into the top of the record */
      if (mode === 'wide') srcs.forEach((s, i) => { const b = box(s), x1 = cx + (i - (srcs.length - 1) / 2) * 13;
        [0, -1, 1].forEach(j => route(j ? `src-${i}-${j}` : 'src-' + i, vS(b.cx + j * 2.4, b.b + 1, x1 + j * 3.6, rb.t, .5 + j * .07), BLUE,
          { cls: j ? 'fb' : '', k0: false, k1: !j, ends: [s, rec] })); });
      else { const g = box(srcList); srcs.forEach((s, i) => { const x0 = g.l + g.w * (i + .5) / srcs.length;
        route('src-' + i, vS(x0, g.b, cx + (i - (srcs.length - 1) / 2) * 6, rb.t, .55), BLUE, { cls: 'fb', ends: [s, rec] }); }); }
      /* 2. two voices go down in a bundle of six fibres, through the preparation of the sound, into the core */
      [CORAL, BLUE, CORAL, BLUE, CORAL, BLUE].forEach((t, j) => { const x = cx + (j - 2.5) * 3.2;
        route(j === 2 ? 'rec-d' : j === 3 ? 'rec-i' : 'rec-' + j, `M${f(x)},${f(rb.b)} V${f(cb.t)}`, t, { cls: 'tn', k0: false, k1: j === 2 || j === 3 }); });
      prep.forEach(li => (li._y = box(li).cy));
      /* 3. one spine through the steps of all agents; a hidden segment per agent carries its signal */
      const sx = box(agents[0].sq).cx, last = agents[agents.length - 1];
      agents.forEach(a => { const q = box(a.sq); a.y0 = q.b + 3; a.steps.forEach(s => (s.y = box(s.el).cy)); a.y1 = a.steps[a.steps.length - 1].y;
        route('seg-' + a.k, `M${f(sx)},${f(a.y0)} V${f(a.y1)}`, a.tone, { hidden: true }); });
      route('spine', `M${f(sx)},${f(agents[0].y0)} V${f(last.y1)}`, 'rgba(240,238,230,.4)', { k0: false, k1: false });
      if (mode === 'wide') {
        /* 4. models cross over to the steps that use them, every line leaves from the tag of the model */
        const outN = {}, outI = {}; agents.forEach(a => a.steps.forEach(s => s.uses.forEach(m => (outN[m] = (outN[m] || 0) + 1))));
        agents.forEach(a => a.steps.forEach(s => { const n = s.uses.length; s.uses.forEach((m, j) => { const q = box($('.lx-sq', modelRow[m])), k = outI[m] = (outI[m] || 0) + 1;
          const y0 = q.cy + (k - 1 - (outN[m] - 1) / 2) * 2, y1 = s.y + (j - (n - 1) / 2) * 3, ends = [modelRow[m], s.el];
          route(`m-${m}-${s.k}`, hS(q.r, y0, cb.l, y1, .52), GLOW[m], { cls: 'tn', ends, k0: false });
          [-1, 1].forEach(d => route(`m-${m}-${s.k}${d}`, hS(q.r, y0 + d * 1.4, cb.l, y1 + d * 1.6, .52 + d * .11), GLOW[m], { cls: 'fb', ends, k0: false, k1: false })); }); }));
        /* 5. the memory of the company: every record feeds the steps that read it */
        const inN = {}, inI = {}; agents.forEach(a => a.steps.forEach(s => s.mem.forEach(m => (inN[m] = (inN[m] || 0) + 1))));
        agents.forEach(a => a.steps.forEach(s => { const n = s.mem.length; s.mem.forEach((m, j) => { if (!MR[m]) return; const q = box($('.lx-sq', MR[m])), k = inI[m] = (inI[m] || 0) + 1;
          const y0 = q.cy + (k - 1 - (inN[m] - 1) / 2) * 2, y1 = s.y + (j - (n - 1) / 2) * 3, ends = [MR[m], s.el];
          route(`mm-${m}-${s.k}`, hS(q.l, y0, cb.r, y1, .52), GREEN, { ends, k0: false });
          [-1, 1].forEach(d => route(`mm-${m}-${s.k}${d}`, hS(q.l, y0 + d * 1.4, cb.r, y1 + d * 1.6, .52 + d * .11), GREEN, { cls: 'fb', ends, k0: false, k1: false })); }); }));
        /* 6. Hermes: the assistant hands a task to the agent on the PC of an employee */
        hrows.forEach((r, j) => { const q = box(r.sq); route('herm-' + r.k, hS(cb.r, STEP.pc.y + (j - .5) * 3, q.l, q.cy, .5), VIOLET, { ends: [STEP.pc.el, r.el], k1: false }); });
        /* 7. the ribbon: the strands leave the core and peel off to the rows on both sides */
        const total = RIBBON.reduce((n, k) => n + B[k].from.length, 0), GAP = 3.2, GAPB = 6.5;
        let x = cx - ((total - RIBBON.length) * GAP + (RIBBON.length - 1) * GAPB) / 2;
        RIBBON.forEach((k, idx) => { const b = B[k], q = box(b.sq), m = b.from.length, left = b.side === 'l';
          b.from.forEach((src, j) => { const o = left ? j : m - 1 - j; /* the outer strand turns first and lands highest */
            route(j ? `br-${k}-${j}` : 'br-' + k, peel(x, cb.b, left ? q.r : q.l, q.cy + (o - (m - 1) / 2) * 2.4, 84 + (m - 1 - o) * 13), b.tone,
              { cls: j ? 'tn' : '', ends: [b.node, STEP[src] && STEP[src].el], k0: false, k1: !j });
            x += GAP; });
          route(`br-${k}-f`, peel(x - GAP * (m + 1) / 2 + 1.6, cb.b, left ? q.r : q.l, q.cy + 1.2, 90 + m * 6), b.tone, { cls: 'fb', ends: [b.node], k0: false, k1: false });
          x += GAPB - GAP; });
      } else {
        /* one rail on the left: memory, Hermes and every output hang on it */
        const rx = 12.5, y0 = cb.b, items = [['mem', box($('.lx-cap', memEl)), GREEN, memEl], ['herm', box(hcap), VIOLET, herm], ...branches.map(b => ['br-' + b.k, box(b.node), b.tone, b.node])];
        let bot = y0;
        items.forEach(([key, e, tone, el]) => { const ay = e.cy; route(key, rail(rx, y0, e.l - 4, ay, 12), tone, { hidden: true });
          route('stub-' + key, rail(rx, ay - 26, e.l - 4, ay, 12), tone, { k0: false, ends: [el] }); bot = Math.max(bot, ay - 26); });
        route('rail', `M${f(rx)},${f(y0)} V${f(bot)}`, 'rgba(240,238,230,.4)', { k1: false });
        hrows.forEach(r => (alias['herm-' + r.k] = 'herm'));
      }
      trees(); sweep(); measure(); reveal(true); if (hov) hover(hov);
    }

    /* scroll: the front runs a bit above the bottom of the screen, lines grow behind it and never shrink back */
    const rv = $$('.lx-rv'), rvTop = new Map(); let lxTop = 0, F = -1e9;
    function measure() { const sy = scrollY; lxTop = lx.getBoundingClientRect().top + sy; rv.forEach(el => rvTop.set(el, el.getBoundingClientRect().top + sy)); }
    function reveal(instant) {
      const fr = scrollY + innerHeight * .86; if (fr > F) F = fr; const fl = F - lxTop;
      for (const k in L) { const l = L[k]; if (l.prog < 1) grow(l, clamp((fl - l.y0) / Math.max(l.y1 - l.y0, 150), 0, 1)); }
      rv.forEach(el => { if (!el.classList.contains('in') && F > rvTop.get(el) + 24) { el.classList.add('in'); if (instant) el.classList.add('now'); } });
    }

    /* hover (mouse only): a row shows where its lines go */
    let hov = null;
    const HOV = '.lx-src,.lx-m,.lx-ss li,.lx-mem li,.lx-hrow,.lx-node,.lx-ah';
    function hover(el) {
      hov = el; lx.classList.toggle('lx-hov', !!el); $$('.hl', lx).forEach(x => x.classList.remove('hl'));
      if (!el) { for (const k in L) L[k].base.classList.remove('hl'), L[k].ov && L[k].ov.classList.remove('hl'); return; }
      const set = new Set([el]); if (el.matches('.lx-ah')) $$('.lx-ss li', el.parentNode).forEach(x => set.add(x));
      for (const k in L) { const l = L[k], on = l.ends.some(x => set.has(x)); l.base.classList.toggle('hl', on); if (l.ov) l.ov.classList.toggle('hl', on);
        if (on) l.ends.forEach(x => x.classList.add('hl')); }
      el.classList.add('hl');
    }
    if (matchMedia('(hover: hover)').matches) {
      lx.addEventListener('pointerover', e => { const el = e.target.closest(HOV); if (el !== hov) hover(el && lx.contains(el) ? el : null); });
      lx.addEventListener('pointerleave', () => hover(null));
    }

    /* signals: a short pixel sound wave runs along a line; the line takes its color behind it */
    const sigs = []; let now = 0;
    function signal(key, tone, ms = 900, done, reverse = false, linear = false) {
      const l = get(key); if (!l || !l.len) { if (done) done(); return; }
      const g = mk('g', { class: 'lx-sig' }, gSig); g.style.color = tone;
      sigs.push({ l, g, p3: mk('path', { class: 'p3' }, g), p2: mk('path', { class: 'p2' }, g), p1: mk('path', { class: 'p1' }, g), tone, t0: now, ms, done, reverse, linear, fired: false, seed: Math.random() * 6.3 });
    }
    function lutOf(l) { if (l.lut) return l.lut; const n = Math.max(2, Math.ceil(l.len / 2)), a = new Float32Array((n + 1) * 2);
      for (let i = 0; i <= n; i++) { const p = l.base.getPointAtLength(l.len * i / n); a[2 * i] = p.x; a[2 * i + 1] = p.y; } return (l.lut = { n, a }); }
    function at(l, s) { const { n, a } = lutOf(l), u = clamp(s / l.len, 0, 1) * n, i = Math.min(n - 1, Math.floor(u)), k = u - i;
      const x0 = a[2 * i], y0 = a[2 * i + 1], x1 = a[2 * i + 2], y1 = a[2 * i + 3], m = Math.hypot(x1 - x0, y1 - y0) || 1;
      return [x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, -(y1 - y0) / m, (x1 - x0) / m]; }
    const CELL = 3, BODY = 24, TAIL = 26, AMP = 6.6;
    const sq = (x, y, s) => `M${f(x - s / 2)} ${f(y - s / 2)}h${s}v${s}h-${s}z`;
    function drawSigs() {
      const ts = now / 1000;
      for (let i = sigs.length - 1; i >= 0; i--) {
        const s = sigs[i], l = s.l;
        if (L[l.key] !== l) { s.g.remove(); sigs.splice(i, 1); if (!s.fired && s.done) s.done(); continue; }
        const raw = (now - s.t0) / s.ms, c = clamp(raw, 0, 1), e = s.linear ? c : inOut(c), dir = s.reverse ? -1 : 1, pos = s.reverse ? l.len * (1 - e) : l.len * e;
        /* a signal never runs ahead of the scroll: past the drawn part of its line it fades out */
        const lim = l.prog * l.len, vis = clamp(1 - (pos - lim) / 24, 0, 1), tf = raw > 1 ? 1 - clamp((raw - 1) * s.ms / 260, 0, 1) : 1, fade = tf * vis;
        if (raw <= 1 && !s.dead) { if (s.reverse) { if (pos < lim) lit(l, pos, lim, s.tone); } else if (lim > 0) lit(l, 0, Math.min(pos, lim), s.tone); }
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
        if (raw >= 1 && !s.fired) { s.fired = true; if (vis > 0 && !s.dead) (s.reverse ? l.k0 : l.k1).classList.add('lit'); if (s.done) s.done(); }
        if (tf <= 0) { s.g.remove(); sigs.splice(i, 1); }
      }
    }

    /* a clock of the story: callbacks run on the animation time, so they pause with it */
    const Q = []; const later = (ms, fn) => Q.push([now + ms, fn]);
    function runQ() { for (let i = 0; i < Q.length; i++) if (Q[i][0] <= now) { const fn = Q[i][1]; Q.splice(i--, 1); fn(); } }

    /* states */
    const say = (el, text) => { const b = el && (el.querySelector('b') || el); if (!b || b.textContent === text) return; b.style.opacity = 0; later(170, () => { b.textContent = text; b.style.opacity = 1; }); };
    const status = (o, s, text) => { const st = o.st || o; st.dataset.s = s; say(st, text); };
    const stage = (text, s = 'run') => { stageEl.className = 'lx-stage ' + s; say(stageEl, text); };
    const hit = (el, ms = 1500) => { if (!el) return; el.classList.add('hit'); later(ms, () => el.classList.remove('hit')); };
    const model = (keys, on = true) => keys.forEach(k => (models[k] || []).forEach(el => el.classList.toggle('on', on)));
    function agentRun(a, ms, text) {
      a.el.classList.remove('ok'); a.el.classList.add('run'); status(a, 'run', text);
      signal('seg-' + a.k, a.tone, ms, null, false, true);
      const span = Math.max(1, a.y1 - a.y0);
      a.steps.forEach(s => { const t = ms * (s.y - a.y0) / span;
        s.uses.forEach((m, j) => later(Math.max(0, t - 520) + j * 70, () => { model([m]); signal(`m-${m}-${s.k}`, GLOW[m], clamp(t, 300, 520)); }));
        s.mem.forEach((m, j) => later(Math.max(0, t - 560) + j * 90, () => { if (MR[m]) MR[m].classList.add('on'); signal(mode === 'wide' ? `mm-${m}-${s.k}` : 'mem', GREEN, clamp(t, 300, 560), null, false); }));
        later(t, () => s.el.classList.add('on')); });
    }
    function agentOk(a, text) {
      a.el.classList.remove('run'); a.el.classList.add('ok'); status(a, 'ok', text);
      const used = [...new Set(a.steps.flatMap(s => s.uses))]; model(used, false);
      a.steps.forEach(s => { s.uses.forEach(m => unlit(get(`m-${m}-${s.k}`))); s.mem.forEach(m => { unlit(get(`mm-${m}-${s.k}`)); if (MR[m]) MR[m].classList.remove('on'); }); });
    }
    function to(k, s, text, ms = 1200, tone) { const b = B[k]; if (!b) return;
      signal('br-' + k, tone || b.tone, ms, () => { hit(b.node); b.el.classList.add('done'); status(b, s, text); b.from.forEach((x, j) => j && litAll(`br-${k}-${j}`, tone || b.tone)); }); }
    function sub(k, key, tone, reverse, done) { const b = B[k], v = b && b.subs.find(x => x.dataset.k === key); if (!v) return;
      if (reverse) { v.classList.add('on'); signal(`sub-${k}-${key}`, tone || b.tone, 700, done, true); } else signal(`sub-${k}-${key}`, tone || b.tone, 600, () => { v.classList.add('on'); if (done) done(); }); }
    function agentPc(k, s, text) { const r = HR[k]; if (!r) return;
      if (mode === 'wide') signal('herm-' + k, VIOLET, 1000, () => { r.el.classList.add('on'); status(r, s, text); });
      else signal('herm', VIOLET, 900, () => { hit(herm); r.el.classList.add('on'); status(r, s, text); }); }

    let live = false, scan = -1, scanT0 = 0, loop = 0, recT0 = 0;
    const STEPS = [
      [0, () => { live = true; recT0 = now; status(recSt, 'run', 'запись'); stage('слушает планёрку'); }],
      [300, () => { const i = loop % srcs.length, s = srcs[i]; if (!s) return; hit(s, 1900); signal('src-' + i, BLUE, 1500, () => hit(rec, 1200)); }],
      [2100, () => { live = false; scan = 0; scanT0 = now; status(recSt, 'ok', 'записано'); stage('готовит звук');
        const rl = get('rec-d'); prep.forEach(li => later(rl ? 1200 * clamp((li._y - rl.y0) / Math.max(1, rl.y1 - rl.y0), 0, 1) : 0, () => li.classList.add('on')));
        signal('rec-d', CORAL, 1200, null, false, true); signal('rec-i', BLUE, 1200, () => { core.classList.add('on'); stage('Слух: два движка'); }, false, true); }],
      [3350, () => agentRun(A.ear, 1700, 'слушает')],
      [5150, () => { agentOk(A.ear, 'текст'); agentRun(A.who, 1600, 'узнаёт'); stage('Кто говорит: голоса из памяти'); }],
      [6850, () => { agentOk(A.who, '2 голоса'); agentRun(A.sec, 2000, 'пишет'); stage('Секретарь: протокол и задачи'); }],
      [8950, () => { agentOk(A.sec, 'протокол'); status(A.ast, 'run', 'в работе'); A.ast.el.classList.add('run'); stage('Ассистент: протокол на проверку'); to('boss', 'run', 'на проверке', 1300); }],
      [10350, () => sub('boss', 'dec')], [10600, () => sub('boss', 'tasks')], [10850, () => sub('boss', 'risk')],
      [11500, () => { status(B.boss, 'ok', 'одобрен'); hit(B.boss.node); stage('Ассистент: рассылает задачи'); agentRun(A.ast, 3600, 'в работе'); }],
      [11700, () => { [['ivan', 'sent', 'доставлено'], ['maria', 'run', 'уточняет'], ['b24', 'sent', 'создано'], ['chat', 'sent', 'отправлено'], ['cal', 'sent', 'в календаре'], ['drive', 'sent', 'сохранено']]
        .forEach(([k, s, t], i) => later(i * 170, () => to(k, s, t, 1250))); }],
      [13300, () => { agentPc('ivan', 'run', 'взял задачу'); stage('Ассистент: задача агенту Ивана'); }],
      [14300, () => sub('maria', 'ask', BLUE)],
      [15500, () => sub('maria', 'reply', CORAL, true, () => { status(B.maria, 'ok', 'срок пт'); hit(B.maria.node); })],
      [16000, () => { agentPc('maria', 'sent', 'срок в плане'); stage('Ассистент: срок в Битрикс24'); }],
      [16300, () => to('b24', 'sent', 'срок обновлён', 1100, BLUE)],
      [17100, () => { sub('ivan', 'remind', CORAL); status(HR.ivan, 'run', 'в работе'); }],
      [18000, () => { agentOk(A.ast, 'готово'); stage('готово · 7 получателей · 2 агента', 'ok'); }]
    ];
    const LOOP = 22400; /* the full picture holds for about four seconds, then the story starts again */
    function reset() {
      Q.length = 0; sigs.splice(0).forEach(s => s.g.remove()); for (const k in L) unlit(L[k]);
      core.classList.remove('on'); model(Object.keys(models), false); prep.forEach(li => li.classList.remove('on'));
      agents.forEach(a => { a.el.classList.remove('run', 'ok'); status(a, 'wait', 'ждёт'); a.steps.forEach(s => s.el.classList.remove('on')); });
      Object.values(MR).forEach(el => el.classList.remove('on'));
      branches.forEach(b => { status(b, 'wait', b.k === 'boss' ? 'готовится' : 'ждёт'); b.node.classList.remove('hit'); b.el.classList.remove('done'); b.subs.forEach(v => v.classList.remove('on')); });
      hrows.forEach(r => { r.el.classList.remove('on'); status(r, 'wait', 'ждёт'); }); herm.classList.remove('hit');
      status(recSt, 'run', 'запись'); stage('ждёт запись', '');
    }
    let t0 = 0, next = 0, started = false, waveDirty = true;
    function tick() {
      runQ();
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
    if (reduced) { /* the final picture at once */
      for (const k in L) grow(L[k], 1);
      for (const k in L) if (/^(rec-[di]|seg-|br-|sub-|stub-|herm)/.test(k)) litAll(k, k.startsWith('seg-') ? L[k].tone : undefined);
      prep.forEach(li => li.classList.add('on'));
      agents.forEach(a => { a.el.classList.add('ok'); status(a, 'ok', 'готово'); a.steps.forEach(s => s.el.classList.add('on')); });
      branches.forEach(b => { b.el.classList.add('done'); status(b, b.k === 'boss' ? 'ok' : 'sent', b.k === 'boss' ? 'одобрен' : 'готово'); b.subs.forEach(v => v.classList.add('on')); });
      hrows.forEach(r => { r.el.classList.add('on'); status(r, 'ok', r.k === 'ivan' ? 'в работе' : 'срок в плане'); });
      status(recSt, 'ok', 'записано'); stage('готово · 7 получателей · 2 агента', 'ok');
      runQ(); now += 1000; runQ(); return;
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
