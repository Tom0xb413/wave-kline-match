"""Live last-N resonance scan: Binance USDT majors + local SQLite pool.

Does not ingest. Failures degrade to local-pool resonance rather than 500.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import numpy as np

from kline_match.config import load_universe
from kline_match.db import load_all_primary_closes
from kline_match.exchanges.binance import BinanceClient, parse_binance_klines
from kline_match.match import MatchBundle, mass_pearson, match_drawn, score_from_r, summarize_forward, zscore
from kline_match.models import Candle, MatchHit
from kline_match.patterns import filter_resonance
from kline_match.timeframes import TF_MS, ms_to_utc, now_ms, normalize_tf

STABLE_BASES = {
    "USDC",
    "BUSD",
    "TUSD",
    "FDUSD",
    "DAI",
    "USDP",
    "EUR",
    "AEUR",
    "UST",
    "USDD",
    "PYUSD",
    "USD1",
    "USDE",
    "EURI",
    "USDT",
}
LEVERAGE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
LIVE_CAP = 80
LIVE_BUDGET_S = 22.0
PACE_FAIL_ABORT = 5


def asset_from_symbol(symbol: str) -> str:
    s = str(symbol or "").upper()
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    return s


def _is_excluded_base(base: str) -> bool:
    b = str(base or "").upper()
    if not b or b in STABLE_BASES:
        return True
    return any(b.endswith(sfx) and len(b) > len(sfx) for sfx in LEVERAGE_SUFFIXES)


def pick_usdt_majors(tickers: list[Any], cap: int = LIVE_CAP) -> list[str]:
    rows: list[tuple[float, str]] = []
    for t in tickers or []:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or "").upper()
        if not sym.endswith("USDT") or len(sym) <= 4:
            continue
        base = sym[:-4]
        if _is_excluded_base(base):
            continue
        try:
            qv = float(t.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            qv = 0.0
        rows.append((qv, sym))
    rows.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, sym in rows:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= int(cap):
            break
    return out


def pearson_equal(series: np.ndarray, query: np.ndarray) -> float:
    series = np.asarray(series, dtype=np.float64).ravel()
    query = np.asarray(query, dtype=np.float64).ravel()
    if series.size != query.size or series.size < 2:
        return float("nan")
    r = mass_pearson(series, query)
    if r.size == 0:
        return float("nan")
    val = float(r[0])
    return val if np.isfinite(val) else float("nan")


def hit_from_closes(
    *,
    asset: str,
    tf: str,
    venue: str,
    native_symbol: str,
    ts: list[int],
    closes: np.ndarray,
    r: float,
    kind: str = "resonance",
    rank: int = 0,
) -> MatchHit:
    closes = np.asarray(closes, dtype=np.float64).ravel()
    n = int(closes.size)
    start_ts = int(ts[0]) if ts else 0
    end_ts = int(ts[-1]) if ts else 0
    step = TF_MS.get(tf, 0)
    return MatchHit(
        rank=rank,
        asset=asset,
        tf=tf,
        start_ts=start_ts,
        end_ts=end_ts,
        start_utc=ms_to_utc(start_ts) if start_ts else "",
        end_utc=ms_to_utc(end_ts + step) if end_ts else "",
        bars=n,
        pearson_r=float(r),
        score=round(score_from_r(r), 4),
        venue=venue,
        zscore=zscore(closes).tolist(),
        kind=kind,
        native_symbol=native_symbol,
        r_close=float(r),
        r_shape=None,
        r_volume=None,
        weights={"close": 1.0, "shape": 0.0, "volume": 0.0},
    )


def fetch_last_closed_klines(
    client: BinanceClient,
    symbol: str,
    tf: str,
    n: int,
    *,
    timeout: float = 8.0,
) -> list[Candle]:
    interval = {"1H": "1h", "4H": "4h", "12H": "12h", "1D": "1d"}[tf]
    raw = client._get(
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": int(n) + 2,
        },
        timeout=float(timeout),
        retries=2,
    )
    rows = parse_binance_klines(raw or [], now_ms())
    return [c for c in rows if c.is_closed]


def scan_binance_last_n(
    query: np.ndarray,
    tf: str,
    n: int,
    *,
    cap: int = LIVE_CAP,
    budget_s: float = LIVE_BUDGET_S,
) -> tuple[list[MatchHit], dict[str, Any]]:
    """Score last-n closed klines of top USDT spot majors. Never raises."""
    tf = normalize_tf(tf)
    n = int(n)
    meta: dict[str, Any] = {
        "ok": False,
        "n_symbols": 0,
        "n_scored": 0,
        "n_failed": 0,
        "error": None,
    }
    query = np.asarray(query, dtype=np.float64).ravel()
    if query.size != n:
        meta["error"] = "query 长度与 N 不一致"
        return [], meta
    client = BinanceClient()
    try:
        tickers = client.fetch_ticker_24hr()
    except Exception as exc:
        meta["error"] = f"ticker 失败: {exc}"
        return [], meta
    if not isinstance(tickers, list) or not tickers:
        meta["error"] = "ticker 为空"
        return [], meta
    symbols = pick_usdt_majors(tickers, cap=cap)
    meta["n_symbols"] = len(symbols)
    if not symbols:
        meta["error"] = "无 USDT 现货"
        return [], meta

    hits: list[MatchHit] = []
    deadline = time.monotonic() + float(budget_s)
    pace_lock = threading.Lock()
    last_call = [0.0]
    min_interval = 0.10  # ~10 req/s
    host = client._host

    def _score(sym: str) -> MatchHit | None:
        if time.monotonic() > deadline:
            return None
        with pace_lock:
            wait = min_interval - (time.monotonic() - last_call[0])
            if wait > 0:
                time.sleep(wait)
            last_call[0] = time.monotonic()
        worker = BinanceClient()
        worker._host = host
        worker._min_interval_s = 0.0
        candles = fetch_last_closed_klines(worker, sym, tf, n, timeout=8.0)
        if len(candles) < n:
            raise RuntimeError("klines short")
        window = candles[-n:]
        closes = np.asarray([c.close for c in window], dtype=np.float64)
        r = pearson_equal(closes, query)
        if not np.isfinite(r):
            raise RuntimeError("r nan")
        ts = [int(c.ts) for c in window]
        return hit_from_closes(
            asset=asset_from_symbol(sym),
            tf=tf,
            venue="binance",
            native_symbol=sym,
            ts=ts,
            closes=closes,
            r=float(r),
        )

    n_fail = 0
    n_ok = 0
    timed_out = False
    workers = 8
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_score, sym): sym for sym in symbols}
        for fut in as_completed(futs):
            if time.monotonic() > deadline:
                timed_out = True
            try:
                hit = fut.result()
            except Exception:
                n_fail += 1
                continue
            if hit is None:
                timed_out = True
                continue
            n_ok += 1
            hits.append(hit)
    meta["n_scored"] = n_ok
    meta["n_failed"] = n_fail
    if timed_out and n_ok < len(symbols):
        meta["error"] = "扫描超时截断"
    if n_ok == 0 and n_fail >= min(PACE_FAIL_ABORT, len(symbols)):
        meta["error"] = meta["error"] or "klines 连续失败，放弃币安扫描"
    meta["ok"] = n_ok > 0
    if not meta["ok"] and not meta["error"]:
        meta["error"] = "没有评上任何币安标的"
    return hits, meta


def scan_local_last_n(conn, query: np.ndarray, tf: str, n: int) -> list[MatchHit]:
    tf = normalize_tf(tf)
    n = int(n)
    query = np.asarray(query, dtype=np.float64).ravel()
    uni_ids = {a.id for a in load_universe().assets}
    pool = load_all_primary_closes(conn, tf)
    hits: list[MatchHit] = []
    for asset, (venue, ts, close) in pool.items():
        if asset not in uni_ids:
            continue
        if len(close) < n:
            continue
        series = np.asarray(close, dtype=np.float64)
        window = series[-n:]
        r = pearson_equal(window, query)
        if not np.isfinite(r):
            continue
        w_ts = list(ts[-n:])
        hits.append(
            hit_from_closes(
                asset=asset,
                tf=tf,
                venue=venue,
                native_symbol="",
                ts=w_ts,
                closes=window,
                r=float(r),
            )
        )
    return hits


def merge_resonance(
    live_hits: list[MatchHit],
    local_hits: list[MatchHit],
    *,
    topk: int = 10,
) -> list[MatchHit]:
    by_asset: dict[str, MatchHit] = {}
    # local first, live overwrites only if r is higher (same last-n should be close)
    for h in list(local_hits) + list(live_hits):
        prev = by_asset.get(h.asset)
        if prev is None or float(h.pearson_r) > float(prev.pearson_r):
            by_asset[h.asset] = h
        elif prev is not None and not prev.native_symbol and h.native_symbol:
            prev.native_symbol = h.native_symbol
    merged = list(by_asset.values())
    return filter_resonance(merged, topk=topk)


def live_resonance(
    conn,
    query: np.ndarray,
    tf: str,
    n: int,
    *,
    live: bool = True,
    topk: int = 10,
    local_hits: list[MatchHit] | None = None,
) -> tuple[list[MatchHit], dict[str, Any]]:
    """Merge Binance live last-n with local-pool last-n. Degrades on live failure."""
    local = list(local_hits) if local_hits is not None else scan_local_last_n(conn, query, tf, n)
    meta: dict[str, Any] = {
        "live": False,
        "n_symbols": 0,
        "n_scored": 0,
        "n_failed": 0,
        "error": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    live_hits: list[MatchHit] = []
    if live:
        live_hits, live_meta = scan_binance_last_n(query, tf, n)
        meta.update(
            {
                "live": bool(live_meta.get("ok")),
                "n_symbols": live_meta.get("n_symbols", 0),
                "n_scored": live_meta.get("n_scored", 0),
                "n_failed": live_meta.get("n_failed", 0),
                "error": live_meta.get("error"),
            }
        )
    hits = merge_resonance(live_hits, local, topk=topk)
    return hits, meta


def match_pattern(
    conn,
    tf: str,
    pattern_id: str,
    n: int | None = None,
    *,
    live: bool = True,
    topk: int = 10,
    w_close: float = 1.0,
    w_shape: float = 0.0,
    w_volume: float = 0.0,
) -> MatchBundle:
    """History via local MASS; 现价 via Binance last-n + local last-n.

    Pattern templates have no real OHLC/V: shape/volume weights are forced off.
    """
    from kline_match.patterns import pattern_path, prefer_gated_history, resolve_pattern

    spec = resolve_pattern(pattern_id)
    n_eff = spec.suggested_n if n is None or int(n) < 16 else int(n)
    y = pattern_path(spec.id, n_eff)
    n_eff = int(y.size)
    drawn = match_drawn(
        conn, tf, y, n=n_eff, topk=max(int(topk) * 3, 30),
        w_close=w_close, w_shape=w_shape, w_volume=w_volume,
    )
    history = prefer_gated_history(drawn.history, topk=topk)
    forward = summarize_forward([h.forward_ret for h in history], n_eff)
    resonance, live_meta = live_resonance(
        conn,
        y,
        tf,
        n_eff,
        live=live,
        topk=topk,
        local_hits=list(drawn.resonance),
    )
    query_meta = {
        "asset": spec.query_asset,
        "tf": normalize_tf(tf),
        "n": n_eff,
        "venue": "pattern",
        "start_ts": 0,
        "end_ts": 0,
        "start_utc": "",
        "end_utc": "",
        "native_symbol": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pattern": spec.id,
        "live_scan": live_meta,
        "weights": drawn.query.get("weights") or {"close": 1.0, "shape": 0.0, "volume": 0.0},
    }
    wnorm = query_meta["weights"]
    for h in resonance:
        if h.weights is None:
            h.weights = dict(wnorm)
        if h.r_close is None:
            h.r_close = float(h.pearson_r)
    return MatchBundle(
        query=query_meta,
        resonance=resonance,
        history=history,
        query_z=zscore(y).tolist(),
        forward=forward,
    )
