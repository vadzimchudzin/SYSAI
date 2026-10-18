(() => {
  const root = document.getElementById('model-settings');
  if (!root) return;
  const status = document.getElementById('model-catalog-status');
  const refresh = document.getElementById('model-catalog-refresh');
  const fields = {
    transcribe: document.getElementById('transcribe-model'),
    report: document.getElementById('report-model'),
    agent: document.getElementById('agent-model'),
    conversation: document.getElementById('conversation-model')
  };
  let loading = false;
  function fill(select, models, predicate) {
    const current = select.dataset.current || select.value;
    const filtered = models.filter(predicate);
    const seen = new Set();
    select.replaceChildren();
    const add = (id, label) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      const option = document.createElement('option');
      option.value = id;
      option.textContent = label;
      select.append(option);
    };
    const currentModel = models.find(model => model.id === current);
    add(current, currentModel ? `${currentModel.name} · ${current}` : `${current} · сохранённая модель`);
    for (const model of filtered) {
      const context = Number(model.context_length) > 0 ? ` · контекст ${Number(model.context_length).toLocaleString('ru-RU')}` : '';
      add(model.id, `${model.name} · ${model.id}${context}`);
    }
    select.value = current;
  }
  async function loadModels() {
    if (loading) return;
    loading = true;
    refresh.disabled = true;
    status.textContent = 'Загружаем каталог OpenRouter…';
    try {
      const response = await fetch('/settings/models', { headers: { accept: 'application/json' }, cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить модели');
      const models = Array.isArray(data.models) ? data.models : [];
      fill(fields.transcribe, models, model => model.audio_input);
      fill(fields.report, models, model => model.text_input && model.text_output);
      fill(fields.agent, models, model => model.text_input && model.text_output);
      fill(fields.conversation, models, model => model.id === 'openrouter/free' || model.id.endsWith(':free'));
      status.textContent = `Загружено моделей: ${data.count}. STT: ${models.filter(model => model.audio_input).length}.`;
    } catch (error) {
      status.textContent = error.message || 'Каталог недоступен; можно сохранить текущие модели.';
    } finally {
      loading = false;
      refresh.disabled = false;
    }
  }
  refresh.addEventListener('click', loadModels);
  loadModels();
})();
