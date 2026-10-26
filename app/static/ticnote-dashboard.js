(() => {
  const card = document.getElementById('tn-dash');
  const stopButton = document.getElementById('tn-dash-stop');
  const stopStatus = document.getElementById('tn-dash-stop-status');
  if (!card || !stopButton) return;

  let timer;
  let current = null;
  let stopping = false;

  async function poll() {
    try {
      const response = await fetch('/ticnote/status', { headers: { Accept: 'application/json' } });
      if (!response.ok) return;
      const data = await response.json();
      const active = (data.recent || []).find(item => item.can_stop);
      current = active || null;
      stopButton.hidden = !current;
      stopButton.disabled = stopping || !current;
      stopButton.textContent = current
        ? (current.state === 'downloading' ? '⏹ Остановить получение записи' : '⏹ Остановить обработку записи')
        : '⏹ Остановить обработку TicNote';
      if (!stopping && !current && stopStatus.dataset.done === '1') {
        stopStatus.textContent = 'Операция остановлена.';
      }
    } catch (_) {
      // Keep the dashboard usable if the status endpoint is temporarily unavailable.
    } finally {
      clearTimeout(timer);
      timer = setTimeout(poll, card.dataset.mode === 'busy' ? 4000 : 10000);
    }
  }

  stopButton.addEventListener('click', async () => {
    if (!current || stopping) return;
    const target = current;
    const message = target.state === 'downloading'
      ? 'Остановить получение этой записи с TicNote? Она не будет автоматически загружена повторно.'
      : 'Остановить обработку этой записи? Уже сохранённый отчёт не удаляется.';
    if (!window.confirm(message)) return;

    stopping = true;
    stopButton.disabled = true;
    stopStatus.dataset.done = '0';
    stopStatus.textContent = 'Отправляю команду остановки…';
    try {
      const response = await fetch('/ticnote/imports/' + encodeURIComponent(target.record_id) + '/stop', {
        method: 'POST',
        headers: { Accept: 'application/json' }
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || 'Не удалось остановить операцию');
      stopStatus.dataset.done = '1';
      stopStatus.textContent = 'Команда остановки принята.';
      current = null;
      await poll();
    } catch (error) {
      stopStatus.textContent = error.message || 'Ошибка остановки. Обновите страницу и попробуйте ещё раз.';
    } finally {
      stopping = false;
      stopButton.disabled = false;
      await poll();
    }
  });

  poll();
})();