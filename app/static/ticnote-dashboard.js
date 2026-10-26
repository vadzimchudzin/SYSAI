(() => {
  const buttons = [
    { button: document.getElementById('tn-dash-stop'), status: document.getElementById('tn-dash-stop-status') },
    { button: document.getElementById('tn-tab-stop'), status: document.getElementById('tn-tab-stop-status') }
  ].filter(item => item.button);
  if (!buttons.length) return;

  let timer;
  let current = null;
  let stopping = false;

  function render() {
    for (const item of buttons) {
      item.button.hidden = !current;
      item.button.disabled = stopping || !current;
      if (current) item.button.textContent =
        current.state === 'downloading' ? '⏹ Остановить получение записи' : '⏹ Остановить обработку записи';
    }
  }

  async function poll() {
    try {
      const response = await fetch('/ticnote/status', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error('Не удалось получить состояние TicNote');
      const data = await response.json();
      current = (data.recent || []).find(item => item.can_stop) || null;
      render();
    } catch (_) {
      // Keep the page usable while status is temporarily unavailable.
    } finally {
      clearTimeout(timer);
      timer = setTimeout(poll, 4000);
    }
  }

  for (const item of buttons) {
    item.button.addEventListener('click', async () => {
      if (!current || stopping) return;
      const target = current;
      const message = target.state === 'downloading'
        ? 'Остановить получение этой записи с TicNote? Автоматическая повторная загрузка будет отключена.'
        : 'Остановить обработку этой записи? Уже сохранённый отчёт не удаляется.';
      if (!window.confirm(message)) return;

      stopping = true;
      render();
      for (const b of buttons) if (b.status) b.status.textContent = 'Отправляю команду остановки…';
      try {
        const response = await fetch('/ticnote/imports/' + encodeURIComponent(target.record_id) + '/stop', {
          method: 'POST',
          headers: { Accept: 'application/json' },
          cache: 'no-store'
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || 'Не удалось остановить операцию');
        current = null;
        for (const b of buttons) if (b.status) b.status.textContent = 'Команда остановки принята.';
      } catch (error) {
        for (const b of buttons) if (b.status) b.status.textContent = error.message || 'Ошибка остановки. Попробуйте ещё раз.';
      } finally {
        stopping = false;
        render();
        await poll();
      }
    });
  }
  poll();
})();