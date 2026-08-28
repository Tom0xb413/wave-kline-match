"""Sector-gated analog pools: alts vs BTC/ETH, no gold/stock mixing."""

from __future__ import annotations

import numpy as np
import pytest

from kline_match.db import connect
from kline_match.match import match_query
from kline_match.sectors import (
    CLASS_TO_SECTOR,
    PLACEHOLDER_SECTORS,
    history_pool,
    resonance_pool,
    scan_ids,
    sector_of,
)
from tests.test_match import _seed


def test_sol_pools() -> None:
    assert sector_of("SOL") == "crypto"
    assert history_pool("SOL") == {"SOL", "BTC", "ETH"}
    assert resonance_pool("SOL") == {"BTC", "ETH"}
    assert scan_ids("SOL") == {"SOL", "BTC", "ETH"}


def test_btc_eth_pools() -> None:
    assert sector_of("BTC") == "crypto"
    assert history_pool("BTC") == {"BTC", "ETH"}
    assert history_pool("ETH") == {"BTC", "ETH"}
    res = resonance_pool("BTC")
    assert "SOL" in res
    assert "PEPE" in res
    assert "ETH" in res
    assert "BTC" not in res
    assert "XAU" not in res
    assert "NVDA" not in res
    assert "TSLA" not in res


def test_gold_and_us_stock_pools() -> None:
    assert sector_of("XAU") == "gold"
    assert history_pool("XAU") == {"XAU"}
    assert resonance_pool("XAU") == set()
    assert sector_of("NVDA") == "us_stock"
    assert history_pool("NVDA") == {"NVDA", "TSLA", "AAPL", "GOOGL"}
    assert resonance_pool("NVDA") == {"TSLA", "AAPL", "GOOGL"}
    assert "BTC" not in history_pool("NVDA")
    assert "XAU" not in history_pool("NVDA")


def test_unknown_fail_closed() -> None:
    assert sector_of("ZZZNOPE") == "unknown"
    assert history_pool("ZZZNOPE") == {"ZZZNOPE"}
    assert resonance_pool("ZZZNOPE") == set()
    assert scan_ids("ZZZNOPE") == {"ZZZNOPE"}


def test_placeholder_sectors_exist_empty() -> None:
    for s in PLACEHOLDER_SECTORS:
        assert s in CLASS_TO_SECTOR.values()
        assert CLASS_TO_SECTOR[s] == s
    # no invented symbols
    for aid in ("KOSPI", "N225", "000300"):
        assert sector_of(aid) == "unknown"
        assert history_pool(aid) == {aid}
        assert resonance_pool(aid) == set()


def test_sol_match_query_excludes_gold_and_stocks(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(21)
    n = 20
    motif = 100 + np.cumsum(rng.normal(scale=0.5, size=n))
    noise = lambda loc: loc + np.cumsum(rng.normal(scale=0.8, size=40))
    # SOL query = last n of sol; perfect copy planted in PEPE/XAU/NVDA history
    sol = np.concatenate([noise(20), motif])
    btc = np.concatenate([motif, noise(200)])  # non-overlapping history analog
    eth = np.concatenate([noise(50)[:30], motif, noise(50)[:10]])
    pepe = np.concatenate([motif, noise(1.0)[:20]])
    xau = np.concatenate([motif, noise(1800)[:20]])
    nvda = np.concatenate([motif, noise(120)[:20]])
    start = 1_600_000_000_000
    _seed(conn, "SOL", "binance", "1H", sol, start_ts=start)
    _seed(conn, "BTC", "binance", "1H", btc, start_ts=start)
    _seed(conn, "ETH", "binance", "1H", eth, start_ts=start)
    _seed(conn, "PEPE", "binance", "1H", pepe, start_ts=start)
    _seed(conn, "XAU", "okx", "1H", xau, start_ts=start)
    _seed(conn, "NVDA", "gate", "1H", nvda, start_ts=start)

    bundle = match_query(conn, "SOL", "1H", n=n, topk=10)
    assert bundle.query["sector"] == "crypto"
    assert set(bundle.query["history_pool"]) == {"SOL", "BTC", "ETH"}
    assert set(bundle.query["resonance_pool"]) == {"BTC", "ETH"}
    hist_assets = {h.asset for h in bundle.history}
    assert hist_assets <= {"SOL", "BTC", "ETH"}
    assert "XAU" not in hist_assets
    assert "NVDA" not in hist_assets
    assert "PEPE" not in hist_assets
    res_assets = {h.asset for h in bundle.resonance}
    assert res_assets <= {"BTC", "ETH"}
    assert "PEPE" not in res_assets
    assert "XAU" not in res_assets
    assert "NVDA" not in res_assets
    # planted BTC motif should still rank as history analog
    assert any(h.asset == "BTC" and h.pearson_r == pytest.approx(1.0, abs=1e-6) for h in bundle.history)


def test_btc_match_query_resonance_alts_not_gold(tmp_path) -> None:
    conn = connect(tmp_path / "t.db")
    rng = np.random.default_rng(22)
    n = 16
    btc = 100 + np.cumsum(rng.normal(size=50))
    motif = btc[-n:]
    start = 1_600_000_000_000
    _seed(conn, "BTC", "binance", "1H", btc, start_ts=start)
    _seed(conn, "ETH", "binance", "1H", np.concatenate([rng.normal(loc=50, size=50 - n), motif]), start_ts=start)
    _seed(conn, "SOL", "binance", "1H", np.concatenate([rng.normal(loc=20, size=50 - n), motif]), start_ts=start)
    _seed(conn, "XAU", "okx", "1H", np.concatenate([rng.normal(loc=1800, size=50 - n), motif]), start_ts=start)
    _seed(conn, "NVDA", "gate", "1H", np.concatenate([rng.normal(loc=120, size=50 - n), motif]), start_ts=start)

    bundle = match_query(conn, "BTC", "1H", n=n, topk=10)
    assert set(bundle.query["history_pool"]) == {"BTC", "ETH"}
    hist_assets = {h.asset for h in bundle.history}
    assert hist_assets <= {"BTC", "ETH"}
    assert "SOL" not in hist_assets
    assert "XAU" not in hist_assets
    assert "NVDA" not in hist_assets
    res_assets = {h.asset for h in bundle.resonance}
    assert "SOL" in res_assets
    assert "ETH" in res_assets
    assert "XAU" not in res_assets
    assert "NVDA" not in res_assets
    assert "BTC" not in res_assets
