"""4H → 12H 重采样：三根 4H 对齐到 00:00 / 12:00 UTC。"""

from __future__ import annotations

from kline_match.models import Candle
from kline_match.timeframes import TF_MS


def resample_4h_to_12h(candles: list[Candle]) -> list[Candle]:
    """把已收盘 4H K 线聚合成 12H。

    桶起点必须是 UTC 00:00 或 12:00。每个桶需要恰好三根：
    ``t, t+4h, t+8h``。缺根或未收盘则跳过该桶，避免写出未完成的 12H。
    """
    bucket_ms = TF_MS["12H"]
    step_ms = TF_MS["4H"]
    by_ts: dict[int, Candle] = {}
    for c in candles:
        if c.is_closed:
            by_ts[c.ts] = c

    buckets = sorted({(ts // bucket_ms) * bucket_ms for ts in by_ts})
    out: list[Candle] = []
    for bucket in buckets:
        expected = [bucket, bucket + step_ms, bucket + 2 * step_ms]
        if any(ts not in by_ts for ts in expected):
            continue
        group = [by_ts[ts] for ts in expected]
        out.append(
            Candle(
                ts=bucket,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                is_closed=True,
            )
        )
    return out
