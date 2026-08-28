"""On-demand instrument catalog: universe ∪ adhoc cache ∪ live exchange lists.

Live catalogs (Binance / OKX / Gate) are stored in SQLite ``catalog_cache``
with a 6-hour TTL plus a process memory cache so keystrokes never stampede
the exchanges.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, Iterable

from kline_match.config import load_universe
from kline_match.db import (
    adhoc_class_map,
    get_catalog_row,
    put_catalog_row,
    ready_assets,
)
from kline_match.timeframes import now_ms, normalize_tf

CATALOG_TTL_MS = 6 * 3600 * 1000
SEARCH_LIMIT = 12

GOLD_BASES = frozenset({"XAU", "XAUT", "PAXG", "GOLD"})
STABLE_BASES = frozenset(
    {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "USDD", "EUR", "USDT", "USDE"}
)
GATE_METAL_FUTS = ("XAU_USDT", "XAUT_USDT", "PAXG_USDT", "GOLD_USDT")

_LEV_SUFFIX = re.compile(r"(UP|DOWN|BULL|BEAR)$")
_NON_ALNUM = re.compile(r"[-_/]")

_MEM: dict[str, tuple[int, list[dict[str, Any]]]] = {}
_MEM_LOCK = threading.Lock()
_FETCH_LOCKS: dict[str, threading.Lock] = {}
_FETCH_GUARD = threading.Lock()


def _fetch_lock(venue: str) -> threading.Lock:
    with _FETCH_GUARD:
        lock = _FETCH_LOCKS.get(venue)
        if lock is None:
            lock = threading.Lock()
            _FETCH_LOCKS[venue] = lock
        return lock


def norm_sym(value: str) -> str:
    return _NON_ALNUM.sub("", str(value or "").upper())


def is_leveraged(base: str) -> bool:
    """Skip BTCUP / ETHDOWN / BULL-BEAR tokens, keep JUP etc."""
    b = str(base or "").upper()
    m = _LEV_SUFFIX.search(b)
    if not m:
        return False
    return len(b[: m.start()]) >= 3


def is_stable_pair(base: str) -> bool:
    return str(base or "").upper() in STABLE_BASES


def classify_asset(*, base: str, native: str, venue: str) -> str:
    b = str(base or "").upper()
    n = str(native or "").upper().replace("-", "_")
    if b in GOLD_BASES or b.startswith("XAU") or b in {"PAXG", "GOLD"}:
        return "gold"
    if venue == "gate" and n.endswith("X_USDT") and len(n) > 7:
        return "tradfi"
    return "crypto"


def gate_asset_id(pair: str) -> str:
    p = str(pair or "").upper()
    if p.endswith("X_USDT") and len(p) > 7:
        return p[: -len("X_USDT")]
    if p.endswith("_USDT"):
        return p[: -len("_USDT")]
    return p


def parse_native(native: str) -> tuple[str, str | None]:
    """Guess (asset_id, venue_hint) from a typed native symbol."""
    raw = str(native or "").strip().upper()
    if not raw:
        return "", None
    if "-" in raw:
        base = raw.split("-", 1)[0]
        return base, "okx"
    if "_" in raw:
        return gate_asset_id(raw), "gate"
    compact = norm_sym(raw)
    if compact.endswith("USDT") and len(compact) > 4:
        return compact[:-4], "binance"
    return compact, None


def item(
    *,
    asset_id: str,
    venue: str,
    native_symbol: str,
    klass: str,
    source: str,
    ready: bool = False,
) -> dict[str, Any]:
    return {
        "id": str(asset_id).upper(),
        "venue": venue,
        "native_symbol": native_symbol,
        "class": klass,
        "source": source,
        "ready": bool(ready),
    }


def universe_items() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in load_universe().assets:
        primary = next((v for v in spec.venues if v.primary), spec.venues[0] if spec.venues else None)
        if primary is None:
            continue
        native = primary.native_symbols[0] if primary.native_symbols else spec.id
        out.append(
            item(
                asset_id=spec.id,
                venue=primary.venue,
                native_symbol=native,
                klass=spec.klass,
                source="universe",
            )
        )
    return out


def adhoc_items(conn) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT asset, venue, native_symbol, class, last_access_at
            FROM adhoc_symbols
            ORDER BY COALESCE(last_access_at, 0) DESC
            """
        ).fetchall()
    except Exception:
        return []
    for r in rows:
        aid = str(r["asset"]).upper()
        if aid in seen:
            continue
        seen.add(aid)
        out.append(
            item(
                asset_id=aid,
                venue=str(r["venue"]),
                native_symbol=str(r["native_symbol"]),
                klass=str(r["class"] or "crypto"),
                source="adhoc",
            )
        )
    return out


def _parse_binance_symbols(raw: Any) -> list[dict[str, Any]]:
    symbols = raw.get("symbols") if isinstance(raw, dict) else raw
    if not isinstance(symbols, list):
        return []
    out: list[dict[str, Any]] = []
    for s in symbols:
        if not isinstance(s, dict):
            continue
        if str(s.get("status") or "").upper() not in {"TRADING", ""}:
            if s.get("status") and str(s.get("status")).upper() != "TRADING":
                continue
        quote = str(s.get("quoteAsset") or s.get("quote") or "").upper()
        native = str(s.get("symbol") or "").upper()
        base = str(s.get("baseAsset") or "").upper()
        if not native:
            continue
        if not base and native.endswith("USDT"):
            base = native[:-4]
            quote = quote or "USDT"
        if quote and quote != "USDT":
            continue
        if not native.endswith("USDT"):
            continue
        if s.get("isSpotTradingAllowed") is False:
            continue
        if is_leveraged(base) or is_stable_pair(base):
            continue
        out.append(
            item(
                asset_id=base,
                venue="binance",
                native_symbol=native,
                klass=classify_asset(base=base, native=native, venue="binance"),
                source="binance",
            )
        )
    return out


def _parse_okx_instruments(raw: Any) -> list[dict[str, Any]]:
    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    for inst in rows:
        if not isinstance(inst, dict):
            continue
        quote = str(inst.get("quoteCcy") or "").upper()
        native = str(inst.get("instId") or "").upper()
        base = str(inst.get("baseCcy") or "").upper()
        state = str(inst.get("state") or "live").lower()
        if quote != "USDT":
            continue
        if state not in {"live", "1", ""}:
            continue
        if not base:
            base = native.split("-", 1)[0] if "-" in native else native
        if is_leveraged(base) or is_stable_pair(base):
            continue
        out.append(
            item(
                asset_id=base,
                venue="okx",
                native_symbol=native,
                klass=classify_asset(base=base, native=native, venue="okx"),
                source="okx",
            )
        )
    return out


def _parse_gate_pairs(spot: Iterable[str], futs: Iterable[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_native: set[str] = set()
    for pair in spot:
        p = str(pair).upper()
        if not p.endswith("_USDT"):
            continue
        base = gate_asset_id(p)
        if is_leveraged(base) or is_stable_pair(base):
            continue
        seen_native.add(p)
        out.append(
            item(
                asset_id=base,
                venue="gate",
                native_symbol=p,
                klass=classify_asset(base=base, native=p, venue="gate"),
                source="gate",
            )
        )
    if futs:
        for name in GATE_METAL_FUTS:
            if name in futs and name not in seen_native:
                base = gate_asset_id(name)
                out.append(
                    item(
                        asset_id=base,
                        venue="gate",
                        native_symbol=name,
                        klass="gold",
                        source="gate",
                    )
                )
    return out


def fetch_binance_catalog() -> list[dict[str, Any]]:
    from kline_match.exchanges.binance import BinanceClient

    client = BinanceClient()
    try:
        raw = client.fetch_exchange_info()
        items = _parse_binance_symbols(raw)
        if items:
            return items
    except Exception:
        pass
    try:
        tickers = client.fetch_ticker_24hr()
    except Exception:
        return []
    fake = []
    for t in tickers or []:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or "").upper()
        if not sym.endswith("USDT"):
            continue
        fake.append({"symbol": sym, "quoteAsset": "USDT", "baseAsset": sym[:-4], "status": "TRADING"})
    return _parse_binance_symbols(fake)


def fetch_okx_catalog() -> list[dict[str, Any]]:
    from kline_match.exchanges.okx import OkxClient

    try:
        raw = OkxClient().fetch_spot_instruments()
    except Exception:
        return []
    return _parse_okx_instruments(raw)


def fetch_gate_catalog() -> list[dict[str, Any]]:
    from kline_match.exchanges.gate import GateClient

    client = GateClient()
    try:
        spot = client.spot_pairs()
    except Exception:
        return []
    futs: set[str] | None = None
    try:
        futs = client.fut_contracts()
    except Exception:
        futs = None
    return _parse_gate_pairs(spot, futs)


_FETCHERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "binance": fetch_binance_catalog,
    "okx": fetch_okx_catalog,
    "gate": fetch_gate_catalog,
}


def _from_db(conn, venue: str) -> tuple[int, list[dict[str, Any]]] | None:
    row = get_catalog_row(conn, venue)
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    return int(row["fetched_at"]), payload


def get_venue_catalog(conn, venue: str, *, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """Return cached catalog for ``venue``. Fetches only if TTL expired."""
    now = now_ms()
    with _MEM_LOCK:
        mem = _MEM.get(venue)
        if mem and now - mem[0] < CATALOG_TTL_MS:
            return list(mem[1])

    db_hit = _from_db(conn, venue)
    if db_hit and now - db_hit[0] < CATALOG_TTL_MS:
        with _MEM_LOCK:
            _MEM[venue] = db_hit
        return list(db_hit[1])

    if not allow_fetch:
        if db_hit:
            return list(db_hit[1])
        return []

    lock = _fetch_lock(venue)
    with lock:
        now = now_ms()
        with _MEM_LOCK:
            mem = _MEM.get(venue)
            if mem and now - mem[0] < CATALOG_TTL_MS:
                return list(mem[1])
        db_hit = _from_db(conn, venue)
        if db_hit and now - db_hit[0] < CATALOG_TTL_MS:
            with _MEM_LOCK:
                _MEM[venue] = db_hit
            return list(db_hit[1])

        fetcher = _FETCHERS.get(venue)
        items: list[dict[str, Any]] = []
        fetched_at = now
        if fetcher is not None:
            try:
                items = fetcher()
                fetched_at = now_ms()
            except Exception:
                items = []
        if items:
            put_catalog_row(conn, venue, fetched_at, json.dumps(items, ensure_ascii=False))
            with _MEM_LOCK:
                _MEM[venue] = (fetched_at, items)
            return list(items)
        if db_hit:
            return list(db_hit[1])
        return []


def merged_catalog(conn, *, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """Union de-duplicated by asset id. Universe and adhoc win."""
    by_id: dict[str, dict[str, Any]] = {}
    for src in (
        universe_items(),
        adhoc_items(conn),
        get_venue_catalog(conn, "binance", allow_fetch=allow_fetch),
        get_venue_catalog(conn, "okx", allow_fetch=allow_fetch),
        get_venue_catalog(conn, "gate", allow_fetch=allow_fetch),
    ):
        for it in src:
            aid = it["id"]
            if aid not in by_id:
                by_id[aid] = dict(it)
    return list(by_id.values())


def _match_rank(q: str, it: dict[str, Any]) -> int | None:
    qn = q.strip().upper()
    if not qn:
        return None
    qz = norm_sym(qn)
    iid = str(it.get("id") or "").upper()
    native = str(it.get("native_symbol") or "").upper()
    nz = norm_sym(native)
    base = iid
    if qn == iid or qn == native or qz == nz or qn == base:
        return 0
    if iid.startswith(qn) or native.startswith(qn) or nz.startswith(qz) or base.startswith(qn):
        return 1
    if qn in iid or qn in native or qz in nz or qn in base:
        return 2
    return None


def _tie_key(it: dict[str, Any]) -> tuple:
    src = str(it.get("source") or "")
    ready = 0 if it.get("ready") else 1
    src_rank = {"universe": 0, "adhoc": 1}.get(src, 2)
    cached = 0 if src in {"universe", "adhoc"} or it.get("ready") else 1
    return (cached, ready, src_rank, str(it.get("id") or ""))


def fuzzy_search(
    q: str,
    items: Iterable[dict[str, Any]],
    *,
    limit: int = SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Rank: exact id / native, then prefix, then contains. Universe/ready first."""
    scored: list[tuple[int, tuple, dict[str, Any]]] = []
    for it in items:
        rank = _match_rank(q, it)
        if rank is None:
            continue
        scored.append((rank, _tie_key(it), it))
    scored.sort(key=lambda t: (t[0], t[1]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _rank, _tie, it in scored:
        aid = str(it.get("id") or "")
        if aid in seen:
            continue
        seen.add(aid)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def search_symbols(conn, q: str, tf: str = "1H") -> list[dict[str, Any]]:
    q = (q or "").strip()
    if len(q) < 1:
        return []
    tf = normalize_tf(tf)
    ready = ready_assets(conn, tf)
    items = merged_catalog(conn, allow_fetch=True)
    classes = adhoc_class_map(conn)
    for it in items:
        aid = it["id"]
        it["ready"] = aid in ready
        if aid in classes and it.get("source") != "universe":
            it["class"] = classes[aid]
    return fuzzy_search(q, items, limit=SEARCH_LIMIT)


def lookup_instrument(
    conn,
    *,
    asset_id: str | None = None,
    native_symbol: str | None = None,
    venue: str | None = None,
    allow_fetch: bool = True,
) -> dict[str, Any] | None:
    """Resolve an id/native to a catalog item. Prefers universe then adhoc then catalogs."""
    aid = (asset_id or "").strip().upper()
    native = (native_symbol or "").strip().upper()
    venue = (venue or "").strip().lower() or None
    if not aid and native:
        aid, hint = parse_native(native)
        if venue is None:
            venue = hint
    if not aid:
        return None
    items = merged_catalog(conn, allow_fetch=allow_fetch)
    cands = [it for it in items if it["id"] == aid]
    if native:
        exact = [it for it in cands if str(it["native_symbol"]).upper() == native or norm_sym(it["native_symbol"]) == norm_sym(native)]
        if exact:
            cands = exact
        else:
            more = [
                it
                for it in items
                if str(it["native_symbol"]).upper() == native or norm_sym(it["native_symbol"]) == norm_sym(native)
            ]
            if more:
                cands = more
    if venue:
        vhit = [it for it in cands if it["venue"] == venue]
        if vhit:
            cands = vhit
    if not cands:
        return None

    def pref(it: dict[str, Any]) -> tuple:
        src = it.get("source")
        klass = it.get("class") or "crypto"
        venue_pref = 0
        if klass == "tradfi":
            venue_pref = 0 if it["venue"] == "gate" else 1
        elif klass == "gold":
            venue_pref = {"okx": 0, "binance": 1, "gate": 2}.get(it["venue"], 9)
        else:
            venue_pref = {"binance": 0, "okx": 1, "gate": 2}.get(it["venue"], 9)
        src_rank = {"universe": 0, "adhoc": 1}.get(src, 2)
        return (src_rank, venue_pref)

    cands.sort(key=pref)
    return dict(cands[0])
