(() => {
  const root = document.getElementById('model-settings');
  if (!root) return;
  const status = document.getElementById('model-catalog-status');
  const refresh = document.getElementById('model-catalog-refresh');
  const ids = {
    transcribe: 'transcribe-model',
    diarization: 'diarization-model',
    report: 'report-model',
    agent: 'agent-model',
    conversation: 'conversation-model'
  };
  const fields = Object.fromEntries(Object.entries(ids).map(([key, id]) => [key, document.getElementById(id)]));
  const filters = {};
  root.querySelectorAll('.model-filter').forEach(input => {
    const key = Object.keys(ids).find(name => ids[name] === input.dataset.target);
    if (key) filters[key] = input;
  });
  // Frequently used vendors first, the rest alphabetically.
  const PROVIDER_ORDER = ['google', 'openai', 'anthropic', 'deepseek', 'x-ai', 'mistralai', 'qwen', 'meta-llama', 'meta', 'openrouter'];
  const catalog = { models: [], stt: [], loaded: false };
  let loading = false;

  const amount = value => {
    const number = Number(value);
    return value !== null && value !== '' && Number.isFinite(number) && number >= 0 ? number : null;
  };
  const short = value => value.toLocaleString('ru-RU', { maximumSignificantDigits: 3 });
  const vendor = id => String(id || '').replace(/^~/, '').split('/')[0];
  const gemini = id => String(id || '').replace(/^~/, '').startsWith('google/gemini');
  const byName = (a, b) => String(a.name).localeCompare(String(b.name), 'ru') || a.id.localeCompare(b.id);
  const plural = (count, forms) => {
    const tens = count % 100;
    const ones = count % 10;
    if (tens >= 11 && tens <= 14) return forms[2];
    return ones === 1 ? forms[0] : ones >= 2 && ones <= 4 ? forms[1] : forms[2];
  };

  function tokenPrice(model) {
    const prompt = amount(model.prompt_price);
    const completion = amount(model.completion_price);
    if (prompt === null || completion === null) return '';
    if (prompt === 0 && completion === 0) return 'бесплатно';
    return `$${short(prompt * 1e6)} / $${short(completion * 1e6)} за 1 млн токенов`;
  }
  function sttPrice(model) {
    const hourly = amount(model.usd_per_hour);
    if (hourly !== null) return `≈ $${short(hourly)} за час аудио`;
    if (amount(model.completion_price)) return tokenPrice(model);
    return 'тариф провайдера';
  }
  const price = model => (model.kind === 'stt' ? sttPrice(model) : tokenPrice(model));
  const label = model => [model.name, price(model)].filter(Boolean).join(' · ');

  function providerGroups(models) {
    const groups = new Map();
    for (const model of models) {
      const key = vendor(model.id);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(model);
    }
    const rank = key => (PROVIDER_ORDER.includes(key) ? PROVIDER_ORDER.indexOf(key) : PROVIDER_ORDER.length);
    return [...groups.entries()]
      .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
      .map(([key, items]) => ({ label: key, models: items.sort(byName) }));
  }

  function groupsFor(key) {
    const live = catalog.models.filter(model => !model.batch);
    if (key === 'transcribe') {
      return [
        { label: 'Распознавание речи (STT): говорящих расставит модель ниже', models: catalog.stt.filter(model => model.kind === 'stt').sort(byName) },
        { label: 'Gemini: текст и говорящие за один проход', models: catalog.stt.filter(model => model.kind !== 'stt').sort(byName) }
      ];
    }
    if (key === 'diarization') {
      const audio = live.filter(model => model.audio_input && model.text_output && amount(model.prompt_price) !== null);
      return [
        { label: 'Gemini: рекомендуется', models: audio.filter(model => gemini(model.id)).sort(byName) },
        { label: 'Другие модели со звуком: не проверялись', models: audio.filter(model => !gemini(model.id)).sort(byName) }
      ];
    }
    if (key === 'conversation') {
      return [{ label: 'Бесплатные', models: live.filter(model => model.id === 'openrouter/free' || model.id.endsWith(':free')).sort(byName) }];
    }
    return providerGroups(live.filter(model => model.text_input && model.text_output));
  }

  function option(value, text) {
    const element = document.createElement('option');
    element.value = value;
    element.textContent = text;
    element.title = value;
    return element;
  }

  function render(key) {
    const select = fields[key];
    if (!select) return;
    const current = select.value || select.dataset.current || '';
    const query = (filters[key]?.value || '').trim().toLocaleLowerCase('ru');
    const groups = groupsFor(key);
    const known = groups.flatMap(group => group.models).find(model => model.id === current)
      || (key === 'transcribe' ? catalog.stt : catalog.models).find(model => model.id === current);
    select.replaceChildren();
    // The chosen model always stays selected: never switch a saved model silently.
    if (current) {
      const head = document.createElement('optgroup');
      head.label = known ? 'Выбрана сейчас' : 'Нет в каталоге OpenRouter';
      head.append(option(current, known ? label(known) : `${current} · модель не найдена в каталоге`));
      select.append(head);
    }
    const matches = model => !query || model.id.toLocaleLowerCase('ru').includes(query)
      || String(model.name).toLocaleLowerCase('ru').includes(query);
    let shown = 0;
    for (const group of groups) {
      const items = group.models.filter(model => model.id !== current && matches(model));
      if (!items.length) continue;
      const element = document.createElement('optgroup');
      element.label = group.label;
      items.forEach(model => element.append(option(model.id, label(model))));
      select.append(element);
      shown += items.length;
    }
    if (query && !shown) {
      const empty = document.createElement('optgroup');
      empty.label = 'По запросу ничего не найдено';
      select.append(empty);
    }
    select.value = current;
    describe(key);
  }

  function describe(key) {
    const select = fields[key];
    const hint = document.getElementById(`${ids[key]}-hint`);
    if (!select || !hint) return;
    const id = select.value;
    const model = (key === 'transcribe' ? catalog.stt : catalog.models).find(item => item.id === id);
    const parts = [id];
    let warning = false;
    if (model) {
      parts.push(price(model));
      if (key === 'transcribe') {
        parts.push(model.kind === 'stt'
          ? 'текст распознаёт эта модель, говорящих расставит модель из поля «Разделение говорящих»'
          : 'модель сама слышит текст и голоса, поле «Разделение говорящих» не используется');
      }
      if (model.batch) {
        parts.push('пакетная очередь: ответы могут идти часами');
        warning = true;
      }
    } else if (catalog.loaded) {
      parts.push('модели нет в каталоге OpenRouter: запросы с ней могут не пройти');
      warning = true;
    }
    if (id !== select.dataset.current) parts.push('изменено, сохраните настройки');
    hint.textContent = parts.filter(Boolean).join(' · ');
    hint.title = model?.description || '';
    hint.classList.toggle('warn', warning);
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
      catalog.models = Array.isArray(data.models) ? data.models : [];
      catalog.stt = Array.isArray(data.transcription_models) ? data.transcription_models : [];
      catalog.loaded = true;
      Object.keys(fields).forEach(render);
      const sttCount = Number(data.stt_count) || catalog.stt.filter(model => model.kind === 'stt').length;
      const geminiCount = catalog.stt.length - sttCount;
      const warnings = Array.isArray(data.warnings) && data.warnings.length ? ` Внимание: ${data.warnings.join('; ')}.` : '';
      const total = catalog.models.length;
      status.textContent = `Каталог OpenRouter: ${total} ${plural(total, ['модель', 'модели', 'моделей'])}, распознавание речи: ${sttCount} + ${geminiCount} Gemini.${warnings}`;
    } catch (error) {
      Object.keys(fields).forEach(describe);
      status.textContent = `${error.message || 'Каталог недоступен'}. Сохранённые модели не меняются.`;
    } finally {
      loading = false;
      refresh.disabled = false;
    }
  }

  Object.entries(fields).forEach(([key, select]) => select?.addEventListener('change', () => (catalog.loaded ? render(key) : describe(key))));
  Object.entries(filters).forEach(([key, input]) => input.addEventListener('input', () => render(key)));
  Object.keys(fields).forEach(describe);
  refresh.addEventListener('click', loadModels);
  loadModels();
})();
