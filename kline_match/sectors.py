"""Sector-gated analog pools for match_query.

Uncorrelated asset classes are never mixed. Crypto alts benchmark against
BTC/ETH cores, not other alts or gold/stocks.

Derived from universe.yaml ``class``, plus ad-hoc symbols registered at ensure
time (or looked up from ``adhoc_symbols`` / optional conn).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from kline_match.config import load_universe

CRYPTO_CORES = frozenset({"BTC", "ETH"})

CLASS_TO_SECTOR: dict[str, str] = {
    "crypto": "crypto",
    "gold": "gold",
    "tradfi": "us_stock",
    "kr_index": "kr_index",
    "jp_index": "jp_index",
    "cn_a": "cn_a",
}

PLACEHOLDER_SECTORS = ("kr_index", "jp_index", "cn_a")

# Runtime ad-hoc class overlays (asset -> class). Cleared when maps rebuild.
_ADHOC_CLASS: dict[str, str] = {}


def _norm(asset: str) -> str:
    return str(asset or "").upper()


def register_adhoc(asset: str, klass: str) -> None:
    """Remember an ad-hoc class and bust the yaml-only lru cache."""
    aid = _norm(asset)
    if not aid:
        return
    _ADHOC_CLASS[aid] = str(klass or "crypto")
    _maps.cache_clear()


def clear_adhoc_overlay() -> None:
    _ADHOC_CLASS.clear()
    _maps.cache_clear()


def _load_adhoc_from_conn(conn: Any) -> dict[str, str]:
    if conn is None:
        return {}
    try:
        from kline_match.db import adhoc_class_map

        return adhoc_class_map(conn)
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _maps() -> tuple[dict[str, str], dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """asset→sector, sector→members, sector→cores (yaml + registered adhoc)."""
    uni = load_universe()
    asset_sector: dict[str, str] = {}
    members: dict[str, set[str]] = {s: set() for s in CLASS_TO_SECTOR.values()}
    for spec in uni.assets:
        sector = CLASS_TO_SECTOR.get(spec.klass, spec.klass)
        aid = spec.id.upper()
        asset_sector[aid] = sector
        members.setdefault(sector, set()).add(aid)
    for aid, klass in list(_ADHOC_CLASS.items()):
        sector = CLASS_TO_SECTOR.get(klass, klass)
        asset_sector[aid] = sector
        members.setdefault(sector, set()).add(aid)
    cores: dict[str, frozenset[str]] = {
        "crypto": frozenset(CRYPTO_CORES & members.get("crypto", set())),
    }
    for sector, ids in members.items():
        if sector not in cores:
            cores[sector] = frozenset(ids)
    frozen_members = {k: frozenset(v) for k, v in members.items()}
    return asset_sector, frozen_members, cores


def _resolve_maps(conn: Any = None) -> tuple[dict[str, str], dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    if conn is not None:
        db_map = _load_adhoc_from_conn(conn)
        dirty = False
        for aid, klass in db_map.items():
            if _ADHOC_CLASS.get(aid) != klass:
                _ADHOC_CLASS[aid] = klass
                dirty = True
        if dirty:
            _maps.cache_clear()
    return _maps()


def class_of(asset: str, conn: Any = None) -> str | None:
    aid = _norm(asset)
    if not aid:
        return None
    if aid in _ADHOC_CLASS:
        return _ADHOC_CLASS[aid]
    asset_sector, _, _ = _resolve_maps(conn)
    sector = asset_sector.get(aid)
    if sector is None:
        if conn is not None:
            db = _load_adhoc_from_conn(conn)
            if aid in db:
                register_adhoc(aid, db[aid])
                return db[aid]
        return None
    for klass, sec in CLASS_TO_SECTOR.items():
        if sec == sector:
            # reverse: prefer the yaml class name when unique enough
            if sector == "us_stock":
                return "tradfi"
            if sector == "crypto":
                return "crypto"
            return klass
    return sector


def sector_of(asset: str, conn: Any = None) -> str:
    aid = _norm(asset)
    asset_sector, _, _ = _resolve_maps(conn)
    if aid in asset_sector:
        return asset_sector[aid]
    if conn is not None:
        db = _load_adhoc_from_conn(conn)
        if aid in db:
            register_adhoc(aid, db[aid])
            return CLASS_TO_SECTOR.get(db[aid], db[aid])
    if aid in _ADHOC_CLASS:
        return CLASS_TO_SECTOR.get(_ADHOC_CLASS[aid], _ADHOC_CLASS[aid])
    return "unknown"


def history_pool(asset: str, conn: Any = None) -> set[str]:
    """Who may appear in 历史类比 for this query asset."""
    aid = _norm(asset)
    if not aid:
        return set()
    asset_sector, members, cores = _resolve_maps(conn)
    sector = asset_sector.get(aid)
    if sector is None and conn is not None:
        sector_of(aid, conn)  # may register
        asset_sector, members, cores = _maps()
        sector = asset_sector.get(aid)
    if sector is None and aid in _ADHOC_CLASS:
        sector = CLASS_TO_SECTOR.get(_ADHOC_CLASS[aid], _ADHOC_CLASS[aid])
        asset_sector, members, cores = _maps()
        sector = asset_sector.get(aid, sector)
    if sector is None:
        return {aid}
    if sector == "crypto":
        core = set(cores.get("crypto", frozenset()))
        if not core:
            core = set(CRYPTO_CORES)
        if aid in core:
            return core
        return {aid} | core
    ids = set(members.get(sector, set()))
    if aid not in ids:
        ids.add(aid)
    return ids if ids else {aid}


def resonance_pool(asset: str, conn: Any = None) -> set[str]:
    """Who may appear in 当前共振 (excludes self)."""
    aid = _norm(asset)
    asset_sector, members, cores = _resolve_maps(conn)
    sector = asset_sector.get(aid)
    if sector is None and conn is not None:
        sector_of(aid, conn)
        asset_sector, members, cores = _maps()
        sector = asset_sector.get(aid)
    if sector is None and aid in _ADHOC_CLASS:
        register_adhoc(aid, _ADHOC_CLASS[aid])
        asset_sector, members, cores = _maps()
        sector = asset_sector.get(aid)
    if sector is None:
        return set()
    if sector == "crypto":
        core = set(cores.get("crypto", frozenset()) or CRYPTO_CORES)
        if aid in core:
            return set(members.get("crypto", set())) - {aid}
        return set(core)
    return set(members.get(sector, set())) - {aid}


def scan_ids(asset: str, conn: Any = None) -> set[str]:
    """Union scanned by MASS: history ∪ resonance ∪ {asset}."""
    aid = _norm(asset)
    out = history_pool(aid, conn) | resonance_pool(aid, conn)
    if aid:
        out.add(aid)
    return out
