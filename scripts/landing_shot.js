// Screenshot of a block of the landing in its final state (animation off, as with «reduce motion» in the system).
// node scripts/landing_shot.js WIDTH HEIGHT SELECTOR OUT.png [WAIT_MS=1500] [DPR=1]
// The page — env U (default http://127.0.0.1:8765/), Chromium — env CHROMIUM (default: the one Playwright installs).
const { chromium } = require('playwright');
(async () => {
  const [,, w, h, sel, out, waitMs = '1500', dpr = '1'] = process.argv;
  if (!out) { console.error('usage: node scripts/landing_shot.js WIDTH HEIGHT SELECTOR OUT.png [WAIT_MS] [DPR]'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: +w, height: +h }, deviceScaleFactor: +dpr, reducedMotion: 'reduce' });
  const page = await ctx.newPage(); const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(process.env.U || 'http://127.0.0.1:8765/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);
  /* scroll down in small steps: the lines are drawn while the page scrolls */
  const y = await page.evaluate(s => document.querySelector(s).getBoundingClientRect().top + scrollY, sel);
  for (let i = 1; i <= 14; i++) { await page.evaluate(v => scrollTo(0, v), y * i / 14); await page.waitForTimeout(50); }
  await page.waitForTimeout(+waitMs);
  const clip = await page.evaluate(s => { const b = document.querySelector(s).getBoundingClientRect();
    return { x: 0, y: Math.max(0, b.top), width: innerWidth, height: Math.min(innerHeight - Math.max(0, b.top), b.height) }; }, sel);
  await page.screenshot({ path: out, clip });
  console.log(out, JSON.stringify(clip), errors.length ? 'page errors: ' + JSON.stringify(errors) : 'no page errors');
  await ctx.close(); await browser.close(); process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
