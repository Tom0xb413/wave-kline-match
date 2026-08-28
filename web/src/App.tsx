import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { deleteTemplate, fetchKlines, fetchLiveKlines, fetchMatchPresets, fetchPatterns, fetchSearch, fetchTemplates, fetchUniverse, postEnsure, postMatch, postTemplate } from "./api";
import { CandleChart } from "./CandleChart";
import { DrawPad } from "./DrawPad";
import { ForwardFan, WindowCandles, ZOverlay } from "./OverlayCharts";
import { Sparkline } from "./Sparkline";
import { cupHandlePath, flattenStrokes, pathStd, pathToStroke, resamplePath, resampleXY, zscore, type Pt } from "./draw";
import { detectLayout, subscribeLayout, type Layout } from "./layout";
import type { Bar, ForwardDist, ForwardStep, Hit, MatchResponse, SearchHit, TF, UniverseAsset, WavePattern, WaveTemplate, WeightPreset } from "./types";

const TFS: TF[] = ["1H", "4H", "12H", "1D"];

const DEFAULT_PRESETS: WeightPreset[] = [
  { id: "recommend", name_zh: "推荐", w_close: 0.6, w_shape: 0.25, w_volume: 0.15 },
  { id: "close_only", name_zh: "仅收盘", w_close: 1, w_shape: 0, w_volume: 0 },
  { id: "shape", name_zh: "形态优先", w_close: 0.35, w_shape: 0.5, w_volume: 0.15 },
  { id: "volume", name_zh: "量价", w_close: 0.5, w_shape: 0, w_volume: 0.5 },
  { id: "custom", name_zh: "自定义", w_close: null, w_shape: null, w_volume: null },
];
const WEIGHT_LS = "wave.channelWeights";

function readStoredWeights() {
  try {
    const raw = JSON.parse(localStorage.getItem(WEIGHT_LS) || "null");
    if (raw && typeof raw.w_close === "number") {
      return {
        preset: String(raw.preset || "recommend"),
        w_close: Number(raw.w_close),
        w_shape: Number(raw.w_shape),
        w_volume: Number(raw.w_volume),
      };
    }
  } catch {
    /* ignore */
  }
  return { preset: "recommend", w_close: 0.6, w_shape: 0.25, w_volume: 0.15 };
}

function ratiosClose(wc: number, ws: number, wv: number) {
  const s = wc + ws + wv;
  if (s <= 0) return null;
  return [wc / s, ws / s, wv / s] as const;
}

function inferPreset(wc: number, ws: number, wv: number, presets: WeightPreset[]) {
  const a = ratiosClose(wc, ws, wv);
  if (!a) return "custom";
  for (const p of presets) {
    if (p.w_close == null || p.w_shape == null || p.w_volume == null) continue;
    const b = ratiosClose(p.w_close, p.w_shape, p.w_volume);
    if (!b) continue;
    if (Math.abs(a[0] - b[0]) < 1e-6 && Math.abs(a[1] - b[1]) < 1e-6 && Math.abs(a[2] - b[2]) < 1e-6) {
      return p.id;
    }
  }
  return "custom";
}

function chanParts(h: Hit) {
  const out: { k: string; n: number }[] = [];
  const push = (k: string, r: number | null | undefined) => {
    if (r == null || !Number.isFinite(r)) return;
    const n = Math.round(Math.max(0, r) * 100);
    if (n <= 0) return;
    out.push({ k, n });
  };
  push("走", h.r_close);
  push("影", h.r_shape);
  push("量", h.r_volume);
  return out;
}

function chanLine(h: Hit) {
  const parts = chanParts(h);
  if (parts.length === 0) return "";
  return parts.map((p) => `${p.k} ${p.n}`).join("  ");
}

function chanSuffix(h: Hit) {
  const parts = chanParts(h);
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0].k;
  return "综";
}

function fmtPx(n: number) {
  if (!Number.isFinite(n)) return "—";
  if (n >= 1000) return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (n >= 1) return n.toFixed(4);
  return n.toPrecision(5);
}

function fmtChg(n: number) {
  const s = (n * 100).toFixed(2);
  return (n >= 0 ? "+" : "") + s + "%";
}

const CLASS_TAG: Record<string, string> = { crypto: "币", tradfi: "股", gold: "金", us_stock: "股" };

function closedBars(bars: Bar[]) {
  return bars.filter((b) => b.is_closed);
}

function lastWindow(bars: Bar[], n: number) {
  const c = closedBars(bars);
  if (c.length < 2) return null;
  const slice = c.slice(-Math.min(n, c.length));
  return { start: slice[0].ts, end: slice[slice.length - 1].ts, n: slice.length };
}

const Z_EPS = 1e-12;

/** Subsequent closes z-scored with the match window close mean/std (population). */
function zTailFromMatchWindow(bars: Bar[], matchStartTs: number, markerTs: number): number[] {
  const matchCloses: number[] = [];
  const futureCloses: number[] = [];
  for (const b of bars) {
    if (b.ts >= matchStartTs && b.ts <= markerTs) matchCloses.push(b.close);
    else if (b.ts > markerTs) futureCloses.push(b.close);
  }
  if (!futureCloses.length || !matchCloses.length) return [];
  let mean = 0;
  for (const x of matchCloses) mean += x;
  mean /= matchCloses.length;
  let acc = 0;
  for (const x of matchCloses) acc += (x - mean) * (x - mean);
  const std = Math.sqrt(acc / matchCloses.length);
  if (std < Z_EPS) return futureCloses.map(() => 0);
  return futureCloses.map((c) => (c - mean) / std);
}


function fmtPctSigned(x: number) {
  const pct = x * 100;
  const mag = Math.abs(pct).toFixed(1);
  if (pct > 0) return `+${mag}%`;
  if (pct < 0) return `−${mag}%`;
  return "0.0%";
}

function pickForwardStep(forward: ForwardDist | null | undefined): ForwardStep | null {
  if (!forward?.steps?.length) return null;
  const { horizon, n_with_full, steps } = forward;
  if (n_with_full >= 3) {
    const last = steps.find((s) => s.i === horizon) ?? steps[steps.length - 1];
    if (last && last.n >= 3) return last;
  }
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].n >= 3) return steps[i];
  }
  return null;
}

function queryLabel(asset: string | undefined, pattern?: string, drawn?: boolean) {
  if (pattern === "cup_handle" || asset === "CUP_HANDLE") return "杯柄";
  if (drawn || asset === "DRAW") return "手绘";
  if (asset === "PATTERN") return "形态";
  return "查询";
}

export default function App() {
  const [asset, setAsset] = useState("BTC");
  const [tf, setTf] = useState<TF>("1D");
  const [n, setN] = useState(30);
  const storedW = useMemo(() => readStoredWeights(), []);
  const [weightPresets, setWeightPresets] = useState<WeightPreset[]>(DEFAULT_PRESETS);
  const [presetId, setPresetId] = useState(storedW.preset);
  const [wClose, setWClose] = useState(storedW.w_close);
  const [wShape, setWShape] = useState(storedW.w_shape);
  const [wVolume, setWVolume] = useState(storedW.w_volume);
  const [search, setSearch] = useState("");
  const [searchHits, setSearchHits] = useState<SearchHit[]>([]);
  const [dropOpen, setDropOpen] = useState(false);
  const [pullMsg, setPullMsg] = useState("");
  const [universe, setUniverse] = useState<UniverseAsset[]>([]);
  const [bootMsg, setBootMsg] = useState("");
  const [bars, setBars] = useState<Bar[]>([]);
  const [klinesErr, setKlinesErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState<{ start: number; end: number } | null>(null);
  const [matching, setMatching] = useState(false);
  const [matchErr, setMatchErr] = useState<string | null>(null);
  const [result, setResult] = useState<MatchResponse | null>(null);
  const [tab, setTab] = useState<"resonance" | "history">("resonance");
  const [selected, setSelected] = useState<Hit | null>(null);
  const [hover, setHover] = useState<Hit | null>(null);
  const [compareBars, setCompareBars] = useState<Bar[]>([]);
  const [hud, setHud] = useState<Bar | null>(null);
  const [returnTo, setReturnTo] = useState<{
    asset: string;
    tf: TF;
    start: number;
    end: number;
  } | null>(null);
  const [leftW, setLeftW] = useState(228);
  const [rightW, setRightW] = useState(320);
  const [bottomH, setBottomH] = useState(300);
  const pendingWindow = useRef<{ start: number; end: number; n: number } | null>(null);
  const skipClearRef = useRef(false);
  const fitKey = `${asset}-${tf}`;
  const searchRef = useRef<HTMLInputElement>(null);
  const lastTapRef = useRef({ key: "", t: 0 });
  const [layout, setLayout] = useState<Layout>(() => detectLayout());
  const [brushMode, setBrushMode] = useState(() => detectLayout() === "mobile");
  const [drawMode, setDrawMode] = useState(false);
  const [strokes, setStrokes] = useState<Pt[][]>([]);
  const [pathOverride, setPathOverride] = useState<number[] | null>(null);
  const [templates, setTemplates] = useState<WaveTemplate[]>([]);
  const [patterns, setPatterns] = useState<WavePattern[]>([]);
  const [activePattern, setActivePattern] = useState<string | null>(null);
  const [matchStatus, setMatchStatus] = useState("");
  const [compareNote, setCompareNote] = useState<string | null>(null);

  useEffect(() => subscribeLayout(setLayout), []);

  useEffect(() => {
    void fetchMatchPresets()
      .then((r) => {
        if (r.presets?.length) setWeightPresets(r.presets);
      })
      .catch(() => {
        /* keep defaults */
      });
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(
        WEIGHT_LS,
        JSON.stringify({ preset: presetId, w_close: wClose, w_shape: wShape, w_volume: wVolume }),
      );
    } catch {
      /* ignore */
    }
  }, [presetId, wClose, wShape, wVolume]);

  const weightBody = { w_close: wClose, w_shape: wShape, w_volume: wVolume };

  function applyPreset(id: string) {
    setPresetId(id);
    const p = weightPresets.find((x) => x.id === id);
    if (!p || p.w_close == null || p.w_shape == null || p.w_volume == null) return;
    setWClose(p.w_close);
    setWShape(p.w_shape);
    setWVolume(p.w_volume);
  }

  function editWeight(which: "close" | "shape" | "volume", raw: string) {
    const v = Number(raw);
    const next = Number.isFinite(v) && v >= 0 ? v : 0;
    const wc = which === "close" ? next : wClose;
    const ws = which === "shape" ? next : wShape;
    const wv = which === "volume" ? next : wVolume;
    if (which === "close") setWClose(next);
    if (which === "shape") setWShape(next);
    if (which === "volume") setWVolume(next);
    setPresetId(inferPreset(wc, ws, wv, weightPresets));
  }

  const loadTemplates = useCallback(async () => {
    try {
      const r = await fetchTemplates();
      setTemplates(r.templates || []);
    } catch {
      /* keep last */
    }
  }, []);

  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  useEffect(() => {
    void fetchPatterns()
      .then((r) => setPatterns(r.patterns || []))
      .catch(() => {
        setPatterns([{ id: "cup_handle", name_zh: "杯柄", suggested_n: 40 }]);
      });
  }, []);

  useEffect(() => {
    setBrushMode(layout === "mobile");
  }, [layout]);

  const loadUni = useCallback(async (t: TF) => {
    try {
      const u = await fetchUniverse(t);
      setUniverse(u.assets);
      setBootMsg(u.boot?.message ? `${u.boot.state} ${u.boot.message}` : u.boot?.state || "");
    } catch (e) {
      setBootMsg(String(e));
    }
  }, []);

  const loadK = useCallback(async (a: string, t: TF, nn: number, keepQuery = false) => {
    setLoading(true);
    setKlinesErr(null);
    try {
      const k = await fetchKlines(a, t);
      setBars(k.bars);
      if (pendingWindow.current) {
        const w = pendingWindow.current;
        pendingWindow.current = null;
        setQuery({ start: w.start, end: w.end });
        setN(w.n);
      } else if (!keepQuery) {
        const w = lastWindow(k.bars, nn);
        setQuery(w ? { start: w.start, end: w.end } : null);
      }
    } catch (e) {
      setBars([]);
      setKlinesErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadUni(tf);
    const id = setInterval(() => void loadUni(tf), 15000);
    return () => clearInterval(id);
  }, [tf, loadUni]);

  useEffect(() => {
    if (!skipClearRef.current) {
      setResult(null);
      setSelected(null);
      setHover(null);
      setMatchErr(null);
    }
    skipClearRef.current = false;
    void loadK(asset, tf, n);
    // n is intentionally not in deps: TF/asset change resets to current N last-window
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asset, tf]);

  useEffect(() => {
    const q = search.trim();
    if (q.length < 1) {
      setSearchHits([]);
      setDropOpen(false);
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(() => {
      void fetchSearch(q, tf)
        .then((r) => {
          if (cancelled) return;
          setSearchHits(r.hits || []);
          setDropOpen(true);
        })
        .catch(() => {
          if (cancelled) return;
          setSearchHits([]);
        });
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [search, tf]);

  const pickSymbol = useCallback(
    async (hit: SearchHit) => {
      const yamlHit = universe.find((a) => a.id === hit.id && !a.adhoc);
      if (!yamlHit) {
        setPullMsg(`拉取 ${hit.id} ${tf}…`);
        try {
          await postEnsure({ id: hit.id, venue: hit.venue, tf });
        } catch (e) {
          setPullMsg(String(e));
          return;
        } finally {
          setPullMsg("");
        }
      }
      setAsset(hit.id);
      setSearch("");
      setSearchHits([]);
      setDropOpen(false);
      searchRef.current?.blur();
      void loadUni(tf);
    },
    [universe, tf, loadUni],
  );

  const submitSearch = useCallback(async () => {
    const q = search.trim().toUpperCase();
    if (!q) return;
    const yamlHit = universe.find((a) => a.id === q && !a.adhoc);
    if (yamlHit) {
      setAsset(yamlHit.id);
      setSearch("");
      setSearchHits([]);
      setDropOpen(false);
      searchRef.current?.blur();
      return;
    }
    const hit =
      searchHits[0] ||
      ({
        id: q,
        venue: "binance",
        native_symbol: q,
        class: "crypto",
        ready: false,
        source: "typed",
      } satisfies SearchHit);
    await pickSymbol(hit);
  }, [search, universe, searchHits, pickSymbol]);

  const qBars = useMemo(() => {
    if (!query) return [];
    return closedBars(bars).filter((b) => b.ts >= query.start && b.ts <= query.end);
  }, [bars, query]);

  const drawPath = useMemo(() => {
    if (pathOverride && pathOverride.length >= 2) {
      if (pathOverride.length === n) return pathOverride;
      return resampleXY(
        pathOverride.map((_, i) => (pathOverride.length === 1 ? 0 : i / (pathOverride.length - 1))),
        pathOverride,
        n,
      );
    }
    const pts = flattenStrokes(strokes);
    if (pts.length < 2) return null;
    const path = resamplePath(pts, n);
    if (path.length < 2 || pathStd(path) < 1e-12) return null;
    return path;
  }, [strokes, n, pathOverride]);

  const drawZ = useMemo(() => (drawPath ? zscore(drawPath) : []), [drawPath]);

  function setChartMode(next: "brush" | "draw" | "pan") {
    if (next === "draw") {
      setDrawMode(true);
      setBrushMode(false);
    } else if (next === "brush") {
      setDrawMode(false);
      setBrushMode(true);
    } else {
      setDrawMode(false);
      setBrushMode(false);
    }
  }

  function onStrokes(next: Pt[][]) {
    setStrokes(next);
    setPathOverride(null);
    setActivePattern(null);
    setResult(null);
    setSelected(null);
  }

  const lastPx = bars.length ? bars[bars.length - 1] : null;
  const hudBar = hud || lastPx;

  function changeTf(next: TF) {
    if (next === tf) return;
    setTf(next);
  }

  function onBrush(start: number, end: number) {
    const c = closedBars(bars).filter((b) => b.ts >= start && b.ts <= end);
    if (c.length < 2) return;
    setQuery({ start: c[0].ts, end: c[c.length - 1].ts });
    setN(c.length);
    setResult(null);
    setSelected(null);
    setActivePattern(null);
  }

  async function runPattern(id: string) {
    if (matching) return;
    const spec = patterns.find((p) => p.id === id);
    const suggested = spec?.suggested_n ?? 40;
    const nn = Math.max(n, suggested);
    const path =
      spec?.path && spec.path.length >= 2
        ? spec.path.length === nn
          ? spec.path
          : resampleXY(
              spec.path.map((_, i) => (spec.path!.length === 1 ? 0 : i / (spec.path!.length - 1))),
              spec.path,
              nn,
            )
        : id === "cup_handle"
          ? cupHandlePath(nn)
          : null;
    if (!path || path.length < 2) {
      setMatchErr("形态路径无效");
      return;
    }
    setChartMode("draw");
    setN(nn);
    setActivePattern(id);
    setStrokes([pathToStroke(path)]);
    setPathOverride(path);
    setMatching(true);
    setMatchErr(null);
    setMatchStatus("扫描现价 · 币安前80 + 本地股票/黄金");
    try {
      const m = await postMatch({ tf, n: nn, pattern: id, ...weightBody });
      setResult(m);
      setSelected(m.resonance[0] || m.history[0] || null);
      setTab("resonance");
    } catch (e) {
      setMatchErr(String(e));
    } finally {
      setMatching(false);
      setMatchStatus("");
    }
  }

  async function runMatch() {
    if (matching) return;
    setMatching(true);
    setMatchErr(null);
    setMatchStatus(activePattern ? "扫描现价 · 币安前80 + 本地股票/黄金" : "");
    try {
      if (activePattern) {
        const m = await postMatch({ tf, n, pattern: activePattern, ...weightBody });
        setResult(m);
        const first = m.resonance[0] || m.history[0] || null;
        setSelected(first);
        setTab("resonance");
      } else if (drawMode) {
        if (!drawPath) {
          setMatchErr("请先画出有起伏的形态");
          return;
        }
        const m = await postMatch({ tf, n, path: drawPath, ...weightBody });
        setResult(m);
        const first = m.resonance[0] || m.history[0] || null;
        setSelected(first);
        setTab("resonance");
      } else {
        if (!query) return;
        const m = await postMatch({
          asset,
          tf,
          n: qBars.length || n,
          start_ts: query.start,
          end_ts: query.end,
          ...weightBody,
        });
        setResult(m);
        const first = m.history[0] || m.resonance[0] || null;
        setSelected(first);
        setTab(m.history.length ? "history" : "resonance");
      }
    } catch (e) {
      setMatchErr(String(e));
    } finally {
      setMatching(false);
      setMatchStatus("");
    }
  }

  async function saveDrawing() {
    if (!drawPath) return;
    const fallback = `手绘 ${tf} N=${n}`;
    const name = window.prompt("波形名称", fallback);
    if (name == null) return;
    try {
      await postTemplate({ name: name.trim() || fallback, tf, n, path: drawPath });
      await loadTemplates();
    } catch (e) {
      setMatchErr(String(e));
    }
  }

  async function loadTemplate(tpl: WaveTemplate) {
    if (tpl.tf !== tf) skipClearRef.current = true;
    setChartMode("draw");
    if (tpl.tf !== tf) setTf(tpl.tf as TF);
    setN(tpl.n);
    setStrokes([pathToStroke(tpl.path)]);
    setPathOverride(tpl.path);
    setMatchErr(null);
    setActivePattern(null);
    setMatching(true);
    try {
      const m = await postMatch({ tf: tpl.tf as TF, n: tpl.n, path: tpl.path, ...weightBody });
      setResult(m);
      setSelected(m.resonance[0] || m.history[0] || null);
      setTab("resonance");
    } catch (e) {
      setMatchErr(String(e));
    } finally {
      setMatching(false);
    }
  }

  async function removeTemplate(id: string) {
    try {
      await deleteTemplate(id);
      await loadTemplates();
    } catch (e) {
      setMatchErr(String(e));
    }
  }

  useEffect(() => {
    const hit = selected;
    if (!hit) {
      setCompareBars([]);
      setCompareNote(null);
      return;
    }
    let cancel = false;
    const pad = hit.bars || n;
    setCompareNote(null);
    void fetchKlines(hit.asset, hit.tf as TF, hit.start_ts, hit.end_ts, pad)
      .then((k) => {
        if (!cancel) {
          setCompareBars(k.bars);
          setCompareNote(null);
        }
      })
      .catch(async () => {
        const liveSym =
          hit.native_symbol ||
          (hit.venue === "binance" ? `${hit.asset}USDT` : "");
        if (liveSym) {
          try {
            const k = await fetchLiveKlines(liveSym, hit.tf as TF, hit.bars || pad);
            if (!cancel) {
              setCompareBars(k.bars);
              setCompareNote("该标的未入库，仅现价窗口");
            }
            return;
          } catch {
            /* fall through */
          }
        }
        if (!cancel) {
          setCompareBars([]);
          setCompareNote("该标的未入库，仅现价窗口");
        }
      });
    return () => {
      cancel = true;
    };
  }, [selected, n]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const tag = (ev.target as HTMLElement)?.tagName;
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      if (ev.key === "Enter" && !ev.repeat) {
        if (typing && searchRef.current === document.activeElement) {
          void submitSearch();
          ev.preventDefault();
          return;
        }
        if (!typing) {
          void runMatch();
          ev.preventDefault();
        }
      }
      if (typing) return;
      if (ev.key === "1") changeTf("1H");
      if (ev.key === "4") changeTf("4H");
      if (ev.key === "5") changeTf("12H");
      if (ev.key === "d" || ev.key === "D") changeTf("1D");
      if (ev.key === "Escape") {
        setSelected(null);
        setHover(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [universe, search, searchHits, submitSearch, query, matching, asset, tf, n, drawMode, drawPath, wClose, wShape, wVolume, presetId]);

  function applyN(next: number) {
    const nn = Math.max(2, Math.min(500, next || 30));
    setN(nn);
    if (!drawMode) {
      const w = lastWindow(bars, nn);
      if (w) setQuery({ start: w.start, end: w.end });
    }
    setResult(null);
    setSelected(null);
    if (!drawMode) setActivePattern(null);
  }

  async function jumpHistory(hit: Hit) {
    setReturnTo({ asset, tf, start: query?.start ?? hit.start_ts, end: query?.end ?? hit.end_ts });
    if (hit.asset === asset && hit.tf === tf) {
      setQuery({ start: hit.start_ts, end: hit.end_ts });
      setN(hit.bars);
      return;
    }
    pendingWindow.current = { start: hit.start_ts, end: hit.end_ts, n: hit.bars };
    setAsset(hit.asset);
    if (hit.tf !== tf) setTf(hit.tf as TF);
  }

  function backToQuery() {
    if (!returnTo) return;
    setAsset(returnTo.asset);
    setTf(returnTo.tf);
    const saved = returnTo;
    setReturnTo(null);
    setTimeout(() => setQuery({ start: saved.start, end: saved.end }), 0);
  }

  const shown = universe.filter((a) => {
    if (!search.trim()) return true;
    const q = search.trim().toUpperCase();
    return a.id.includes(q) || a.native_symbol.toUpperCase().includes(q);
  });

  const rows = tab === "resonance" ? result?.resonance ?? [] : result?.history ?? [];
  const qz = result?.query_z ?? (drawMode ? drawZ : []);
  const afterCount = useMemo(() => {
    if (!selected) return 0;
    return compareBars.filter((b) => b.ts > selected.end_ts).length;
  }, [compareBars, selected]);
  const matchExtended = useMemo(() => {
    if (!selected?.zscore?.length) return null;
    if (!compareBars.length) return selected.zscore;
    const tail = zTailFromMatchWindow(compareBars, selected.start_ts, selected.end_ts);
    return tail.length ? selected.zscore.concat(tail) : selected.zscore;
  }, [selected, compareBars]);

  function startSplit(which: "left" | "right" | "bottom", ev: React.PointerEvent) {
    ev.preventDefault();
    const x0 = ev.clientX;
    const y0 = ev.clientY;
    const l0 = leftW;
    const r0 = rightW;
    const b0 = bottomH;
    const move = (e: PointerEvent) => {
      if (which === "left") setLeftW(Math.max(160, Math.min(360, l0 + (e.clientX - x0))));
      if (which === "right") setRightW(Math.max(240, Math.min(480, r0 - (e.clientX - x0))));
      if (which === "bottom") setBottomH(Math.max(180, Math.min(480, b0 - (e.clientY - y0))));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  const showForwardUi = tab === "history" && !!result;
  const forward = result?.forward ?? null;
  const forwardOk = !!forward && forward.n_with_full > 0;
  const forwardStep = pickForwardStep(forward);
  const style = {
    ["--left" as string]: `${leftW}px`,
    ["--right" as string]: `${rightW}px`,
    ["--bottom" as string]: `${bottomH + (showForwardUi ? 140 : 0)}px`,
  } as React.CSSProperties;

  const pxClass = lastPx && lastPx.close >= lastPx.open ? "up" : "down";

  return (
    <div className={`app ${layout === "mobile" ? "layout-mobile" : "layout-desktop"}`} style={style}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">WAVE</span>
          <span className="brand-sub">波形终端</span>
        </div>
        <div className="search-wrap">
          <input
            ref={searchRef}
            className="search mono"
            placeholder="搜索 BTC / ETH / NVDA"
            value={search}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              setSearch(e.target.value);
              if (e.target.value.trim()) setDropOpen(true);
            }}
            onFocus={() => {
              if (search.trim() && searchHits.length) setDropOpen(true);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                e.stopPropagation();
                void submitSearch();
              }
              if (e.key === "Escape") {
                setDropOpen(false);
                searchRef.current?.blur();
              }
            }}
          />
          {dropOpen && search.trim() && searchHits.length > 0 ? (
            <div className="search-drop" role="listbox">
              {searchHits.map((h) => (
                <button
                  type="button"
                  key={`${h.source}-${h.id}-${h.venue}`}
                  className="search-hit"
                  role="option"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => void pickSymbol(h)}
                >
                  <span className="search-hit-id">{h.id}</span>
                  <span className="search-hit-nat">{h.native_symbol}</span>
                  <span className="search-hit-venue">{h.venue}</span>
                  <span className={`search-hit-cls ${h.class}`}>{CLASS_TAG[h.class] || h.class}</span>
                  {h.ready ? <span className="search-hit-ready">已缓存</span> : null}
                </button>
              ))}
            </div>
          ) : null}
        </div>
        <div className="seg">
          {TFS.map((t) => (
            <button key={t} className={t === tf ? "on" : ""} onClick={() => changeTf(t)}>
              {t}
            </button>
          ))}
        </div>
        <button
          type="button"
          className={`btn-brush topbar-brush ${brushMode && !drawMode ? "on" : ""}`}
          aria-pressed={brushMode && !drawMode}
          onClick={() => setChartMode(brushMode && !drawMode ? "pan" : "brush")}
        >
          框选
        </button>
        <button
          type="button"
          className={`btn-brush topbar-draw ${drawMode ? "on" : ""}`}
          aria-pressed={drawMode}
          onClick={() => setChartMode(drawMode ? "pan" : "draw")}
        >
          手绘
        </button>
        <div className="pat-group topbar-pattern" aria-label="形态">
          <span className="pat-label">形态</span>
          {(patterns.length ? patterns : [{ id: "cup_handle", name_zh: "杯柄", suggested_n: 40 }]).map((pat) => (
            <button
              key={pat.id}
              type="button"
              className={`btn-brush pat-chip ${activePattern === pat.id ? "on" : ""}`}
              aria-pressed={activePattern === pat.id}
              onClick={() => void runPattern(pat.id)}
            >
              {pat.path && pat.path.length >= 2 ? <Sparkline data={pat.path} color="#f0b90b" /> : null}
              {pat.name_zh}
            </button>
          ))}
        </div>
        <label className="n-field">
          N
          <input
            className="mono"
            type="number"
            min={2}
            max={500}
            value={n}
            onChange={(e) => applyN(Number(e.target.value))}
          />
        </label>
        <label className="n-field w-field">
          权重
          <select
            value={presetId}
            onChange={(e) => applyPreset(e.target.value)}
            aria-label="通道权重"
          >
            {weightPresets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name_zh}
              </option>
            ))}
          </select>
        </label>
        <div className={`w-nums${presetId === "custom" ? " custom" : ""}`}>
          <label>
            走势
            <input
              className="mono"
              type="number"
              min={0}
              step="any"
              value={wClose}
              onChange={(e) => editWeight("close", e.target.value)}
            />
          </label>
          <label>
            结构
            <input
              className="mono"
              type="number"
              min={0}
              step="any"
              value={wShape}
              onChange={(e) => editWeight("shape", e.target.value)}
            />
          </label>
          <label>
            量能
            <input
              className="mono"
              type="number"
              min={0}
              step="any"
              value={wVolume}
              onChange={(e) => editWeight("volume", e.target.value)}
            />
          </label>
        </div>
        {drawMode || activePattern ? <span className="w-hint">手绘/形态只用走势</span> : null}
        <div className="win-meta">
          {activePattern
            ? `形态 ${activePattern === "cup_handle" ? "杯柄" : activePattern} ${tf} N=${n}`
            : drawMode
            ? drawPath
              ? `手绘 ${tf} N=${n} · ${flattenStrokes(strokes).length} 点`
              : "手绘：画出要找的形态"
            : query
              ? `${qBars.length} 根  ${new Date(query.start).toISOString().slice(0, 16).replace("T", " ")} → ${new Date(query.end).toISOString().slice(0, 16).replace("T", " ")} UTC`
              : "未选择查询段"}
        </div>
        <div className="spacer" />
        {returnTo ? (
          <button className="btn-ghost" onClick={backToQuery}>
            返回查询段
          </button>
        ) : null}
        <span className={`boot-pill ${bootMsg.includes("ing") || pullMsg ? "busy" : ""}`}>{pullMsg || bootMsg}</span>
        {drawMode && drawPath ? (
          <button className="btn-ghost" onClick={() => void saveDrawing()}>
            保存波形
          </button>
        ) : null}
        <button
          className="btn-match"
          disabled={matching || (drawMode ? !drawPath : !query)}
          onClick={() => void runMatch()}
        >
          {matching ? matchStatus || "匹配中…" : "匹配波形"}
        </button>
      </header>
      <div className={`tpl-row${templates.length ? "" : " empty"}`} aria-label="已存波形">
        {templates.map((tpl) => (
          <button
            type="button"
            key={tpl.id}
            className="tpl-chip"
            onClick={() => void loadTemplate(tpl)}
            title={`${tpl.name} ${tpl.tf} N=${tpl.n}`}
          >
            <Sparkline data={tpl.path} color="#f0b90b" />
            <span className="tpl-name">{tpl.name}</span>
            <span className="tpl-meta">
              {tpl.tf} N={tpl.n}
            </span>
            <span
              className="tpl-x"
              role="button"
              aria-label="删除"
              onClick={(e) => {
                e.stopPropagation();
                void removeTemplate(tpl.id);
              }}
            >
              ×
            </span>
          </button>
        ))}
      </div>

      <div className="universe-chips" aria-label="宇宙监视">
        {shown.length === 0 ? (
          <div className="watch-empty">NO SYMBOLS</div>
        ) : (
          shown.map((a) => {
            const up = (a.last?.change ?? 0) >= 0;
            return (
              <button
                type="button"
                key={a.id}
                className={`watch-chip ${a.id === asset ? "on" : ""}`}
                onClick={() => setAsset(a.id)}
              >
                <span className="watch-id">{a.id}{a.adhoc ? " ·临" : ""}</span>
                <span className={`watch-px ${a.last ? (up ? "up" : "down") : ""}`}>
                  {a.last ? fmtPx(a.last.close) : "—"}
                </span>
              </button>
            );
          })
        )}
      </div>

      <div className="workspace">
        <aside className="dock left">
          <div className="dock-h">宇宙监视</div>
          <div className="watch-list">
            {shown.length === 0 ? (
              <div className="watch-empty">NO SYMBOLS</div>
            ) : (
              shown.map((a) => {
                const up = (a.last?.change ?? 0) >= 0;
                return (
                  <div
                    key={a.id}
                    className={`watch-row ${a.id === asset ? "on" : ""}`}
                    onClick={() => setAsset(a.id)}
                  >
                    <div className="watch-id">{a.id}{a.adhoc ? <span className="adhoc-tag">临</span> : null}</div>
                    <div className="watch-sym">{a.native_symbol}</div>
                    <div className={`watch-px ${a.last ? (up ? "up" : "down") : ""}`}>
                      {a.last ? fmtPx(a.last.close) : "—"}
                    </div>
                  </div>
                );
              })
            )}
          </div>
          <div className="split left" onPointerDown={(e) => startSplit("left", e)} />
        </aside>

        <main className="center">
          <section className="chart-stack">
            <div className="hud">
              <b>
                {asset}/{tf}
              </b>
              {pullMsg ? <span className="pull-msg">{pullMsg}</span> : null}
              {hudBar ? (
                <>
                  <span>
                    O <b className={pxClass}>{fmtPx(hudBar.open)}</b>
                  </span>
                  <span>
                    H <b className={pxClass}>{fmtPx(hudBar.high)}</b>
                  </span>
                  <span>
                    L <b className={pxClass}>{fmtPx(hudBar.low)}</b>
                  </span>
                  <span>
                    C <b className={pxClass}>{fmtPx(hudBar.close)}</b>
                  </span>
                  <span>VOL {hudBar.volume.toLocaleString("en-US", { maximumFractionDigits: 2 })}</span>
                  {!hudBar.is_closed ? <span>FORMING</span> : null}
                </>
              ) : (
                <span>等待 K 线…</span>
              )}
              <div className="spacer" />
              <button
                type="button"
                className={`btn-brush ${brushMode && !drawMode ? "on" : ""}`}
                aria-pressed={brushMode && !drawMode}
                onClick={() => setChartMode(brushMode && !drawMode ? "pan" : "brush")}
              >
                框选
              </button>
              <button
                type="button"
                className={`btn-brush ${drawMode ? "on" : ""}`}
                aria-pressed={drawMode}
                onClick={() => setChartMode(drawMode ? "pan" : "draw")}
              >
                手绘
              </button>
              <div className="pat-group hud-pattern" aria-label="形态">
                <span className="pat-label">形态</span>
                {(patterns.length ? patterns : [{ id: "cup_handle", name_zh: "杯柄", suggested_n: 40 }]).map((pat) => (
                  <button
                    key={pat.id}
                    type="button"
                    className={`btn-brush pat-chip ${activePattern === pat.id ? "on" : ""}`}
                    aria-pressed={activePattern === pat.id}
                    onClick={() => void runPattern(pat.id)}
                  >
                    {pat.path && pat.path.length >= 2 ? <Sparkline data={pat.path} color="#f0b90b" /> : null}
                    {pat.name_zh}
                  </button>
                ))}
              </div>
              <span className="hud-kbd">
                {drawMode ? (
                  <>
                    手绘开 · 在图上画出形态 · 撤销 / 清除 / 保存波形 · <kbd>Enter</kbd> 匹配全池
                  </>
                ) : brushMode ? (
                  <>
                    框选开 · 图上拖出查询段 · 也可拖黄条两端 · <kbd>1</kbd>
                    <kbd>4</kbd>
                    <kbd>5</kbd>
                    <kbd>D</kbd> 周期 · <kbd>Enter</kbd> 匹配
                  </>
                ) : (
                  <>
                    <kbd>Shift</kbd> 拖拽框选 · 框选开后图上拖选 · 拖黄条改窗口 · <kbd>1</kbd>
                    <kbd>4</kbd>
                    <kbd>5</kbd>
                    <kbd>D</kbd> 周期 · <kbd>Enter</kbd> 匹配
                  </>
                )}
              </span>
              <span className="hud-hint">
                {drawMode
                  ? "手绘：在图上画出形态后点匹配波形"
                  : brushMode
                    ? "框选开：图上拖出查询段 · 也可拖黄条两端"
                    : "框选关：拖图平移 · 开框选或拖黄条改查询段"}
              </span>
            </div>
            {klinesErr ? <div className="term-err">{klinesErr}</div> : null}
            {!loading && !klinesErr && bars.length === 0 ? (
              <div className="term-empty">
                <div>NO SERIES</div>
                <div>主序列尚未入库，等待 ingest / sync</div>
              </div>
            ) : null}
            <div className={`chart-stage${drawMode ? " draw-on" : ""}`}>
              <CandleChart
                bars={bars}
                fromTs={drawMode ? null : (query?.start ?? null)}
                toTs={drawMode ? null : (query?.end ?? null)}
                onBrush={onBrush}
                onHud={setHud}
                fitKey={fitKey}
                brushMode={brushMode && !drawMode}
              />
              {drawMode ? <DrawPad strokes={strokes} n={n} onCommit={onStrokes} /> : null}
            </div>
          </section>
          <section className={`compare${showForwardUi ? " has-forward" : ""}`} style={{ position: "relative" }}>
            <div className="split-h" onPointerDown={(e) => startSplit("bottom", e)} />
            {showForwardUi ? (
              <div className="forward-bar">
                {forwardOk && forwardStep ? (
                  <>
                    事后 {forward!.horizon} 根 · {forward!.n_with_full}/{forward!.n_hits} 走满 · 中位{" "}
                    <span className={forwardStep.p50 >= 0 ? "up" : "down"}>{fmtPctSigned(forwardStep.p50)}</span>
                    {" · 四分位 "}
                    <span className={forwardStep.p25 >= 0 ? "up" : "down"}>{fmtPctSigned(forwardStep.p25)}</span>
                    {" ~ "}
                    <span className={forwardStep.p75 >= 0 ? "up" : "down"}>{fmtPctSigned(forwardStep.p75)}</span>
                    {" · 收涨 "}
                    <span className={forwardStep.pct_up >= 0.5 ? "up" : "down"}>
                      {Math.round(forwardStep.pct_up * 100)}%
                    </span>
                  </>
                ) : (
                  <span className="forward-empty">事后样本不足</span>
                )}
              </div>
            ) : null}
            <div className="compare-pane">
              <div className="compare-h">
                Z-SCORE 叠加 · {queryLabel(result?.query?.asset, result?.query?.pattern, result?.query?.drawn)} vs {selected ? selected.asset : "—"}
                {hover && hover !== selected ? ` · 预览 ${hover.asset}` : ""}
              </div>
              <div className="compare-host">
                {qz.length ? (
                  <ZOverlay
                    query={qz}
                    match={matchExtended}
                    hover={hover && hover !== selected ? hover.zscore : null}
                    markerIndex={selected && qz.length ? qz.length - 1 : null}
                  />
                ) : (
                  <div className="term-empty" style={{ background: "transparent" }}>
                    运行「匹配波形」后在此对齐对比
                  </div>
                )}
              </div>
            </div>
            <div className="compare-pane compare-candles">
              <div
                className="compare-h"
                title={selected ? `${selected.asset} ${selected.start_utc} → ${selected.end_utc}` : undefined}
              >
                {selected
                  ? compareNote
                    ? `类比 K 线 ${selected.asset} · ${compareNote}`
                    : selected.kind === "resonance"
                    ? `类比 K 线 ${selected.asset} · 同时段没有之后的走势 · 后面尚未走出 · 对齐点 ${selected.end_utc}`
                    : `类比 K 线 ${selected.asset} · 匹配段 + 之后 ${afterCount} 根 · 对齐点 ${selected.end_utc}`
                  : "类比 K 线"}
              </div>
              <div className="compare-host">
                {compareBars.length ? (
                  <WindowCandles
                    bars={compareBars}
                    markerTs={selected?.end_ts ?? null}
                    matchStartTs={selected?.start_ts ?? null}
                  />
                ) : (
                  <div className="term-empty" style={{ background: "transparent" }}>
                    {compareNote ? (
                      <div>{compareNote}</div>
                    ) : (
                      <>
                        <div>点击右侧结果查看真实蜡烛</div>
                        <div>对齐点之后是后来的走势</div>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>
            {showForwardUi ? (
              <div className="forward-fan-wrap">
                <div className="forward-fan-h">对齐后涨跌分布</div>
                {forwardOk && forward?.steps?.length ? (
                  <div className="forward-fan-host">
                    <ForwardFan steps={forward.steps} horizon={forward.horizon} />
                  </div>
                ) : (
                  <div className="forward-fan-empty">事后样本不足</div>
                )}
              </div>
            ) : null}
          </section>
        </main>

        <aside className="dock right">
          <div className="dock-h" style={{ padding: 0 }}>
            <div className="tabs">
              <button className={tab === "resonance" ? "on" : ""} onClick={() => setTab("resonance")}>
                当前共振 {result ? result.resonance.length : ""}
              </button>
              <button className={tab === "history" ? "on" : ""} onClick={() => setTab("history")}>
                历史类比 {result ? result.history.length : ""}
              </button>
            </div>
          </div>
          {result?.query?.history_pool && !result.query.drawn && !result.query.pattern ? (
            <div className="pool-line" title={tab === "history" ? (result.query.history_pool || []).join(" · ") : (result.query.resonance_pool || []).join(" · ")}>
              {tab === "history"
                ? `历史池 ${(result.query.history_pool || []).join(" · ") || "—"}`
                : `共振池 ${(result.query.resonance_pool || []).length ? (result.query.resonance_pool || []).join(" · ") : "无"}`}
            </div>
          ) : null}
          {matching ? <div className="watch-empty">{matchStatus || "SCANNING POOL…"}</div> : null}
          {matchErr ? <div className="term-err" style={{ position: "relative", background: "transparent" }}>{matchErr}</div> : null}
          <div className="result-list">
            {!matching && !result ? (
              <div className="watch-empty">
                空池
                <br />
                {activePattern ? "点「匹配波形」或再点杯柄扫描现价" : drawMode ? "画出形态后点「匹配波形」" : "框选查询段后点「匹配波形」"}
              </div>
            ) : null}
            {result && rows.length === 0 && !matching ? (
              <div className="watch-empty">{tab === "resonance" ? "当前窗口无跨资产共振" : "无历史窗口"}</div>
            ) : null}
            {rows.map((h) => (
              <div
                key={`${h.kind}-${h.asset}-${h.start_ts}`}
                className={`result-row ${selected?.asset === h.asset && selected.start_ts === h.start_ts ? "on" : ""} ${hover?.asset === h.asset && hover.start_ts === h.start_ts ? "hov" : ""}`}
                onClick={() => {
                  const key = `${h.kind}-${h.asset}-${h.start_ts}`;
                  const now = performance.now();
                  const dbl = lastTapRef.current.key === key && now - lastTapRef.current.t < 400;
                  lastTapRef.current = { key, t: now };
                  setSelected(h);
                  if (dbl && h.kind === "history") void jumpHistory(h);
                }}
                onMouseEnter={() => {
                  if (layout !== "mobile") setHover(h);
                }}
                onMouseLeave={() => setHover(null)}
                onDoubleClick={() => {
                  if (h.kind === "history") void jumpHistory(h);
                }}
              >
                <div className="rk">{h.rank}</div>
                <div className="watch-id">{h.asset}</div>
                <div className="score-col">
                  <div className="score-n">
                    {h.score.toFixed(1)}
                    {chanSuffix(h) ? <span className="score-sfx">{chanSuffix(h)}</span> : null}
                  </div>
                  <div className="bar-thin">
                    <i style={{ width: `${Math.max(0, Math.min(100, h.score))}%` }} />
                  </div>
                  {chanLine(h) ? <div className="chan-sub">{chanLine(h)}</div> : null}
                </div>
                <Sparkline data={h.zscore} color={h.score >= 70 ? "#0ecb81" : "#f0b90b"} />
                <div className="dates">
                  {h.start_utc} → {h.end_utc}
                </div>
              </div>
            ))}
          </div>
          <div className="split right" onPointerDown={(e) => startSplit("right", e)} />
        </aside>
      </div>
    </div>
  );
}
