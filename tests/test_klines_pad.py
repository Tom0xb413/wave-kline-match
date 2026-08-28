"""pad_after 只延长返回窗口，不编造 OHLC。"""

from kline_match.server import select_klines_window


def _bars(n: int = 10, start: int = 0, step: int = 1000):
    return [{"ts": start + i * step, "close": float(i)} for i in range(n)]


def test_pad_after_zero_matches_inclusive_window():
    bars = _bars()
    got = select_klines_window(bars, 2000, 5000, 0)
    assert [b["ts"] for b in got] == [2000, 3000, 4000, 5000]


def test_pad_after_includes_following_bars():
    bars = _bars()
    got = select_klines_window(bars, 2000, 5000, 3)
    assert [b["ts"] for b in got] == [2000, 3000, 4000, 5000, 6000, 7000, 8000]


def test_pad_after_stops_at_history_end():
    bars = _bars()
    got = select_klines_window(bars, 7000, 8000, 10)
    assert [b["ts"] for b in got] == [7000, 8000, 9000]


def test_no_bars_at_or_before_end():
    bars = _bars()
    assert select_klines_window(bars, None, -1, 2) == []


def test_pad_ignored_without_end_ts():
    bars = _bars()
    got = select_klines_window(bars, 8000, None, 5)
    assert [b["ts"] for b in got] == [8000, 9000]


def test_empty_series():
    assert select_klines_window([], 0, 1, 3) == []
