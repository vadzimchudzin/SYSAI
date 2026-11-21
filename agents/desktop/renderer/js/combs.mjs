// Раскладка сот роя в ряды: шестиугольники касаются гранями, нечётный ряд сдвинут на полсоты — сетка читается как
// соты, а не как таблица квадратов. Сколько сот влезает в ряд — тоже здесь: окно знает только свою ширину.
// Чистая функция без DOM — раскладку проверяет юнит-тест (unit.test.js импортирует этот файл).

// Сколько шестиугольников влезает в ряд: у соты ширина `cell`, соседние не пересекаются — шаг ряда ровно ширина.
export function perRow(width, cell) {
  const w = Number(width);
  const c = Number(cell);
  if (!Number.isFinite(w) || !Number.isFinite(c) || w <= 0 || c <= 0) return 1;
  return Math.max(1, Math.floor(w / c));
}

// Индексы по рядам: [ [0, 1, 2], [3, 4], … ] — подряд, без потерь и дублей; остаток — последний, возможно короткий ряд.
export function rowsOf(count, per) {
  const n = Math.max(0, Math.floor(Number(count) || 0));
  const p = Math.max(1, Math.floor(Number(per) || 1));
  const rows = [];
  for (let i = 0; i < n; i += p) rows.push(Array.from({ length: Math.min(p, n - i) }, (_, k) => i + k));
  return rows;
}

// Ряд нечётный (второй, четвёртый…) — его сдвигаем на полсоты: ряды сцепляются гранями, как настоящие соты.
export const isOdd = (index) => index % 2 === 1;
