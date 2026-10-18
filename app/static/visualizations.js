(() => {
  const source = document.getElementById('meeting-visualization-data');
  const target = document.getElementById('meeting-charts');
  if (!source || !target) return;
  const ns = 'http://www.w3.org/2000/svg';
  const make = (tag, attrs = {}, text = '') => {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== '') node.textContent = text;
    return node;
  };
  const textNode = (x, y, text, className = '') => make('text', { x, y, class: className }, text);
  const finiteValues = chart => (chart.series || []).flatMap(series =>
    (series.values || []).filter(value => typeof value === 'number' && Number.isFinite(value)));
  const fmt = value => Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 });

  function draw(chart) {
    if (!chart || !['bar', 'line'].includes(chart.type) || !Array.isArray(chart.labels) ||
        !Array.isArray(chart.series) || !chart.labels.length) return;
    const card = document.createElement('article');
    card.className = 'chart-card';
    const heading = document.createElement('div');
    heading.className = 'row between';
    const title = document.createElement('h3');
    title.textContent = chart.title || 'Показатели';
    heading.append(title);
    if (chart.unit) {
      const unit = document.createElement('span');
      unit.className = 'pill gray';
      unit.textContent = chart.unit;
      heading.append(unit);
    }
    const svg = make('svg', { viewBox: '0 0 720 300', class: 'interactive-chart', role: 'img',
      'aria-label': chart.title || 'График показателей', preserveAspectRatio: 'xMidYMid meet' });
    const description = document.createElement('p');
    description.className = 'chart-active-value small';
    description.textContent = 'Выберите столбец или точку, чтобы посмотреть значение.';
    const evidence = document.createElement('details');
    evidence.className = 'chart-evidence';
    const summary = document.createElement('summary');
    summary.textContent = 'Источник чисел';
    const quote = document.createElement('p');
    quote.textContent = chart.source_quote || 'Источник не указан';
    evidence.append(summary, quote);
    card.append(heading, svg, description, evidence);

    const labels = chart.labels.slice(0, 12);
    const series = chart.series.slice(0, 4).filter(item => Array.isArray(item.values) && item.values.length === labels.length);
    const values = finiteValues({ series });
    if (!series.length || !values.length) return;
    let min = Math.min(0, ...values), max = Math.max(0, ...values);
    if (min === max) max = min + 1;
    const left = 58, right = 690, top = 20, bottom = 235, width = right - left, height = bottom - top;
    const y = value => bottom - ((value - min) / (max - min)) * height;
    const baseline = y(0);

    for (let tick = 0; tick <= 4; tick++) {
      const value = min + (max - min) * tick / 4;
      const yy = y(value);
      svg.append(make('line', { x1: left, y1: yy, x2: right, y2: yy, class: 'chart-grid-line' }));
      svg.append(textNode(left - 9, yy + 4, fmt(value), 'chart-axis-label'));
    }
    svg.append(make('line', { x1: left, y1: top, x2: left, y2: bottom, class: 'chart-axis-line' }));
    svg.append(make('line', { x1: left, y1: baseline, x2: right, y2: baseline, class: 'chart-axis-line' }));
    const groupWidth = width / labels.length;
    const activate = (seriesName, label, value) => {
      description.textContent = `${seriesName} · ${label}: ${fmt(value)}${chart.unit ? ' ' + chart.unit : ''}`;
    };

    if (chart.type === 'bar') {
      const barWidth = Math.min(38, groupWidth / (series.length + 0.5));
      const gap = Math.min(5, groupWidth * 0.06);
      labels.forEach((label, i) => {
        const total = series.length * barWidth + (series.length - 1) * gap;
        const startX = left + i * groupWidth + (groupWidth - total) / 2;
        series.forEach((line, j) => {
          const value = line.values[i];
          if (typeof value !== 'number' || !Number.isFinite(value)) return;
          const yy = y(value), rectY = Math.min(yy, baseline), rectH = Math.max(1, Math.abs(baseline - yy));
          const rect = make('rect', { x: startX + j * (barWidth + gap), y: rectY, width: barWidth,
            height: rectH, rx: 3, class: `chart-mark chart-series-${j}`, tabindex: 0,
            'aria-label': `${line.name}, ${label}: ${fmt(value)} ${chart.unit || ''}` });
          rect.append(make('title', {}, `${line.name} · ${label}: ${fmt(value)} ${chart.unit || ''}`));
          rect.addEventListener('pointerenter', () => activate(line.name, label, value));
          rect.addEventListener('focus', () => activate(line.name, label, value));
          rect.addEventListener('click', () => activate(line.name, label, value));
          svg.append(rect);
        });
        const labelText = label.length > 14 ? label.slice(0, 13) + '…' : label;
        const labelNode = textNode(left + i * groupWidth + groupWidth / 2, bottom + 19, labelText, 'chart-axis-label');
        labelNode.setAttribute('text-anchor', 'middle');
        labelNode.append(make('title', {}, label));
        svg.append(labelNode);
      });
    } else {
      labels.forEach((label, i) => {
        const x = left + (labels.length === 1 ? width / 2 : i * width / (labels.length - 1));
        const labelText = label.length > 14 ? label.slice(0, 13) + '…' : label;
        const labelNode = textNode(x, bottom + 19, labelText, 'chart-axis-label');
        labelNode.setAttribute('text-anchor', 'middle');
        labelNode.append(make('title', {}, label));
        svg.append(labelNode);
      });
      series.forEach((line, j) => {
        const points = line.values.map((value, i) => ({
          x: left + (labels.length === 1 ? width / 2 : i * width / (labels.length - 1)),
          y: y(value), value, label: labels[i]
        })).filter(point => typeof point.value === 'number' && Number.isFinite(point.value));
        if (!points.length) return;
        svg.append(make('polyline', { points: points.map(point => `${point.x},${point.y}`).join(' '),
          class: `chart-line chart-series-${j}`, fill: 'none' }));
        points.forEach(point => {
          const circle = make('circle', { cx: point.x, cy: point.y, r: 5, class: `chart-mark chart-series-${j}`,
            tabindex: 0, 'aria-label': `${line.name}, ${point.label}: ${fmt(point.value)} ${chart.unit || ''}` });
          circle.append(make('title', {}, `${line.name} · ${point.label}: ${fmt(point.value)} ${chart.unit || ''}`));
          circle.addEventListener('pointerenter', () => activate(line.name, point.label, point.value));
          circle.addEventListener('focus', () => activate(line.name, point.label, point.value));
          circle.addEventListener('click', () => activate(line.name, point.label, point.value));
          svg.append(circle);
        });
      });
    }

    const legend = document.createElement('div');
    legend.className = 'chart-legend';
    series.forEach((line, i) => {
      const item = document.createElement('span');
      item.className = 'chart-legend-item';
      const swatch = document.createElement('i');
      swatch.className = `chart-swatch chart-series-${i}`;
      const label = document.createElement('span');
      label.textContent = line.name;
      item.append(swatch, label);
      legend.append(item);
    });
    card.append(legend);
    target.append(card);
  }

  try {
    const charts = JSON.parse(source.textContent || '[]');
    if (Array.isArray(charts)) charts.forEach(draw);
    if (!target.children.length) {
      const empty = document.createElement('p');
      empty.className = 'muted small';
      empty.textContent = 'Для этой записи нет проверенных числовых данных для графиков.';
      target.append(empty);
    }
  } catch (_) {
    target.textContent = 'Не удалось отобразить графики.';
  }
})();
