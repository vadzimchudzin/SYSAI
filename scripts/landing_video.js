// Video of the landing: node scripts/landing_video.js WIDTH HEIGHT OUT.mp4 "ACTIONS"
// ACTIONS, separated by spaces: w:MS — wait, s:Y — jump to Y, sm:Y:MS — smooth scroll to Y (or to #block) in MS.
// The page — env U (default http://127.0.0.1:8765/), Chromium — env CHROMIUM (default: the one Playwright installs). Needs ffmpeg.
const { chromium } = require('playwright');
const fs = require('fs'), os = require('os'), path = require('path');
const { execFileSync } = require('child_process');
(async () => {
  const [,, w, h, out, acts = 'w:5000'] = process.argv;
  if (!out) { console.error('usage: node scripts/landing_video.js WIDTH HEIGHT OUT.mp4 "w:1000 sm:1150:1800 w:8000"'); process.exit(2); }
  const W = +w, H = +h, dir = fs.mkdtempSync(path.join(os.tmpdir(), 'landing-video-'));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, recordVideo: { dir, size: { width: W, height: H } } });
  const page = await ctx.newPage(); const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(process.env.U || 'http://127.0.0.1:8765/', { waitUntil: 'networkidle' });
  const t0 = Date.now();
  for (const a of acts.split(' ').filter(Boolean)) {
    const [k, v, ms] = a.split(':');
    if (k === 'w') await page.waitForTimeout(+v);
    else if (k === 's') await page.evaluate(y => scrollTo(0, +y), v);
    else if (k === 'sm') {
      const y0 = await page.evaluate(() => scrollY);
      const y1 = v.startsWith('#') ? await page.evaluate(s => document.querySelector(s).getBoundingClientRect().top + scrollY, v) : +v;
      const n = Math.max(1, Math.round(+ms / 33));
      for (let i = 1; i <= n; i++) { const e = i / n, q = e < .5 ? 2 * e * e : 1 - Math.pow(-2 * e + 2, 2) / 2;
        await page.evaluate(y => scrollTo(0, y), y0 + (y1 - y0) * q); await page.waitForTimeout(33); }
    }
  }
  const seconds = (Date.now() - t0) / 1000;
  const raw = await page.video().path(); await ctx.close(); await browser.close();
  /* the first half second is the blank page while it loads */
  execFileSync('ffmpeg', ['-y', '-v', 'error', '-i', raw, '-ss', '0.6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20',
    '-preset', 'slow', '-movflags', '+faststart', '-r', '30', out]);
  fs.rmSync(dir, { recursive: true, force: true });
  console.log(out, seconds.toFixed(1) + ' s', errors.length ? 'page errors: ' + JSON.stringify(errors) : 'no page errors');
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
