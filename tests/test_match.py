"""合成数据上的匹配规则测试。"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from kline_match.db import connect, touch_series, upsert_bars, upsert_series
from kline_match.exchanges.binance import parse_binance_klines
from kline_match.exchanges.okx import parse_okx_candles
from kline_match.match import (
    forward_path,
    mass_pearson,
    match_primary,
    match_query,
    nms_same_asset,
    overlap_too_much,
    ranges_overlap,
    summarize_forward,
    zscore,
)
from kline_match.models import Candle
from kline_match.resample import resample_4h_to_12h
from kline_match.timeframes import TF_MS


def _seed(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    tf: str,
    closes: list[float] | np.ndarray,
    *,
    start_ts: int = 1_600_000_000_000,
    primary: int = 1,
) -> None:
    step = TF_MS[tf]
    candles = []
    for i, close in enumerate(closes):
        c = float(close)
        candles.append(
            Candle(
                ts=start_ts + i * step,
                open=c,
                high=c + 0.1,
                low=c - 0.1,
                close=c,
                volume=1.0,
                is_closed=True,
            )
        )
    upsert_series(
        conn,
        asset=asset,
        venue=venue,
        tf=tf,
        native_symbol=f"{asset}-SYN",
        is_primary=primary,
    )
    upsert_bars(conn, asset, venue, tf, candles)
    touch_series(conn, asset, venue, tf)
    conn.commit()


def test_mass_matches_corrcoef() -> None:
    rng = np.random.default_rng(0)
    series = np.cumsum(rng.normal(size=200))
    query = series[40:70]
    r = mass_pearson(series, query)
    brute = []
    for i in range(series.size - 30 + 1):
        brute.append(np.corrcoef(series[i : i + 30], query)[0, 1])
    brute = np.asarray(brute)
    assert r.shape == brute.shape
    np.testing.assert_allclose(r, brute, atol=1e-8)
    assert r[40] == pytest.approx(1.0, abs=1e-8)


def test_identical_window_ranks_first(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(1)
    eth = 100 + np.cumsum(rng.normal(scale=0.8, size=180))
    motif = eth[60:90]
    btc_body = 200 + np.cumsum(rng.normal(scale=1.2, size=90))
    btc = np.concatenate([btc_body, motif])  # 查询 = motif
    _seed(conn, "ETH", "binance", "1H", eth)
    _seed(conn, "BTC", "binance", "1H", btc)
    bundle = match_query(conn, "BTC", "1H", n=30, topk=10)
    hits = bundle.history
    assert hits
    assert hits[0].asset == "ETH"
    assert hits[0].pearson_r == pytest.approx(1.0, abs=1e-6)
    assert hits[0].score == pytest.approx(100.0, abs=1e-4)
    assert hits[0].tf == "1H"
    q_start, q_end = bundle.query["start_ts"], bundle.query["end_ts"]
    for h in hits:
        if h.asset == "BTC":
            assert not (h.start_ts == q_start and h.end_ts == q_end)
        assert not ranges_overlap(h.start_ts, h.end_ts, q_start, q_end)


def test_query_window_excluded(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(2)
    closes = 50 + np.cumsum(rng.normal(size=80))
    _seed(conn, "BTC", "binance", "1H", closes)
    meta, hits, _ = match_primary(conn, "BTC", "1H", n=30, topk=10)
    for h in hits:
        assert not (h.start_ts == meta["start_ts"] and h.end_ts == meta["end_ts"])
        assert not ranges_overlap(h.start_ts, h.end_ts, meta["start_ts"], meta["end_ts"])


def test_mixed_timeframe_never_appears(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(3)
    hourly = 100 + np.cumsum(rng.normal(size=120))
    motif = hourly[-30:]
    # 日线里放一段与查询完全相同的收盘路径，若跨周期混搜会排到第一
    daily = np.concatenate([rng.normal(loc=3000, size=40), motif, rng.normal(loc=3000, size=10)])
    _seed(conn, "BTC", "binance", "1H", hourly)
    _seed(conn, "SOL", "binance", "1D", daily)
    _, hits, _ = match_primary(conn, "BTC", "1H", n=30, topk=10)
    assert all(h.tf == "1H" for h in hits)
    assert all(h.asset != "SOL" for h in hits)


def test_nms_drops_overlaps() -> None:
    n = 30
    items = [
        (0, 0.99),
        (5, 0.98),  # 与 0 重叠 25/30 > 50%
        (20, 0.50),  # 与 0 重叠 10/30 < 50%
        (21, 0.49),  # 与 20 重叠过高
    ]
    kept = nms_same_asset(items, n)
    starts = [i for i, _ in kept]
    assert starts == [0, 20]
    assert overlap_too_much(0, 5, n) is True
    assert overlap_too_much(0, 20, n) is False
    assert overlap_too_much(0, 15, n) is False  # 刚好 50%，规则是 > 50%


def test_resonance_same_timestamps(tmp_path) -> None:
    """其它资产同一时间窗进入当前共振，且不得进入历史类比。"""
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(4)
    btc = 100 + np.cumsum(rng.normal(size=80))
    eth_body = 50 + np.cumsum(rng.normal(size=50))
    eth = np.concatenate([eth_body, btc[-30:]])  # 末 30 根时间对齐且形态相同
    start = 1_600_000_000_000
    _seed(conn, "BTC", "binance", "1H", btc, start_ts=start)
    _seed(conn, "ETH", "binance", "1H", eth, start_ts=start)
    bundle = match_query(conn, "BTC", "1H", n=30, topk=10)
    assert bundle.resonance
    assert bundle.resonance[0].asset == "ETH"
    assert bundle.resonance[0].pearson_r == pytest.approx(1.0, abs=1e-6)
    assert bundle.resonance[0].start_ts == bundle.query["start_ts"]
    assert all(h.asset != "ETH" or h.start_ts != bundle.query["start_ts"] for h in bundle.history)
    assert all(h.asset != "BTC" for h in bundle.resonance)


def test_history_excludes_any_query_overlap(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(5)
    closes = 80 + np.cumsum(rng.normal(size=90))
    _seed(conn, "BTC", "binance", "1H", closes)
    bundle = match_query(conn, "BTC", "1H", n=30, topk=10)
    q0, q1 = bundle.query["start_ts"], bundle.query["end_ts"]
    for h in bundle.history:
        assert not ranges_overlap(h.start_ts, h.end_ts, q0, q1)


def test_resample_4h_to_12h_alignment() -> None:
    t0 = 1_600_000_000_000  # 对齐检查用固定起点
    # 对齐到 12H 桶
    bucket = (t0 // TF_MS["12H"]) * TF_MS["12H"]
    step = TF_MS["4H"]
    group = []
    prices = [(1, 2, 0.5, 1.5, 10), (1.5, 3, 1.4, 2.8, 7), (2.8, 2.9, 2.0, 2.1, 4)]
    for i, (o, h, l, c, v) in enumerate(prices):
        group.append(
            Candle(ts=bucket + i * step, open=o, high=h, low=l, close=c, volume=v, is_closed=True)
        )
    # 缺一根的桶必须丢弃
    incomplete = group[:2]
    extra_unaligned = Candle(
        ts=bucket + 1_000, open=1, high=1, low=1, close=1, volume=1, is_closed=True
    )
    out = resample_4h_to_12h(group + incomplete + [extra_unaligned])
    assert len(out) == 1
    bar = out[0]
    assert bar.ts == bucket
    assert bar.open == 1
    assert bar.high == 3
    assert bar.low == 0.5
    assert bar.close == 2.1
    assert bar.volume == 21
    assert bar.is_closed is True


def test_binance_open_candle_not_closed() -> None:
    now = 1_700_000_000_000
    open_ts = now - 1_800_000
    close_time = open_ts + TF_MS["1H"] - 1
    raw = [[open_ts, "1", "2", "0.5", "1.5", "10", close_time]]
    candles = parse_binance_klines(raw, now)
    assert candles[0].is_closed is False
    candles2 = parse_binance_klines(raw, close_time + 1)
    assert candles2[0].is_closed is True


def test_okx_confirm_flag() -> None:
    now = 1_700_000_000_000
    row_open = [now - 1000, "1", "2", "0.5", "1.5", "3", "3", "3", "0"]
    row_closed = [now - 3_600_000, "1", "2", "0.5", "1.5", "3", "3", "3", "1"]
    out = parse_okx_candles([row_open, row_closed], now)
    assert out[0].is_closed is False
    assert out[1].is_closed is True


def test_zscore_shift_scale_invariant() -> None:
    x = np.array([1.0, 2.0, 3.0, 2.5, 4.0])
    y = 100 + 3.5 * x
    np.testing.assert_allclose(zscore(x), zscore(y), atol=1e-10)


def test_forward_path_from_align() -> None:
    ts = [1000, 2000, 3000, 4000, 5000]
    close = np.array([100.0, 110.0, 105.0, 120.0, 90.0])
    got = forward_path(ts, close, end_ts=2000, horizon=3)
    np.testing.assert_allclose(got, [105 / 110 - 1, 120 / 110 - 1, 90 / 110 - 1])
    assert forward_path(ts, close, end_ts=5000, horizon=3) == []
    assert forward_path(ts, close, end_ts=9999, horizon=3) == []


def test_summarize_forward_percentiles() -> None:
    paths = [
        [0.10, 0.20, 0.30],
        [-0.10, 0.00, 0.10],
        [0.00, 0.10],
    ]
    out = summarize_forward(paths, horizon=3)
    assert out["horizon"] == 3
    assert out["n_hits"] == 3
    assert out["n_with_full"] == 2
    assert [s["n"] for s in out["steps"]] == [3, 3, 2]
    s1 = out["steps"][0]
    assert s1["i"] == 1
    assert s1["p50"] == pytest.approx(0.0)
    assert s1["pct_up"] == pytest.approx(1 / 3)
    assert summarize_forward([], 30) is None


def test_history_forward_attached(tmp_path) -> None:
    """History hit gets post-align returns; resonance is not in the fan."""
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(11)
    n = 10
    motif = 100 + np.cumsum(rng.normal(scale=0.4, size=n))
    align = float(motif[-1])
    future = align * np.array([1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08, 1.09, 1.10])
    eth = np.concatenate([rng.normal(loc=80, scale=1, size=20), motif, future])
    btc = np.concatenate([rng.normal(loc=200, scale=2, size=30), motif])
    start = 1_600_000_000_000
    _seed(conn, "ETH", "binance", "1H", eth, start_ts=start)
    _seed(conn, "BTC", "binance", "1H", btc, start_ts=start)
    bundle = match_query(conn, "BTC", "1H", n=n, topk=10)
    assert bundle.history
    hit = next(h for h in bundle.history if h.asset == "ETH")
    assert len(hit.forward_ret) == n
    np.testing.assert_allclose(hit.forward_ret[0], 0.01, atol=1e-9)
    np.testing.assert_allclose(hit.forward_ret[-1], 0.10, atol=1e-9)
    assert bundle.forward is not None
    assert bundle.forward["horizon"] == n
    assert bundle.forward["n_hits"] == len(bundle.history)
    assert bundle.forward["n_with_full"] >= 1
    last = bundle.forward["steps"][-1]
    assert last["i"] == n
    assert isinstance(last["p50"], float)
    for h in bundle.resonance:
        assert h.forward_ret == []



def test_resample_path_preserves_ends_and_length() -> None:
    y = np.array([1.0, 3.0, 2.0, 5.0, 0.0])
    from kline_match.match import resample_path

    out = resample_path(y, 20)
    assert out.shape == (20,)
    assert out[0] == pytest.approx(y[0])
    assert out[-1] == pytest.approx(y[-1])
    same = resample_path(y, 5)
    np.testing.assert_allclose(same, y)


def test_resample_nearly_vertical_uses_index() -> None:
    from kline_match.match import resample_path

    xs = np.array([0.5, 0.5, 0.5])
    ys = np.array([0.0, 0.5, 1.0])
    out = resample_path(ys, 5, x=xs)
    np.testing.assert_allclose(out, np.linspace(0.0, 1.0, 5), atol=1e-12)


def test_match_drawn_v_shape_finds_v_window(tmp_path) -> None:
    from kline_match.match import match_drawn

    conn = connect(tmp_path / "t.db")
    n = 20
    v = np.concatenate([np.linspace(10.0, 0.0, n // 2), np.linspace(0.0, 10.0, n - n // 2)])
    rng = np.random.default_rng(7)
    eth = np.concatenate(
        [
            50 + np.cumsum(rng.normal(scale=0.3, size=40)),
            v,
            50 + np.cumsum(rng.normal(scale=0.3, size=30)),
        ]
    )
    btc_body = 100 + np.cumsum(rng.normal(scale=0.5, size=70))
    btc = np.concatenate([btc_body, v])
    start = 1_600_000_000_000
    _seed(conn, "ETH", "binance", "1H", eth, start_ts=start)
    _seed(conn, "BTC", "binance", "1H", btc, start_ts=start)

    bundle = match_drawn(conn, "1H", v, n=n, topk=10)
    assert bundle.query["asset"] == "DRAW"
    assert bundle.query["venue"] == "drawn"
    assert bundle.query.get("drawn") is True
    assert bundle.query["start_ts"] == 0
    assert len(bundle.query_z) == n

    assert bundle.history
    hit = next(h for h in bundle.history if h.asset == "ETH")
    assert hit.pearson_r == pytest.approx(1.0, abs=1e-6)
    # embedded V starts after 40 bars
    step = TF_MS["1H"]
    assert hit.start_ts == start + 40 * step

    assert bundle.resonance
    res_btc = next(h for h in bundle.resonance if h.asset == "BTC")
    assert res_btc.pearson_r == pytest.approx(1.0, abs=1e-6)
    # last-n of BTC is the V
    assert res_btc.start_ts == start + 70 * step
    # last-n of ETH is trailing noise, not the V
    res_eth = next(h for h in bundle.resonance if h.asset == "ETH")
    assert res_eth.pearson_r < 0.99

    # no query window: last-n of BTC (the V) is allowed in history
    btc_hist = [h for h in bundle.history if h.asset == "BTC"]
    assert any(h.start_ts == start + 70 * step for h in btc_hist)
    assert bundle.forward is not None
    for h in bundle.history:
        assert isinstance(h.forward_ret, list)


def test_match_drawn_resonance_is_last_n(tmp_path) -> None:
    from kline_match.match import match_drawn

    conn = connect(tmp_path / "t.db")
    n = 10
    motif = np.linspace(1.0, 5.0, n)
    other = np.linspace(5.0, 1.0, n)
    # SOL last-n = motif; ETH last-n = other; ETH also has motif in the middle
    eth = np.concatenate([np.ones(15), motif, other])
    sol = np.concatenate([np.ones(20) * 3.0, motif])
    start = 1_600_000_000_000
    _seed(conn, "ETH", "binance", "1H", eth, start_ts=start)
    _seed(conn, "SOL", "binance", "1H", sol, start_ts=start)
    bundle = match_drawn(conn, "1H", motif, n=n, topk=10)
    by_asset = {h.asset: h for h in bundle.resonance}
    assert "SOL" in by_asset and "ETH" in by_asset
    assert by_asset["SOL"].pearson_r == pytest.approx(1.0, abs=1e-6)
    assert by_asset["ETH"].pearson_r < 0.5
    # ranks by r: SOL first
    assert bundle.resonance[0].asset == "SOL"


def test_match_drawn_rejects_flat() -> None:
    from kline_match.match import match_drawn
    import sqlite3

    conn = sqlite3.connect(":memory:")
    with pytest.raises(RuntimeError, match="近乎水平"):
        match_drawn(conn, "1H", np.ones(30), n=30)
