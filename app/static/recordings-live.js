(() => {
  const stateLabel = status => ({
    recording: 'Идёт запись', finishing: 'Сборка аудио', error: 'Ошибка передачи',
    finished: 'Передано на обработку', cancelled: 'Отменено'
  }[status] || status || 'Неизвестно');
  const duration = seconds => {
    const n = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = Math.floor(n % 60);
    return h ? `${h} ч ${String(m).padStart(2, '0')} мин` : `${m} мин ${String(s).padStart(2, '0')} сек`;
  };
  const size = bytes => `${((Number(bytes) || 0) / 1048576).toFixed(1)} МБ`;

  const panel = document.getElementById('active-recordings');
  if (panel) {
    const list = document.getElementById('active-recordings-list');
    const poll = async () => {
      try {
        const response = await fetch('/recordings/active', { headers: { accept: 'application/json' }, cache: 'no-store' });
        if (!response.ok) throw new Error('Не удалось получить статус');
        const data = await response.json();
        const rows = Array.isArray(data.recordings) ? data.recordings : [];
        panel.hidden = rows.length === 0;
        list.replaceChildren();
        for (const rec of rows) {
          const item = document.createElement('div');
          item.className = 'active-recording';
          const info = document.createElement('div');
          const title = document.createElement('b');
          title.textContent = rec.title || 'Совещание без названия';
          const detail = document.createElement('div');
          detail.className = 'small muted';
          detail.textContent = `${rec.device_label || 'Устройство'} · ${stateLabel(rec.status)} · ${duration(rec.elapsed_seconds)} · ${size(rec.size_bytes)} · частей: ${rec.next_sequence}`;
          info.append(title, detail);
          const link = document.createElement('a');
          link.className = 'btn sm';
          link.href = '/recordings/' + encodeURIComponent(rec.id) + '/monitor';
          link.textContent = 'Наблюдать';
          item.append(info, link);
          list.append(item);
        }
      } catch (_) { /* A transient polling error must not interrupt recording. */ }
      setTimeout(poll, 3000);
    };
    poll();
  }

  const monitor = document.getElementById('recording-monitor');
  if (monitor) {
    const id = monitor.dataset.id;
    const statusEl = document.getElementById('monitor-status');
    const message = document.getElementById('monitor-message');
    const meetingLink = document.getElementById('monitor-meeting-link');
    const poll = async () => {
      try {
        const response = await fetch('/recordings/' + encodeURIComponent(id) + '/status', {
          headers: { accept: 'application/json' }, cache: 'no-store'
        });
        if (!response.ok) throw new Error('Статус записи недоступен');
        const rec = await response.json();
        document.getElementById('monitor-title').textContent = rec.title || 'Совещание без названия';
        document.getElementById('monitor-device').textContent = rec.device_label || 'Неизвестное устройство';
        document.getElementById('monitor-created').textContent = rec.created_at || '—';
        document.getElementById('monitor-elapsed').textContent = duration(rec.elapsed_seconds);
        document.getElementById('monitor-size').textContent = size(rec.size_bytes);
        document.getElementById('monitor-chunks').textContent = String(rec.next_sequence || 0);
        statusEl.textContent = stateLabel(rec.status);
        statusEl.className = 'pill ' + (rec.status === 'recording' ? 'amber' : rec.status === 'error' ? 'red' : rec.status === 'finished' ? 'green' : 'gray');
        if (rec.status === 'recording') message.textContent = 'Микрофон и передача управляются исходным устройством. Этот экран показывает актуальный статус.';
        else if (rec.status === 'finishing') message.textContent = 'Аудиочасти собираются и проверяются перед запуском расшифровки.';
        else if (rec.status === 'error') message.textContent = 'Передача или сборка столкнулась с ошибкой. На исходном устройстве можно повторить передачу или скачать локальную копию.';
        else if (rec.status === 'finished') message.textContent = 'Запись передана в обработку.';
        else if (rec.status === 'cancelled') message.textContent = 'Запись отменена.';
        if (rec.meeting_url) { meetingLink.href = rec.meeting_url; meetingLink.hidden = false; }
        else meetingLink.hidden = true;
      } catch (_) {
        message.textContent = 'Не удалось обновить статус. Повторяем подключение…';
      }
      setTimeout(poll, 2500);
    };
    poll();
  }
})();
