"""同一周期下的收盘价路径形态匹配。

表示：收盘价序列的 z-score（对价格水平和振幅不变）。
相似度：两段 z-score 向量的 Pearson r。排名按 r 降序。
分数：score = max(0, r) * 100。
检索：MASS（FFT 滑动点积 + 滑动均值/方差）。

两份名单（``match_query`` 按板块门控，互不跨类）：
- 当前共振：同板块允许名单上、与查询窗口时间戳完全相同的那一段。
- 历史类比：允许名单上与查询 [start,end] **无重叠** 的窗口；同资产 NMS（重叠>50% 留更高 r）。
山寨只对标 BTC/ETH；黄金/美股/未来股指各成一池。手绘与杯柄全市场扫描，不走板块门控。
查询窗口自身永远排除。不抬高 BTC/ETH 分数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from kline_match.config import data_dir, load_universe
from kline_match.db import adhoc_asset_ids, load_all_primary_closes, load_all_primary_ohlcv
from kline_match.models import MatchHit
from kline_match.plot import write_overlay_png
from kline_match.sectors import history_pool, register_adhoc, resonance_pool, sector_of
from kline_match.timeframes import TF_MS, ms_to_utc, normalize_tf

_EPS = 1e-12


def zscore(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    mu = float(x.mean())
    sd = float(x.std(ddof=0))
    if sd < _EPS:
        return np.zeros_like(x)
    return (x - mu) / sd


def _sliding_mean_std(x: np.ndarray, m: int) -> tuple[np.ndarray, np.ndarray]:
    prefix = np.cumsum(np.insert(x, 0, 0.0))
    prefix2 = np.cumsum(np.insert(x * x, 0, 0.0))
    s = prefix[m:] - prefix[:-m]
    s2 = prefix2[m:] - prefix2[:-m]
    mean = s / m
    var = np.maximum(s2 / m - mean * mean, 0.0)
    std = np.sqrt(var)
    return mean, std


def _sliding_dot(series: np.ndarray, query: np.ndarray) -> np.ndarray:
    """FFT 卷积实现滑动点积 ``sum(series[i:i+m] * query)``。"""
    n = series.size
    m = query.size
    nfft = 1 << (n + m - 1).bit_length()
    xf = np.fft.rfft(series, nfft)
    yf = np.fft.rfft(query[::-1], nfft)
    corr = np.fft.irfft(xf * yf, nfft)
    return np.real(corr[m - 1 : n])


def mass_pearson(series: np.ndarray, query: np.ndarray) -> np.ndarray:
    """所有长度为 n 的窗口相对 query 的 Pearson r。

    对已 z-normalize 的向量：``r = 1 - d^2 / (2n)``，其中 d 是 z-normalized Euclidean。
    """
    series = np.asarray(series, dtype=np.float64)
    query = np.asarray(query, dtype=np.float64)
    n = series.size
    m = query.size
    if m < 2 or n < m:
        return np.empty(0, dtype=np.float64)
    q_std = float(query.std(ddof=0))
    if q_std < _EPS:
        return np.full(n - m + 1, np.nan)
    meanx, stdx = _sliding_mean_std(series, m)
    dots = _sliding_dot(series, query)
    meanq = float(query.mean())
    denom = m * stdx * q_std
    with np.errstate(divide="ignore", invalid="ignore"):
        r = (dots - m * meanx * meanq) / denom
    r = np.where(stdx < _EPS, np.nan, r)
    r = np.clip(r, -1.0, 1.0)
    return r.astype(np.float64)


def overlap_too_much(i: int, j: int, n: int, threshold: float = 0.5) -> bool:
    """两窗口起点差导致的重叠比例是否 **大于** threshold。"""
    if i == j:
        return True
    inter = n - abs(i - j)
    if inter <= 0:
        return False
    return (inter / n) > threshold


def nms_same_asset(
    items: list[tuple[int, float]],
    n: int,
    *,
    pinned_idx: list[int] | None = None,
    overlap: float = 0.5,
) -> list[tuple[int, float]]:
    """同一资产内 NMS。``items`` 为 ``(start_idx, r)``。"""
    taken = list(pinned_idx or [])
    kept: list[tuple[int, float]] = []
    for idx, r in sorted(items, key=lambda t: (-t[1], t[0])):
        if any(overlap_too_much(idx, t, n, overlap) for t in taken):
            continue
        taken.append(idx)
        kept.append((idx, r))
    return kept


def score_from_r(r: float) -> float:
    return max(0.0, float(r)) * 100.0


def resolve_weights(
    w_close: float = 0.6,
    w_shape: float = 0.25,
    w_volume: float = 0.15,
    *,
    close_only: bool = False,
) -> tuple[float, float, float, dict[str, float]]:
    """Return raw weights plus a normalized dict that sums to 1.

    ``close_only`` zeros shape/volume (drawn / cup-handle). If that would
    leave every weight at 0, close is forced to 1 so we never 400.
    """
    wc = float(w_close)
    ws = float(w_shape)
    wv = float(w_volume)
    if close_only:
        ws = 0.0
        wv = 0.0
        if wc <= 0.0:
            wc = 1.0
    if wc < 0 or ws < 0 or wv < 0:
        raise ValueError("权重不能为负")
    total = wc + ws + wv
    if total <= 0.0:
        raise ValueError("权重不能全为 0")
    norm = {"close": wc / total, "shape": ws / total, "volume": wv / total}
    return wc, ws, wv, norm


def bar_shape_features(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-bar (upper wick, lower wick, body, signed_body) / range."""
    o = np.asarray(open_, dtype=np.float64).ravel()
    h = np.asarray(high, dtype=np.float64).ravel()
    lo = np.asarray(low, dtype=np.float64).ravel()
    c = np.asarray(close, dtype=np.float64).ravel()
    rng = np.maximum(h - lo, _EPS)
    upper = (h - np.maximum(o, c)) / rng
    lower = (np.minimum(o, c) - lo) / rng
    body = np.abs(c - o) / rng
    signed_body = (c - o) / rng
    return upper, lower, body, signed_body


def log_volume(volume: np.ndarray) -> np.ndarray:
    v = np.asarray(volume, dtype=np.float64).ravel()
    v = np.where(np.isfinite(v), np.maximum(v, 0.0), 0.0)
    return np.log1p(v)


def mass_shape(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    q_open: np.ndarray,
    q_high: np.ndarray,
    q_low: np.ndarray,
    q_close: np.ndarray,
) -> np.ndarray:
    """Mean Pearson r of the four candlestick-structure series via MASS.

    Near-constant query series are omitted from the mean. Per-window nans
    (constant target windows) are skipped by nanmean.
    """
    q_feats = bar_shape_features(q_open, q_high, q_low, q_close)
    s_feats = bar_shape_features(open_, high, low, close)
    m = int(np.asarray(q_close, dtype=np.float64).ravel().size)
    n = int(np.asarray(close, dtype=np.float64).ravel().size)
    nwin = n - m + 1
    if nwin < 1 or m < 2:
        return np.empty(0, dtype=np.float64)
    rows: list[np.ndarray] = []
    for q, s in zip(q_feats, s_feats):
        if float(np.std(q, ddof=0)) < _EPS:
            continue
        rows.append(mass_pearson(s, q))
    if not rows:
        return np.full(nwin, np.nan, dtype=np.float64)
    stacked = np.vstack(rows)
    with np.errstate(all="ignore"):
        return np.nanmean(stacked, axis=0)


def blend_channel_r(
    r_close: np.ndarray | None,
    r_shape: np.ndarray | None,
    r_vol: np.ndarray | None,
    w_close: float,
    w_shape: float,
    w_volume: float,
) -> np.ndarray:
    """Weighted mean of enabled channels; nan channel dropped per window."""
    nwin = None
    for vec in (r_close, r_shape, r_vol):
        if vec is not None:
            nwin = int(vec.size)
            break
    if nwin is None:
        return np.empty(0, dtype=np.float64)
    num = np.zeros(nwin, dtype=np.float64)
    den = np.zeros(nwin, dtype=np.float64)
    for vec, w in ((r_close, w_close), (r_shape, w_shape), (r_vol, w_volume)):
        if vec is None or w <= 0.0:
            continue
        valid = np.isfinite(vec)
        num = np.where(valid, num + w * vec, num)
        den = np.where(valid, den + w, den)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num / den
    out = np.where(den > 0.0, out, np.nan)
    return np.clip(out, -1.0, 1.0).astype(np.float64)


def _chan_at(vec: np.ndarray | None, i: int) -> float | None:
    if vec is None or i < 0 or i >= vec.size:
        return None
    val = float(vec[i])
    return val if np.isfinite(val) else None


def _unpack_asset(rec):
    """venue, ts, close, open, high, low, volume (last four None if close-only)."""
    if len(rec) == 3:
        venue, ts, close = rec
        return venue, ts, np.asarray(close, dtype=np.float64), None, None, None, None
    venue, ts, o, h, l, c, v = rec
    return (
        venue,
        ts,
        np.asarray(c, dtype=np.float64),
        np.asarray(o, dtype=np.float64),
        np.asarray(h, dtype=np.float64),
        np.asarray(l, dtype=np.float64),
        np.asarray(v, dtype=np.float64),
    )


def _query_channels_ok(
    query_close: np.ndarray,
    query_open: np.ndarray | None,
    query_high: np.ndarray | None,
    query_low: np.ndarray | None,
    query_volume: np.ndarray | None,
    w_close: float,
    w_shape: float,
    w_volume: float,
) -> bool:
    if w_close > 0 and float(np.std(query_close, ddof=0)) >= _EPS:
        return True
    if w_shape > 0 and query_open is not None and query_high is not None and query_low is not None:
        feats = bar_shape_features(query_open, query_high, query_low, query_close)
        if any(float(np.std(f, ddof=0)) >= _EPS for f in feats):
            return True
    if w_volume > 0 and query_volume is not None:
        if float(np.std(log_volume(query_volume), ddof=0)) >= _EPS:
            return True
    return False


def ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def resample_path(
    y: np.ndarray,
    n: int,
    *,
    x: np.ndarray | None = None,
) -> np.ndarray:
    """Resample a polyline to ``n`` y-values by arc-length.

    ``x`` defaults to ``0 .. m-1``. If the path is nearly vertical (x-span ~ 0),
    fall back to uniform sampling along the vertex index.
    """
    y = np.asarray(y, dtype=np.float64).ravel()
    m = int(y.size)
    if n < 2:
        raise ValueError("N 至少为 2")
    if m < 2:
        raise ValueError("path 至少 2 个点")
    if x is None:
        xs = np.arange(m, dtype=np.float64)
    else:
        xs = np.asarray(x, dtype=np.float64).ravel()
        if xs.size != m:
            raise ValueError("x 与 y 长度不一致")
    if m == n and x is None:
        return y.copy()
    x_span = float(np.abs(xs[-1] - xs[0]))
    y_span = float(np.ptp(y))
    if x_span < 1e-9 * max(y_span, 1.0):
        t = np.linspace(0.0, m - 1, n)
        return np.interp(t, np.arange(m, dtype=np.float64), y)
    segs = np.hypot(np.diff(xs), np.diff(y))
    total = float(segs.sum())
    if total < _EPS:
        return np.full(n, float(y[0]))
    cum = np.concatenate([[0.0], np.cumsum(segs)])
    targets = np.linspace(0.0, cum[-1], n)
    return np.interp(targets, cum, y)


def snap_query_window(
    ts: list[int],
    n: int,
    start_ts: int | None,
    end_ts: int | None,
) -> tuple[int, int, int]:
    """把可选起止时间对齐到已收盘 K 线，返回 (start_idx, end_idx_inclusive, n_eff)。"""
    if not ts:
        raise RuntimeError("序列为空")
    arr = np.asarray(ts, dtype=np.int64)
    if start_ts is None and end_ts is None:
        if len(ts) < n:
            raise RuntimeError(f"已收盘 K 线只有 {len(ts)} 根，不足 N={n}")
        return len(ts) - n, len(ts) - 1, n
    if start_ts is not None and end_ts is not None:
        i0 = int(np.searchsorted(arr, start_ts, side="left"))
        i1 = int(np.searchsorted(arr, end_ts, side="right")) - 1
        if i0 < 0 or i1 >= len(ts) or i1 - i0 + 1 < 2:
            raise RuntimeError("框选区间内没有足够的已收盘 K 线")
        return i0, i1, i1 - i0 + 1
    if start_ts is not None:
        i0 = int(np.searchsorted(arr, start_ts, side="left"))
        i1 = i0 + n - 1
        if i1 >= len(ts):
            raise RuntimeError("从 start_ts 起不足 N 根")
        return i0, i1, n
    i1 = int(np.searchsorted(arr, end_ts, side="right")) - 1
    i0 = i1 - n + 1
    if i0 < 0:
        raise RuntimeError("到 end_ts 止不足 N 根")
    return i0, i1, n


def _hit(
    *,
    rank: int,
    asset: str,
    tf: str,
    ts: list[int],
    series: np.ndarray,
    idx: int,
    n: int,
    r: float,
    venue: str,
    kind: str,
    r_close: float | None = None,
    r_shape: float | None = None,
    r_volume: float | None = None,
    weights: dict[str, float] | None = None,
) -> MatchHit:
    start_ts = int(ts[idx])
    end_ts = int(ts[idx + n - 1])
    window = series[idx : idx + n]
    return MatchHit(
        rank=rank,
        asset=asset,
        tf=tf,
        start_ts=start_ts,
        end_ts=end_ts,
        start_utc=ms_to_utc(start_ts),
        end_utc=ms_to_utc(end_ts + TF_MS[tf]),
        bars=n,
        pearson_r=float(r),
        score=round(score_from_r(r), 4),
        venue=venue,
        zscore=zscore(window).tolist(),
        kind=kind,
        r_close=r_close,
        r_shape=r_shape,
        r_volume=r_volume,
        weights=dict(weights) if weights else None,
    )


@dataclass
class MatchBundle:
    query: dict
    resonance: list[MatchHit] = field(default_factory=list)
    history: list[MatchHit] = field(default_factory=list)
    query_z: list[float] = field(default_factory=list)
    forward: dict | None = None


def forward_path(
    ts: list[int],
    close: np.ndarray,
    end_ts: int,
    horizon: int,
) -> list[float]:
    """Closed-bar forward returns after align ``end_ts``: close[k]/close[align]-1."""
    if horizon <= 0 or not ts:
        return []
    arr = np.asarray(ts, dtype=np.int64)
    i = int(np.searchsorted(arr, end_ts, side="left"))
    if i >= arr.size or int(arr[i]) != int(end_ts):
        return []
    series = np.asarray(close, dtype=np.float64)
    align = float(series[i])
    if not np.isfinite(align) or abs(align) < _EPS:
        return []
    last = min(i + int(horizon), series.size - 1)
    out: list[float] = []
    for k in range(i + 1, last + 1):
        ck = float(series[k])
        if not np.isfinite(ck):
            break
        out.append(ck / align - 1.0)
    return out


def summarize_forward(paths: list[list[float]], horizon: int) -> dict | None:
    """Percentile fan of history-hit forward returns. None if no history hits."""
    if not paths:
        return None
    horizon = int(horizon)
    n_with_full = sum(1 for p in paths if len(p) >= horizon)
    steps: list[dict] = []
    for i in range(1, horizon + 1):
        vals = [p[i - 1] for p in paths if len(p) >= i]
        if not vals:
            break
        arr = np.asarray(vals, dtype=np.float64)
        steps.append(
            {
                "i": i,
                "n": int(arr.size),
                "p25": float(np.percentile(arr, 25)),
                "p50": float(np.percentile(arr, 50)),
                "p75": float(np.percentile(arr, 75)),
                "pct_up": float(np.mean(arr > 0)),
            }
        )
    return {
        "horizon": horizon,
        "n_hits": len(paths),
        "n_with_full": n_with_full,
        "steps": steps,
    }


def _pool_ids(conn) -> set[str]:
    ids = {a.id.upper() for a in load_universe().assets}
    ids.update(adhoc_asset_ids(conn))
    try:
        from kline_match.db import adhoc_class_map

        for aid, klass in adhoc_class_map(conn).items():
            register_adhoc(aid, klass)
    except Exception:
        pass
    return ids


def _universe_pool(conn, tf: str) -> dict[str, tuple[str, list[int], list[float]]]:
    allow = _pool_ids(conn)
    return {k: v for k, v in load_all_primary_closes(conn, tf).items() if k in allow}


def _universe_ohlcv(conn, tf: str):
    allow = _pool_ids(conn)
    return {k: v for k, v in load_all_primary_ohlcv(conn, tf).items() if k in allow}


def _hits_from_raw(
    raw: list[tuple],
    pool: dict[str, tuple[str, list[int], np.ndarray]],
    tf: str,
    n: int,
    kind: str,
    *,
    limit: int | None = None,
    weights: dict[str, float] | None = None,
) -> list[MatchHit]:
    chosen = raw if limit is None else raw[:limit]
    hits: list[MatchHit] = []
    for rank, item in enumerate(chosen, start=1):
        a, idx, r, venue = item[0], item[1], item[2], item[3]
        rc = item[4] if len(item) > 4 else None
        rs = item[5] if len(item) > 5 else None
        rv = item[6] if len(item) > 6 else None
        hits.append(
            _hit(
                rank=rank,
                asset=a,
                tf=tf,
                ts=pool[a][1],
                series=pool[a][2],
                idx=idx,
                n=n,
                r=r,
                venue=venue,
                kind=kind,
                r_close=rc,
                r_shape=rs,
                r_volume=rv,
                weights=weights,
            )
        )
    return hits


def _attach_history_forward(
    history: list[MatchHit],
    pool: dict[str, tuple[str, list[int], np.ndarray]],
    n: int,
) -> dict | None:
    if not history:
        return None
    for h in history:
        rec = pool.get(h.asset)
        if rec is None:
            h.forward_ret = []
            continue
        _venue, ts, series = rec
        h.forward_ret = forward_path(ts, series, h.end_ts, n)
    return summarize_forward([h.forward_ret for h in history], n)


def _scan_pool(
    pool_raw: dict,
    query_close: np.ndarray,
    tf: str,
    n: int,
    *,
    topk: int = 10,
    query_asset: str | None = None,
    query_ts: list[int] | None = None,
    skip_overlap: bool = True,
    resonance_mode: str = "aligned",
    resonance_limit: int | None = None,
    history_ids: set[str] | None = None,
    resonance_ids: set[str] | None = None,
    w_close: float = 1.0,
    w_shape: float = 0.0,
    w_volume: float = 0.0,
    query_open: np.ndarray | None = None,
    query_high: np.ndarray | None = None,
    query_low: np.ndarray | None = None,
    query_volume: np.ndarray | None = None,
    weights: dict[str, float] | None = None,
) -> tuple[list[MatchHit], list[MatchHit], dict[str, tuple[str, list[int], np.ndarray]]]:
    """MASS over the scan pool. Ranking is combined Pearson r, NMS overlap>50%.

    ``history_ids`` / ``resonance_ids``: if given, MASS runs only on the union
    (plus ``query_asset``); history/resonance hits are filtered to each set.
    ``None`` means no gate (drawn / cup-handle full-market scan).

    Weight 0 skips that channel (no extra FFT). ``pool_raw`` values are either
    close-only ``(venue, ts, close)`` or OHLCV
    ``(venue, ts, open, high, low, close, volume)``.

    ``resonance_mode``:
    - ``aligned``: other assets, timestamps identical to ``query_ts`` (brush query).
    - ``last_n``: last N closed bars of every asset vs the query (drawn query).
    ``skip_overlap`` drops history windows that overlap the query time range.
    Drawn queries have no query window, so leave it False.
    """
    query_close = np.asarray(query_close, dtype=np.float64).ravel()
    q_open = np.asarray(query_open, dtype=np.float64).ravel() if query_open is not None else None
    q_high = np.asarray(query_high, dtype=np.float64).ravel() if query_high is not None else None
    q_low = np.asarray(query_low, dtype=np.float64).ravel() if query_low is not None else None
    q_vol = np.asarray(query_volume, dtype=np.float64).ravel() if query_volume is not None else None
    q_log_vol = log_volume(q_vol) if (w_volume > 0 and q_vol is not None) else None
    wnorm = weights if weights is not None else {
        "close": float(w_close),
        "shape": float(w_shape),
        "volume": float(w_volume),
    }

    q_start = q_end = None
    q_ts_set: list[int] = []
    if query_ts:
        q_ts_set = list(query_ts)
        q_start, q_end = int(query_ts[0]), int(query_ts[-1])

    pool: dict[str, tuple[str, list[int], np.ndarray]] = {}
    resonance_raw: list[tuple] = []
    history_items: dict[str, list[tuple[int, float]]] = {}
    chan_at: dict[str, dict[int, tuple[float | None, float | None, float | None]]] = {}

    scan_ids: set[str] | None = None
    if history_ids is not None or resonance_ids is not None:
        scan_ids = set(history_ids or ()) | set(resonance_ids or ())
        if query_asset:
            scan_ids.add(query_asset)

    def _pack(a: str, idx: int, ri: float, venue: str) -> tuple:
        rc, rs, rv = chan_at.get(a, {}).get(idx, (None, None, None))
        return (a, idx, ri, venue, rc, rs, rv)

    for a, rec in pool_raw.items():
        if scan_ids is not None and a not in scan_ids:
            continue
        venue, ts, series, o, h, lo, vol = _unpack_asset(rec)
        if series.size < n:
            continue
        r_close_vec = mass_pearson(series, query_close) if w_close > 0 else None
        r_shape_vec = None
        if w_shape > 0 and o is not None and q_open is not None and q_high is not None and q_low is not None:
            r_shape_vec = mass_shape(o, h, lo, series, q_open, q_high, q_low, query_close)
        r_vol_vec = None
        if w_volume > 0 and vol is not None and q_log_vol is not None:
            r_vol_vec = mass_pearson(log_volume(vol), q_log_vol)
        r_vec = blend_channel_r(r_close_vec, r_shape_vec, r_vol_vec, w_close, w_shape, w_volume)
        pool[a] = (venue, ts, series)
        lookup: dict[int, tuple[float | None, float | None, float | None]] = {}
        nwin = int(r_vec.size)
        for i in range(nwin):
            lookup[i] = (_chan_at(r_close_vec, i), _chan_at(r_shape_vec, i), _chan_at(r_vol_vec, i))
        chan_at[a] = lookup

        allow_res = resonance_ids is None or a in resonance_ids
        if allow_res and resonance_mode == "last_n":
            idx_last = len(ts) - n
            ri = r_vec[idx_last] if 0 <= idx_last < r_vec.size else np.nan
            if np.isfinite(ri):
                resonance_raw.append(_pack(a, int(idx_last), float(ri), venue))
        elif allow_res and query_asset is not None and a != query_asset and q_ts_set:
            index_of = {int(t): i for i, t in enumerate(ts)}
            idxs = [index_of.get(t) for t in q_ts_set]
            if (
                all(i is not None for i in idxs)
                and idxs[0] is not None
                and idxs == list(range(idxs[0], idxs[0] + n))
            ):
                ri = r_vec[idxs[0]] if idxs[0] < r_vec.size else np.nan
                if np.isfinite(ri):
                    resonance_raw.append(_pack(a, int(idxs[0]), float(ri), venue))

        if history_ids is not None and a not in history_ids:
            continue
        hist: list[tuple[int, float]] = []
        for i, ri in enumerate(r_vec):
            if not np.isfinite(ri):
                continue
            if skip_overlap and q_start is not None and q_end is not None:
                w_start = int(ts[i])
                w_end = int(ts[i + n - 1])
                if query_asset and a == query_asset and w_start == q_start and w_end == q_end:
                    continue
                if ranges_overlap(w_start, w_end, q_start, q_end):
                    continue
            hist.append((i, float(ri)))
        history_items[a] = nms_same_asset(hist, n)

    resonance_raw.sort(key=lambda t: (-t[2], t[0]))
    resonance = _hits_from_raw(
        resonance_raw, pool, tf, n, "resonance", limit=resonance_limit, weights=wnorm
    )

    flat: list[tuple] = []
    for a, items in history_items.items():
        venue = pool[a][0]
        for idx, r in items:
            flat.append(_pack(a, idx, r, venue))
    flat.sort(key=lambda t: (-t[2], t[0], t[1]))
    history = _hits_from_raw(flat, pool, tf, n, "history", limit=topk, weights=wnorm)
    return resonance, history, pool


def match_query(
    conn,
    asset: str,
    tf: str,
    n: int = 30,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
    topk: int = 10,
    w_close: float = 0.6,
    w_shape: float = 0.25,
    w_volume: float = 0.15,
) -> MatchBundle:
    """在 ``tf`` 的板块门控主序列上匹配查询窗口。"""
    asset = asset.upper()
    tf = normalize_tf(tf)
    if n < 2:
        raise ValueError("N 至少为 2")
    wc, ws, wv, wnorm = resolve_weights(w_close, w_shape, w_volume)

    need_ohlcv = ws > 0 or wv > 0
    pool_raw = _universe_ohlcv(conn, tf) if need_ohlcv else _universe_pool(conn, tf)
    if asset not in pool_raw:
        raise RuntimeError(f"没有 {asset} {tf} 的主序列，请先 ingest")
    query_venue, q_ts, q_close_full, q_open_full, q_high_full, q_low_full, q_vol_full = _unpack_asset(
        pool_raw[asset]
    )
    query_row_symbol = conn.execute(
        "SELECT native_symbol FROM series WHERE asset=? AND venue=? AND tf=?",
        (asset, query_venue, tf),
    ).fetchone()
    native_symbol = query_row_symbol["native_symbol"] if query_row_symbol else ""
    i0, i1, n_eff = snap_query_window(q_ts, n, start_ts, end_ts)
    query_close = np.asarray(q_close_full[i0 : i1 + 1], dtype=np.float64)
    query_open = np.asarray(q_open_full[i0 : i1 + 1], dtype=np.float64) if q_open_full is not None else None
    query_high = np.asarray(q_high_full[i0 : i1 + 1], dtype=np.float64) if q_high_full is not None else None
    query_low = np.asarray(q_low_full[i0 : i1 + 1], dtype=np.float64) if q_low_full is not None else None
    query_volume = np.asarray(q_vol_full[i0 : i1 + 1], dtype=np.float64) if q_vol_full is not None else None
    query_ts = q_ts[i0 : i1 + 1]
    n = n_eff
    if not _query_channels_ok(
        query_close, query_open, query_high, query_low, query_volume, wc, ws, wv
    ):
        if ws <= 0 and wv <= 0:
            raise RuntimeError("查询窗口收盘价近乎常数，z-score / Pearson 无定义")
        raise RuntimeError("查询窗口各通道近乎常数，Pearson 无定义")

    q_start, q_end = int(query_ts[0]), int(query_ts[-1])
    query_z = zscore(query_close)
    hist_ids = history_pool(asset)
    res_ids = resonance_pool(asset)
    scan_keys = hist_ids | res_ids | {asset}
    scan_raw = {k: v for k, v in pool_raw.items() if k in scan_keys}
    resonance, history, pool = _scan_pool(
        scan_raw,
        query_close,
        tf,
        n,
        topk=topk,
        query_asset=asset,
        query_ts=query_ts,
        skip_overlap=True,
        resonance_mode="aligned",
        resonance_limit=None,
        history_ids=hist_ids,
        resonance_ids=res_ids,
        w_close=wc,
        w_shape=ws,
        w_volume=wv,
        query_open=query_open,
        query_high=query_high,
        query_low=query_low,
        query_volume=query_volume,
        weights=wnorm,
    )
    query_meta = {
        "asset": asset,
        "tf": tf,
        "n": n,
        "venue": query_venue,
        "start_ts": q_start,
        "end_ts": q_end,
        "start_utc": ms_to_utc(q_start),
        "end_utc": ms_to_utc(q_end + TF_MS[tf]),
        "native_symbol": native_symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sector": sector_of(asset),
        "history_pool": sorted(hist_ids),
        "resonance_pool": sorted(res_ids),
        "weights": wnorm,
    }
    forward = _attach_history_forward(history, pool, n)
    return MatchBundle(
        query=query_meta,
        resonance=resonance,
        history=history,
        query_z=query_z.tolist(),
        forward=forward,
    )


def match_drawn(
    conn,
    tf: str,
    query_close,
    n: int | None = None,
    topk: int = 10,
    *,
    w_close: float = 1.0,
    w_shape: float = 0.0,
    w_volume: float = 0.0,
) -> MatchBundle:
    """Match a synthetic close path against every primary series on ``tf``.

    当前共振 = last N closed bars of each asset (no shared timestamps).
    历史类比 = sliding MASS + NMS; no query window, so overlapping-with-query
    is not dropped. Constant windows are still skipped (MASS → nan).
    Hand-drawn paths have no OHLC/V: shape and volume weights are forced to 0.
    """
    tf = normalize_tf(tf)
    y = np.asarray(query_close, dtype=np.float64).ravel()
    if y.size < 2:
        raise ValueError("path 至少 2 个点")
    if not np.isfinite(y).all():
        raise ValueError("path 含非有限值")
    if n is None:
        n = int(y.size)
    n = int(n)
    if n < 2:
        raise ValueError("N 至少为 2")
    if n > 500:
        raise ValueError("N 不超过 500")
    if y.size != n:
        y = resample_path(y, n)
    if float(y.std(ddof=0)) < _EPS:
        raise RuntimeError("手绘路径近乎水平，z-score / Pearson 无定义")

    wc, ws, wv, wnorm = resolve_weights(w_close, w_shape, w_volume, close_only=True)
    pool_raw = _universe_pool(conn, tf)
    if not pool_raw:
        raise RuntimeError(f"没有 {tf} 的主序列，请先 ingest")
    query_z = zscore(y)
    resonance, history, pool = _scan_pool(
        pool_raw,
        y,
        tf,
        n,
        topk=topk,
        query_asset=None,
        query_ts=None,
        skip_overlap=False,
        resonance_mode="last_n",
        resonance_limit=topk,
        w_close=wc,
        w_shape=ws,
        w_volume=wv,
        weights=wnorm,
    )
    query_meta = {
        "asset": "DRAW",
        "tf": tf,
        "n": n,
        "venue": "drawn",
        "start_ts": 0,
        "end_ts": 0,
        "start_utc": "",
        "end_utc": "",
        "native_symbol": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drawn": True,
        "weights": wnorm,
    }
    forward = _attach_history_forward(history, pool, n)
    return MatchBundle(
        query=query_meta,
        resonance=resonance,
        history=history,
        query_z=query_z.tolist(),
        forward=forward,
    )


def match_primary(
    conn,
    asset: str,
    tf: str,
    n: int,
    topk: int = 10,
) -> tuple[dict, list[MatchHit], dict[str, np.ndarray]]:
    """兼容旧 CLI/测试：返回历史 TOP 列表。"""
    bundle = match_query(conn, asset, tf, n=n, topk=topk)
    overlay: dict[str, np.ndarray] = {"query": np.asarray(bundle.query_z, dtype=np.float64)}
    for h in bundle.history[:3]:
        overlay[f"#{h.rank} {h.asset}"] = np.asarray(h.zscore, dtype=np.float64)
    return bundle.query, bundle.history, overlay


def format_table(hits: list[MatchHit]) -> str:
    headers = (
        "rank",
        "asset",
        "tf",
        "start_utc",
        "end_utc",
        "bars",
        "pearson_r",
        "score",
        "venue",
    )
    rows = [
        (
            str(h.rank),
            h.asset,
            h.tf,
            h.start_utc,
            h.end_utc,
            str(h.bars),
            f"{h.pearson_r:.6f}",
            f"{h.score:.2f}",
            h.venue,
        )
        for h in hits
    ]
    if not rows:
        cols = [[h] for h in headers]
    else:
        cols = list(zip(*([headers] + rows)))
    widths = [max(len(x) for x in col) for col in cols]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(s.ljust(w) for s, w in zip(row, widths))

    line = fmt(headers)
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join(fmt(r) for r in rows) if rows else "(无匹配)"
    return f"{line}\n{sep}\n{body}"


def run_match(
    conn,
    asset: str,
    tf: str,
    n: int | None = None,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
    write_outputs: bool = True,
) -> MatchBundle:
    uni = load_universe()
    n = int(n if n is not None else uni.default_n)
    bundle = match_query(
        conn, asset, tf, n=n, start_ts=start_ts, end_ts=end_ts, topk=uni.topk
    )
    if write_outputs:
        out_dir = data_dir()
        payload = {
            "query": bundle.query,
            "resonance": [h.as_dict() for h in bundle.resonance],
            "history": [h.as_dict() for h in bundle.history],
            "matches": [h.as_dict() for h in bundle.history],
            "query_z": bundle.query_z,
            "forward": bundle.forward,
        }
        json_path = out_dir / "last_match.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        overlay = {"query": np.asarray(bundle.query_z, dtype=np.float64)}
        for h in bundle.history[:3]:
            overlay[f"#{h.rank} {h.asset}"] = np.asarray(h.zscore, dtype=np.float64)
        png_path = out_dir / "last_match.png"
        write_overlay_png(
            overlay,
            png_path,
            title=f"{bundle.query['asset']} {bundle.query['tf']} N={bundle.query['n']}",
        )
        bundle.query["json_path"] = str(json_path)
        bundle.query["png_path"] = str(png_path)
    return bundle
