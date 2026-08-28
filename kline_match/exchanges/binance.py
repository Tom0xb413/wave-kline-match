"""Binance 现货 K 线。优先 api.binance.com，遇 451/403/封禁则切到 data-api.binance.vision。"""

from __future__ import annotations

import time
from typing import Any

from kline_match.exchanges.base import drop_bad
from kline_match.http import HttpError, get_json
from kline_match.models import Candle
from kline_match.timeframes import TF_MS, now_ms

INTERVAL = {"1H": "1h", "4H": "4h", "12H": "12h", "1D": "1d"}
HOSTS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)
MAX_LIMIT = 1000


class BinanceClient:
    venue = "binance"
    native_12h = True

    def __init__(self) -> None:
        self._host = HOSTS[0]
        self._min_interval_s = 0.12
        self._last_call = 0.0

    def _pace(self) -> None:
        wait = self._min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(
        self,
        path: str,
        params: dict[str, Any],
        *,
        timeout: float = 40.0,
        retries: int = 6,
    ) -> Any:
        errors: list[Exception] = []
        # 当前 host 放前，失败再轮询。451 视为该 host 不可用。
        hosts = [self._host] + [h for h in HOSTS if h != self._host]
        for host in hosts:
            self._pace()
            url = f"{host}{path}"
            try:
                data = get_json(url, params, timeout=timeout, retries=retries)
                self._host = host
                return data
            except HttpError as exc:
                errors.append(exc)
                if exc.status in {403, 418, 451, 404}:
                    continue
                if exc.status and exc.status >= 400 and exc.status not in {429, 500, 502, 503, 504}:
                    raise
                continue
        raise errors[-1] if errors else HttpError("Binance 全部 host 失败")

    def fetch_exchange_info(self) -> dict[str, Any]:
        data = self._get("/api/v3/exchangeInfo", {}, timeout=30.0, retries=3)
        if not isinstance(data, dict):
            raise HttpError("Binance exchangeInfo 非对象")
        return data

    def fetch_ticker_24hr(self) -> list[Any]:
        data = self._get("/api/v3/ticker/24hr", {}, timeout=20.0, retries=3)
        if not isinstance(data, list):
            raise HttpError("Binance ticker/24hr 非数组")
        return data

    def fetch_range(
        self,
        native_symbol: str,
        tf: str,
        start_ms: int | None,
        end_ms: int | None,
    ) -> list[Candle]:
        interval = INTERVAL[tf]
        step = TF_MS[tf]
        now = now_ms()
        end = min(end_ms or now, now)
        start = 0 if start_ms is None else max(0, start_ms)

        out: dict[int, Candle] = {}
        cursor_end = end
        empty_streak = 0
        while cursor_end > start:
            chunk_start = max(start, cursor_end - MAX_LIMIT * step)
            raw = self._get(
                "/api/v3/klines",
                {
                    "symbol": native_symbol,
                    "interval": interval,
                    "startTime": chunk_start,
                    "endTime": cursor_end,
                    "limit": MAX_LIMIT,
                },
            )
            rows = parse_binance_klines(raw or [], now)
            if not rows:
                empty_streak += 1
                if empty_streak >= 2 or chunk_start <= start:
                    break
                cursor_end = chunk_start - 1
                continue
            empty_streak = 0
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


def parse_binance_klines(raw: list[Any], now_ms_value: int) -> list[Candle]:
    """解析 Binance klines 数组；``closeTime >= now`` 视为未收盘。"""
    candles: list[Candle] = []
    for row in raw:
        open_ts = int(row[0])
        close_time = int(row[6])
        candles.append(
            Candle(
                ts=open_ts,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                is_closed=close_time < now_ms_value,
            )
        )
    return candles
