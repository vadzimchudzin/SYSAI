(() => {
  const stateLabel = status => ({
    recording: 'Идёт запись',
    stopping: 'Останавливается',
    finishing: 'Сборка аудио',
    error: 'Нужна проверка',
    finished: 'Передано на обработку',
    cancelled: 'Отменено'
  }[status] || status || 'Неизвестно');
  const duration = seconds => {
    const n = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = Math.floor(n % 60);
    return h ? h + ' ч ' + String(m).padStart(2, '0') + ' мин' : m + ' мин ' + String(s).padStart(2, '0') + ' сек';
  };
  const size = bytes => ((Number(bytes) || 0) / 1048576).toFixed(1) + ' МБ';
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: { accept: 'application/json', ...(options.headers || {}) },
      credentials: 'same-origin',
      cache: 'no-store'
    });
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.detail || 'Ошибка запроса (' + response.status + ')');
    return body;
  };
  const stopIconButton = (id, title) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'recording-stop-button';
    button.title = 'Остановить эту запись';
    button.setAttribute('aria-label', button.title);
    button.textContent = '×';
    button.addEventListener('click', async () => {
      if (!confirm('Остановить запись «' + title + '» и отправить именно её аудио на обработку?')) return;
      button.disabled = true;
      try {
        await api('/recordings/' + encodeURIComponent(id) + '/stop', { method: 'POST' });
        button.title = 'Остановка запрошена';
        button.setAttribute('aria-label', button.title);
      } catch (exc) {
        button.title = exc.message || 'Не удалось остановить запись';
        button.setAttribute('aria-label', button.title);
        button.disabled = false;
      }
    });
    return button;
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
          detail.textContent = (rec.device_label || 'Устройство') + ' · ' +
            (rec.status === 'error' ? 'Остановлена · можно восстановить' : stateLabel(rec.status)) +
            ' · ' + duration(rec.elapsed_seconds) + ' · ' + size(rec.size_bytes) + ' · частей: ' + rec.next_sequence;
          info.append(title, detail);
          const actions = document.createElement('div');
          actions.className = 'recording-row-actions';
          const link = document.createElement('a');
          link.className = 'btn sm';
          link.href = '/recordings/' + encodeURIComponent(rec.id) + '/monitor';
          link.target = '_blank';
          link.rel = 'noopener';
          link.textContent = rec.status === 'error' ? 'Восстановить' : 'Наблюдать';
          actions.append(link);
          if (rec.status === 'recording') actions.append(stopIconButton(rec.id, rec.title || 'Совещание без названия'));
          item.append(info, actions);
          list.append(item);
        }
      } catch (_) { /* Transient polling errors do not interrupt recording. */ }
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
  const stopButton = document.getElementById('monitor-stop');
  const finishButton = document.getElementById('monitor-finish');
  const cancelButton = document.getElementById('monitor-cancel');
  const finishLabel = document.getElementById('monitor-finish-label');
  const cancelLabel = document.getElementById('monitor-cancel-label');
  const titleEl = document.getElementById('monitor-title');
  const TERMINAL = new Set(['finished', 'cancelled', 'error']);
  let stopped = false, timer = null, lastRecord = null;

  function render(rec) {
    lastRecord = { ...(lastRecord || {}), ...rec };
    monitor.dataset.status = rec.status || '';
    titleEl.textContent = rec.title || 'Совещание без названия';
    document.getElementById('monitor-device').textContent = rec.device_label || 'Неизвестное устройство';
    document.getElementById('monitor-created').textContent = rec.created_at || '—';
    document.getElementById('monitor-elapsed').textContent = duration(rec.elapsed_seconds);
    document.getElementById('monitor-size').textContent = size(rec.size_bytes);
    document.getElementById('monitor-chunks').textContent = String(rec.next_sequence || 0);

    const updatedAt = Date.parse(rec.updated_at || '');
    const waitingTooLong = rec.status === 'stopping' && Number.isFinite(updatedAt) && Date.now() - updatedAt > 12000;
    statusEl.textContent = waitingTooLong ? 'Источник не отвечает' : stateLabel(rec.status);
    const tone = rec.status === 'recording' ? 'amber' : rec.status === 'error' || waitingTooLong ? 'red' : rec.status === 'finished' ? 'green' : 'gray';
    statusEl.className = 'pill ' + tone;
    monitor.classList.toggle('is-stuck', rec.status === 'error' || waitingTooLong);

    if (rec.status === 'recording') {
      message.textContent = 'Запись продолжается на исходной вкладке. Монитор можно оставить открытым; сворачивание записи её не останавливает.';
    } else if (rec.status === 'stopping') {
      message.textContent = waitingTooLong
        ? 'Исходная вкладка не подтвердила остановку. Возможно, её уже закрыли. Можно обработать только переданное аудио или закрыть зависшую сессию.'
        : 'Команда остановки отправлена именно для этой записи. Ждём, пока исходная вкладка остановит микрофон и передаст последний фрагмент…';
    } else if (rec.status === 'finishing') {
      message.textContent = 'Сервер собирает аудиочасти и запускает обработку.';
    } else if (rec.status === 'error') {
      message.textContent = Number(rec.size_bytes) > 0
        ? 'Запись остановилась с ошибкой. Загруженные аудиочасти сохранены: сначала скачайте их, затем попробуйте обработку.'
        : 'Запись остановилась до передачи аудио. Можно закрыть эту сессию и начать заново.';
    } else if (rec.status === 'finished') {
      message.textContent = 'Аудио собрано и передано на обработку.';
    } else if (rec.status === 'cancelled') {
      message.textContent = 'Запись закрыта. Серверные аудиочасти удалены.';
    }

    stopButton.hidden = rec.status !== 'recording';
    stopButton.disabled = rec.status !== 'recording';
    downloadLink.hidden = !Number(rec.size_bytes) || rec.status === 'cancelled';
    finishButton.hidden = !(rec.status === 'error' || waitingTooLong) || !Number(rec.size_bytes);
    cancelButton.hidden = !(rec.status === 'error' || waitingTooLong);
    finishLabel.textContent = waitingTooLong ? 'Обработать переданное аудио' : 'Обработать загруженные части';
    cancelLabel.textContent = 'Закрыть зависшую запись';
    if (rec.meeting_url) { meetingLink.href = rec.meeting_url; meetingLink.hidden = false; }
    else meetingLink.hidden = true;

    stopped = TERMINAL.has(rec.status);
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(poll, document.hidden ? 10000 : 2500);
  }

  async function poll() {
    if (stopped) return;
    try {
      render(await api('/recordings/' + encodeURIComponent(id) + '/status'));
    } catch (exc) {
      message.textContent = (exc.message || 'Не удалось обновить статус записи') + '. Повторим проверку автоматически.';
      clearTimeout(timer);
      timer = setTimeout(poll, 8000);
    }
  }

  stopButton.addEventListener('click', async () => {
    if (!confirm('Остановить запись «' + (titleEl.textContent || 'Совещание') + '»? Микрофон остановится на исходной вкладке, аудио будет сохранено и отправлено на обработку.')) return;
    stopButton.disabled = true;
    try {
      await api('/recordings/' + encodeURIComponent(id) + '/stop', { method: 'POST' });
      message.textContent = 'Команда остановки отправлена именно для этой записи.';
      stopped = false;
      await poll();
    } catch (exc) {
      message.textContent = 'Не удалось отправить команду остановки: ' + (exc.message || exc);
      stopButton.disabled = false;
    }
  });

  finishButton.addEventListener('click', async () => {
    if (!confirm('Попробовать собрать только те аудиочасти, которые уже загружены на сервер?')) return;
    finishButton.disabled = true;
    finishLabel.textContent = 'Собираем аудио…';
    try {
      const result = await api('/recordings/' + encodeURIComponent(id) + '/finish', { method: 'POST' });
      if (result.url) { location.assign(result.url); return; }
    } catch (exc) {
      message.textContent = 'Не удалось собрать запись: ' + (exc.message || exc) + '. Скачайте аудио или закройте незавершённую сессию.';
    } finally {
      finishButton.disabled = false;
    }
    stopped = false;
    await poll();
  });

  cancelButton.addEventListener('click', async () => {
    if (!confirm('Закрыть только эту незавершённую запись? Все её аудиочасти на сервере будут удалены. Скачайте данные, если они нужны.')) return;
    cancelButton.disabled = true;
    cancelLabel.textContent = 'Закрываем…';
    try {
      const result = await api('/recordings/' + encodeURIComponent(id) + '/cancel', { method: 'POST' });
      render({ ...(lastRecord || {}), id, status: result.status || 'cancelled', size_bytes: 0, next_sequence: 0 });
      message.textContent = 'Эта запись закрыта. Остальные записи не затронуты.';
    } catch (exc) {
      message.textContent = 'Не удалось закрыть запись: ' + (exc.message || exc);
      cancelButton.disabled = false;
      cancelLabel.textContent = 'Закрыть зависшую запись';
    }
  });

  poll();
})();
