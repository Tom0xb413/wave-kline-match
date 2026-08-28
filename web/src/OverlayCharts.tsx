import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar, ForwardStep } from "./types";

const UP = "#0ecb81";
const DOWN = "#f6465d";
const GOLD = "#f0b90b";

function ohlcBounds(bars: Bar[]): { min: number; max: number } | null {
  let min = Infinity;
  let max = -Infinity;
  for (const b of bars) {
    if (b.low < min) min = b.low;
    if (b.high > max) max = b.high;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return { min, max };
}

/** Default LWC minMove is 0.01 — PEPE/SHIB etc. then autoscale to 0..0.01 and look flat. */
function priceFormatForBars(bars: Bar[]): { type: "price"; precision: number; minMove: number } {
  const b = ohlcBounds(bars);
  const span = b ? Math.max(b.max - b.min, 0) : 0;
  const abs = b ? Math.max(Math.abs(b.min), Math.abs(b.max)) : 0;
  const step = Math.max(span / 500, abs * 1e-8, 1e-12);
  const exp = Math.floor(Math.log10(step));
  const minMove = 10 ** Math.max(-12, Math.min(2, exp));
  const precision = Math.max(0, Math.min(12, -Math.floor(Math.log10(minMove))));
  return { type: "price", precision, minMove };
}

function tightAutoscale(bars: Bar[]) {
  const b = ohlcBounds(bars);
  if (!b) return null;
  if (b.max <= b.min) {
    const pad = Math.max(Math.abs(b.min) * 0.002, 1e-12);
    return { priceRange: { minValue: b.min - pad, maxValue: b.min + pad } };
  }
  return { priceRange: { minValue: b.min, maxValue: b.max } };
}

function paintVertical(
  chart: IChartApi,
  time: Time,
  el: HTMLDivElement | null,
  host: HTMLDivElement | null,
) {
  if (!el) return;
  const x = chart.timeScale().timeToCoordinate(time);
  if (x == null) {
    el.style.display = "none";
    return;
  }
  el.style.display = "block";
  el.style.left = `${x}px`;
  const label = el.querySelector(".analog-now-label") as HTMLElement | null;
  if (label && host) {
    if (x > host.clientWidth - 80) label.classList.add("near-right");
    else label.classList.remove("near-right");
  }
}

function paintMatchBand(chart: IChartApi, fromSec: number, toSec: number, el: HTMLDivElement | null) {
  if (!el) return;
  const ts = chart.timeScale();
  const x0 = ts.timeToCoordinate(fromSec as UTCTimestamp);
  const x1 = ts.timeToCoordinate(toSec as UTCTimestamp);
  if (x0 == null && x1 == null) {
    el.style.display = "none";
    return;
  }
  const left = x0 ?? 0;
  const right = x1 ?? left;
  const a = Math.min(left, right);
  const w = Math.max(2, Math.abs(right - left) + 8);
  el.style.display = "block";
  el.style.left = `${a}px`;
  el.style.width = `${w}px`;
}

export function ZOverlay({
  query,
  match,
  hover,
  markerIndex,
}: {
  query: number[];
  match: number[] | null;
  hover: number[] | null;
  markerIndex?: number | null;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const elRef = useRef<HTMLDivElement>(null);
  const markRef = useRef<HTMLDivElement>(null);
  const seriesRef = useRef<{
    chart: IChartApi;
    q: ISeriesApi<"Line">;
    m: ISeriesApi<"Line">;
    h: ISeriesApi<"Line">;
  } | null>(null);
  const markerIndexRef = useRef(markerIndex);
  markerIndexRef.current = markerIndex;

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0b0e11" },
        textColor: "#848e9c",
        fontFamily: "IBM Plex Mono, monospace",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#1e2329" },
        horzLines: { color: "#1e2329" },
      },
      rightPriceScale: { borderColor: "#1e2329", autoScale: true, scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: "#1e2329", visible: false },
      crosshair: { mode: CrosshairMode.Magnet },
      autoSize: true,
    });
    const q = chart.addSeries(LineSeries, {
      color: GOLD,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const m = chart.addSeries(LineSeries, {
      color: UP,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    const h = chart.addSeries(LineSeries, {
      color: "#848e9c",
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    seriesRef.current = { chart, q, m, h };
    const pingSize = () => {
      const w = el.clientWidth;
      const hh = el.clientHeight;
      if (w > 2 && hh > 2) chart.applyOptions({ width: w, height: hh });
      else chart.applyOptions({});
    };
    const paint = () => {
      const idx = markerIndexRef.current;
      if (idx == null || idx < 0) {
        if (markRef.current) markRef.current.style.display = "none";
        return;
      }
      paintVertical(chart, (idx + 1) as UTCTimestamp, markRef.current, hostRef.current);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(paint);
    const ro = new ResizeObserver(() => {
      pingSize();
      paint();
    });
    ro.observe(el);
    const mo = new MutationObserver(() => requestAnimationFrame(pingSize));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-layout"] });
    window.addEventListener("resize", pingSize);
    return () => {
      window.removeEventListener("resize", pingSize);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(paint);
      ro.disconnect();
      mo.disconnect();
      chart.remove();
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    const s = seriesRef.current;
    if (!s) return;
    const toLine = (arr: number[]) => arr.map((v, i) => ({ time: (i + 1) as UTCTimestamp, value: v }));
    s.q.setData(toLine(query.length ? query : [0]));
    s.m.setData(match && match.length ? toLine(match) : []);
    s.h.setData(hover && hover.length ? toLine(hover) : []);
    s.chart.timeScale().fitContent();
    requestAnimationFrame(() => {
      const idx = markerIndexRef.current;
      if (idx == null || idx < 0) {
        if (markRef.current) markRef.current.style.display = "none";
        return;
      }
      paintVertical(s.chart, (idx + 1) as UTCTimestamp, markRef.current, hostRef.current);
    });
  }, [query, match, hover, markerIndex]);

  return (
    <div className="tv-wrap" ref={hostRef}>
      <div className="tv" ref={elRef} />
      <div className="analog-now" ref={markRef} style={{ display: "none" }} />
    </div>
  );
}

export function WindowCandles({
  bars,
  markerTs,
  matchStartTs,
}: {
  bars: Bar[];
  markerTs?: number | null;
  matchStartTs?: number | null;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const elRef = useRef<HTMLDivElement>(null);
  const bandRef = useRef<HTMLDivElement>(null);
  const markRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0b0e11" },
        textColor: "#848e9c",
        fontFamily: "IBM Plex Sans, sans-serif",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "#1e2329" },
        horzLines: { color: "#1e2329" },
      },
      rightPriceScale: {
        borderColor: "#1e2329",
        autoScale: true,
        scaleMargins: { top: 0.05, bottom: 0.22 },
      },
      timeScale: { borderColor: "#1e2329", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });
    const s = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      priceLineVisible: false,
      lastValueVisible: true,
      priceFormat: priceFormatForBars(bars),
      autoscaleInfoProvider: () => tightAutoscale(bars),
    });
    const vol = chart.addSeries(HistogramSeries, {
      priceScaleId: "vol",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      borderVisible: false,
    });
    s.setData(
      bars.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    vol.setData(
      bars.map((b) => ({
        time: b.time as UTCTimestamp,
        value: b.volume,
        color: b.close >= b.open ? "rgba(14,203,129,0.45)" : "rgba(246,70,93,0.45)",
      })),
    );

    let markerBar: Bar | null = null;
    if (markerTs != null && bars.length) {
      for (const b of bars) {
        if (b.ts <= markerTs) markerBar = b;
        else break;
      }
    }
    if (markerBar) {
      createSeriesMarkers(s, [
        {
          time: markerBar.time as UTCTimestamp,
          position: "aboveBar",
          shape: "arrowDown",
          color: GOLD,
          text: "当时此刻",
          size: 1.6,
        },
      ]);
    }

    chart.timeScale().fitContent();

    const paint = () => {
      if (matchStartTs != null && markerTs != null) {
        paintMatchBand(chart, matchStartTs / 1000, markerTs / 1000, bandRef.current);
      } else if (bandRef.current) {
        bandRef.current.style.display = "none";
      }
      if (markerBar) {
        paintVertical(chart, markerBar.time as UTCTimestamp, markRef.current, hostRef.current);
      } else if (markRef.current) {
        markRef.current.style.display = "none";
      }
    };
    paint();
    chart.timeScale().subscribeVisibleLogicalRangeChange(paint);
    const pingSize = () => {
      const w = el.clientWidth;
      const hh = el.clientHeight;
      if (w > 2 && hh > 2) chart.applyOptions({ width: w, height: hh });
      else chart.applyOptions({});
      paint();
    };
    const ro = new ResizeObserver(pingSize);
    ro.observe(el);
    const mo = new MutationObserver(() => requestAnimationFrame(pingSize));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-layout"] });
    window.addEventListener("resize", pingSize);
    requestAnimationFrame(paint);
    return () => {
      window.removeEventListener("resize", pingSize);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(paint);
      ro.disconnect();
      mo.disconnect();
      chart.remove();
    };
  }, [bars, markerTs, matchStartTs]);

  return (
    <div className="tv-wrap" ref={hostRef}>
      <div className="tv" ref={elRef} />
      <div className="match-band" ref={bandRef} style={{ display: "none" }} />
      <div className="analog-now has-axis" ref={markRef} style={{ display: "none" }}>
        <span className="analog-now-label">当时此刻</span>
      </div>
    </div>
  );
}

const FAN_UP = "#0ecb81";
const FAN_DOWN = "#f6465d";
const FAN_MUTED = "#848e9c";
const FAN_BAND = "rgba(240, 185, 11, 0.22)";

export function ForwardFan({
  steps,
  horizon,
}: {
  steps: ForwardStep[];
  horizon: number;
}) {
  if (!steps.length) return null;
  const w = 640;
  const h = 108;
  const pad = { l: 8, r: 8, t: 8, b: 16 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  let yMin = 0;
  let yMax = 0;
  for (const s of steps) {
    if (s.p25 < yMin) yMin = s.p25;
    if (s.p75 > yMax) yMax = s.p75;
    if (s.p50 < yMin) yMin = s.p50;
    if (s.p50 > yMax) yMax = s.p50;
  }
  const span = yMax - yMin || 0.01;
  const padY = span * 0.12;
  yMin -= padY;
  yMax += padY;
  const denom = Math.max(horizon - 1, 1);
  const xOf = (i: number) => pad.l + ((i - 1) / denom) * innerW;
  const yOf = (v: number) => pad.t + ((yMax - v) / (yMax - yMin)) * innerH;
  const band = [
    ...steps.map((s) => `${xOf(s.i).toFixed(2)},${yOf(s.p75).toFixed(2)}`),
    ...[...steps].reverse().map((s) => `${xOf(s.i).toFixed(2)},${yOf(s.p25).toFixed(2)}`),
  ].join(" ");
  const p50 = steps.map((s) => `${xOf(s.i).toFixed(2)},${yOf(s.p50).toFixed(2)}`).join(" ");
  const y0 = yOf(0);
  const last = steps[steps.length - 1];
  const p50Color = last.p50 >= 0 ? FAN_UP : FAN_DOWN;
  return (
    <svg
      className="forward-fan-svg"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="对齐后涨跌分布"
    >
      <line
        x1={pad.l}
        x2={w - pad.r}
        y1={y0}
        y2={y0}
        stroke={FAN_MUTED}
        strokeWidth="1"
        strokeDasharray="4 3"
      />
      <polygon points={band} fill={FAN_BAND} />
      <polyline points={p50} fill="none" stroke={p50Color} strokeWidth="1.8" />
      <text x={pad.l} y={h - 3} fill={FAN_MUTED} fontSize="9" fontFamily="IBM Plex Mono, monospace">
        1
      </text>
      <text
        x={w - pad.r}
        y={h - 3}
        fill={FAN_MUTED}
        fontSize="9"
        fontFamily="IBM Plex Mono, monospace"
        textAnchor="end"
      >
        {horizon}
      </text>
    </svg>
  );
}

