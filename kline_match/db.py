"""SQLite 访问：bars / series 幂等写入。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from kline_match.models import Candle
from kline_match.timeframes import now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  asset TEXT NOT NULL,
  venue TEXT NOT NULL,
  tf TEXT NOT NULL,
  ts INTEGER NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  is_closed INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (asset, venue, tf, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_primary_scan ON bars (tf, asset, ts);

CREATE TABLE IF NOT EXISTS series (
  asset TEXT NOT NULL,
  venue TEXT NOT NULL,
  tf TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  is_primary INTEGER NOT NULL,
  last_ts INTEGER,
  last_sync_at INTEGER,
  PRIMARY KEY (asset, venue, tf)
);

CREATE TABLE IF NOT EXISTS catalog_cache (
  venue TEXT PRIMARY KEY,
  fetched_at INTEGER NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adhoc_symbols (
  asset TEXT NOT NULL,
  venue TEXT NOT NULL,
  tf TEXT NOT NULL,
  native_symbol TEXT NOT NULL,
  class TEXT NOT NULL,
  last_sync_at INTEGER,
  last_access_at INTEGER,
  window_bars INTEGER,
  PRIMARY KEY (asset, venue, tf)
);
CREATE INDEX IF NOT EXISTS idx_adhoc_access ON adhoc_symbols (last_access_at);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_series(
    conn: sqlite3.Connection,
    *,
    asset: str,
    venue: str,
    tf: str,
    native_symbol: str,
    is_primary: int,
) -> None:
    conn.execute(
        """
        INSERT INTO series (asset, venue, tf, native_symbol, is_primary, last_ts, last_sync_at)
        VALUES (?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(asset, venue, tf) DO UPDATE SET
          native_symbol=excluded.native_symbol,
          is_primary=excluded.is_primary
        """,
        (asset, venue, tf, native_symbol, int(is_primary)),
    )


def upsert_bars(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    tf: str,
    candles: Sequence[Candle],
) -> int:
    if not candles:
        return 0
    rows = [
        (
            asset,
            venue,
            tf,
            int(c.ts),
            float(c.open),
            float(c.high),
            float(c.low),
            float(c.close),
            float(c.volume),
            1 if c.is_closed else 0,
        )
        for c in candles
    ]
    conn.executemany(
        """
        INSERT INTO bars (asset, venue, tf, ts, open, high, low, close, volume, is_closed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset, venue, tf, ts) DO UPDATE SET
          open=excluded.open,
          high=excluded.high,
          low=excluded.low,
          close=excluded.close,
          volume=excluded.volume,
          is_closed=excluded.is_closed
        """,
        rows,
    )
    return len(rows)


def touch_series(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    tf: str,
) -> None:
    row = conn.execute(
        """
        SELECT MAX(CASE WHEN is_closed=1 THEN ts END) AS last_closed
        FROM bars WHERE asset=? AND venue=? AND tf=?
        """,
        (asset, venue, tf),
    ).fetchone()
    last_ts = int(row["last_closed"]) if row and row["last_closed"] is not None else None
    conn.execute(
        "UPDATE series SET last_ts=?, last_sync_at=? WHERE asset=? AND venue=? AND tf=?",
        (last_ts, now_ms(), asset, venue, tf),
    )


def series_bounds(
    conn: sqlite3.Connection, asset: str, venue: str, tf: str
) -> tuple[int | None, int | None, int]:
    row = conn.execute(
        """
        SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n
        FROM bars
        WHERE asset=? AND venue=? AND tf=? AND is_closed=1
        """,
        (asset, venue, tf),
    ).fetchone()
    lo = int(row["lo"]) if row["lo"] is not None else None
    hi = int(row["hi"]) if row["hi"] is not None else None
    return lo, hi, int(row["n"] or 0)


def primary_of(conn: sqlite3.Connection, asset: str, tf: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT asset, venue, tf, native_symbol, last_ts, is_primary
        FROM series
        WHERE asset=? AND tf=? AND is_primary=1
        """,
        (asset.upper(), tf),
    ).fetchone()


def load_ohlcv(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    tf: str,
    *,
    closed_only: bool = False,
) -> list[sqlite3.Row]:
    sql = """
        SELECT ts, open, high, low, close, volume, is_closed
        FROM bars
        WHERE asset=? AND venue=? AND tf=?
    """
    if closed_only:
        sql += " AND is_closed=1"
    sql += " ORDER BY ts ASC"
    return conn.execute(sql, (asset, venue, tf)).fetchall()


def load_closed_series(
    conn: sqlite3.Connection, asset: str, venue: str, tf: str
) -> tuple[list[int], list[float]]:
    rows = conn.execute(
        """
        SELECT ts, close FROM bars
        WHERE asset=? AND venue=? AND tf=? AND is_closed=1
        ORDER BY ts ASC
        """,
        (asset, venue, tf),
    ).fetchall()
    ts = [int(r["ts"]) for r in rows]
    close = [float(r["close"]) for r in rows]
    return ts, close


def load_all_primary_closes(
    conn: sqlite3.Connection, tf: str
) -> dict[str, tuple[str, list[int], list[float]]]:
    """一次扫出某周期全部主序列的已收盘收盘价。"""
    rows = conn.execute(
        """
        SELECT b.asset, s.venue, b.ts, b.close
        FROM bars b
        JOIN series s ON s.asset=b.asset AND s.venue=b.venue AND s.tf=b.tf
        WHERE b.tf=? AND s.is_primary=1 AND b.is_closed=1
        ORDER BY b.asset, b.ts
        """,
        (tf,),
    ).fetchall()
    out: dict[str, tuple[str, list[int], list[float]]] = {}
    for r in rows:
        a = r["asset"]
        if a not in out:
            out[a] = (r["venue"], [], [])
        out[a][1].append(int(r["ts"]))
        out[a][2].append(float(r["close"]))
    return out


def load_all_primary_ohlcv(
    conn: sqlite3.Connection, tf: str
) -> dict[str, tuple[str, list[int], list[float], list[float], list[float], list[float], list[float]]]:
    """一次扫出某周期全部主序列的已收盘 OHLCV。

    返回 ``asset -> (venue, ts, open, high, low, close, volume)``。
    """
    rows = conn.execute(
        """
        SELECT b.asset, s.venue, b.ts, b.open, b.high, b.low, b.close, b.volume
        FROM bars b
        JOIN series s ON s.asset=b.asset AND s.venue=b.venue AND s.tf=b.tf
        WHERE b.tf=? AND s.is_primary=1 AND b.is_closed=1
        ORDER BY b.asset, b.ts
        """,
        (tf,),
    ).fetchall()
    out: dict[str, tuple[str, list[int], list[float], list[float], list[float], list[float], list[float]]] = {}
    for r in rows:
        a = r["asset"]
        if a not in out:
            out[a] = (r["venue"], [], [], [], [], [], [])
        rec = out[a]
        rec[1].append(int(r["ts"]))
        rec[2].append(float(r["open"]))
        rec[3].append(float(r["high"]))
        rec[4].append(float(r["low"]))
        rec[5].append(float(r["close"]))
        rec[6].append(float(r["volume"]))
    return out


def status_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          s.asset, s.venue, s.tf, s.native_symbol, s.is_primary,
          s.last_ts, s.last_sync_at,
          COUNT(b.ts) AS n_closed,
          MIN(b.ts) AS first_ts,
          MAX(b.ts) AS last_bar_ts
        FROM series s
        LEFT JOIN bars b
          ON b.asset=s.asset AND b.venue=s.venue AND b.tf=s.tf AND b.is_closed=1
        GROUP BY s.asset, s.venue, s.tf
        ORDER BY s.asset, s.tf, s.venue
        """
    ).fetchall()


def count_closed_bars(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM bars WHERE is_closed=1").fetchone()
    return int(row["n"] or 0)


def last_closed_ts(conn: sqlite3.Connection, asset: str, venue: str, tf: str) -> int | None:
    row = conn.execute(
        """
        SELECT MAX(ts) AS hi FROM bars
        WHERE asset=? AND venue=? AND tf=? AND is_closed=1
        """,
        (asset.upper(), venue, tf),
    ).fetchone()
    if row is None or row["hi"] is None:
        return None
    return int(row["hi"])


def ready_assets(conn: sqlite3.Connection, tf: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT s.asset
        FROM series s
        JOIN bars b ON b.asset=s.asset AND b.venue=s.venue AND b.tf=s.tf AND b.is_closed=1
        WHERE s.tf=? AND s.is_primary=1
        """,
        (tf,),
    ).fetchall()
    return {str(r["asset"]).upper() for r in rows}


def get_catalog_row(conn: sqlite3.Connection, venue: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT venue, fetched_at, payload FROM catalog_cache WHERE venue=?",
        (venue,),
    ).fetchone()


def put_catalog_row(conn: sqlite3.Connection, venue: str, fetched_at: int, payload: str) -> None:
    conn.execute(
        """
        INSERT INTO catalog_cache (venue, fetched_at, payload)
        VALUES (?, ?, ?)
        ON CONFLICT(venue) DO UPDATE SET
          fetched_at=excluded.fetched_at,
          payload=excluded.payload
        """,
        (venue, int(fetched_at), payload),
    )
    conn.commit()


def upsert_adhoc(
    conn: sqlite3.Connection,
    *,
    asset: str,
    venue: str,
    tf: str,
    native_symbol: str,
    klass: str,
    last_sync_at: int | None,
    last_access_at: int | None,
    window_bars: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO adhoc_symbols (
          asset, venue, tf, native_symbol, class,
          last_sync_at, last_access_at, window_bars
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset, venue, tf) DO UPDATE SET
          native_symbol=excluded.native_symbol,
          class=excluded.class,
          last_sync_at=COALESCE(excluded.last_sync_at, adhoc_symbols.last_sync_at),
          last_access_at=COALESCE(excluded.last_access_at, adhoc_symbols.last_access_at),
          window_bars=COALESCE(excluded.window_bars, adhoc_symbols.window_bars)
        """,
        (
            asset.upper(),
            venue,
            tf,
            native_symbol,
            klass,
            last_sync_at,
            last_access_at,
            window_bars,
        ),
    )


def touch_adhoc_access(conn: sqlite3.Connection, asset: str, tf: str, when_ms: int | None = None) -> None:
    ts = int(when_ms if when_ms is not None else now_ms())
    conn.execute(
        "UPDATE adhoc_symbols SET last_access_at=? WHERE asset=? AND tf=?",
        (ts, asset.upper(), tf),
    )


def adhoc_asset_ids(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT DISTINCT asset FROM adhoc_symbols").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(r["asset"]).upper() for r in rows}


def adhoc_class_map(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT DISTINCT asset, class FROM adhoc_symbols").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r["asset"]).upper(): str(r["class"]) for r in rows}


def list_recent_adhoc(
    conn: sqlite3.Connection,
    *,
    since_ms: int,
    prefer_tf: str | None = None,
    limit: int = 20,
) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT asset, venue, tf, native_symbol, class, last_sync_at, last_access_at, window_bars
        FROM adhoc_symbols
        WHERE last_access_at IS NOT NULL AND last_access_at >= ?
        ORDER BY last_access_at DESC
        """,
        (int(since_ms),),
    ).fetchall()
    seen: set[str] = set()
    out: list[sqlite3.Row] = []
    preferred: list[sqlite3.Row] = []
    rest: list[sqlite3.Row] = []
    tf = (prefer_tf or "").upper()
    for r in rows:
        if tf and str(r["tf"]).upper() == tf:
            preferred.append(r)
        else:
            rest.append(r)
    for r in preferred + rest:
        aid = str(r["asset"]).upper()
        if aid in seen:
            continue
        seen.add(aid)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def trim_bars_before(
    conn: sqlite3.Connection,
    asset: str,
    venue: str,
    tf: str,
    min_ts: int,
) -> int:
    cur = conn.execute(
        "DELETE FROM bars WHERE asset=? AND venue=? AND tf=? AND ts < ?",
        (asset.upper(), venue, tf, int(min_ts)),
    )
    return int(cur.rowcount or 0)

