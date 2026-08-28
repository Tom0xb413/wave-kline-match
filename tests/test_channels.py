"""Multi-channel WAVE matching: close + candlestick structure + volume."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from kline_match.config import db_path
from kline_match.db import connect
from kline_match.match import (
    bar_shape_features,
    blend_channel_r,
    match_query,
    resolve_weights,
)
from kline_match.server import MATCH_PRESETS
from tests.test_match import _seed


def test_bar_shape_long_upper_wick() -> None:
    # open=close=low, long upper shadow → upper≈1
    o = np.array([10.0])
    h = np.array([20.0])
    lo = np.array([10.0])
    c = np.array([10.0])
    upper, lower, body, signed = bar_shape_features(o, h, lo, c)
    assert upper[0] == pytest.approx(1.0)
    assert lower[0] == pytest.approx(0.0)
    assert body[0] == pytest.approx(0.0)
    assert signed[0] == pytest.approx(0.0)


def test_bar_shape_marubozu() -> None:
    # bullish marubozu: body fills the range, wicks≈0
    o = np.array([10.0])
    h = np.array([20.0])
    lo = np.array([10.0])
    c = np.array([20.0])
    upper, lower, body, signed = bar_shape_features(o, h, lo, c)
    assert body[0] == pytest.approx(1.0)
    assert upper[0] == pytest.approx(0.0)
    assert lower[0] == pytest.approx(0.0)
    assert signed[0] == pytest.approx(1.0)
    # bearish
    o2 = np.array([20.0])
    c2 = np.array([10.0])
    upper, lower, body, signed = bar_shape_features(o2, h, lo, c2)
    assert body[0] == pytest.approx(1.0)
    assert upper[0] == pytest.approx(0.0)
    assert lower[0] == pytest.approx(0.0)
    assert signed[0] == pytest.approx(-1.0)


def test_resolve_weights_ratios_and_zero() -> None:
    wc, ws, wv, norm = resolve_weights(6, 2.5, 1.5)
    assert norm["close"] == pytest.approx(0.6)
    assert norm["shape"] == pytest.approx(0.25)
    assert norm["volume"] == pytest.approx(0.15)
    with pytest.raises(ValueError, match="全为 0"):
        resolve_weights(0, 0, 0)
    wc, ws, wv, norm = resolve_weights(0, 1, 0, close_only=True)
    assert wc == pytest.approx(1.0)
    assert ws == 0.0 and wv == 0.0
    assert norm["close"] == pytest.approx(1.0)


def test_all_weights_zero_match_query() -> None:
    with pytest.raises(ValueError, match="全为 0"):
        match_query(None, "BTC", "1H", n=30, w_close=0, w_shape=0, w_volume=0)


def test_close_only_weights_match_pearson(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(1)
    eth = 100 + np.cumsum(rng.normal(scale=0.8, size=180))
    motif = eth[60:90]
    btc_body = 200 + np.cumsum(rng.normal(scale=1.2, size=90))
    btc = np.concatenate([btc_body, motif])
    _seed(conn, "ETH", "binance", "1H", eth)
    _seed(conn, "BTC", "binance", "1H", btc)
    close_only = match_query(conn, "BTC", "1H", n=30, topk=10, w_close=1, w_shape=0, w_volume=0)
    recommend = match_query(conn, "BTC", "1H", n=30, topk=10, w_close=0.6, w_shape=0.25, w_volume=0.15)
    assert close_only.history
    hit = close_only.history[0]
    assert hit.asset == "ETH"
    assert hit.r_close == pytest.approx(hit.pearson_r)
    assert hit.r_shape is None
    assert hit.r_volume is None
    assert hit.weights == {"close": 1.0, "shape": 0.0, "volume": 0.0}
    assert recommend.history[0].asset == hit.asset
    assert recommend.query["weights"]["close"] == pytest.approx(0.6)


def test_blend_drops_nan_channel() -> None:
    r_c = np.array([0.8, np.nan, 0.4])
    r_s = np.array([0.2, 1.0, np.nan])
    r_v = np.array([np.nan, np.nan, np.nan])
    out = blend_channel_r(r_c, r_s, r_v, 0.6, 0.4, 0.0)
    assert out[0] == pytest.approx(0.6 * 0.8 + 0.4 * 0.2)
    assert out[1] == pytest.approx(1.0)  # only shape finite; close nan dropped
    assert out[2] == pytest.approx(0.4)  # only close finite
    # volume weight 0 → not used even if we passed a vec
    out2 = blend_channel_r(r_c, None, np.array([0.9, 0.9, 0.9]), 1.0, 0.0, 0.0)
    assert out2[0] == pytest.approx(0.8)
    assert np.isnan(out2[1])


def test_presets_list() -> None:
    ids = [p["id"] for p in MATCH_PRESETS]
    assert ids == ["recommend", "close_only", "shape", "volume", "custom"]
    rec = next(p for p in MATCH_PRESETS if p["id"] == "recommend")
    assert rec["w_close"] == 0.6 and rec["w_shape"] == 0.25 and rec["w_volume"] == 0.15
    custom = next(p for p in MATCH_PRESETS if p["id"] == "custom")
    assert custom["w_close"] is None


def test_shape_only_hits_on_real_btc() -> None:
    db = db_path()
    if not Path(db).is_file():
        pytest.skip("no DB")
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM bars WHERE asset='BTC' AND tf='1H' AND is_closed=1"
        ).fetchone()
        if not row or int(row["n"] or 0) < 80:
            pytest.skip("no BTC 1H")
        bundle = match_query(
            conn, "BTC", "1H", n=30, topk=10, w_close=0.0, w_shape=1.0, w_volume=0.0
        )
        hits = list(bundle.history) + list(bundle.resonance)
        assert hits, "shape-only scan returned no hits"
        for h in hits:
            assert h.r_close is None
            assert h.r_shape is not None
            assert h.r_volume is None
            assert h.pearson_r == pytest.approx(h.r_shape)
            assert h.weights["shape"] == pytest.approx(1.0)
    finally:
        conn.close()
