(() => {
  const stateLabel = status => ({
    recording: 'Идёт запись',
    finishing: 'Сборка аудио',
    error: 'Нужна проверка',
    finished: 'Передано на обработку',
    cancelled: 'Отменено'
  }[status] || status || 'Неизвестно');
  const duration = seconds => {
    const n = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = Math.floor(n % 60);
    return h ? `${h} ч ${String(m).padStart(2, '0')} мин` : `${m} мин ${String(s).padStart(2, '0')} сек`;
  };
  const size = bytes => `${((Number(bytes) || 0) / 1048576).toFixed(1)} МБ`;
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: { accept: 'application/json', ...(options.headers || {}) },
      credentials: 'same-origin',
      cache: 'no-store'
    });
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.detail || `Ошибка запроса (${response.status})`);
    return body;
  };

  const panel = document.getElementById('active-recordings');
  if (panel) {
    const list = document.getElementById('active-recordings-list');
    let timer;
    const poll = async () => {
      try {
        const data = await api('/recordings/active');
        const rows = Array.isArray(data.recordings) ? data.recordings : [];
        panel.hidden = rows.length === 0;
        list.replaceChildren();
        for (const rec of rows) {
          const item = document.createElement('div');
          item.className = 'active-recording' + (rec.status === 'error' ? ' needs-attention' : '');
          const info = document.createElement('div');
          info.className = 'active-recording-info';
          const title = document.createElement('b');
          title.textContent = rec.title || 'Совещание без названия';
          const detail = document.createElement('div');
          detail.className = 'small muted';
          const status = rec.status === 'error' ? 'Остановлена · можно восстановить' : stateLabel(rec.status);
          detail.textContent = `${rec.device_label || 'Устройство'} · ${status} · ${duration(rec.elapsed_seconds)} · ${size(rec.size_bytes)} · частей: ${rec.next_sequence}`;
          info.append(title, detail);
          const link = document.createElement('a');
          link.className = 'btn sm';
          link.href = '/recordings/' + encodeURIComponent(rec.id) + '/monitor';
          link.textContent = rec.status === 'error' ? 'Восстановить' : 'Наблюдать';
          item.append(info, link);
          list.append(item);
        }
      } catch (_) { /* A transient polling error must not interrupt recording. */ }
      clearTimeout(timer);
      timer = setTimeout(poll, document.hidden ? 12000 : 5000);
    };
    poll();
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) { clearTimeout(timer); poll(); }
    });
  }

  const monitor = document.getElementById('recording-monitor');
  if (!monitor) return;

  const id = monitor.dataset.id;
  const statusEl = document.getElementById('monitor-status');
  const message = document.getElementById('monitor-message');
  const meetingLink = document.getElementById('monitor-meeting-link');
  const downloadLink = document.getElementById('monitor-download');
  const finishButton = document.getElementById('monitor-finish');
  const cancelButton = document.getElementById('monitor-cancel');
  const titleEl = document.getElementById('monitor-title');
  const TERMINAL = new Set(['finished', 'cancelled', 'error']);
  let stopped = false, timer = null, lastStatus = null;

  function render(rec) {
    lastStatus = rec.status;
    titleEl.textContent = rec.title || 'Совещание без названия';
    document.getElementById('monitor-device').textContent = rec.device_label || 'Неизвестное устройство';
    document.getElementById('monitor-created').textContent = rec.created_at || '—';
    document.getElementById('monitor-elapsed').textContent = duration(rec.elapsed_seconds);
    document.getElementById('monitor-size').textContent = size(rec.size_bytes);
    document.getElementById('monitor-chunks').textContent = String(rec.next_sequence || 0);
    statusEl.textContent = stateLabel(rec.status);
    const tone = rec.status === 'recording' ? 'amber' : rec.status === 'error' ? 'red' : rec.status === 'finished' ? 'green' : 'gray';
    statusEl.className = 'pill ' + tone;
    monitor.classList.toggle('is-stuck', rec.status === 'error');
    if (rec.status === 'recording') {
      message.textContent = 'Запись активна на исходной вкладке. Если вы уже закрыли её, обновите статус или используйте действия ниже.';
    } else if (rec.status === 'finishing') {
      message.textContent = 'Сервер собирает аудиочасти. Если этот этап завис, можно скачать переданные данные или закрыть сессию.';
    } else if (rec.status === 'error') {
      message.textContent = Number(rec.size_bytes) > 0
        ? 'Исходная вкладка закрыта или передача остановилась. Загруженные аудиочасти сохранены: сначала скачайте их, затем попробуйте обработку.'
        : 'Запись остановилась до передачи аудио. Можно закрыть эту сессию и начать заново.';
    } else if (rec.status === 'finished') {
      message.textContent = 'Аудио собрано и передано на обработку.';
    } else if (rec.status === 'cancelled') {
      message.textContent = 'Запись закрыта. Серверные аудиочасти удалены.';
    }
    downloadLink.hidden = !Number(rec.size_bytes) || rec.status === 'cancelled';
    finishButton.hidden = !['recording', 'error'].includes(rec.status) || !Number(rec.size_bytes);
    cancelButton.hidden = !['recording', 'finishing', 'error'].includes(rec.status);
    if (rec.meeting_url) { meetingLink.href = rec.meeting_url; meetingLink.hidden = false; }
    else meetingLink.hidden = true;
    stopped = TERMINAL.has(rec.status);
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(poll, document.hidden ? 12000 : 2500);
  }

  async function poll() {
    if (stopped) return;
    try {
      render(await api('/recordings/' + encodeURIComponent(id) + '/status'));
    } catch (exc) {
      message.textContent = `${exc.message || 'Не удалось обновить статус записи'}. Повторим проверку автоматически.`;
      clearTimeout(timer);
      timer = setTimeout(poll, 8000);
    }
  }

  finishButton.addEventListener('click', async () => {
    if (!confirm('Попробовать собрать и обработать аудио, уже загруженное на сервер? Если исходная вкладка ещё пишет звук, сначала остановите её.')) return;
    finishButton.disabled = true;
    finishButton.textContent = 'Проверяем аудио…';
    try {
      const result = await api('/recordings/' + encodeURIComponent(id) + '/finish', { method: 'POST' });
      if (result.url) { location.assign(result.url); return; }
    } catch (exc) {
      message.textContent = `Не удалось собрать запись: ${exc.message}. Скачайте аудио или закройте незавершённую сессию.`;
    } finally {
      finishButton.disabled = false;
      finishButton.textContent = 'Обработать загруженные части';
    }
    stopped = false;
    await poll();
  });

  cancelButton.addEventListener('click', async () => {
    if (!confirm('Закрыть незавершённую запись? Все аудиочасти на сервере будут удалены. Если они нужны, сначала нажмите «Скачать загруженное аудио».')) return;
    cancelButton.disabled = true;
    cancelButton.textContent = 'Закрываем…';
    try {
      const result = await api('/recordings/' + encodeURIComponent(id) + '/cancel', { method: 'POST' });
      render({ id, status: result.status || 'cancelled', size_bytes: 0, next_sequence: 0 });
      message.textContent = 'Запись закрыта. Её больше не будет в списке активных записей.';
    } catch (exc) {
      message.textContent = `Не удалось закрыть запись: ${exc.message}`;
      cancelButton.disabled = false;
      cancelButton.textContent = 'Закрыть зависшую запись';
    }
  });

  poll();
})();
