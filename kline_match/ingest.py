"""把宇宙资产从 Binance / OKX / Gate 拉进本地 SQLite。

失败的交易所/标的记入报告后继续，绝不编造 K 线。未收盘 K 线以 is_closed=0 写入，收盘后覆盖。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kline_match.config import AssetSpec, Universe, VenueSpec, data_dir, load_universe
from kline_match.db import (
    connect,
    series_bounds,
    touch_series,
    upsert_bars,
    upsert_series,
)
from kline_match.exchanges.binance import BinanceClient
from kline_match.exchanges.gate import GateClient
from kline_match.exchanges.okx import OkxClient
from kline_match.models import Candle
from kline_match.resample import resample_4h_to_12h
from kline_match.timeframes import TF_MS, now_ms, parse_depth

TF_ORDER = ("1H", "4H", "12H", "1D")


@dataclass
class IngestReport:
    mode: str
    started_at: str
    finished_at: str | None = None
    ok: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def add_ok(self, asset: str, venue: str, tf: str, bars: int, native_symbol: str) -> None:
        self.ok.append(
            {
                "asset": asset,
                "venue": venue,
                "tf": tf,
                "bars_upserted": bars,
                "native_symbol": native_symbol,
            }
        )

    def add_err(self, asset: str, venue: str, tf: str, error: str) -> None:
        self.errors.append({"asset": asset, "venue": venue, "tf": tf, "error": error})
        print(f"  ! {asset} {venue} {tf}: {error}", flush=True)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class ClientHub:
    def __init__(self) -> None:
        self.binance = BinanceClient()
        self.okx = OkxClient()
        self.gate = GateClient()

    def get(self, venue: str) -> Any:
        if venue == "binance":
            return self.binance
        if venue == "okx":
            return self.okx
        if venue == "gate":
            return self.gate
        raise KeyError(venue)


def _resolve_native(hub: ClientHub, venue: str, spec: VenueSpec, asset: str) -> tuple[str, str | None]:
    """返回 (native_symbol, gate_market_or_None)。"""
    if venue == "gate":
        symbol, market = hub.gate.resolve(spec.native_symbols, asset=asset)
        return symbol, market
    if not spec.native_symbols:
        raise RuntimeError(f"{asset} {venue} 未配置 native_symbol")
    return spec.native_symbols[0], None


def _needs_resample_12h(hub: ClientHub, venue: str, symbol: str, market: str | None) -> bool:
    if venue != "gate":
        return False
    return not hub.gate.native_12h_for(symbol, market)


def _load_ohlcv(conn, asset: str, venue: str, tf: str) -> list[Candle]:
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close, volume, is_closed
        FROM bars WHERE asset=? AND venue=? AND tf=?
        ORDER BY ts
        """,
        (asset, venue, tf),
    ).fetchall()
    return [
        Candle(
            ts=int(r["ts"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
            is_closed=bool(r["is_closed"]),
        )
        for r in rows
    ]


def _fetch(
    hub: ClientHub,
    venue: str,
    symbol: str,
    tf: str,
    start_ms: int | None,
    end_ms: int | None,
    market: str | None,
) -> list[Candle]:
    client = hub.get(venue)
    if venue == "gate":
        return client.fetch_range(symbol, tf, start_ms, end_ms, market=market)
    return client.fetch_range(symbol, tf, start_ms, end_ms)


def fill_series(
    conn,
    hub: ClientHub,
    *,
    asset: str,
    venue: str,
    tf: str,
    symbol: str,
    market: str | None,
    is_primary: int,
    target_start: int | None,
    mode: str,
) -> int:
    """回填缺口 + 向前增量。返回 upsert 根数。"""
    upsert_series(
        conn,
        asset=asset,
        venue=venue,
        tf=tf,
        native_symbol=symbol,
        is_primary=is_primary,
    )
    lo, hi, n = series_bounds(conn, asset, venue, tf)
    now = now_ms()
    total = 0

    def pull(a: int | None, b: int | None) -> int:
        candles = _fetch(hub, venue, symbol, tf, a, b, market)
        k = upsert_bars(conn, asset, venue, tf, candles)
        return k

    if mode == "incremental":
        start = hi if hi is not None else target_start
        total += pull(start, now)
    else:
        if n == 0:
            total += pull(target_start, now)
        else:
            assert lo is not None and hi is not None
            if target_start is not None and lo > target_start + TF_MS[tf]:
                total += pull(target_start, lo)
            total += pull(hi, now)
    touch_series(conn, asset, venue, tf)
    conn.commit()
    return total


def _fill_resampled_12h(
    conn,
    hub: ClientHub,
    *,
    asset: str,
    venue: str,
    symbol: str,
    market: str | None,
    is_primary: int,
    target_start: int | None,
    mode: str,
) -> int:
    """Gate 现货无 12h：先保证 4H 覆盖 12H 深度，再合成 12H。"""
    four_h_start = target_start
    try:
        fill_series(
            conn,
            hub,
            asset=asset,
            venue=venue,
            tf="4H",
            symbol=symbol,
            market=market,
            is_primary=is_primary,
            target_start=four_h_start,
            mode=mode,
        )
    except Exception as exc:
        _log(f"{asset} {venue} 12H 用已有 4H 重采样（4H 补洞失败: {exc}）")
    upsert_series(
        conn,
        asset=asset,
        venue=venue,
        tf="12H",
        native_symbol=symbol,
        is_primary=is_primary,
    )
    candles_4h = _load_ohlcv(conn, asset, venue, "4H")
    bars_12h = resample_4h_to_12h(candles_4h)
    if target_start is not None:
        bars_12h = [c for c in bars_12h if c.ts >= target_start]
    n = upsert_bars(conn, asset, venue, tf="12H", candles=bars_12h)
    touch_series(conn, asset, venue, "12H")
    conn.commit()
    return n


def _ingest_asset(
    conn,
    hub: ClientHub,
    universe: Universe,
    asset: AssetSpec,
    *,
    mode: str,
    tfs: list[str],
    report: IngestReport,
) -> None:
    now = now_ms()
    # 先解析所有 venue 的真实合约，再按 yaml 主序列优先写入。
    resolved: list[tuple[VenueSpec, str, str | None]] = []
    for v in asset.venues:
        try:
            symbol, market = _resolve_native(hub, v.venue, v, asset.id)
            resolved.append((v, symbol, market))
            _log(f"{asset.id} {v.venue} 解析为 {symbol}" + (f" ({market})" if market else ""))
        except Exception as exc:
            for tf in tfs:
                report.add_err(asset.id, v.venue, tf, f"解析失败: {exc}")

    successes: list[str] = []
    for v, symbol, market in resolved:
        venue_ok = False
        for tf in tfs:
            depth_spec = universe.depths.get(tf, "full")
            target_start = parse_depth(depth_spec, now)
            try:
                if tf == "12H" and _needs_resample_12h(hub, v.venue, symbol, market):
                    n = _fill_resampled_12h(
                        conn,
                        hub,
                        asset=asset.id,
                        venue=v.venue,
                        symbol=symbol,
                        market=market,
                        is_primary=0,
                        target_start=target_start,
                        mode=mode,
                    )
                else:
                    n = fill_series(
                        conn,
                        hub,
                        asset=asset.id,
                        venue=v.venue,
                        tf=tf,
                        symbol=symbol,
                        market=market,
                        is_primary=0,
                        target_start=target_start,
                        mode=mode,
                    )
                _, _, count = series_bounds(conn, asset.id, v.venue, tf)
                report.add_ok(asset.id, v.venue, tf, n, symbol)
                _log(f"{asset.id} {v.venue} {tf} upsert {n} 根，库内已收盘 {count}")
                if count > 0:
                    venue_ok = True
            except Exception as exc:
                report.add_err(asset.id, v.venue, tf, str(exc))
        if venue_ok:
            successes.append(v.venue)

    primary_venue = None
    for v, _, _ in resolved:
        if v.primary and v.venue in successes:
            primary_venue = v.venue
            break
    if primary_venue is None and successes:
        primary_venue = successes[0]

    if primary_venue is None:
        _log(f"{asset.id} 没有任何 venue 入库成功")
        return

    for v, symbol, _ in resolved:
        flag = 1 if v.venue == primary_venue else 0
        for tf in tfs:
            row = conn.execute(
                "SELECT 1 FROM series WHERE asset=? AND venue=? AND tf=?",
                (asset.id, v.venue, tf),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE series SET is_primary=?, native_symbol=? WHERE asset=? AND venue=? AND tf=?",
                    (flag, symbol, asset.id, v.venue, tf),
                )
    conn.commit()
    _log(f"{asset.id} 主序列 venue={primary_venue}")


def ingest_lock_path() -> Path:
    return data_dir() / "ingest.lock"


def ingest_is_locked() -> bool:
    path = ingest_lock_path()
    if not path.exists():
        return False
    import fcntl

    try:
        with path.open("r") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


class _FileLock:
    def __init__(self) -> None:
        self._fh = None

    def __enter__(self) -> "_FileLock":
        import fcntl

        path = ingest_lock_path()
        self._fh = path.open("w")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        import fcntl

        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def run_ingest(
    conn,
    universe: Universe | None = None,
    *,
    mode: str = "full",
    assets: list[str] | None = None,
    tfs: list[str] | None = None,
) -> IngestReport:
    universe = universe or load_universe()
    wanted = {a.upper() for a in assets} if assets else None
    tfs = tfs or list(universe.timeframes)
    selected = [a for a in universe.assets if wanted is None or a.id in wanted]
    report = IngestReport(
        mode=mode,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    hub = ClientHub()
    _log(f"开始 {mode} ingest，资产 {len(selected)} 个，周期 {tfs}")
    with _FileLock():
        for asset in selected:
            _log(f"== {asset.id} ({asset.klass}) ==")
            _ingest_asset(conn, hub, universe, asset, mode=mode, tfs=tfs, report=report)
    report.finished_at = datetime.now(timezone.utc).isoformat()
    out = data_dir() / ("last_ingest.json" if mode != "incremental" else "last_sync.json")
    out.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"完成：成功 {len(report.ok)} 项，失败 {len(report.errors)} 项 → {out}")
    return report


def ingest_cli(db_file: Path, *, mode: str, assets: list[str] | None, tfs: list[str] | None) -> int:
    conn = connect(db_file)
    try:
        report = run_ingest(conn, mode=mode, assets=assets, tfs=tfs)
    finally:
        conn.close()
    return 1 if (mode == "full" and not report.ok) else 0
