"""OKX 现货 K 线。日线/12H 使用 UTC 对齐的 ``1Dutc`` / ``12Hutc``。"""

from __future__ import annotations

import time
from typing import Any

from kline_match.exchanges.base import drop_bad
from kline_match.http import HttpError, get_json
from kline_match.models import Candle
from kline_match.timeframes import now_ms

BASE = "https://www.okx.com"
INTERVAL = {"1H": "1H", "4H": "4H", "12H": "12Hutc", "1D": "1Dutc"}
MAX_LIMIT = 300


class OkxClient:
    venue = "okx"
    native_12h = True

    def __init__(self) -> None:
        self._min_interval_s = 0.12
        self._last_call = 0.0

    def _pace(self) -> None:
        wait = self._min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, Any]) -> list[Any]:
        self._pace()
        payload = get_json(f"{BASE}{path}", params)
        if not isinstance(payload, dict):
            raise HttpError(f"OKX 响应异常: {payload!r}")
        if str(payload.get("code")) != "0":
            raise HttpError(f"OKX {path} code={payload.get('code')} msg={payload.get('msg')}")
        data = payload.get("data") or []
        if not isinstance(data, list):
            return []
        return data

    def fetch_spot_instruments(self) -> list[Any]:
        return self._get(
            "/api/v5/public/instruments",
            {"instType": "SPOT"},
        )

    def fetch_range(
        self,
        native_symbol: str,
        tf: str,
        start_ms: int | None,
        end_ms: int | None,
    ) -> list[Candle]:
        bar = INTERVAL[tf]
        now = now_ms()
        end = min(end_ms or now, now)
        start = 0 if start_ms is None else max(0, start_ms)
        out: dict[int, Candle] = {}

        # 最近一段（含未收盘）走 /candles
        try:
            recent = self._get(
                "/api/v5/market/candles",
                {"instId": native_symbol, "bar": bar, "limit": "100"},
            )
            for c in parse_okx_candles(recent, now):
                if start <= c.ts <= end:
                    out[c.ts] = c
        except HttpError:
            # 部分合约没有 /candles 近期，继续 history
            pass

        after: int | None = end
        empty_streak = 0
        while True:
            params: dict[str, Any] = {
                "instId": native_symbol,
                "bar": bar,
                "limit": str(MAX_LIMIT),
            }
            if after is not None:
                params["after"] = str(after)
            try:
                raw = self._get("/api/v5/market/history-candles", params)
            except HttpError as exc:
                if empty_streak == 0 and not out:
                    raise
                raise HttpError(str(exc)) from exc
            rows = parse_okx_candles(raw, now)
            if not rows:
                empty_streak += 1
                break
            empty_streak = 0
            for c in rows:
                if start <= c.ts <= end:
                    out[c.ts] = c
            oldest = min(c.ts for c in rows)
            if oldest <= start:
                break
            if after is not None and oldest >= after:
                break
            after = oldest
            if len(rows) < 10:
                break
        return drop_bad(sorted(out.values(), key=lambda c: c.ts))


def parse_okx_candles(raw: list[Any], now_ms_value: int) -> list[Candle]:
    """OKX 行: ts, o, h, l, c, vol, ... confirm。confirm=0 为未收盘。"""
    candles: list[Candle] = []
    for row in raw:
        ts = int(row[0])
        confirm = str(row[8]) if len(row) > 8 else "1"
        is_closed = confirm == "1" and ts < now_ms_value
        candles.append(
            Candle(
                ts=ts,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                is_closed=is_closed,
            )
        )
    return candles
