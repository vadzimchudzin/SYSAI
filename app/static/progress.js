(() => {
  // Live refresh of the Progress page: asks for a version every few seconds and swaps the feed only when something
  // changed. Never while a person is typing in a form, and the open «Ход дела» / «Вмешаться» panels stay open.
  const feed = document.getElementById('progress-feed');
  if (!feed) return;
  const live = document.getElementById('progress-live');
  const scope = encodeURIComponent(feed.dataset.scope || 'last');
  let version = feed.dataset.version;
  let busy = false;

  const typing = () => {
    const a = document.activeElement;
    if (a && feed.contains(a) && /^(TEXTAREA|INPUT|SELECT)$/.test(a.tagName)) return true;
    return [...feed.querySelectorAll('textarea, input[type=file]')].some((x) => x.value);
  };
  const stamp = (text) => { if (live) live.textContent = text; };

  async function tick() {
    if (busy || document.hidden || typing()) return;
    busy = true;
    try {
      const r = await fetch(`/progress/version?scope=${scope}`, {credentials: 'same-origin', cache: 'no-store'});
      if (!r.ok) throw new Error(r.status);
      const v = (await r.json()).version;
      if (v !== version) {
        const html = await (await fetch(`/progress/feed?scope=${scope}`, {credentials: 'same-origin', cache: 'no-store'})).text();
        if (!typing()) {
          const open = [...feed.querySelectorAll('details[open]')].map((d) => d.dataset.key);
          feed.innerHTML = html;
          open.forEach((k) => { const d = feed.querySelector(`details[data-key="${k}"]`); if (d) d.open = true; });
          version = v;
        }
      }
      stamp('Обновлено в ' + new Date().toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', second: '2-digit'}));
    } catch (e) {
      stamp('Нет связи с сервером, повторю через несколько секунд…');
    } finally {
      busy = false;
    }
  }
  setInterval(tick, 8000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
})();
