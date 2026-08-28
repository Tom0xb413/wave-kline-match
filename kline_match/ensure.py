"""On-demand rolling-window cache for ad-hoc symbols.

Cache validity is one fully closed bar of the requested timeframe.
First visitor pays the download into the shared SQLite; later visitors
read the same rows until the window goes stale.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from kline_match.catalog import classify_asset, lookup_instrument, parse_native
from kline_match.config import load_universe
from kline_match.db import (
    last_closed_ts,
    series_bounds,
    touch_adhoc_access,
    trim_bars_before,
    upsert_adhoc,
    upsert_series,
)
from kline_match.ingest import ClientHub, _fill_resampled_12h, _needs_resample_12h, fill_series
from kline_match.sectors import register_adhoc
from kline_match.timeframes import TF_MS, now_ms, normalize_tf

ROLLING_W: dict[str, int] = {
    "1H": 2000,
    "4H": 1500,
    "12H": 1200,
    "1D": 800,
}

_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


def _asset_lock(key: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def current_closed_ts(tf: str, now: int | None = None) -> int:
    """Open-ts of the latest fully closed bar (UTC-aligned)."""
    tf = normalize_tf(tf)
    now_v = now_ms() if now is None else int(now)
    step = TF_MS[tf]
    forming = (now_v // step) * step
    return forming - step


def cache_is_fresh(last_bar_ts: int | None, tf: str, now: int | None = None) -> bool:
    """True iff last closed bar is the current fully closed bar."""
    if last_bar_ts is None:
        return False
    return int(last_bar_ts) == current_closed_ts(tf, now)


def rolling_start_ms(tf: str, now: int | None = None) -> int:
    tf = normalize_tf(tf)
    w = ROLLING_W[tf]
    step = TF_MS[tf]
    closed = current_closed_ts(tf, now)
    return max(0, closed - (w - 1) * step)


def _yaml_ids() -> set[str]:
    return {a.id.upper() for a in load_universe().assets}


def _yaml_spec(asset_id: str):
    key = asset_id.upper()
    for spec in load_universe().assets:
        if spec.id.upper() == key:
            return spec
    return None


def _constructed(asset_id: str, venue: str | None, klass: str) -> tuple[str, str, str | None]:
    """Fallback native when catalogs missed. Never invent Gate NAS100-style pairs."""
    aid = asset_id.upper()
    if venue == "okx":
        return "okx", f"{aid}-USDT", None
    if venue == "gate":
        if klass == "tradfi":
            return "gate", f"{aid}X_USDT", "spot"
        return "gate", f"{aid}_USDT", "spot"
    return "binance", f"{aid}USDT", None


def resolve_target(
    conn,
    *,
    asset_id: str | None,
    native_symbol: str | None,
    venue: str | None,
) -> dict[str, Any]:
    aid = (asset_id or "").strip().upper()
    native = (native_symbol or "").strip().upper()
    venue = (venue or "").strip().lower() or None
    if not aid and native:
        aid, hint = parse_native(native)
        if venue is None:
            venue = hint
    if not aid:
        raise ValueError("需要 id 或 native_symbol")

    spec = _yaml_spec(aid)
    if spec is not None:
        primary = next((v for v in spec.venues if v.primary), spec.venues[0])
        if venue:
            picked = next((v for v in spec.venues if v.venue == venue), primary)
        else:
            picked = primary
        nat = native or (picked.native_symbols[0] if picked.native_symbols else f"{aid}USDT")
        return {
            "id": aid,
            "venue": picked.venue,
            "native_symbol": nat,
            "class": spec.klass,
            "in_universe": True,
        }

    hit = lookup_instrument(conn, asset_id=aid, native_symbol=native or None, venue=venue)
    if hit is not None:
        return {
            "id": hit["id"],
            "venue": hit["venue"],
            "native_symbol": hit["native_symbol"],
            "class": hit["class"],
            "in_universe": False,
        }

    klass = classify_asset(base=aid, native=native or aid, venue=venue or "binance")
    if venue == "gate" or klass == "tradfi":
        v, nat, _m = _constructed(aid, "gate", klass)
    elif venue:
        v, nat, _m = _constructed(aid, venue, klass)
    else:
        v, nat, _m = _constructed(aid, "binance", klass)
    if native:
        nat = native
        if venue:
            v = venue
    return {
        "id": aid,
        "venue": v,
        "native_symbol": nat,
        "class": klass,
        "in_universe": False,
    }


def _gate_market(hub: ClientHub, symbol: str, asset: str) -> tuple[str, str]:
    try:
        return hub.gate.resolve([symbol], asset=asset)
    except Exception:
        return symbol, "spot"


def _pull(
    conn,
    hub: ClientHub,
    *,
    asset: str,
    venue: str,
    tf: str,
    symbol: str,
    market: str | None,
    mode: str,
    target_start: int | None,
    is_primary: int,
) -> int:
    if tf == "12H" and _needs_resample_12h(hub, venue, symbol, market):
        return _fill_resampled_12h(
            conn,
            hub,
            asset=asset,
            venue=venue,
            symbol=symbol,
            market=market,
            is_primary=is_primary,
            target_start=target_start,
            mode=mode,
        )
    return fill_series(
        conn,
        hub,
        asset=asset,
        venue=venue,
        tf=tf,
        symbol=symbol,
        market=market,
        is_primary=is_primary,
        target_start=target_start,
        mode=mode,
    )


def ensure_symbol(
    conn,
    *,
    asset_id: str | None = None,
    native_symbol: str | None = None,
    venue: str | None = None,
    tf: str,
    hub: ClientHub | None = None,
) -> dict[str, Any]:
    """Make sure ``asset`` has a rolling window of closed bars for ``tf``.

    Fresh cache (last closed bar == current closed) is served as-is.
    Missing/stale series are filled from the exchange under a per-asset lock.
    """
    tf = normalize_tf(tf)
    t0 = time.monotonic()
    target = resolve_target(conn, asset_id=asset_id, native_symbol=native_symbol, venue=venue)
    asset = target["id"]
    venue_n = target["venue"]
    native = target["native_symbol"]
    klass = target["class"]
    in_uni = bool(target["in_universe"])
    w = ROLLING_W[tf]
    now = now_ms()

    lock = _asset_lock(f"{asset}:{tf}")
    with lock:
        upsert_series(
            conn,
            asset=asset,
            venue=venue_n,
            tf=tf,
            native_symbol=native,
            is_primary=1,
        )
        last = last_closed_ts(conn, asset, venue_n, tf)
        _, _, n_closed = series_bounds(conn, asset, venue_n, tf)
        fresh = cache_is_fresh(last, tf, now)
        fetched = 0
        if not fresh:
            own_hub = hub is None
            hub = hub or ClientHub()
            market: str | None = None
            if venue_n == "gate":
                native, market = _gate_market(hub, native, asset)
                upsert_series(
                    conn,
                    asset=asset,
                    venue=venue_n,
                    tf=tf,
                    native_symbol=native,
                    is_primary=1,
                )
            never = n_closed == 0
            start = rolling_start_ms(tf, now) if never else None
            mode = "full" if never else "incremental"
            try:
                fetched = _pull(
                    conn,
                    hub,
                    asset=asset,
                    venue=venue_n,
                    tf=tf,
                    symbol=native,
                    market=market,
                    mode=mode,
                    target_start=start,
                    is_primary=1,
                )
            finally:
                if own_hub:
                    pass
            last = last_closed_ts(conn, asset, venue_n, tf)
            _, _, n_closed = series_bounds(conn, asset, venue_n, tf)
            if not in_uni and n_closed > w:
                cutoff = current_closed_ts(tf) - (w - 1) * TF_MS[tf]
                trim_bars_before(conn, asset, venue_n, tf, cutoff)
                _, _, n_closed = series_bounds(conn, asset, venue_n, tf)
            conn.commit()

        access = now_ms()
        if not in_uni:
            upsert_adhoc(
                conn,
                asset=asset,
                venue=venue_n,
                tf=tf,
                native_symbol=native,
                klass=klass,
                last_sync_at=access,
                last_access_at=access,
                window_bars=w,
            )
            register_adhoc(asset, klass)
        else:
            # yaml primary: still touch series access via last_sync already set
            pass
        conn.commit()

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return {
        "id": asset,
        "asset": asset,
        "venue": venue_n,
        "tf": tf,
        "native_symbol": native,
        "class": klass,
        "ready": n_closed > 0,
        "bars": int(n_closed or 0),
        "fresh": cache_is_fresh(last, tf),
        "cached": fetched == 0,
        "fetched": int(fetched or 0),
        "window_bars": w,
        "adhoc": not in_uni,
        "elapsed_ms": elapsed_ms,
    }
