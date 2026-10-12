"""Charts for reports: a validated spec goes in, a self-contained SVG comes out.

No third-party plotting library: the SVG is drawn here, so it renders the same in the browser (with hover tooltips from
<title> elements), inside the PDF (WeasyPrint) and on Render's small free instance.

Spec: {"type": "bar|hbar|pie|line", "title": str, "unit": str, "labels": [str], "series": [{"name": str, "values": [num]}]}
"""
import html
import math

TYPES = ("bar", "hbar", "pie", "line")
PALETTE = ("#c98179", "#6f8fc4", "#3fa37a", "#b06ad4", "#e0a526", "#d0586e", "#52a9c4", "#7a8793")
MAX_LABELS = 30
MAX_SERIES = 4
FONT = "Inter, Arial, Helvetica, sans-serif"


class ChartError(ValueError):
    """The chart spec is unusable (Russian message for the panel and for the model)."""


def validate(spec) -> dict:
    if not isinstance(spec, dict):
        raise ChartError("График должен быть объектом")
    kind = spec.get("type")
    if kind not in TYPES:
        raise ChartError("Тип графика: bar, hbar, pie или line")
    labels = spec.get("labels")
    if not isinstance(labels, list) or not 1 <= len(labels) <= MAX_LABELS or not all(isinstance(x, (str, int, float)) for x in labels):
        raise ChartError(f"labels: список из 1-{MAX_LABELS} подписей")
    labels = [str(x).strip() or "?" for x in labels]
    series = spec.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        raise ChartError(f"series: от 1 до {MAX_SERIES} рядов")
    clean = []
    for item in series:
        values = item.get("values") if isinstance(item, dict) else None
        if not isinstance(values, list) or len(values) != len(labels):
            raise ChartError("В каждом ряду значений столько же, сколько подписей")
        numbers = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ChartError("Значения должны быть конечными числами")
            numbers.append(float(value))
        clean.append({"name": str(item.get("name") or "").strip()[:60], "values": numbers})
    if kind == "pie":
        if len(clean) != 1:
            raise ChartError("Круговая диаграмма: один ряд")
        if any(v < 0 for v in clean[0]["values"]) or sum(clean[0]["values"]) <= 0:
            raise ChartError("Круговая диаграмма: неотрицательные значения с ненулевой суммой")
    title = str(spec.get("title") or "").strip()
    if not title:
        raise ChartError("Нужен заголовок графика")
    return {"type": kind, "title": title[:120], "unit": str(spec.get("unit") or "").strip()[:20], "labels": labels,
            "series": clean}


def values_of(spec: dict) -> list:
    return [v for item in spec["series"] for v in item["values"]]


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _short(text: str, limit: int = 22) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _num(value: float, unit: str = "") -> str:
    text = f"{value:.0f}" if abs(value - round(value)) < 1e-9 else f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}{(' ' + unit) if unit else ''}"


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * exponent:
            return step * exponent
    return 10 * exponent


def _frame(width, height, title, body) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="{_e(title)}" font-family="{FONT}">'
            f'<style>.pt{{cursor:pointer}}.pt:hover{{opacity:.75}}</style>'
            f'<rect width="{width}" height="{height}" fill="#ffffff" rx="10"/>'
            f'<text x="24" y="34" font-size="17" font-weight="700" fill="#1f2328">{_e(title)}</text>{body}</svg>')


def _legend(series, x, y) -> str:
    if len(series) < 2:
        return ""
    out = []
    for i, item in enumerate(series):
        out.append(f'<rect x="{x}" y="{y + i * 20 - 10}" width="12" height="12" rx="3" fill="{PALETTE[i % len(PALETTE)]}"/>'
                   f'<text x="{x + 18}" y="{y + i * 20}" font-size="12" fill="#444">{_e(_short(item["name"] or f"Ряд {i + 1}", 26))}</text>')
    return "".join(out)


def _axis_ticks(top: float, steps: int = 5):
    return [top * i / steps for i in range(steps + 1)]


def _bars(spec) -> str:
    labels, series, unit = spec["labels"], spec["series"], spec["unit"]
    width, height = 720, 400
    left, right, top, bottom = 56, 24, 64, 86
    plot_w, plot_h = width - left - right, height - top - bottom
    peak = _nice_max(max(values_of(spec) + [0]))
    body = []
    for tick in _axis_ticks(peak):
        y = top + plot_h - plot_h * tick / peak
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e5e7eb"/>'
                    f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" fill="#6b7280">{_num(tick)}</text>')
    group = plot_w / len(labels)
    bar = min(48.0, group * 0.7 / len(series))
    for i, label in enumerate(labels):
        x0 = left + group * i + (group - bar * len(series)) / 2
        for j, item in enumerate(series):
            value = item["values"][i]
            h = plot_h * value / peak
            x, y = x0 + bar * j, top + plot_h - h
            tip = f'{label}: {_num(value, unit)}' + (f' ({item["name"]})' if item["name"] else "")
            body.append(f'<g class="pt"><title>{_e(tip)}</title><rect x="{x:.1f}" y="{y:.1f}" width="{bar - 3:.1f}" '
                        f'height="{max(h, 0):.1f}" rx="4" fill="{PALETTE[j % len(PALETTE)]}"/>'
                        f'<text x="{x + (bar - 3) / 2:.1f}" y="{y - 5:.1f}" font-size="11" text-anchor="middle" fill="#374151">{_num(value)}</text></g>')
        cx, cy = left + group * i + group / 2, top + plot_h + 18
        body.append(f'<text x="{cx:.1f}" y="{cy}" font-size="11" text-anchor="{"middle" if len(labels) <= 6 else "end"}" fill="#374151" '
                    + ('' if len(labels) <= 6 else f'transform="rotate(-35 {cx:.1f} {cy})" ') + f'><title>{_e(label)}</title>{_e(_short(label, 16))}</text>')
    body.append(_legend(series, width - 190, 30))
    return _frame(width, height, spec["title"], "".join(body))


def _hbars(spec) -> str:
    labels, series, unit = spec["labels"], spec["series"], spec["unit"]
    row = 30 if len(series) == 1 else 22 * len(series) + 8
    top, left, right, bottom = 60, 190, 60, 24
    width, height = 720, top + row * len(labels) + bottom
    plot_w = width - left - right
    peak = _nice_max(max(values_of(spec) + [0]))
    body = []
    for tick in _axis_ticks(peak, 4):
        x = left + plot_w * tick / peak
        body.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{height - bottom}" stroke="#e5e7eb"/>'
                    f'<text x="{x:.1f}" y="{top - 14}" font-size="11" text-anchor="middle" fill="#6b7280">{_num(tick)}</text>')
    bar = 18 if len(series) == 1 else 16
    for i, label in enumerate(labels):
        y0 = top + row * i
        body.append(f'<text x="{left - 10}" y="{y0 + 15}" font-size="12" text-anchor="end" fill="#374151"><title>{_e(label)}</title>{_e(_short(label, 28))}</text>')
        for j, item in enumerate(series):
            value = item["values"][i]
            w = plot_w * value / peak
            y = y0 + 3 + j * (bar + 3)
            tip = f'{label}: {_num(value, unit)}' + (f' ({item["name"]})' if item["name"] else "")
            body.append(f'<g class="pt"><title>{_e(tip)}</title><rect x="{left}" y="{y}" width="{max(w, 0):.1f}" height="{bar}" rx="4" '
                        f'fill="{PALETTE[j % len(PALETTE)]}"/><text x="{left + w + 6:.1f}" y="{y + bar - 4}" font-size="11" fill="#374151">{_num(value)}</text></g>')
    body.append(_legend(series, width - 190, 30))
    return _frame(width, height, spec["title"], "".join(body))


def _pie(spec) -> str:
    labels, values, unit = spec["labels"], spec["series"][0]["values"], spec["unit"]
    width, height, cx, cy, radius = 720, 380, 220, 215, 130
    total = sum(values)
    body, angle = [], -math.pi / 2
    for i, (label, value) in enumerate(zip(labels, values)):
        if value <= 0:
            continue
        sweep = 2 * math.pi * value / total
        color = PALETTE[i % len(PALETTE)]
        tip = f"{label}: {_num(value, unit)} ({value / total * 100:.0f}%)"
        if sweep >= 2 * math.pi - 1e-6:
            body.append(f'<g class="pt"><title>{_e(tip)}</title><circle cx="{cx}" cy="{cy}" r="{radius}" fill="{color}"/></g>')
        else:
            x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            x2, y2 = cx + radius * math.cos(angle + sweep), cy + radius * math.sin(angle + sweep)
            large = 1 if sweep > math.pi else 0
            body.append(f'<g class="pt"><title>{_e(tip)}</title><path d="M{cx} {cy} L{x1:.1f} {y1:.1f} A{radius} {radius} 0 {large} 1 {x2:.1f} {y2:.1f} Z" '
                        f'fill="{color}" stroke="#ffffff" stroke-width="2"/></g>')
        angle += sweep
    for i, (label, value) in enumerate(zip(labels, values)):
        y = 90 + i * 22
        if y > height - 20:
            break
        body.append(f'<rect x="420" y="{y - 11}" width="13" height="13" rx="3" fill="{PALETTE[i % len(PALETTE)]}"/>'
                    f'<text x="442" y="{y}" font-size="13" fill="#374151"><title>{_e(label)}</title>{_e(_short(label, 30))}: {_num(value, unit)} ({value / total * 100:.0f}%)</text>')
    return _frame(width, height, spec["title"], "".join(body))


def _lines(spec) -> str:
    labels, series, unit = spec["labels"], spec["series"], spec["unit"]
    width, height = 720, 400
    left, right, top, bottom = 56, 28, 64, 86
    plot_w, plot_h = width - left - right, height - top - bottom
    peak = _nice_max(max(values_of(spec) + [0]))
    step = plot_w / max(len(labels) - 1, 1)
    body = []
    for tick in _axis_ticks(peak):
        y = top + plot_h - plot_h * tick / peak
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e5e7eb"/>'
                    f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" fill="#6b7280">{_num(tick)}</text>')
    for j, item in enumerate(series):
        color = PALETTE[j % len(PALETTE)]
        points = [(left + step * i if len(labels) > 1 else left + plot_w / 2, top + plot_h - plot_h * v / peak)
                  for i, v in enumerate(item["values"])]
        body.append(f'<polyline fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}"/>')
        for (x, y), label, value in zip(points, labels, item["values"]):
            tip = f'{label}: {_num(value, unit)}' + (f' ({item["name"]})' if item["name"] else "")
            body.append(f'<g class="pt"><title>{_e(tip)}</title><circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" stroke="#fff" stroke-width="2"/></g>')
    for i, label in enumerate(labels):
        x = left + step * i if len(labels) > 1 else left + plot_w / 2
        y = top + plot_h + 18
        body.append(f'<text x="{x:.1f}" y="{y}" font-size="11" text-anchor="{"middle" if len(labels) <= 6 else "end"}" fill="#374151" '
                    + ('' if len(labels) <= 6 else f'transform="rotate(-35 {x:.1f} {y})" ') + f'><title>{_e(label)}</title>{_e(_short(label, 16))}</text>')
    body.append(_legend(series, width - 190, 30))
    return _frame(width, height, spec["title"], "".join(body))


def svg(spec: dict) -> str:
    """Draws a spec (it is validated here too, so a bad one never produces a half picture)."""
    spec = validate(spec)
    kind = spec["type"]
    if kind == "hbar" or (kind == "bar" and (len(spec["labels"]) > 12 or max(len(x) for x in spec["labels"]) > 14 and len(spec["labels"]) > 6)):
        return _hbars(spec)
    return {"bar": _bars, "pie": _pie, "line": _lines}[kind](spec)
