import { useEffect, useRef } from "react";
import { flattenStrokes, resamplePath, type Pt } from "./draw";

const GOLD = "#f0b90b";

type Props = {
  strokes: Pt[][];
  n: number;
  onCommit: (strokes: Pt[][]) => void;
};

function toNorm(el: HTMLElement, clientX: number, clientY: number): Pt {
  const r = el.getBoundingClientRect();
  const w = Math.max(r.width, 1);
  const h = Math.max(r.height, 1);
  const x = Math.min(1, Math.max(0, (clientX - r.left) / w));
  const y = Math.min(1, Math.max(0, 1 - (clientY - r.top) / h));
  return { x, y };
}

export function DrawPad({ strokes, n, onCommit }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const strokesRef = useRef(strokes);
  strokesRef.current = strokes;
  const liveRef = useRef<Pt[] | null>(null);
  const nRef = useRef(n);
  nRef.current = n;
  const onCommitRef = useRef(onCommit);
  onCommitRef.current = onCommit;
  const pointerRef = useRef(-1);
  const paintRef = useRef<() => void>(() => {});

  useEffect(() => {
    const canvas = canvasRef.current;
    const host = hostRef.current;
    if (!canvas || !host) return;

    const paint = () => {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const rect = host.getBoundingClientRect();
      const cssW = Math.max(1, rect.width);
      const cssH = Math.max(1, rect.height);
      const dpr = window.devicePixelRatio || 1;
      const pw = Math.max(1, Math.floor(cssW * dpr));
      const ph = Math.max(1, Math.floor(cssH * dpr));
      if (canvas.width !== pw || canvas.height !== ph) {
        canvas.width = pw;
        canvas.height = ph;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      const all = strokesRef.current.slice();
      if (liveRef.current && liveRef.current.length) all.push(liveRef.current);

      for (const pts of all) {
        if (pts.length < 2) {
          if (pts.length === 1) {
            ctx.fillStyle = GOLD;
            ctx.beginPath();
            ctx.arc(pts[0].x * cssW, (1 - pts[0].y) * cssH, 2.5, 0, Math.PI * 2);
            ctx.fill();
          }
          continue;
        }
        ctx.strokeStyle = GOLD;
        ctx.lineWidth = 2.2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(pts[0].x * cssW, (1 - pts[0].y) * cssH);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x * cssW, (1 - pts[i].y) * cssH);
        }
        ctx.stroke();
      }

      const flat = flattenStrokes(all);
      if (flat.length >= 2) {
        const sampled = resamplePath(flat, nRef.current);
        if (sampled.length >= 2) {
          let lo = sampled[0];
          let hi = sampled[0];
          for (const v of sampled) {
            if (v < lo) lo = v;
            if (v > hi) hi = v;
          }
          const span = hi - lo || 1;
          const sparkH = Math.max(28, cssH * 0.16);
          const sparkTop = cssH - sparkH - 8;
          ctx.fillStyle = "rgba(11,14,17,0.55)";
          ctx.fillRect(8, sparkTop - 4, cssW - 16, sparkH + 8);
          ctx.strokeStyle = "rgba(240,185,11,0.9)";
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          sampled.forEach((v, i) => {
            const x = 12 + (i / (sampled.length - 1)) * (cssW - 24);
            const y = sparkTop + sparkH - 4 - ((v - lo) / span) * (sparkH - 8);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          });
          ctx.stroke();
        }
      }
    };
    paintRef.current = paint;
    paint();
    const ro = new ResizeObserver(() => paint());
    ro.observe(host);

    const minDist = 1.5;
    const onDown = (ev: PointerEvent) => {
      if (pointerRef.current >= 0) return;
      if (ev.button !== 0 && ev.pointerType === "mouse") return;
      const t = ev.target as HTMLElement;
      if (t.closest("button")) return;
      pointerRef.current = ev.pointerId;
      liveRef.current = [toNorm(host, ev.clientX, ev.clientY)];
      try {
        host.setPointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      if (ev.cancelable) ev.preventDefault();
      paint();
    };
    const onMove = (ev: PointerEvent) => {
      if (pointerRef.current !== ev.pointerId || !liveRef.current) return;
      if (ev.cancelable) ev.preventDefault();
      const p = toNorm(host, ev.clientX, ev.clientY);
      const last = liveRef.current[liveRef.current.length - 1];
      const dx = (p.x - last.x) * host.clientWidth;
      const dy = (p.y - last.y) * host.clientHeight;
      if (Math.hypot(dx, dy) < minDist) return;
      liveRef.current.push(p);
      paint();
    };
    const finish = (ev: PointerEvent) => {
      if (pointerRef.current !== ev.pointerId && ev.type !== "pointercancel") return;
      pointerRef.current = -1;
      const live = liveRef.current;
      liveRef.current = null;
      try {
        if (host.hasPointerCapture(ev.pointerId)) host.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      if (live && live.length >= 2) {
        onCommitRef.current([...strokesRef.current, live]);
      } else {
        paint();
      }
    };

    host.addEventListener("pointerdown", onDown, { passive: false });
    host.addEventListener("pointermove", onMove, { passive: false });
    host.addEventListener("pointerup", finish);
    host.addEventListener("pointercancel", finish);

    return () => {
      ro.disconnect();
      host.removeEventListener("pointerdown", onDown);
      host.removeEventListener("pointermove", onMove);
      host.removeEventListener("pointerup", finish);
      host.removeEventListener("pointercancel", finish);
    };
  }, []);

  useEffect(() => {
    paintRef.current();
  }, [strokes, n]);

  function undo(ev: React.PointerEvent | React.MouseEvent) {
    ev.preventDefault();
    ev.stopPropagation();
    if (!strokes.length) return;
    onCommit(strokes.slice(0, -1));
  }

  function clear(ev: React.PointerEvent | React.MouseEvent) {
    ev.preventDefault();
    ev.stopPropagation();
    onCommit([]);
  }

  const empty = strokes.length === 0;

  return (
    <div className="draw-pad" ref={hostRef}>
      <canvas ref={canvasRef} className="draw-canvas" />
      <div className="draw-toolbar">
        <button type="button" className="btn-ghost" disabled={!strokes.length} onPointerDown={undo}>
          撤销
        </button>
        <button type="button" className="btn-ghost" disabled={!strokes.length} onPointerDown={clear}>
          清除
        </button>
      </div>
      {empty ? <div className="draw-hint">在此画出要匹配的形态 · 手指或鼠标一笔</div> : null}
    </div>
  );
}
