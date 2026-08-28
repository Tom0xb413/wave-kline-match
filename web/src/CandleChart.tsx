import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  HistogramSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar } from "./types";

const UP = "#0ecb81";
const DOWN = "#f6465d";
const MIN_DRAG_PX = 8;

type DragMode = "brush" | "left" | "right" | "slide";

type Props = {
  bars: Bar[];
  fromTs: number | null;
  toTs: number | null;
  onBrush: (startTs: number, endTs: number) => void;
  onHud: (bar: Bar | null) => void;
  fitKey: string;
  brushMode: boolean;
};

function snapBar(bars: Bar[], tSec: number): Bar | null {
  if (!bars.length) return null;
  let best = bars[0];
  let bestD = Math.abs(bars[0].time - tSec);
  for (const b of bars) {
    const d = Math.abs(b.time - tSec);
    if (d < bestD) {
      best = b;
      bestD = d;
    }
  }
  return best;
}

function closedBarsOf(bars: Bar[]) {
  return bars.filter((b) => b.is_closed);
}

function idxOfTs(list: Bar[], ts: number): number {
  if (!list.length) return 0;
  let best = 0;
  let bestD = Math.abs(list[0].ts - ts);
  for (let i = 1; i < list.length; i++) {
    const d = Math.abs(list[i].ts - ts);
    if (d < bestD) {
      best = i;
      bestD = d;
    }
  }
  return best;
}

function applyPan(chart: IChartApi, enabled: boolean) {
  chart.applyOptions({
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: enabled,
      horzTouchDrag: enabled,
      vertTouchDrag: enabled,
    },
  });
}

export function CandleChart({ bars, fromTs, toTs, onBrush, onHud, fitKey, brushMode }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const tvRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const barsRef = useRef(bars);
  barsRef.current = bars;
  const onBrushRef = useRef(onBrush);
  onBrushRef.current = onBrush;
  const onHudRef = useRef(onHud);
  onHudRef.current = onHud;
  const bandRef = useRef<HTMLDivElement>(null);
  const brushRef = useRef<HTMLDivElement>(null);
  const fromRef = useRef(fromTs);
  fromRef.current = fromTs;
  const toRef = useRef(toTs);
  toRef.current = toTs;
  const brushModeRef = useRef(brushMode);
  brushModeRef.current = brushMode;
  const liveRange = useRef<{ start: number; end: number } | null>(null);
  const paintBandRef = useRef<() => void>(() => {});
  const fittedTokenRef = useRef("");
  const drag = useRef<{
    x0: number;
    x1: number;
    active: boolean;
    mode: DragMode | null;
    pointerId: number;
    barCount: number;
    grabOffset: number;
    startFromTs: number | null;
    startToTs: number | null;
  }>({
    x0: 0,
    x1: 0,
    active: false,
    mode: null,
    pointerId: -1,
    barCount: 0,
    grabOffset: 0,
    startFromTs: null,
    startToTs: null,
  });

  function hostX(clientX: number): number {
    const host = hostRef.current;
    if (!host) return clientX;
    return clientX - host.getBoundingClientRect().left;
  }

  function xToBar(x: number): Bar | null {
    const chart = chartRef.current;
    const list = barsRef.current;
    if (!chart || !list.length) return null;
    const t = chart.timeScale().coordinateToTime(x) as number | null;
    if (t != null) return snapBar(list, t);
    const xFirst = chart.timeScale().timeToCoordinate(list[0].time as Time);
    const xLast = chart.timeScale().timeToCoordinate(list[list.length - 1].time as Time);
    if (xFirst != null && x <= xFirst) return list[0];
    if (xLast != null && x >= xLast) return list[list.length - 1];
    return list[0];
  }

  function lockPan() {
    const chart = chartRef.current;
    if (chart) applyPan(chart, false);
  }

  function unlockPan() {
    const chart = chartRef.current;
    if (chart) applyPan(chart, !brushModeRef.current);
  }

  function commitLive() {
    const live = liveRange.current;
    liveRange.current = null;
    if (!live) return;
    const c = closedBarsOf(barsRef.current).filter((b) => b.ts >= live.start && b.ts <= live.end);
    if (c.length < 2) return;
    const start = c[0].ts;
    const end = c[c.length - 1].ts;
    if (start === fromRef.current && end === toRef.current) return;
    onBrushRef.current(start, end);
  }

  useEffect(() => {
    const el = tvRef.current;
    if (!el) return;
    fittedTokenRef.current = "";
    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: "#0b0e11" },
        textColor: "#848e9c",
        fontFamily: "IBM Plex Sans, Noto Sans SC, sans-serif",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1e2329" },
        horzLines: { color: "#1e2329" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#848e9c", width: 1, style: LineStyle.Dashed, labelBackgroundColor: "#1e2329" },
        horzLine: { color: "#848e9c", width: 1, style: LineStyle.Dashed, labelBackgroundColor: "#1e2329" },
      },
      rightPriceScale: { borderColor: "#1e2329", scaleMargins: { top: 0.06, bottom: 0.22 } },
      timeScale: {
        borderColor: "#1e2329",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4,
      },
      autoSize: true,
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: !brushModeRef.current,
        horzTouchDrag: !brushModeRef.current,
        vertTouchDrag: !brushModeRef.current,
      },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      priceLineVisible: true,
      lastValueVisible: true,
      priceLineWidth: 1,
      priceLineStyle: LineStyle.Dashed,
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
    chartRef.current = chart;
    candleRef.current = candles;
    volRef.current = vol;

    chart.subscribeCrosshairMove((param) => {
      const t = param.time as number | undefined;
      if (t == null) {
        onHudRef.current(null);
        return;
      }
      const bar = snapBar(barsRef.current, t);
      onHudRef.current(bar);
    });

    const paintBrush = () => {
      const band = brushRef.current;
      if (!band || !drag.current.active || drag.current.mode !== "brush") return;
      const { x0, x1 } = drag.current;
      const left = Math.min(x0, x1);
      const width = Math.abs(x1 - x0);
      band.style.display = "block";
      band.style.left = `${left}px`;
      band.style.width = `${Math.max(2, width)}px`;
    };

    const updateHandleDrag = (x: number) => {
      const mode = drag.current.mode;
      const c = closedBarsOf(barsRef.current);
      if (!c.length || !mode) return;
      const bar = xToBar(x);
      if (!bar) return;
      if (mode === "left") {
        const endI = idxOfTs(c, drag.current.startToTs ?? c[c.length - 1].ts);
        let startI = idxOfTs(c, bar.ts);
        startI = Math.max(0, Math.min(startI, endI - 1));
        liveRange.current = { start: c[startI].ts, end: c[endI].ts };
      } else if (mode === "right") {
        const startI = idxOfTs(c, drag.current.startFromTs ?? c[0].ts);
        let endI = idxOfTs(c, bar.ts);
        endI = Math.min(c.length - 1, Math.max(endI, startI + 1));
        liveRange.current = { start: c[startI].ts, end: c[endI].ts };
      } else if (mode === "slide") {
        const count = drag.current.barCount;
        if (count < 2) return;
        const grabI = idxOfTs(c, bar.ts);
        let newI0 = grabI - drag.current.grabOffset;
        newI0 = Math.max(0, Math.min(newI0, c.length - count));
        const newI1 = newI0 + count - 1;
        liveRange.current = { start: c[newI0].ts, end: c[newI1].ts };
      }
      paintBandRef.current();
    };

    const onDown = (ev: PointerEvent) => {
      if (drag.current.active) return;
      if (ev.button !== 0 && ev.pointerType === "mouse") return;
      const wantBrush = brushModeRef.current || ev.shiftKey;
      if (!wantBrush) return;
      const x = hostX(ev.clientX);
      drag.current = {
        x0: x,
        x1: x,
        active: true,
        mode: "brush",
        pointerId: ev.pointerId,
        barCount: 0,
        grabOffset: 0,
        startFromTs: fromRef.current,
        startToTs: toRef.current,
      };
      lockPan();
      try {
        el.setPointerCapture(ev.pointerId);
      } catch {
        /* already captured / non-element */
      }
      if (ev.cancelable) ev.preventDefault();
    };

    const onMove = (ev: PointerEvent) => {
      if (!drag.current.active) return;
      if (drag.current.pointerId >= 0 && ev.pointerId !== drag.current.pointerId) return;
      const x = hostX(ev.clientX);
      drag.current.x1 = x;
      if (ev.cancelable) ev.preventDefault();
      if (drag.current.mode === "brush") paintBrush();
      else updateHandleDrag(x);
    };

    const onUp = (ev: PointerEvent) => {
      if (!drag.current.active) return;
      if (drag.current.pointerId >= 0 && ev.pointerId !== drag.current.pointerId && ev.type !== "pointercancel") {
        return;
      }
      const { x0, x1, mode } = drag.current;
      drag.current.active = false;
      drag.current.mode = null;
      drag.current.pointerId = -1;
      unlockPan();
      if (brushRef.current) brushRef.current.style.display = "none";
      try {
        if (el.hasPointerCapture(ev.pointerId)) el.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      if (mode === "brush") {
        liveRange.current = null;
        if (Math.abs(x1 - x0) < MIN_DRAG_PX) {
          paintBandRef.current();
          return;
        }
        const ts = chart.timeScale();
        const t0 = ts.coordinateToTime(Math.min(x0, x1)) as number | null;
        const t1 = ts.coordinateToTime(Math.max(x0, x1)) as number | null;
        if (t0 == null || t1 == null) return;
        const a = snapBar(barsRef.current, t0);
        const b = snapBar(barsRef.current, t1);
        if (a && b) {
          const s = Math.min(a.ts, b.ts);
          const e = Math.max(a.ts, b.ts);
          if (s !== e) onBrushRef.current(s, e);
        }
        return;
      }
      commitLive();
    };

    const onTouchMove = (ev: TouchEvent) => {
      if (!drag.current.active) return;
      if (ev.cancelable) ev.preventDefault();
    };

    el.addEventListener("pointerdown", onDown, { passive: false });
    window.addEventListener("pointermove", onMove, { passive: false });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    window.addEventListener("touchmove", onTouchMove, { passive: false });

    const pingSize = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (w > 2 && h > 2) chart.applyOptions({ width: w, height: h });
      else chart.applyOptions({});
    };
    const ro = new ResizeObserver(pingSize);
    ro.observe(el);
    const mo = new MutationObserver(() => requestAnimationFrame(pingSize));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-layout"] });
    window.addEventListener("resize", pingSize);

    return () => {
      el.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("resize", pingSize);
      ro.disconnect();
      mo.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || drag.current.active) return;
    applyPan(chart, !brushMode);
  }, [brushMode]);

  useEffect(() => {
    const candles = candleRef.current;
    const vol = volRef.current;
    const chart = chartRef.current;
    if (!candles || !vol || !chart) return;
    const cdata = bars.map((b) => ({
      time: b.time as UTCTimestamp,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
    const vdata = bars.map((b) => ({
      time: b.time as UTCTimestamp,
      value: b.volume,
      color: b.close >= b.open ? "rgba(14,203,129,0.45)" : "rgba(246,70,93,0.45)",
    }));
    candles.setData(cdata);
    vol.setData(vdata);
    if (bars.length) {
      const last = bars[bars.length - 1];
      candles.applyOptions({
        priceLineColor: last.close >= last.open ? UP : DOWN,
      });
    }
    if (!bars.length) return;
    const token = `${fitKey}:${bars.length}:${bars[0].ts}:${bars[bars.length - 1].ts}`;
    if (fittedTokenRef.current === token) return;
    fittedTokenRef.current = token;
    const qFrom = fromRef.current;
    const qTo = toRef.current;
    if (qFrom == null || qTo == null) {
      chart.timeScale().fitContent();
      return;
    }
    const i0 = bars.findIndex((b) => b.ts >= qFrom);
    let i1 = -1;
    for (let i = bars.length - 1; i >= 0; i--) {
      if (bars[i].ts <= qTo) {
        i1 = i;
        break;
      }
    }
    if (i0 < 0) {
      chart.timeScale().fitContent();
      return;
    }
    const end = i1 < 0 ? i0 : i1;
    const pad = 48;
    chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, i0 - pad),
      to: Math.min(bars.length + 2, end + pad),
    });
  }, [bars, fitKey]);

  useEffect(() => {
    const chart = chartRef.current;
    const band = bandRef.current;
    const host = hostRef.current;
    if (!chart || !band || !host) return;

    const paint = () => {
      const live = liveRange.current;
      const start = live?.start ?? fromTs;
      const end = live?.end ?? toTs;
      if (start == null || end == null || !bars.length) {
        band.style.display = "none";
        return;
      }
      const ts = chart.timeScale();
      const x0 = ts.timeToCoordinate((start / 1000) as Time);
      const x1 = ts.timeToCoordinate((end / 1000) as Time);
      if (x0 == null && x1 == null) {
        band.style.display = "none";
        return;
      }
      const left = x0 ?? 0;
      const right = x1 ?? host.clientWidth;
      const a = Math.min(left, right);
      const w = Math.max(2, Math.abs(right - left) + 6);
      band.style.display = "block";
      band.style.left = `${a}px`;
      band.style.width = `${w}px`;
    };
    paintBandRef.current = paint;
    paint();
    chart.timeScale().subscribeVisibleLogicalRangeChange(paint);
    const ro = new ResizeObserver(paint);
    ro.observe(host);
    return () => {
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(paint);
      ro.disconnect();
    };
  }, [fromTs, toTs, bars]);

  function startOverlayDrag(ev: React.PointerEvent, mode: DragMode) {
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.button !== 0 && ev.pointerType === "mouse") return;
    if (fromRef.current == null || toRef.current == null) return;
    const x = hostX(ev.clientX);
    const c = closedBarsOf(barsRef.current);
    if (c.length < 2) return;
    const i0 = idxOfTs(c, fromRef.current);
    const i1 = idxOfTs(c, toRef.current);
    const count = Math.max(2, i1 - i0 + 1);
    const grab = xToBar(x);
    const grabI = grab ? idxOfTs(c, grab.ts) : i0;
    drag.current = {
      x0: x,
      x1: x,
      active: true,
      mode,
      pointerId: ev.pointerId,
      barCount: count,
      grabOffset: grabI - i0,
      startFromTs: c[i0].ts,
      startToTs: c[i1].ts,
    };
    liveRange.current = { start: c[i0].ts, end: c[i1].ts };
    lockPan();
    try {
      (ev.currentTarget as HTMLElement).setPointerCapture(ev.pointerId);
    } catch {
      /* ignore */
    }
  }

  return (
    <div className={`chart-host${brushMode ? " brush-on" : ""}`} ref={hostRef}>
      <div className="tv" ref={tvRef} />
      <div
        className="range-band"
        ref={bandRef}
        style={{ display: "none" }}
        onPointerDown={(ev) => startOverlayDrag(ev, "slide")}
      >
        <div
          className="range-handle range-handle-left"
          onPointerDown={(ev) => startOverlayDrag(ev, "left")}
        />
        <div
          className="range-handle range-handle-right"
          onPointerDown={(ev) => startOverlayDrag(ev, "right")}
        />
      </div>
      <div className="brush-band" ref={brushRef} style={{ display: "none" }} />
    </div>
  );
}
