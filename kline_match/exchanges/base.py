"""交易所客户端共用辅助。"""

from __future__ import annotations

from kline_match.models import Candle
from kline_match.timeframes import TF_MS, now_ms


def closed_by_end(open_ts: int, tf: str, current_ms: int | None = None) -> bool:
    """开盘 + 周期结束后视为已收盘（用于没有 confirm 字段的接口）。"""
    now = now_ms() if current_ms is None else current_ms
    return open_ts + TF_MS[tf] <= now


def drop_bad(candles: list[Candle]) -> list[Candle]:
    """丢掉非正价格或高低错乱的脏数据，不造假。"""
    out: list[Candle] = []
    for c in candles:
        if c.ts <= 0:
            continue
        if min(c.open, c.high, c.low, c.close) <= 0:
            continue
        if c.high < c.low:
            continue
        out.append(c)
    return out
