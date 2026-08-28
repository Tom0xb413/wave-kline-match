export type Pt = { x: number; y: number };

const EPS = 1e-12;

export function resamplePath(points: Pt[], n: number): number[] {
  if (n < 2 || points.length < 2) return [];
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  return resampleXY(xs, ys, n);
}

export function resampleXY(xs: number[], ys: number[], n: number): number[] {
  const m = ys.length;
  if (n < 2 || m < 2 || xs.length !== m) return [];
  if (m === n) return ys.slice();
  const xSpan = Math.abs(xs[m - 1] - xs[0]);
  let yMin = ys[0];
  let yMax = ys[0];
  for (const y of ys) {
    if (y < yMin) yMin = y;
    if (y > yMax) yMax = y;
  }
  const ySpan = yMax - yMin;
  if (xSpan < 1e-9 * Math.max(ySpan, 1)) {
    const out: number[] = [];
    for (let i = 0; i < n; i++) {
      const t = (i / (n - 1)) * (m - 1);
      out.push(interp1(ys, t));
    }
    return out;
  }
  const cum = new Array<number>(m);
  cum[0] = 0;
  for (let i = 1; i < m; i++) {
    const dx = xs[i] - xs[i - 1];
    const dy = ys[i] - ys[i - 1];
    cum[i] = cum[i - 1] + Math.hypot(dx, dy);
  }
  const total = cum[m - 1];
  if (total < EPS) return Array.from({ length: n }, () => ys[0]);
  const out: number[] = [];
  let j = 0;
  for (let i = 0; i < n; i++) {
    const target = (i / (n - 1)) * total;
    while (j < m - 2 && cum[j + 1] < target) j++;
    const a = cum[j];
    const b = cum[j + 1];
    const w = b - a < EPS ? 0 : (target - a) / (b - a);
    out.push(ys[j] + w * (ys[j + 1] - ys[j]));
  }
  return out;
}

function interp1(ys: number[], t: number): number {
  if (t <= 0) return ys[0];
  if (t >= ys.length - 1) return ys[ys.length - 1];
  const i = Math.floor(t);
  const w = t - i;
  return ys[i] + w * (ys[i + 1] - ys[i]);
}

export function zscore(arr: number[]): number[] {
  if (!arr.length) return [];
  let mean = 0;
  for (const x of arr) mean += x;
  mean /= arr.length;
  let acc = 0;
  for (const x of arr) acc += (x - mean) * (x - mean);
  const std = Math.sqrt(acc / arr.length);
  if (std < EPS) return arr.map(() => 0);
  return arr.map((x) => (x - mean) / std);
}

export function pathStd(arr: number[]): number {
  if (arr.length < 2) return 0;
  let mean = 0;
  for (const x of arr) mean += x;
  mean /= arr.length;
  let acc = 0;
  for (const x of arr) acc += (x - mean) * (x - mean);
  return Math.sqrt(acc / arr.length);
}

/** Rebuild a 0–1 stroke from a 1D path for canvas display. */
export function pathToStroke(path: number[]): Pt[] {
  if (path.length < 2) return [];
  let min = path[0];
  let max = path[0];
  for (const y of path) {
    if (y < min) min = y;
    if (y > max) max = y;
  }
  const span = max - min || 1;
  const last = path.length - 1;
  return path.map((y, i) => ({
    x: i / last,
    y: 0.12 + ((y - min) / span) * 0.76,
  }));
}

export function flattenStrokes(strokes: Pt[][]): Pt[] {
  const out: Pt[] = [];
  for (const s of strokes) {
    for (const p of s) out.push(p);
  }
  return out;
}

/** Same geometry as kline_match.patterns.cup_handle_path (preview only). */
export function cupHandlePath(n: number): number[] {
  const m = Math.max(16, Math.floor(n));
  const y = new Array<number>(m);
  const cupEnd = 0.72;
  for (let i = 0; i < m; i++) {
    const t = m === 1 ? 0 : i / (m - 1);
    if (t <= cupEnd) {
      const u = t / cupEnd;
      const rim = 1.0 + (0.98 - 1.0) * u;
      const bowl = 0.5 * (1.0 + Math.cos(2.0 * Math.PI * u));
      y[i] = 0.62 + (rim - 0.62) * bowl;
    } else {
      const uh = (t - cupEnd) / (1.0 - cupEnd);
      const bottomU = 0.62;
      if (uh <= bottomU) {
        const w = uh / bottomU;
        y[i] = 0.98 + (0.9 - 0.98) * 0.5 * (1.0 - Math.cos(Math.PI * w));
      } else {
        const w = (uh - bottomU) / (1.0 - bottomU);
        y[i] = 0.9 + (0.93 - 0.9) * 0.5 * (1.0 - Math.cos(Math.PI * w));
      }
    }
  }
  return y;
}
