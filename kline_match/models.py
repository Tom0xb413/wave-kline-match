"""内存中的 K 线与匹配结果结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Candle:
    """单根 OHLCV。``ts`` 为开盘时间 UTC 毫秒。"""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True


@dataclass(slots=True)
class MatchHit:
    """一条历史相似窗口。"""

    rank: int
    asset: str
    tf: str
    start_ts: int
    end_ts: int
    start_utc: str
    end_utc: str
    bars: int
    pearson_r: float
    score: float
    venue: str
    zscore: list[float] = field(default_factory=list)
    kind: str = "history"
    forward_ret: list[float] = field(default_factory=list)
    native_symbol: str = ""
    r_close: float | None = None
    r_shape: float | None = None
    r_volume: float | None = None
    weights: dict[str, float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
