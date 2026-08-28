"""On-demand catalog search + rolling-window cache helpers (no live exchanges)."""

from __future__ import annotations

from kline_match.catalog import fuzzy_search, item
from kline_match.ensure import cache_is_fresh, current_closed_ts
from kline_match.sectors import history_pool, register_adhoc, resonance_pool, scan_ids, sector_of
from kline_match.timeframes import TF_MS


def _it(aid: str, native: str, *, source: str = "binance", ready: bool = False, klass: str = "crypto"):
    return item(
        asset_id=aid,
        venue="binance",
        native_symbol=native,
        klass=klass,
        source=source,
        ready=ready,
    )


def test_fuzzy_rank_wif_prefers_wifusdt() -> None:
    items = [
        _it("SWIF", "SWIFUSDT", source="binance"),
        _it("WIFI", "WIFIUSDT", source="binance"),
        _it("WIF", "WIFUSDT", source="binance"),
        _it("DOGE", "DOGEUSDT", source="universe", ready=True),
    ]
    hits = fuzzy_search("WIF", items, limit=12)
    assert hits, "expected hits"
    assert hits[0]["id"] == "WIF"
    assert hits[0]["native_symbol"] == "WIFUSDT"
    ids = [h["id"] for h in hits]
    assert ids.index("WIF") < ids.index("WIFI")
    assert ids.index("WIFI") < ids.index("SWIF")
    assert "DOGE" not in ids


def test_fuzzy_prefers_universe_ready_on_tie() -> None:
    items = [
        _it("ABC", "ABCUSDT", source="binance", ready=False),
        _it("ABC", "ABCUSDT", source="universe", ready=True),
        _it("ABCD", "ABCDUSDT", source="binance", ready=False),
    ]
    # de-dupe is caller's job; ranking still puts universe/ready first among exact
    hits = fuzzy_search("ABC", items, limit=12)
    assert hits[0]["source"] == "universe"
    assert hits[0]["ready"] is True


def test_cache_validity_one_closed_bar() -> None:
    step = TF_MS["1H"]
    now = 1_700_000_400_000  # not aligned
    cur = current_closed_ts("1H", now)
    assert cur == (now // step) * step - step
    assert cache_is_fresh(cur, "1H", now)
    assert not cache_is_fresh(cur - step, "1H", now)
    assert not cache_is_fresh(None, "1H", now)
    assert not cache_is_fresh(cur - 2 * step, "1H", now)


def test_sector_of_adhoc_crypto_alt_pools() -> None:
    register_adhoc("WIF", "crypto")
    assert sector_of("WIF") == "crypto"
    assert history_pool("WIF") == {"WIF", "BTC", "ETH"}
    assert resonance_pool("WIF") == {"BTC", "ETH"}
    assert scan_ids("WIF") == {"WIF", "BTC", "ETH"}


def test_sector_of_adhoc_tradfi_joins_yaml_stocks() -> None:
    register_adhoc("MSFT", "tradfi")
    assert sector_of("MSFT") == "us_stock"
    hist = history_pool("MSFT")
    assert "MSFT" in hist
    assert "NVDA" in hist
    assert "TSLA" in hist
    assert "BTC" not in hist
    res = resonance_pool("MSFT")
    assert "NVDA" in res
    assert "MSFT" not in res


def test_unknown_still_fail_closed() -> None:
    assert sector_of("ZZZNOPE") == "unknown"
    assert history_pool("ZZZNOPE") == {"ZZZNOPE"}
    assert resonance_pool("ZZZNOPE") == set()
