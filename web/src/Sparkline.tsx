import { useEffect, useMemo, useRef } from "react";

export function Sparkline({ data, color = "#f0b90b" }: { data: number[]; color?: string }) {
  const d = data && data.length ? data : [0];
  const min = Math.min(...d);
  const max = Math.max(...d);
  const span = max - min || 1;
  const w = 72;
  const h = 28;
  const pts = d
    .map((y, i) => {
      const x = d.length === 1 ? w / 2 : (i / (d.length - 1)) * (w - 2) + 1;
      const yy = h - 2 - ((y - min) / span) * (h - 4);
      return `${x.toFixed(1)},${yy.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <polyline fill="none" stroke={color} strokeWidth="1.2" points={pts} />
    </svg>
  );
}
