(() => {
  const canvas = document.getElementById('login-pixels');
  const visual = canvas?.closest('.login-visual');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Password visibility toggle; form submission and server-side auth remain unchanged.
  const password = document.getElementById('login-password');
  const toggle = document.getElementById('login-password-toggle');
  if (password && toggle) {
    toggle.addEventListener('click', () => {
      const reveal = password.type === 'password';
      password.type = reveal ? 'text' : 'password';
      toggle.setAttribute('aria-label', reveal ? 'Скрыть пароль' : 'Показать пароль');
      toggle.setAttribute('title', reveal ? 'Скрыть пароль' : 'Показать пароль');
      password.focus({ preventScroll: true });
    });
  }

  if (!canvas || !visual) return;
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) return;

  const colors = [
    [217, 142, 138], // SYS AT clay
    [143, 168, 216], // SYS AI blue
    [240, 238, 230], // warm white
    [198, 144, 108]  // muted copper
  ];
  const pointer = { x: -10000, y: -10000, tx: -10000, ty: -10000, active: false };
  let width = 0, height = 0, dpr = 1, raf = 0, previous = 0, elapsed = 0;
  const spacing = 13.5;

  function resize() {
    const box = visual.getBoundingClientRect();
    width = Math.max(1, box.width);
    height = Math.max(1, box.height);
    dpr = Math.min(window.devicePixelRatio || 1, 1.75);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw(performance.now());
  }

  function draw(now) {
    if (!width || !height) return;
    elapsed = now * 0.00023;
    pointer.x += (pointer.tx - pointer.x) * 0.12;
    pointer.y += (pointer.ty - pointer.y) * 0.12;
    ctx.clearRect(0, 0, width, height);

    const cols = Math.ceil(width / spacing);
    const rows = Math.ceil(height / spacing);
    const cx = width * 0.57;
    const cy = height * 0.43;
    const radiusX = width * 0.76;
    const radiusY = height * 0.78;

    for (let row = 0; row <= rows; row++) {
      const y = row * spacing + 6;
      for (let col = 0; col <= cols; col++) {
        const x = col * spacing + 6;
        const nx = (x - cx) / radiusX;
        const ny = (y - cy) / radiusY;
        const radius = Math.sqrt(nx * nx + ny * ny);
        const angle = Math.atan2(ny, nx);

        // Multiple overlapping waves keep the whole field visibly alive.
        const rings = (Math.sin(radius * 23 - angle * 1.8 - elapsed * 4.7) + 1) * 0.5;
        const flow = (Math.sin(x * 0.012 + y * 0.017 + Math.sin(y * 0.009 + elapsed * 1.4) * 2.1) + 1) * 0.5;
        const sweep = (Math.cos(x * 0.008 - y * 0.014 + elapsed * 3.4) + 1) * 0.5;
        const shape = Math.max(0, 1 - Math.abs(radius - (0.56 + Math.sin(angle * 2.2 + elapsed) * 0.10)) * 3.5);
        const structure = Math.max(0, (rings * 0.46 + flow * 0.29 + sweep * 0.18 + shape * 0.4) - 0.28);
        const edge = Math.max(0.18, 1 - Math.max(0, radius - 0.32) * 0.54);
        let alpha = Math.min(0.94, (0.12 + structure * 0.78) * edge);

        const dx = x - pointer.x;
        const dy = y - pointer.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        // Smooth exponential falloff brightens individual pixels without drawing a cursor-shaped spotlight.
        const glow = pointer.active ? Math.exp(-distance / 145) * 0.55 : 0;
        const pulse = (Math.sin(elapsed * 2 + radius * 8 - angle) + 1) * 0.5;
        alpha = Math.min(1, alpha + glow * (0.13 + flow * 0.25) + pulse * structure * 0.07);

        // Brighter pixels grow slightly as a moving cursor wave passes through.
        const size = 1.9 + Math.pow(Math.max(rings, flow, sweep), 1.65) * 2.65 + glow * 1.35;
        const colorIndex = ((Math.floor((angle + Math.PI) * 1.7) + Math.floor(radius * 9) + row % 5) % colors.length + colors.length) % colors.length;
        let color = colors[colorIndex];
        if ((rings > 0.67 && sweep < 0.58) || flow > 0.88) color = colors[1];
        else if (structure > 0.34 && pulse > 0.72) color = colors[0];

        ctx.fillStyle = 'rgba(' + color[0] + ',' + color[1] + ',' + color[2] + ',' + alpha + ')';
        ctx.fillRect(x, y, size, size);
      }
    }
  }

  function animate(now) {
    if (reduceMotion) return;
    if (now - previous > 32) {
      draw(now);
      previous = now;
    }
    raf = requestAnimationFrame(animate);
  }

  visual.addEventListener('pointermove', event => {
    const rect = visual.getBoundingClientRect();
    pointer.tx = event.clientX - rect.left;
    pointer.ty = event.clientY - rect.top;
    pointer.active = true;
    if (reduceMotion) draw(performance.now());
  }, { passive: true });
  visual.addEventListener('pointerleave', () => {
    pointer.tx = -1000;
    pointer.ty = -1000;
    pointer.active = false;
    if (reduceMotion) draw(performance.now());
  }, { passive: true });

  const observer = new ResizeObserver(resize);
  observer.observe(visual);
  resize();
  if (!reduceMotion) raf = requestAnimationFrame(animate);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      cancelAnimationFrame(raf);
    } else if (!reduceMotion) {
      previous = 0;
      raf = requestAnimationFrame(animate);
    } else {
      draw(performance.now());
    }
  });
})();
