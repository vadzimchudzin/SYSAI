/* Voice diagram on the meeting page: who spoke when, over the loudness of the recording (app/voice_diagram.py). */
(() => {
  const root = document.getElementById('voice-diagram');
  const source = document.getElementById('voice-diagram-data');

  // «Гость»: the name field is needed only for a guest. Works for every speaker form on the page.
  document.querySelectorAll('[data-vd-assign]').forEach(form => {
    const choice = form.querySelector('[data-vd-choice]');
    const guest = form.querySelector('[data-vd-guest]');
    if (!choice || !guest) return;
    const sync = () => { guest.hidden = choice.value !== 'guest'; };
    choice.addEventListener('change', sync);
    sync();
  });

  if (!root || !source) return;
  let data;
  try { data = JSON.parse(source.textContent); } catch (error) { return; }
  if (!data || !Array.isArray(data.turns) || !data.turns.length || !(data.duration > 0)) return;

  const NS = 'http://www.w3.org/2000/svg';
  const stage = root.querySelector('[data-vd-stage]');
  const detail = root.querySelector('[data-vd-detail]');
  const tools = root.querySelector('[data-vd-tools]');
  const zoomValue = root.querySelector('[data-vd-zoom-value]');
  const zoomButtons = [...root.querySelectorAll('[data-vd-zoom]')];
  const chips = [...root.querySelectorAll('[data-vd-speaker]')];
  const fixForm = root.querySelector('.vd-fix');
  // «Отметка голоса»: a part of the recording marked here goes into a person's voice profile (POST …/voice-marks).
  const markForm = root.querySelector('[data-vd-mark]');
  const markInfo = data.mark && typeof data.mark === 'object' ? data.mark : null;
  const marking = Boolean(markForm && markInfo && data.audio && data.audio.src);
  const MARK_MIN = marking ? Number(markInfo.min) || 5 : 5;
  const MARK_MAX = marking ? Number(markInfo.max) || 30 : 30;
  const savedSamples = marking && Array.isArray(markInfo.samples) ? markInfo.samples : [];  // [start, end, name, id, status]
  const markField = name => (markForm ? markForm.querySelector(`[data-vd-mark-${name}]`) : null);
  const markPlay = markField('play');
  let drag = null;          // drawing a mark with the mouse, or moving one of its edges
  let swallowUntil = 0;     // the click that ends a drawn mark does not select a replica
  const speakers = data.speakers || [];
  const turns = data.turns;              // [speaker, start, end, first segment, last segment]
  const segments = data.segments || [];  // [transcript index, speaker, start, end], by start time
  const duration = data.duration;
  const peaks = Array.isArray(data.peaks) && data.peaks.length ? data.peaks : null;
  const peaksDuration = peaks ? (data.peaksDuration || duration) : 0;
  const overlaps = data.overlaps || [];
  const ZOOMS = [1, 2, 4, 8, 16, 32];
  const LANE = 32;      // callout lane
  const CALLOUT = 24;   // callout height
  const GAP = 10;       // between callouts and the band
  const HEAD = 18;      // a row above the callouts for the time of the selected replica
  const state = { zoom: 1, current: -1, focus: -1, mark: null };
  const starts = turns.map(turn => turn[1]);
  const turnOfSegment = new Map();
  turns.forEach((turn, index) => {
    for (let i = turn[3]; i <= turn[4]; i += 1) if (segments[i]) turnOfSegment.set(segments[i][0], index);
  });
  const firstTurns = new Set();
  const seenSpeakers = new Set();
  turns.forEach((turn, index) => { if (!seenSpeakers.has(turn[0])) { seenSpeakers.add(turn[0]); firstTurns.add(index); } });

  const html = (tag, attrs = {}, text) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const svg = (tag, attrs = {}, text) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const pad = value => String(value).padStart(2, '0');
  const clock = seconds => {
    const whole = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    return hours ? `${hours}:${pad(minutes)}:${pad(whole % 60)}` : `${pad(minutes)}:${pad(whole % 60)}`;
  };
  const length = seconds => {
    const whole = Math.max(1, Math.round(seconds));
    return whole < 60 ? `${whole} с` : `${Math.floor(whole / 60)} мин${whole % 60 ? ` ${whole % 60} с` : ''}`;
  };
  const shortName = name => {
    if (name.length <= 18) return name;
    const words = name.split(/\s+/);
    if (words.length > 1 && words[0].length <= 15) return `${words[0]} ${words[1][0]}.`;
    return `${name.slice(0, 17)}…`;
  };
  const speakerOf = index => speakers[turns[index][0]] || { name: '', color: '#8f8c82' };

  // ---- scaffold ---------------------------------------------------------------------------------------------
  const scroller = html('div', { class: 'vd-scroll', tabindex: '0', role: 'group',
    'aria-label': 'Диаграмма голосов. Стрелки влево и вправо — соседние реплики, плюс и минус — масштаб.' });
  const canvas = html('div', { class: 'vd-canvas' });
  const drawing = svg('svg', { class: 'vd-svg', 'aria-hidden': 'true', focusable: 'false' });
  const calloutLayer = html('div', { class: 'vd-callouts' });
  const tip = html('div', { class: 'vd-tip', role: 'tooltip' });
  tip.hidden = true;
  const playhead = html('div', { class: 'vd-playhead', 'aria-hidden': 'true' });
  const playheadTime = html('time');
  playhead.append(playheadTime);
  playhead.hidden = true;
  const markLayer = html('div', { class: 'vd-mark-layer' });
  const markBox = html('div', { class: 'vd-mark-box', 'aria-hidden': 'true' });
  const markEdges = ['from', 'to'].map(edge => html('button', { type: 'button', class: `vd-mark-edge is-${edge}`,
    'data-vd-mark-edge': edge, 'aria-label': edge === 'from' ? 'Начало отметки: ← и → двигают' : 'Конец отметки: ← и → двигают' }));
  markLayer.append(markBox, ...markEdges);
  markLayer.hidden = true;
  canvas.append(drawing, markLayer, playhead, calloutLayer);
  scroller.append(canvas);
  stage.replaceChildren(scroller, tip);
  if (tools) tools.hidden = false;

  const measureContext = document.createElement('canvas').getContext('2d');
  const widths = new Map();
  const calloutWidth = (name, time) => {
    const key = `${name}\u0000${time}`;
    if (!widths.has(key)) {
      const family = getComputedStyle(root).fontFamily || 'sans-serif';
      measureContext.font = `500 12.5px ${family}`;
      const nameWidth = measureContext.measureText(name).width;
      measureContext.font = `400 11.5px ${family}`;
      widths.set(key, Math.ceil(nameWidth + measureContext.measureText(time).width + 40));
    }
    return widths.get(key);
  };

  const metrics = () => {
    const band = root.clientWidth < 560 ? 92 : 124;
    const top = HEAD + LANE * 2 + GAP;
    const bottom = top + band;
    const axis = bottom + GAP + LANE * 2 + 2;
    return { band, top, bottom, mid: top + band / 2, axis, height: axis + 24 };
  };
  const allowedZooms = viewport => ZOOMS.filter(z => z === 1 || (viewport * z / duration <= 80 && viewport * z <= 60000));

  function lastStartingBy(time) {
    let lo = 0;
    let hi = turns.length - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (starts[mid] <= time) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return found;
  }
  function turnAt(time) {
    const found = lastStartingBy(time);
    for (let i = found; i >= 0 && i >= found - 40; i -= 1) if (turns[i][2] >= time) return i;
    return -1;
  }
  function nearestTurn(time) {
    const inside = turnAt(time);
    if (inside >= 0) return inside;
    let best = 0;
    let distance = Infinity;
    turns.forEach((turn, index) => {
      const d = time < turn[1] ? turn[1] - time : time - turn[2];
      if (d < distance) { distance = d; best = index; }
    });
    return best;
  }
  function amplitude(from, to) {
    if (!peaks || from >= peaksDuration) return 0;
    const scale = peaks.length / peaksDuration;
    const a = from * scale;
    const b = Math.min(peaks.length, to * scale);
    if (b - a < 1) {  // zoomed past the stored resolution: interpolate between neighbouring values
      const position = Math.max(0, Math.min(peaks.length - 1, (a + b) / 2 - 0.5));
      const left = Math.floor(position);
      const right = Math.min(peaks.length - 1, left + 1);
      return (peaks[left] + (peaks[right] - peaks[left]) * (position - left)) / 100;
    }
    let sum = 0;  // a bar wider than the stored values: their root mean square keeps the shape, a maximum would flatten it
    let count = 0;
    for (let i = Math.floor(a); i < Math.ceil(b); i += 1) { sum += (peaks[i] || 0) ** 2; count += 1; }
    return count ? Math.sqrt(sum / count) / 100 : 0;
  }

  // ---- callout placement: two lanes above and two below the band, longest turns first ------------------------
  function placeCallouts(width, perSecond) {
    const lanes = [[], [], [], []];          // 0 top near, 1 top far, 2 bottom near, 3 bottom far
    const stems = [[], []];                  // x of far callouts' stems crossing the near lane: top, bottom
    const free = (lane, a, b) => lanes[lane].every(([l, r]) => b <= l || a >= r);
    const candidates = [];
    turns.forEach((turn, index) => {
      if (state.focus >= 0 && turn[0] !== state.focus) return;
      const name = shortName(speakers[turn[0]].name);
      const time = clock(turn[1]);
      candidates.push({ index, name, time, x: turn[1] * perSecond, width: calloutWidth(name, time),
        rank: (index === state.current ? 4e9 : 0) + (firstTurns.has(index) ? 2e9 : 0) + (turn[2] - turn[1]) });
    });
    candidates.sort((a, b) => b.rank - a.rank);
    const placed = [];
    for (const item of candidates) {
      const left = Math.max(0, Math.min(item.x - 11, width - item.width));
      const right = left + item.width;
      const order = item.index % 2 === 0 ? [0, 1, 2, 3] : [2, 3, 0, 1];
      for (const lane of order) {
        const side = lane < 2 ? 0 : 1;
        const near = lane % 2 === 0;
        if (!free(lane, left - 8, right + 8)) continue;
        if (near && stems[side].some(x => x > left - 3 && x < right + 3)) continue;
        if (!near && !free(lane - 1, item.x - 3, item.x + 3)) continue;
        lanes[lane].push([left, right]);
        if (!near) stems[side].push(item.x);
        placed.push({ ...item, lane, left });
        break;
      }
    }
    return placed;
  }

  // ---- drawing --------------------------------------------------------------------------------------------------
  function render() {
    const viewport = scroller.clientWidth;
    if (!viewport) return;
    const zooms = allowedZooms(viewport);
    if (!zooms.includes(state.zoom)) state.zoom = zooms[zooms.length - 1];
    const m = metrics();
    const width = Math.round(viewport * state.zoom);
    const perSecond = width / duration;
    const x = time => time * perSecond;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${m.height}px`;
    drawing.setAttribute('width', width);
    drawing.setAttribute('height', m.height);
    drawing.setAttribute('viewBox', `0 0 ${width} ${m.height}`);
    const dim = speaker => state.focus >= 0 && speaker !== state.focus;

    const defs = svg('defs');
    const hatch = svg('pattern', { id: 'vd-hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
    hatch.append(svg('line', { x1: 0, y1: 0, x2: 0, y2: 6, class: 'vd-hatch-line' }));
    defs.append(hatch);
    const band = svg('rect', { class: 'vd-band', x: 0.5, y: m.top + 0.5, width: Math.max(0, width - 1), height: m.band - 1, rx: 8 });

    const tints = svg('g');
    turns.forEach(turn => {
      const left = x(turn[1]);
      tints.append(svg('rect', { class: `vd-tint${dim(turn[0]) ? ' is-dim' : ''}`, x: left, y: m.top, width: Math.max(1, x(turn[2]) - left),
        height: m.band, fill: speakers[turn[0]].color }));
    });

    const shapes = svg('g', { class: peaks ? 'vd-bars' : 'vd-blocks' });
    if (peaks) {
      const step = 4;
      const half = m.band / 2 - 7;
      const paths = speakers.map(() => []);
      const silent = [];
      for (let i = 0; i * step < width; i += 1) {
        const from = (i * step) / perSecond;
        const to = Math.min(duration, ((i + 1) * step) / perSecond);
        const level = amplitude(from, to);
        const height = Math.max(1.2, level * half);
        const owner = turnAt((from + to) / 2);
        const cx = (i * step + step / 2).toFixed(1);
        (owner >= 0 ? paths[turns[owner][0]] : silent).push(`M${cx} ${(m.mid - height).toFixed(1)}V${(m.mid + height).toFixed(1)}`);
      }
      paths.forEach((list, speaker) => {
        if (list.length) shapes.append(svg('path', { d: list.join(''), stroke: speakers[speaker].color, 'stroke-width': 2, class: dim(speaker) ? 'is-dim' : '' }));
      });
      if (silent.length) shapes.append(svg('path', { d: silent.join(''), stroke: '#4a4842', 'stroke-width': 2 }));
    } else {
      const height = m.band * 0.42;
      turns.forEach(turn => {
        const left = x(turn[1]);
        shapes.append(svg('rect', { x: left, y: m.mid - height / 2, width: Math.max(2, x(turn[2]) - left), height, rx: 3,
          fill: speakers[turn[0]].color, class: dim(turn[0]) ? 'is-dim' : '' }));
      });
    }

    const cross = svg('g');
    overlaps.forEach(([from, to]) => {
      const left = x(from);
      cross.append(svg('rect', { x: left, y: m.top + 1, width: Math.max(3, x(to) - left), height: m.band - 2, fill: 'url(#vd-hatch)' }));
    });

    const kept = svg('g', { class: 'vd-samples' });  // the samples already cut from this meeting into voice profiles
    savedSamples.forEach(([from, to, name]) => {
      const left = x(from);
      const strip = svg('rect', { class: 'vd-sample-strip', x: left, y: m.bottom - 6, width: Math.max(3, x(to) - left), height: 4, rx: 2 });
      strip.append(svg('title', {}, `Образец голоса в профиле: ${name} · ${clock(from)}–${clock(to)}`));
      kept.append(strip);
    });

    const axis = svg('g');
    axis.append(svg('line', { class: 'vd-axis-line', x1: 0, y1: m.axis, x2: width, y2: m.axis }));
    const steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
    const tick = steps.find(value => value * perSecond >= 84) || 7200;
    for (let t = 0; t <= duration + 0.001; t += tick) {
      const tx = x(t);
      axis.append(svg('line', { class: 'vd-tick', x1: tx, y1: m.axis, x2: tx, y2: m.axis + 5 }));
      const anchor = tx < 20 ? 'start' : (width - tx < 20 ? 'end' : 'middle');
      axis.append(svg('text', { class: 'vd-tick-label', x: tx, y: m.axis + 18, 'text-anchor': anchor }, clock(t)));
    }

    const stems = svg('g');
    const buttons = [];
    for (const item of placeCallouts(width, perSecond)) {
      const turn = turns[item.index];
      const person = speakers[turn[0]];
      const topSide = item.lane < 2;
      const near = item.lane % 2 === 0;
      const y = topSide ? (near ? HEAD + LANE : HEAD) : (near ? m.bottom + GAP : m.bottom + GAP + LANE);
      const y1 = topSide ? y + CALLOUT : m.bottom;
      const y2 = topSide ? m.top : y;
      stems.append(svg('line', { class: `vd-stem${dim(turn[0]) ? ' is-dim' : ''}`, x1: item.x, y1, x2: item.x, y2, stroke: person.color }));
      const button = html('button', { type: 'button', class: `vd-callout${item.index === state.current ? ' is-current' : ''}`,
        style: `left:${item.left}px;top:${y}px;--c:${person.color}`, 'data-turn': item.index,
        'aria-label': `${person.name}, ${item.time}, ${length(turn[2] - turn[1])}` });
      button.append(html('i', { class: 'vd-dot', 'aria-hidden': 'true' }), document.createTextNode(item.name), html('time', {}, item.time));
      buttons.push(button);
    }

    const cursor = svg('g', { class: 'vd-cursor' });
    if (state.current >= 0) {
      const cx = x(turns[state.current][1]);
      cursor.append(svg('line', { class: 'vd-cursor-line', x1: cx, y1: HEAD - 2, x2: cx, y2: m.axis }));
      const label = clock(turns[state.current][1]);
      const right = width - cx < 52;
      cursor.append(svg('text', { class: 'vd-cursor-label', x: right ? cx - 5 : cx + 5, y: 12, 'text-anchor': right ? 'end' : 'start' }, label));
    }
    hoverLine = svg('line', { class: 'vd-hover-line', x1: 0, y1: m.top, x2: 0, y2: m.bottom, visibility: 'hidden' });
    const hit = svg('rect', { class: 'vd-hit', x: 0, y: m.top, width, height: m.band });

    drawing.replaceChildren(defs, band, tints, shapes, cross, kept, axis, stems, cursor, hoverLine, hit);
    calloutLayer.replaceChildren(...buttons);
    buttons.forEach(button => {  // the text was measured before fonts settled: never let a callout leave the canvas
      const left = parseFloat(button.style.left);
      if (left + button.offsetWidth > width) button.style.left = `${Math.max(0, width - button.offsetWidth)}px`;
    });
    layout = { width, perSecond, m };
    paintMark();
    paintPlayhead();
    if (zoomValue) zoomValue.textContent = `×${state.zoom}`;
    zoomButtons.forEach(button => {
      const index = zooms.indexOf(state.zoom);
      button.disabled = Number(button.dataset.vdZoom) < 0 ? index <= 0 : index >= zooms.length - 1;
    });
  }
  let layout = null;
  let hoverLine = null;

  // ---- selection, the reading panel and the transcript ---------------------------------------------------------
  let marked = [];
  function rowsOf(index) {
    const turn = turns[index];
    const rows = [];
    for (let i = turn[3]; i <= turn[4]; i += 1) {
      const row = segments[i] && document.getElementById(`seg-${segments[i][0]}`);
      if (row) rows.push(row);
    }
    return rows;
  }
  function markTranscript(index) {
    marked.forEach(row => { row.classList.remove('vd-current'); row.style.removeProperty('--c'); });
    marked = index >= 0 ? rowsOf(index) : [];
    const color = index >= 0 ? speakerOf(index).color : '';
    marked.forEach(row => { row.classList.add('vd-current'); row.style.setProperty('--c', color); });
    const first = marked[0];
    const box = first && first.closest('.transcript');
    if (box && box.getClientRects().length) {  // keep the open transcript near the replica without moving the page
      const delta = first.getBoundingClientRect().top - box.getBoundingClientRect().top;
      if (delta < 0 || delta > box.clientHeight - 40) box.scrollTop += delta - 60;
    }
  }
  function textOf(index) {
    return rowsOf(index).map(row => {
      const copy = row.cloneNode(true);
      copy.querySelectorAll('.t, b').forEach(node => node.remove());
      return copy.textContent.trim();
    }).filter(Boolean);
  }
  function neighbour(direction) {
    let index = state.current;
    if (index < 0) return direction > 0 ? firstMatching(0, 1) : firstMatching(turns.length - 1, -1);
    return firstMatching(index + direction, direction);
  }
  function firstMatching(from, direction) {
    for (let i = from; i >= 0 && i < turns.length; i += direction) if (state.focus < 0 || turns[i][0] === state.focus) return i;
    return -1;
  }
  function showDetail(index) {
    if (!detail) return;
    if (index < 0) { detail.hidden = true; return; }
    const turn = turns[index];
    const person = speakerOf(index);
    const own = turns.filter(item => item[0] === turn[0]);
    const position = own.indexOf(turn) + 1;
    detail.style.setProperty('--c', person.color);
    const head = html('div', { class: 'vd-detail-head' });
    const who = html('div', { class: 'vd-detail-who' });
    who.append(html('i', { class: 'vd-dot', 'aria-hidden': 'true', style: `--c:${person.color}` }), html('b', {}, person.name),
      html('span', { class: 't' }, `${clock(turn[1])}–${clock(turn[2])}`),
      html('span', { class: 'small muted' }, `${length(turn[2] - turn[1])} · реплика ${position} из ${own.length}`));
    const nav = html('div', { class: 'vd-detail-nav' });
    const previous = html('button', { type: 'button', class: 'btn sm', 'data-vd-step': '-1' }, '← Предыдущая');
    const next = html('button', { type: 'button', class: 'btn sm', 'data-vd-step': '1' }, 'Следующая →');
    previous.disabled = neighbour(-1) < 0;
    next.disabled = neighbour(1) < 0;
    nav.append(previous, next);
    if (canPlay) {
      const listen = html('button', { type: 'button', class: 'btn sm vd-play-turn', 'data-vd-play-turn': '' });
      listen.append(...playIcons(), html('span', {}, 'Слушать реплику'));
      nav.append(listen);
    }
    if (rowsOf(index).length) nav.append(html('button', { type: 'button', class: 'btn sm', 'data-vd-open': '' }, 'В расшифровке'));
    if (marking) nav.append(html('button', { type: 'button', class: 'btn sm', 'data-vd-mark-turn': '',
      title: 'Отметить эту реплику на волне, прослушать и сохранить голос в профиль человека' }, 'Отметить голос'));
    if (fixForm) nav.append(html('button', { type: 'button', class: 'btn sm ghost', 'data-vd-fix': '' }, 'Исправить говорящего'));
    head.append(who, nav);
    const body = html('div', { class: 'vd-detail-text' });
    const parts = textOf(index);
    if (parts.length) parts.forEach(part => body.append(html('p', {}, part)));
    else body.append(html('p', { class: 'muted' }, 'Текст реплики есть в расшифровке ниже.'));
    detail.replaceChildren(head, body);
    detail.hidden = false;
  }
  function reveal(time) {
    if (!layout) return;
    const position = time * layout.perSecond;
    const view = scroller.clientWidth;
    if (position < scroller.scrollLeft + 24 || position > scroller.scrollLeft + view - 24) {
      scroller.scrollLeft = Math.max(0, position - view / 3);
    }
  }
  // A click, an arrow or a time stamp selects a replica and moves the listening position there (`at`: an exact time);
  // `follow` is the player moving the selection along with the sound, without seeking.
  function select(index, { focusCallout = false, at, follow = false } = {}) {
    if (index < 0 || index >= turns.length) return;
    const focused = scroller.contains(document.activeElement);
    state.current = index;
    render();
    if (!follow) reveal(at === undefined ? turns[index][1] : at);
    showDetail(index);
    markTranscript(index);
    if (focusCallout) {
      const button = calloutLayer.querySelector(`[data-turn="${index}"]`);
      if (button) button.focus({ preventScroll: true });
    } else if (focused && !scroller.contains(document.activeElement)) scroller.focus({ preventScroll: true });
    if (!follow) cue(at === undefined ? turns[index][1] : at, at !== undefined);
    else syncControls();
  }

  function zoomBy(direction, anchorX) {
    const viewport = scroller.clientWidth;
    const zooms = allowedZooms(viewport);
    const next = zooms[Math.max(0, Math.min(zooms.length - 1, zooms.indexOf(state.zoom) + direction))];
    if (!next || next === state.zoom || !layout) return;
    let offset;
    if (anchorX !== undefined) offset = anchorX - scroller.getBoundingClientRect().left;
    else if (state.current >= 0) {
      offset = turns[state.current][1] * layout.perSecond - scroller.scrollLeft;
      if (offset < 0 || offset > viewport) offset = viewport / 2;
    } else offset = viewport / 2;
    const time = (scroller.scrollLeft + offset) / layout.perSecond;
    state.zoom = next;
    render();
    scroller.scrollLeft = Math.max(0, time * layout.perSecond - offset);
  }

  function setFocus(speaker) {
    state.focus = state.focus === speaker ? -1 : speaker;
    chips.forEach(chip => chip.setAttribute('aria-pressed', String(Number(chip.dataset.vdSpeaker) === state.focus)));
    root.classList.toggle('is-focused', state.focus >= 0);
    if (state.focus >= 0) {
      const picker = fixForm && fixForm.querySelector('select[name="speaker"]');
      if (picker && speakers[state.focus]) picker.value = speakers[state.focus].name;
      if (state.current >= 0 && turns[state.current][0] !== state.focus) state.current = -1;
    }
    render();
    showDetail(state.current);
    markTranscript(state.current);
  }

  // ---- listening: the kept recording (audio_archive.py) from the selected place ---------------------------------
  const playButton = root.querySelector('[data-vd-play]');
  const playLabel = root.querySelector('[data-vd-play-label]');
  const rateButton = root.querySelector('[data-vd-rate]');
  const playError = root.querySelector('[data-vd-play-error]');
  const audioSource = data.audio && typeof data.audio.src === 'string' ? data.audio.src : '';
  const canPlay = Boolean(audioSource && playButton);
  const RATES = [1, 1.25, 1.5, 2];
  const RATE_KEY = 'sysai.voiceDiagram.rate';
  const player = { audio: null, position: 0, armed: false, playing: false, loading: false, stopAt: null, quote: null,
    turn: -1, pendingSeek: null, retried: false, frame: 0, scrolledAt: 0, rate: 1 };
  const segmentBySeq = new Map(segments.map(item => [item[0], item]));
  const clampTime = time => Math.max(0, Math.min(duration, Number(time) || 0));
  const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  function playIcons() {
    if (!playButton) return [];
    return ['.vd-ico-play', '.vd-ico-pause'].map(selector => {
      const icon = playButton.querySelector(selector);
      return icon ? icon.cloneNode(true) : document.createTextNode('');
    });
  }
  function paintPlayhead() {
    const shown = canPlay && player.armed && layout;
    playhead.hidden = !shown;
    root.classList.toggle('has-playhead', Boolean(shown));
    if (!shown) return;
    const x = player.position * layout.perSecond;
    playhead.style.height = `${layout.m.axis}px`;
    playhead.style.transform = `translateX(${x.toFixed(1)}px)`;
    playhead.classList.toggle('is-right', layout.width - x < 60);
    playheadTime.textContent = clock(player.position);
  }
  function syncControls() {
    if (!canPlay) return;
    const playing = player.playing;
    playButton.classList.toggle('is-playing', playing);
    playButton.classList.toggle('is-loading', playing && player.loading);
    playButton.setAttribute('aria-label', playing ? 'Пауза' : 'Слушать');
    if (playLabel) playLabel.textContent = playing ? 'Пауза' : 'Слушать';
    root.classList.toggle('is-playing', playing);
    document.querySelectorAll('[data-vd-play-seq]').forEach(button => {
      const on = playing && player.quote === Number(button.dataset.vdPlaySeq);
      button.classList.toggle('is-playing', on);
      button.setAttribute('aria-label', `${on ? 'Пауза' : 'Прослушать фрагмент'} ${button.dataset.label || ''}`.trim());
    });
    document.querySelectorAll('[data-vd-play-at]').forEach(button => {
      const on = playing && player.quote === button.dataset.vdPlayKey;
      button.classList.toggle('is-playing', on);
      button.setAttribute('aria-label', `${on ? 'Пауза' : 'Прослушать момент'} ${button.dataset.label || ''}`.trim());
    });
    if (markPlay) {
      const on = playing && player.quote === 'mark';
      markPlay.classList.toggle('is-playing', on);
      const text = markPlay.querySelector('span:last-child');
      if (text) text.textContent = on ? 'Пауза' : 'Слушать отметку';
    }
    const listen = detail && detail.querySelector('[data-vd-play-turn]');
    if (listen) {
      const on = playing && player.turn >= 0 && player.turn === state.current;
      listen.classList.toggle('is-playing', on);
      const text = listen.querySelector('span:last-child');
      if (text) text.textContent = on ? 'Пауза' : 'Слушать реплику';
    }
  }
  function showError(text) {
    if (!playError) return;
    playError.textContent = text;
    playError.hidden = !text;
  }
  function fresh() {  // the page's address answers with a new short-lived link to the storage
    return `${audioSource}${audioSource.includes('?') ? '&' : '?'}t=${Date.now()}`;
  }
  function seekAudio(time) {
    const audio = player.audio;
    if (!audio) return;
    if (audio.readyState >= 1) {
      try { audio.currentTime = time; player.pendingSeek = null; return; } catch (error) { /* not seekable yet */ }
    }
    player.pendingSeek = time;
  }
  // The listening position: where «Слушать» starts, or where the sound jumps while it plays.
  function cue(time, exact = false) {
    if (!canPlay) return;
    player.position = clampTime(time);
    player.stopAt = null;
    player.quote = null;
    player.turn = -1;
    if (exact) player.armed = true;
    if (player.playing) seekAudio(player.position);
    paintPlayhead();
    syncControls();
  }
  function ensureAudio() {
    if (player.audio) return player.audio;
    const audio = new Audio();
    audio.preload = 'auto';
    audio.defaultPlaybackRate = player.rate;
    audio.playbackRate = player.rate;
    audio.addEventListener('loadedmetadata', () => {
      audio.playbackRate = player.rate;
      if (player.pendingSeek !== null) seekAudio(player.pendingSeek);
    });
    audio.addEventListener('waiting', () => { player.loading = true; syncControls(); });
    audio.addEventListener('playing', () => {
      player.loading = false;
      player.retried = false;
      showError('');
      syncControls();
    });
    audio.addEventListener('pause', () => {
      if (!player.playing) return;
      player.playing = false;
      player.loading = false;
      syncControls();
    });
    audio.addEventListener('ended', () => {
      player.playing = false;
      player.loading = false;
      player.position = duration;
      paintPlayhead();
      syncControls();
    });
    audio.addEventListener('timeupdate', () => {  // the animation frames stop in a background tab, this does not
      if (player.playing && document.hidden) advance(audio.currentTime);
    });
    audio.addEventListener('error', recover);
    audio.src = audioSource;
    player.audio = audio;
    return audio;
  }
  // A link to the storage lives a few hours: after a long pause, or a network failure, ask the page for a new one once.
  function recover() {
    const audio = player.audio;
    if (!audio || !audio.error) return;
    const resume = player.playing;
    if (!player.retried) {
      player.retried = true;
      player.pendingSeek = player.position;
      audio.src = fresh();
      audio.load();
      if (resume) start();
      return;
    }
    player.playing = false;
    player.loading = false;
    syncControls();
    showError(audio.error.code === 4
      ? 'Запись не открылась: хранилище недоступно или файл удалён. Обновите страницу; если не поможет — Настройки → «Проверить хранилище».'
      : 'Запись не загрузилась: проверьте соединение и нажмите «Слушать» ещё раз.');
  }
  function start() {
    const audio = player.audio;
    player.playing = true;
    player.loading = audio.readyState < 3;
    syncControls();
    const begun = audio.play();
    if (begun && typeof begun.catch === 'function') {
      begun.catch(error => {
        if (error && error.name === 'AbortError') return;  // paused or reloaded before it started
        if (audio.error) return;                            // the error handler explains it
        player.playing = false;
        player.loading = false;
        syncControls();
        if (!error || error.name !== 'NotAllowedError') showError('Не удалось начать воспроизведение, попробуйте ещё раз.');
      });
    }
    loop();
  }
  function play({ from, until = null, quote = null, turn = -1 } = {}) {
    if (!canPlay) return;
    const audio = ensureAudio();
    if (audio.error) {  // a previous failure: try again with a new link
      player.retried = true;
      audio.src = fresh();
      audio.load();
    }
    showError('');
    if (from !== undefined) player.position = clampTime(from);
    if (player.position >= duration - 0.25) player.position = 0;
    player.stopAt = until;
    player.quote = quote;
    player.turn = turn;
    player.armed = true;
    if (audio.readyState < 1 || Math.abs(audio.currentTime - player.position) > 0.05) seekAudio(player.position);
    audio.playbackRate = player.rate;
    paintPlayhead();
    start();
  }
  function pause() {
    player.playing = false;
    player.loading = false;
    if (player.audio) player.audio.pause();
    syncControls();
  }
  const toggle = () => (player.playing ? pause() : play());

  // While a speaker is selected only their replicas sound: a gap or someone else's replica is skipped.
  function focusedTurnAt(time) {
    const found = lastStartingBy(time + 0.25);
    for (let i = found; i >= 0 && i >= found - 60; i -= 1) if (turns[i][0] === state.focus && turns[i][2] >= time) return i;
    return -1;
  }
  function nextFocusedTurn(time) {
    for (let i = Math.max(0, lastStartingBy(time)); i < turns.length; i += 1) {
      if (turns[i][0] === state.focus && turns[i][1] > time) return i;
    }
    return -1;
  }
  // Checks the sound's position: stops at the end of a replica or fragment, skips to the selected speaker. Returns the
  // position, or null when playback stopped.
  function advance(time) {
    if (player.stopAt !== null && time >= player.stopAt) {
      const end = player.stopAt;
      pause();
      player.position = clampTime(end);
      paintPlayhead();
      return null;
    }
    if (state.focus >= 0 && player.stopAt === null && focusedTurnAt(time) < 0) {
      const next = nextFocusedTurn(time);
      if (next < 0) {
        pause();
        return null;
      }
      seekAudio(turns[next][1]);
      return turns[next][1];
    }
    return time;
  }
  function keepInView(time) {
    if (!layout || performance.now() - player.scrolledAt < 4000) return;  // the user is looking around: do not pull
    const x = time * layout.perSecond;
    const view = scroller.clientWidth;
    if (x < scroller.scrollLeft + 8 || x > scroller.scrollLeft + view - 24) scroller.scrollLeft = Math.max(0, x - view * 0.2);
  }
  function loop() {
    if (player.frame) return;
    const frame = () => {
      player.frame = 0;
      const audio = player.audio;
      if (!audio || !player.playing) return;
      const time = advance(player.pendingSeek !== null ? player.pendingSeek : audio.currentTime);
      if (time === null) return;
      player.position = clampTime(time);
      if (player.stopAt === null) {
        const index = state.focus >= 0 ? focusedTurnAt(player.position) : turnAt(player.position);
        if (index >= 0 && index !== state.current) select(index, { follow: true });
      }
      paintPlayhead();
      keepInView(player.position);
      player.frame = requestAnimationFrame(frame);
    };
    player.frame = requestAnimationFrame(frame);
  }

  if (canPlay) {
    try {
      const saved = Number(localStorage.getItem(RATE_KEY));
      if (RATES.includes(saved)) player.rate = saved;
    } catch (error) { /* private mode */ }
    const rateText = rate => `${String(rate).replace('.', ',')}×`;
    const syncRate = () => {
      if (!rateButton) return;
      rateButton.textContent = rateText(player.rate);
      rateButton.setAttribute('aria-label', `Скорость воспроизведения ${rateText(player.rate)}`);
      rateButton.classList.toggle('is-changed', player.rate !== 1);
    };
    syncRate();
    if (rateButton) {
      rateButton.addEventListener('click', () => {
        player.rate = RATES[(RATES.indexOf(player.rate) + 1) % RATES.length];
        try { localStorage.setItem(RATE_KEY, String(player.rate)); } catch (error) { /* private mode */ }
        if (player.audio) {
          player.audio.defaultPlaybackRate = player.rate;
          player.audio.playbackRate = player.rate;
        }
        syncRate();
      });
    }
    playButton.addEventListener('click', toggle);
    document.documentElement.classList.add('vd-can-play');  // the task quotes' ▶ (meeting.html), re-rendered by app.js
    document.querySelectorAll('[data-vd-play-seq]').forEach(button => {
      button.dataset.label = (button.getAttribute('aria-label') || '').replace(/^Прослушать фрагмент\s*/, '');
      button.hidden = false;
    });
    // «Кто это?»: a fragment sounds from its start to its end, the page stays where it is.
    document.addEventListener('click', event => {
      const button = event.target.closest('[data-vd-play-seq]');
      if (!button) return;
      event.preventDefault();
      const seq = Number(button.dataset.vdPlaySeq);
      if (player.playing && player.quote === seq) { pause(); return; }
      const segment = segmentBySeq.get(seq);
      const from = segment ? segment[2] : Number(button.dataset.from);
      const to = segment ? segment[3] : Number(button.dataset.to);
      const index = turnOfSegment.get(seq);
      if (index !== undefined) {
        if (state.focus >= 0 && turns[index][0] !== state.focus) setFocus(state.focus);
        select(index, { at: from });
      }
      play({ from, until: to > from ? to : null, quote: seq });
    });
    // A task's quote «▶ 19:39»: the stamp is a whole second, so the sound starts with the segment that begins within it
    // (or at the stamp itself) and stops at the end of that replica.
    const segmentFrom = at => {
      let lo = 0;
      let hi = segments.length - 1;
      let found = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (segments[mid][2] >= at) { found = mid; hi = mid - 1; } else lo = mid + 1;
      }
      return found >= 0 && segments[found][2] < at + 1 ? segments[found] : null;
    };
    document.addEventListener('click', event => {
      const button = event.target.closest('[data-vd-play-at]');
      if (!button) return;
      event.preventDefault();
      const key = button.dataset.vdPlayKey;
      if (player.playing && player.quote === key) { pause(); return; }
      const at = clampTime(button.dataset.vdPlayAt);
      const segment = segmentFrom(at);
      const from = segment ? segment[2] : at;
      const index = segment && turnOfSegment.has(segment[0]) ? turnOfSegment.get(segment[0]) : nearestTurn(from);
      const end = index >= 0 ? turns[index][2] : null;
      if (index >= 0) {
        if (state.focus >= 0 && turns[index][0] !== state.focus) setFocus(state.focus);
        select(index, { at: from });
      }
      play({ from, until: end !== null && end > from + 0.5 ? end : null, quote: key });
    });
    const looked = () => { player.scrolledAt = performance.now(); };
    scroller.addEventListener('wheel', looked, { passive: true });
    scroller.addEventListener('touchstart', looked, { passive: true });
    scroller.addEventListener('pointerdown', event => { if (event.target === scroller) looked(); });
  }

  // ---- «Отметка голоса»: a part of the recording for a person's voice profile ------------------------------------
  const replicasOf = speakers.map((_, index) => segments.filter(item => item[1] === index).length);
  const precise = seconds => `${clock(seconds)},${Math.floor((Math.max(0, seconds) % 1) * 10)}`;
  const secondsText = seconds => `${String(Math.round(seconds * 10) / 10).replace('.', ',')} с`;
  const shared = (a, b, mark) => Math.min(b, mark.to) - Math.max(a, mark.from);
  const timeAt = clientX => clampTime((clientX - drawing.getBoundingClientRect().left) / layout.perSecond);

  // The mark between a fixed edge and a moving one; never longer than a sample may be.
  function setMark(fixed, moving) {
    let from = Math.min(fixed, moving);
    let to = Math.max(fixed, moving);
    if (to - from > MARK_MAX) {
      if (moving > fixed) to = from + MARK_MAX;
      else from = to - MARK_MAX;
    }
    state.mark = { from: clampTime(from), to: clampTime(to) };
    paintMark();
    updateMark();
  }
  function clearMark() {
    if (player.playing && player.quote === 'mark') pause();
    state.mark = null;
    paintMark();
    updateMark();
  }
  function paintMark() {
    if (!marking) return;
    const mark = state.mark;
    markLayer.hidden = !mark || !layout;
    if (!mark || !layout) return;
    const left = mark.from * layout.perSecond;
    const width = Math.max(2, (mark.to - mark.from) * layout.perSecond);
    markLayer.style.top = `${layout.m.top}px`;
    markLayer.style.height = `${layout.m.band}px`;
    markBox.style.left = `${left}px`;
    markBox.style.width = `${width}px`;
    markEdges[0].style.left = `${left}px`;
    markEdges[1].style.left = `${left + width}px`;
    markLayer.classList.toggle('is-short', mark.to - mark.from < MARK_MIN);
  }
  // What is in the mark by the transcript; the same rules sign the replicas on the server (voice_diagram.inside).
  function inMark(mark) {
    const spoken = new Map();
    let inside = 0;
    segments.forEach(([, speaker, start, end]) => {
      if (end > start) {
        const common = shared(start, end, mark);
        if (common > 0) spoken.set(speaker, (spoken.get(speaker) || 0) + common);
        if (common > 0.5 * (end - start)) inside += 1;
      } else if (start >= mark.from && start < mark.to) inside += 1;
    });
    const ranked = [...spoken.entries()].sort((a, b) => b[1] - a[1]);
    const total = ranked.reduce((sum, item) => sum + item[1], 0);
    return { ranked, inside, main: ranked.length && ranked[0][1] >= 0.8 * total ? ranked[0][0] : -1 };
  }
  function showProfile() {
    const who = markField('who');
    const line = markField('profile');
    if (!who || !line) return;
    if (!who.value) { line.textContent = ''; return; }
    const count = Number((markInfo.profiles || {})[who.value] || 0);
    const need = Number(markInfo.need) || 3;
    const here = savedSamples.some(item => String(item[3]) === who.value);
    if (!count) line.textContent = here
      ? 'Образец с этого совещания уже есть, но признаки голоса ещё не извлечены — проверьте его в карточке сотрудника.'
      : 'В профиле голоса пока нет образцов: этот будет первым, и голос начнут узнавать со следующего совещания.';
    else line.textContent = `В профиле голоса записей с разных совещаний: ${count}${count < need ? ` (надёжнее с ${need})` : ''}.`
      + (here ? ' С этого совещания образец уже есть — новый уточнит голос, но записей не прибавит.' : '');
  }
  function updateMark() {
    if (!marking) return;
    const mark = state.mark;
    markForm.hidden = !mark;
    if (!mark) return;
    const length = mark.to - mark.from;
    markField('start').value = mark.from.toFixed(3);
    markField('end').value = mark.to.toFixed(3);
    markField('range').textContent = `${precise(mark.from)}–${precise(mark.to)}`;
    markField('length').textContent = secondsText(length);
    const { ranked, inside, main } = inMark(mark);
    markField('inside').textContent = ranked.length
      ? `По расшифровке в отметке: ${ranked.map(([speaker, seconds]) => `${speakers[speaker].name} — ${secondsText(seconds)}`).join(', ')}.`
      : 'В отметке нет реплик по расшифровке.';
    const warnings = [];
    let blocked = false;
    if (length < MARK_MIN) {
      warnings.push(`Отметка короче ${MARK_MIN} с — растяните её за край.`);
      blocked = true;
    }
    const taken = savedSamples.find(([from, to]) => shared(from, to, mark) > 0);
    if (taken) {
      warnings.push(`Эта часть уже сохранена образцом голоса (${taken[2]}) — отметьте другое место.`);
      blocked = true;
    }
    if (overlaps.some(([from, to]) => shared(from, to, mark) > 0.3)) {
      warnings.push('Здесь говорят одновременно (штриховка), а в образце должен быть один голос.');
    }
    if (ranked.filter(item => item[1] >= 0.5).length > 1) {
      warnings.push('По расшифровке здесь говорят несколько человек — проверьте на слух, что голос один.');
    }
    const warn = markField('warn');
    warn.textContent = warnings.join(' ');
    warn.hidden = !warnings.length;
    markField('save').disabled = blocked;
    markField('apply-mark').textContent = `Реплики в отметке (${inside})`;
    const labelRow = markField('label-row');
    labelRow.hidden = main < 0;
    if (main >= 0) markField('apply-label').textContent = `Все реплики «${speakers[main].name}» (${replicasOf[main]})`;
    else if (labelRow.querySelector('input').checked) markForm.querySelector('input[name="apply"][value="mark"]').checked = true;
    const who = markField('who');
    const known = main >= 0 ? speakers[main].employee : null;
    if (known && !who.value && [...who.options].some(option => option.value === String(known))) who.value = String(known);
    showProfile();
    if (player.quote === 'mark') player.stopAt = mark.to;
  }

  if (marking) {
    drawing.addEventListener('pointerdown', event => {
      if (!layout || event.button !== 0 || event.pointerType === 'touch' || !event.target.classList.contains('vd-hit')) return;
      drag = { id: event.pointerId, x: event.clientX, fixed: timeAt(event.clientX), moved: false };
    });
    markEdges.forEach(handle => {
      handle.addEventListener('pointerdown', event => {
        if (!state.mark || event.button !== 0) return;
        event.preventDefault();
        drag = { id: event.pointerId, moved: true, fixed: handle.dataset.vdMarkEdge === 'from' ? state.mark.to : state.mark.from };
        try { handle.setPointerCapture(event.pointerId); } catch (error) { /* an old browser */ }
      });
      handle.addEventListener('keydown', event => {
        if (!state.mark || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) return;
        event.preventDefault();
        event.stopPropagation();  // the diagram's arrows go to the neighbouring replicas
        const step = (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 1 : 0.1);
        if (handle.dataset.vdMarkEdge === 'from') setMark(state.mark.to, clampTime(state.mark.from + step));
        else setMark(state.mark.from, clampTime(state.mark.to + step));
      });
    });
    window.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.id || !layout) return;
      if (!drag.moved) {
        if (Math.abs(event.clientX - drag.x) < 5) return;
        drag.moved = true;
        try { drawing.setPointerCapture(drag.id); } catch (error) { /* an old browser */ }
        hideTip();
      }
      setMark(drag.fixed, timeAt(event.clientX));
    });
    const drop = event => {
      if (!drag || event.pointerId !== drag.id) return;
      if (drag.moved) swallowUntil = performance.now() + 400;
      drag = null;
    };
    window.addEventListener('pointerup', drop);
    window.addEventListener('pointercancel', drop);
    markForm.addEventListener('click', event => {
      if (event.target.closest('[data-vd-mark-play]') && state.mark) {
        if (player.playing && player.quote === 'mark') { pause(); return; }
        play({ from: state.mark.from, until: state.mark.to, quote: 'mark' });
        return;
      }
      if (event.target.closest('[data-vd-mark-clear]')) clearMark();
    });
    markField('who').addEventListener('change', showProfile);
    markForm.addEventListener('submit', event => {
      if (!state.mark || state.mark.to - state.mark.from < MARK_MIN) { event.preventDefault(); return; }
      markField('save').disabled = true;  // one sample per press: the voice service is asked once
    });
  }

  // ---- events ---------------------------------------------------------------------------------------------------
  calloutLayer.addEventListener('click', event => {
    const button = event.target.closest('[data-turn]');
    if (button) select(Number(button.dataset.turn), { focusCallout: true });
  });
  drawing.addEventListener('click', event => {
    if (!event.target.classList.contains('vd-hit') || !layout || performance.now() < swallowUntil) return;
    const time = clampTime((event.clientX - drawing.getBoundingClientRect().left) / layout.perSecond);
    select(nearestTurn(time), { at: time });
  });
  drawing.addEventListener('pointermove', event => {
    if (!event.target.classList.contains('vd-hit') || !layout || (drag && drag.moved)) { hideTip(); return; }
    const offset = event.clientX - drawing.getBoundingClientRect().left;
    const time = offset / layout.perSecond;
    const index = turnAt(time);
    hoverLine.setAttribute('x1', offset);
    hoverLine.setAttribute('x2', offset);
    hoverLine.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    if (index >= 0) {
      const turn = turns[index];
      const person = speakerOf(index);
      tip.append(html('i', { class: 'vd-dot', style: `--c:${person.color}` }), html('b', {}, person.name),
        document.createTextNode(` · ${clock(turn[1])}–${clock(turn[2])} · ${length(turn[2] - turn[1])}`));
    } else {
      tip.append(document.createTextNode(`${clock(time)} · без речи в расшифровке`));
    }
    const sample = savedSamples.find(([from, to]) => time >= from && time <= to);
    if (sample) tip.append(document.createTextNode(` · образец голоса: ${sample[2]}`));
    const stageBox = stage.getBoundingClientRect();
    tip.style.left = `${Math.max(90, Math.min(stageBox.width - 90, event.clientX - stageBox.left))}px`;
    tip.style.top = `${layout.m.top}px`;
    tip.hidden = false;
  });
  const hideTip = () => {
    tip.hidden = true;
    if (hoverLine) hoverLine.setAttribute('visibility', 'hidden');
  };
  drawing.addEventListener('pointerleave', hideTip);
  scroller.addEventListener('scroll', hideTip, { passive: true });

  scroller.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === ' ' && canPlay) { event.preventDefault(); toggle(); return; }
    let target = -2;
    if (event.key === 'ArrowRight') target = neighbour(1);
    else if (event.key === 'ArrowLeft') target = neighbour(-1);
    else if (event.key === 'Home') target = firstMatching(0, 1);
    else if (event.key === 'End') target = firstMatching(turns.length - 1, -1);
    else if (event.key === '+' || event.key === '=') { zoomBy(1); event.preventDefault(); return; }
    else if (event.key === '-' || event.key === '_') { zoomBy(-1); event.preventDefault(); return; }
    else return;
    event.preventDefault();
    if (target >= 0) select(target, { focusCallout: document.activeElement !== scroller });
  });
  let wheel = 0;
  scroller.addEventListener('wheel', event => {
    if (!event.ctrlKey && !event.metaKey) return;  // pinch on a trackpad arrives as ctrl + wheel
    event.preventDefault();
    wheel += event.deltaY;
    if (Math.abs(wheel) < 40) return;
    zoomBy(wheel < 0 ? 1 : -1, event.clientX);
    wheel = 0;
  }, { passive: false });

  zoomButtons.forEach(button => button.addEventListener('click', () => zoomBy(Number(button.dataset.vdZoom))));
  chips.forEach(chip => chip.addEventListener('click', () => setFocus(Number(chip.dataset.vdSpeaker))));

  if (detail) {
    detail.addEventListener('click', event => {
      if (event.target.closest('[data-vd-play-turn]') && state.current >= 0) {
        if (player.playing && player.turn === state.current) { pause(); return; }
        const turn = turns[state.current];
        play({ from: turn[1], until: turn[2], turn: state.current });
        return;
      }
      const step = event.target.closest('[data-vd-step]');
      if (step) {
        const target = neighbour(Number(step.dataset.vdStep));
        if (target >= 0) select(target);
        return;
      }
      if (event.target.closest('[data-vd-open]') && state.current >= 0) {
        const details = document.getElementById('meeting-transcript');
        if (details) details.open = true;
        markTranscript(state.current);
        const row = marked[0];
        if (row) row.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
        return;
      }
      if (event.target.closest('[data-vd-mark-turn]') && marking && state.current >= 0) {
        const turn = turns[state.current];
        setMark(turn[1], Math.min(turn[2], turn[1] + MARK_MAX));
        markForm.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
        return;
      }
      if (event.target.closest('[data-vd-fix]') && fixForm && state.current >= 0) {
        fixForm.open = true;
        const picker = fixForm.querySelector('select[name="speaker"]');
        if (picker) picker.value = speakerOf(state.current).name;
        const choice = fixForm.querySelector('[data-vd-choice]');
        fixForm.scrollIntoView({ block: 'nearest' });
        if (choice) choice.focus({ preventScroll: true });
      }
    });
  }

  // Time stamps in the transcript and in «Кто это?» select the replica on the diagram.
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-vd-seq]');
    if (!link) return;
    const seq = Number(link.dataset.vdSeq);
    const index = turnOfSegment.get(seq);
    if (index === undefined) return;
    event.preventDefault();
    if (state.focus >= 0 && turns[index][0] !== state.focus) setFocus(state.focus);
    const segment = segmentBySeq.get(seq);
    select(index, segment ? { at: segment[2] } : {});
    const box = root.getBoundingClientRect();
    if (box.top < 0 || box.top > window.innerHeight * 0.6) {
      root.scrollIntoView({ block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth' });
    }
  });

  let lastWidth = 0;
  let pending = 0;
  const schedule = () => {
    if (pending) return;
    pending = requestAnimationFrame(() => {
      pending = 0;
      if (scroller.clientWidth !== lastWidth) {
        lastWidth = scroller.clientWidth;
        render();
      }
    });
  };
  if ('ResizeObserver' in window) new ResizeObserver(schedule).observe(scroller);
  else window.addEventListener('resize', schedule);
  render();
  lastWidth = scroller.clientWidth;
  if (document.fonts) {
    const refresh = () => { widths.clear(); render(); };
    if (document.fonts.ready) document.fonts.ready.then(refresh);
    if (document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', refresh);
  }
})();

// «Переразметить голоса»: while the run goes on, the page asks how it is and reloads when it is done.
(() => {
  const box = document.querySelector('[data-vd-relabel-running]');
  if (!box || !window.fetch) return;
  const url = box.getAttribute('data-vd-relabel-running');
  const label = box.querySelector('[data-vd-relabel-progress]');
  const tick = () => fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
    .then((response) => (response.ok ? response.json() : null))
    .then((data) => {
      if (!data) { window.setTimeout(tick, 15000); return; }
      if (data.state !== 'running') {
        const next = new URL(window.location.href);
        next.searchParams.set('relabel', 'done');
        next.hash = 'voice-relabel';
        window.history.replaceState(null, '', next.toString());
        window.location.reload();
        return;
      }
      if (label && data.progress) label.textContent = data.progress;
      window.setTimeout(tick, 6000);
    })
    .catch(() => window.setTimeout(tick, 15000));
  window.setTimeout(tick, 5000);
})();
