import type { Bar, MatchResponse, SearchHit, TF, WavePattern, WaveTemplate, UniverseAsset, WeightPreset } from "./types";

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = `${res.status}`;
    try {
      const body = await res.json();
      msg = body.detail || body.message || JSON.stringify(body);
    } catch {
      msg = await res.text();
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export async function fetchUniverse(tf: TF) {
  const res = await fetch(`/api/universe?tf=${encodeURIComponent(tf)}`);
  return parse<{
    tf: TF;
    default_n: number;
    timeframes: TF[];
    assets: UniverseAsset[];
    boot: { state: string; message: string };
  }>(res);
}

export async function fetchKlines(
  asset: string,
  tf: TF,
  startTs?: number,
  endTs?: number,
  padAfter?: number,
) {
  const q = new URLSearchParams({ asset, tf });
  if (startTs != null) q.set("start_ts", String(startTs));
  if (endTs != null) q.set("end_ts", String(endTs));
  if (padAfter != null && padAfter > 0) q.set("pad_after", String(padAfter));
  const res = await fetch(`/api/klines?${q.toString()}`);
  return parse<{
    asset: string;
    tf: TF;
    venue: string;
    native_symbol: string;
    interval_ms: number;
    bars: Bar[];
  }>(res);
}

export async function fetchMatchPresets() {
  const res = await fetch("/api/match/presets");
  return parse<{ presets: WeightPreset[] }>(res);
}

export async function postMatch(body: {
  asset?: string;
  tf: TF;
  n: number;
  start_ts?: number;
  end_ts?: number;
  path?: number[];
  pattern?: string;
  live?: boolean;
  w_close?: number;
  w_shape?: number;
  w_volume?: number;
  preset?: string;
}) {
  const res = await fetch("/api/match", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<MatchResponse>(res);
}

export async function fetchPatterns() {
  const res = await fetch("/api/patterns");
  return parse<{ patterns: WavePattern[] }>(res);
}

export async function fetchLiveKlines(symbol: string, tf: TF, n?: number) {
  const q = new URLSearchParams({ symbol, tf });
  if (n != null) q.set("n", String(n));
  const res = await fetch(`/api/live_klines?${q.toString()}`);
  return parse<{
    asset: string;
    tf: TF;
    venue: string;
    native_symbol: string;
    interval_ms: number;
    bars: Bar[];
    live?: boolean;
  }>(res);
}

export async function fetchTemplates() {
  const res = await fetch("/api/templates");
  return parse<{ templates: WaveTemplate[] }>(res);
}

export async function postTemplate(body: { name: string; tf: TF; n: number; path: number[] }) {
  const res = await fetch("/api/templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<WaveTemplate>(res);
}

export async function deleteTemplate(id: string) {
  const res = await fetch(`/api/templates/${encodeURIComponent(id)}`, { method: "DELETE" });
  return parse<{ ok: boolean; id: string }>(res);
}

export async function fetchStatus() {
  const res = await fetch("/api/status");
  return parse<{ boot: { state: string; message: string }; closed_bars: number }>(res);
}

export async function fetchSearch(q: string, tf: TF) {
  const query = new URLSearchParams({ q, tf });
  const res = await fetch(`/api/search?${query.toString()}`);
  return parse<{ q: string; tf: TF; hits: SearchHit[] }>(res);
}

export async function postEnsure(body: { id?: string; native_symbol?: string; venue?: string; tf: TF }) {
  const res = await fetch("/api/symbols/ensure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parse<{
    id: string;
    asset: string;
    venue: string;
    tf: TF;
    native_symbol: string;
    class: string;
    ready: boolean;
    bars: number;
    fresh: boolean;
    cached: boolean;
    fetched: number;
    elapsed_ms: number;
    adhoc?: boolean;
  }>(res);
}
