"""Gate 现货 + USDT 永续 K 线。

现货没有 12h，需要时由上层用 4H 重采样。黄金 XAU_USDT、指数 NAS100 等在永续。
交易对名一律对照 list 接口解析，不盲猜。
"""

from __future__ import annotations

import time
from typing import Any

from kline_match.exchanges.base import closed_by_end, drop_bad
from kline_match.http import HttpError, get_json
from kline_match.models import Candle
from kline_match.timeframes import TF_MS, now_ms

BASE = "https://api.gateio.ws"
SPOT_INTERVAL = {"1H": "1h", "4H": "4h", "1D": "1d"}
FUT_INTERVAL = {"1H": "1h", "4H": "4h", "12H": "12h", "1D": "1d"}
SPOT_MAX = 900
FUT_MAX = 900
# Gate 现货/永续 K 线最多回溯约 10000 根，再早会 400。
MAX_LOOKBACK_BARS = 10000


class GateClient:
    venue = "gate"

    def __init__(self) -> None:
        self._min_interval_s = 0.12
        self._last_call = 0.0
        self._spot_pairs: set[str] | None = None
        self._fut_contracts: set[str] | None = None
        self._resolved: dict[str, tuple[str, str]] = {}

    def _pace(self) -> None:
        wait = self._min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._pace()
        return get_json(f"{BASE}{path}", params)

    def spot_pairs(self) -> set[str]:
        if self._spot_pairs is None:
            data = self._get("/api/v4/spot/currency_pairs")
            self._spot_pairs = {str(p["id"]) for p in data if isinstance(p, dict) and "id" in p}
        return self._spot_pairs

    def fut_contracts(self) -> set[str]:
        if self._fut_contracts is None:
            data = self._get("/api/v4/futures/usdt/contracts")
            names: set[str] = set()
            for p in data or []:
                if isinstance(p, dict) and p.get("name"):
                    names.add(str(p["name"]))
            self._fut_contracts = names
        return self._fut_contracts

    def resolve(self, candidates: list[str], asset: str | None = None) -> tuple[str, str]:
        """返回 (native_symbol, market)，market 为 spot 或 futures。现货优先。"""
        key = ",".join(candidates) + "|" + (asset or "")
        if key in self._resolved:
            return self._resolved[key]
        spot = self.spot_pairs()
        fut = self.fut_contracts()
        ordered: list[str] = list(candidates)
        if asset:
            for extra in (f"{asset}_USDT", f"{asset}X_USDT", f"{asset}T_USDT"):
                if extra not in ordered:
                    ordered.append(extra)
        for c in ordered:
            if c in spot:
                self._resolved[key] = (c, "spot")
                return c, "spot"
        for c in ordered:
            if c in fut:
                self._resolved[key] = (c, "futures")
                return c, "futures"
        raise HttpError(
            f"Gate 列表中找不到候选 {candidates} (asset={asset})"
        )

    def native_12h_for(self, native_symbol: str, market: str | None = None) -> bool:
        mkt = market or self._market_of(native_symbol)
        return mkt == "futures"

    def _market_of(self, native_symbol: str) -> str:
        if native_symbol in self.spot_pairs():
            return "spot"
        if native_symbol in self.fut_contracts():
            return "futures"
        raise HttpError(f"Gate 无法判断 {native_symbol} 属于现货还是永续")

    def fetch_range(
        self,
        native_symbol: str,
        tf: str,
        start_ms: int | None,
        end_ms: int | None,
        market: str | None = None,
    ) -> list[Candle]:
        mkt = market or self._market_of(native_symbol)
        if mkt == "spot":
            if tf == "12H":
                raise HttpError("Gate 现货无 12h 周期，应由上层用 4H 重采样")
            return self._fetch_spot(native_symbol, tf, start_ms, end_ms)
        return self._fetch_futures(native_symbol, tf, start_ms, end_ms)

    def _clamp_start(self, start_ms: int | None, end: int, step: int) -> int:
        floor = max(0, end - MAX_LOOKBACK_BARS * step)
        if start_ms is None:
            return floor
        return max(start_ms, floor)

    def _fetch_spot(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None
    ) -> list[Candle]:
        interval = SPOT_INTERVAL[tf]
        step = TF_MS[tf]
        now = now_ms()
        end = min(end_ms or now, now)
        start = self._clamp_start(start_ms, end, step)
        out: dict[int, Candle] = {}
        cursor_end = end
        empty = 0
        while cursor_end > start:
            chunk_start = max(start, cursor_end - SPOT_MAX * step)
            # spot: from/to 与 limit 互斥，且 from/to 为秒
            try:
                raw = self._get(
                    "/api/v4/spot/candlesticks",
                    {
                        "currency_pair": symbol,
                        "interval": interval,
                        "from": chunk_start // 1000,
                        "to": cursor_end // 1000,
                    },
                )
            except HttpError as exc:
                if exc.status == 400 and exc.body and "too long ago" in exc.body:
                    break
                raise
            rows = parse_gate_spot(raw or [], tf, now)
            if not rows:
                empty += 1
                if empty >= 2 or chunk_start <= start:
                    break
                cursor_end = chunk_start - 1
                continue
            empty = 0
            for c in rows:
                if start <= c.ts <= end:
                    out[c.ts] = c
            oldest = min(c.ts for c in rows)
            if oldest <= start:
                break
            next_end = oldest - 1
            if next_end >= cursor_end:
                break
            cursor_end = next_end
        return drop_bad(sorted(out.values(), key=lambda c: c.ts))

    def _fetch_futures(
        self, symbol: str, tf: str, start_ms: int | None, end_ms: int | None
    ) -> list[Candle]:
        interval = FUT_INTERVAL[tf]
        step = TF_MS[tf]
        now = now_ms()
        end = min(end_ms or now, now)
        start = self._clamp_start(start_ms, end, step)
        out: dict[int, Candle] = {}
        cursor_end = end
        empty = 0
        while cursor_end > start:
            chunk_start = max(start, cursor_end - FUT_MAX * step)
            try:
                raw = self._get(
                    "/api/v4/futures/usdt/candlesticks",
                    {
                        "contract": symbol,
                        "interval": interval,
                        "from": chunk_start // 1000,
                        "to": cursor_end // 1000,
                    },
                )
            except HttpError as exc:
                if exc.status == 400 and exc.body and "too long ago" in exc.body:
                    break
                raise
            rows = parse_gate_futures(raw or [], tf, now)
            if not rows:
                empty += 1
                if empty >= 2 or chunk_start <= start:
                    break
                cursor_end = chunk_start - 1
                continue
            empty = 0
            for c in rows:
                if start <= c.ts <= end:
                    out[c.ts] = c
            oldest = min(c.ts for c in rows)
            if oldest <= start:
                break
            next_end = oldest - 1
            if next_end >= cursor_end:
                break
            cursor_end = next_end
        return drop_bad(sorted(out.values(), key=lambda c: c.ts))


def parse_gate_spot(raw: list[Any], tf: str, now_ms_value: int) -> list[Candle]:
    """现货: ``[t, vol, close, high, low, open, amount?, is_closed?]``，t 为秒。"""
    candles: list[Candle] = []
    for row in raw:
        ts_s = int(float(row[0]))
        ts = ts_s * 1000
        closed: bool
        if len(row) >= 8:
            flag = row[7]
            if isinstance(flag, bool):
                closed = flag
            else:
                closed = str(flag).lower() in {"true", "1"}
        else:
            closed = closed_by_end(ts, tf, now_ms_value)
        candles.append(
            Candle(
                ts=ts,
                volume=float(row[1]),
                close=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                open=float(row[5]),
                is_closed=closed,
            )
        )
    return candles


def parse_gate_futures(raw: list[Any], tf: str, now_ms_value: int) -> list[Candle]:
    """永续: ``{t, o, h, l, c, v}``，t 为秒。无 confirm，按周期结束判断收盘。"""
    candles: list[Candle] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        ts = int(row["t"]) * 1000
        candles.append(
            Candle(
                ts=ts,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row.get("v") or 0.0),
                is_closed=closed_by_end(ts, tf, now_ms_value),
            )
        )
    return candles
