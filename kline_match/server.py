"""FastAPI 交易终端后端：K 线、匹配、宇宙、状态。"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kline_match.config import data_dir, db_path, load_universe, project_root
from kline_match.catalog import search_symbols
from kline_match.db import (
    connect,
    count_closed_bars,
    list_recent_adhoc,
    load_ohlcv,
    primary_of,
    status_rows,
    touch_adhoc_access,
)
from kline_match.ensure import ensure_symbol
from kline_match.ingest import ingest_is_locked, run_ingest
from kline_match.exchanges.binance import BinanceClient
from kline_match.live_scan import fetch_last_closed_klines, match_pattern
from kline_match.match import match_drawn, match_query, resample_path, resolve_weights
from kline_match.patterns import list_patterns, resolve_pattern
from kline_match.timeframes import TF_MS, ms_to_utc, normalize_tf, now_ms

MATCH_PRESETS: list[dict[str, Any]] = [
    {"id": "recommend", "name_zh": "推荐", "w_close": 0.6, "w_shape": 0.25, "w_volume": 0.15},
    {"id": "close_only", "name_zh": "仅收盘", "w_close": 1, "w_shape": 0, "w_volume": 0},
    {"id": "shape", "name_zh": "形态优先", "w_close": 0.35, "w_shape": 0.5, "w_volume": 0.15},
    {"id": "volume", "name_zh": "量价", "w_close": 0.5, "w_shape": 0, "w_volume": 0.5},
    {"id": "custom", "name_zh": "自定义", "w_close": None, "w_shape": None, "w_volume": None},
]
_PRESET_BY_ID = {p["id"]: p for p in MATCH_PRESETS if p["id"] != "custom"}

_TEMPLATES_CAP = 50
_TEMPLATES_LOCK = threading.Lock()
_TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

def select_klines_window(
    bars: list[Any],
    start_ts: int | None,
    end_ts: int | None,
    pad_after: int = 0,
) -> list[Any]:
    """Keep [start_ts, end_ts] plus ``pad_after`` bars after last ts<=end_ts.

    Index-based: find the last bar with ``ts <= end_ts``, then include the next
    ``pad_after`` bars in the series. Does not invent OHLC. If history ends,
    returns whatever exists. ``pad_after`` is ignored when ``end_ts`` is None.
    """
    if not bars:
        return []
    pad = max(0, int(pad_after or 0))
    if end_ts is None:
        stop = len(bars) - 1
    else:
        last_le = None
        for i, b in enumerate(bars):
            if int(b["ts"]) <= end_ts:
                last_le = i
        if last_le is None:
            return []
        stop = min(len(bars) - 1, last_le + pad)

    start_i = 0
    if start_ts is not None:
        start_i = len(bars)
        for i, b in enumerate(bars):
            if int(b["ts"]) >= start_ts:
                start_i = i
                break
    if start_i > stop:
        return []
    return list(bars[start_i : stop + 1])


BOOT: dict[str, Any] = {
    "state": "idle",
    "message": "",
    "started_at": None,
    "finished_at": None,
    "errors": 0,
}


class MatchRequest(BaseModel):
    asset: str = ""
    tf: str
    n: int = Field(default=30, ge=2, le=500)
    start_ts: int | None = None
    end_ts: int | None = None
    path: list[float] | None = Field(default=None, min_length=2, max_length=2000)
    pattern: str | None = None
    live: bool = True
    w_close: float = Field(default=0.6, ge=0)
    w_shape: float = Field(default=0.25, ge=0)
    w_volume: float = Field(default=0.15, ge=0)
    preset: str | None = None


class TemplateIn(BaseModel):
    name: str = ""
    tf: str
    n: int = Field(ge=2, le=500)
    path: list[float] = Field(min_length=2, max_length=2000)


class EnsureRequest(BaseModel):
    id: str | None = None
    native_symbol: str | None = None
    venue: str | None = None
    tf: str
    asset: str | None = None


def _templates_path() -> Path:
    return data_dir() / "templates.json"


def _read_templates() -> list[dict[str, Any]]:
    path = _templates_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "")
        if not _TEMPLATE_ID_RE.fullmatch(tid):
            continue
        out.append(item)
    return out


def _write_templates(items: list[dict[str, Any]]) -> None:
    path = _templates_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _conn():
    return connect(db_path())


def _web_dist() -> Path:
    return project_root() / "web" / "dist"


def _weights_from_request(req: MatchRequest, *, close_only: bool) -> tuple[float, float, float]:
    """Explicit w_* win over preset. Path/pattern force close-only (never 400)."""
    fields = req.model_fields_set
    has_w = any(k in fields for k in ("w_close", "w_shape", "w_volume"))
    if has_w:
        wc, ws, wv = float(req.w_close), float(req.w_shape), float(req.w_volume)
    elif req.preset and req.preset in _PRESET_BY_ID:
        p = _PRESET_BY_ID[req.preset]
        wc, ws, wv = float(p["w_close"]), float(p["w_shape"]), float(p["w_volume"])
    else:
        wc, ws, wv = float(req.w_close), float(req.w_shape), float(req.w_volume)
    wc, ws, wv, _norm = resolve_weights(wc, ws, wv, close_only=close_only)
    return wc, ws, wv


def _last_quote(conn, asset: str, tf: str, venue: str | None = None, native: str | None = None) -> tuple[str | None, str | None, dict[str, Any] | None]:
    row = primary_of(conn, asset, tf)
    use_venue = row["venue"] if row else venue
    use_native = row["native_symbol"] if row else native
    last = None
    if row:
        bars = load_ohlcv(conn, asset, row["venue"], tf, closed_only=True)
        if bars:
            cur = bars[-1]
            prev = bars[-2] if len(bars) > 1 else None
            close = float(cur["close"])
            prev_c = float(prev["close"]) if prev else close
            chg = (close - prev_c) / prev_c if prev_c else 0.0
            last = {
                "ts": int(cur["ts"]),
                "close": close,
                "change": chg,
                "time": ms_to_utc(int(cur["ts"])),
            }
    return use_venue, use_native, last


def _try_ensure(asset: str, tf: str, *, venue: str | None = None, native: str | None = None) -> dict[str, Any] | None:
    conn = _conn()
    try:
        return ensure_symbol(
            conn,
            asset_id=asset,
            native_symbol=native,
            venue=venue,
            tf=tf,
        )
    except Exception:
        return None
    finally:
        conn.close()


def create_app(*, boot_sync: bool = True) -> FastAPI:
    app = FastAPI(title="WAVE 波形终端", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "boot": BOOT["state"]}

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        conn = _conn()
        try:
            rows = []
            for r in status_rows(conn):
                rows.append(
                    {
                        "asset": r["asset"],
                        "venue": r["venue"],
                        "tf": r["tf"],
                        "primary": bool(r["is_primary"]),
                        "native_symbol": r["native_symbol"],
                        "bars": int(r["n_closed"] or 0),
                        "first_utc": ms_to_utc(int(r["first_ts"])) if r["first_ts"] else None,
                        "last_utc": ms_to_utc(int(r["last_bar_ts"])) if r["last_bar_ts"] else None,
                        "last_sync_utc": ms_to_utc(int(r["last_sync_at"])) if r["last_sync_at"] else None,
                    }
                )
            return {
                "boot": BOOT,
                "closed_bars": count_closed_bars(conn),
                "series": rows,
            }
        finally:
            conn.close()

    @app.get("/api/universe")
    def api_universe(tf: str = Query(default="1H")) -> dict[str, Any]:
        tf = normalize_tf(tf)
        uni = load_universe()
        conn = _conn()
        try:
            assets = []
            yaml_ids: set[str] = set()
            for spec in uni.assets:
                yaml_ids.add(spec.id.upper())
                venue, native, last = _last_quote(conn, spec.id, tf)
                if venue is None:
                    venue = spec.venues[0].venue
                if native is None:
                    native = spec.venues[0].native_symbols[0]
                assets.append(
                    {
                        "id": spec.id,
                        "class": spec.klass,
                        "venue": venue,
                        "native_symbol": native,
                        "ready": bool(last),
                        "last": last,
                        "adhoc": False,
                    }
                )
            week = now_ms() - 7 * 24 * 3600 * 1000
            for r in list_recent_adhoc(conn, since_ms=week, prefer_tf=tf, limit=20):
                aid = str(r["asset"]).upper()
                if aid in yaml_ids:
                    continue
                venue, native, last = _last_quote(
                    conn, aid, tf, venue=str(r["venue"]), native=str(r["native_symbol"])
                )
                assets.append(
                    {
                        "id": aid,
                        "class": str(r["class"] or "crypto"),
                        "venue": venue or str(r["venue"]),
                        "native_symbol": native or str(r["native_symbol"]),
                        "ready": bool(last),
                        "last": last,
                        "adhoc": True,
                    }
                )
            return {
                "tf": tf,
                "default_n": uni.default_n,
                "timeframes": uni.timeframes,
                "assets": assets,
                "boot": BOOT,
            }
        finally:
            conn.close()

    @app.get("/api/search")
    def api_search(
        q: str = Query(..., min_length=1),
        tf: str = Query(default="1H"),
    ) -> dict[str, Any]:
        q = q.strip()
        if len(q) < 1:
            raise HTTPException(400, "q 至少 1 个字符")
        try:
            tf_n = normalize_tf(tf)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        conn = _conn()
        try:
            hits = search_symbols(conn, q, tf_n)
        finally:
            conn.close()
        return {"q": q, "tf": tf_n, "hits": hits}

    @app.post("/api/symbols/ensure")
    def api_ensure(req: EnsureRequest) -> dict[str, Any]:
        try:
            tf = normalize_tf(req.tf)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        asset = (req.id or req.asset or "").strip()
        native = (req.native_symbol or "").strip() or None
        if not asset and not native:
            raise HTTPException(400, "需要 id 或 native_symbol")
        conn = _conn()
        try:
            try:
                out = ensure_symbol(
                    conn,
                    asset_id=asset or None,
                    native_symbol=native,
                    venue=(req.venue or None),
                    tf=tf,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(502, f"拉取失败: {exc}") from exc
        finally:
            conn.close()
        if not out.get("ready"):
            raise HTTPException(404, f"没有 {out.get('id') or asset} {tf} K 线")
        return out

    @app.get("/api/klines")
    def api_klines(
        asset: str,
        tf: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        closed_only: bool = False,
        pad_after: int = Query(default=0, ge=0, le=2000),
    ) -> dict[str, Any]:
        tf = normalize_tf(tf)
        asset = asset.upper()
        conn = _conn()
        try:
            row = primary_of(conn, asset, tf)
            if row is None:
                conn.close()
                ensured = _try_ensure(asset, tf)
                conn = _conn()
                row = primary_of(conn, asset, tf)
                if row is None:
                    raise HTTPException(404, f"没有 {asset} {tf} 主序列" + (f": {ensured}" if ensured else ""))
            bars = load_ohlcv(conn, asset, row["venue"], tf, closed_only=closed_only)
            chosen = select_klines_window(bars, start_ts, end_ts, pad_after)
            out = []
            for b in chosen:
                ts = int(b["ts"])
                out.append(
                    {
                        "ts": ts,
                        "time": ts // 1000,
                        "open": float(b["open"]),
                        "high": float(b["high"]),
                        "low": float(b["low"]),
                        "close": float(b["close"]),
                        "volume": float(b["volume"]),
                        "is_closed": bool(b["is_closed"]),
                    }
                )
            touch_adhoc_access(conn, asset, tf)
            conn.commit()
            return {
                "asset": asset,
                "tf": tf,
                "venue": row["venue"],
                "native_symbol": row["native_symbol"],
                "interval_ms": TF_MS[tf],
                "bars": out,
            }
        finally:
            conn.close()

    @app.get("/api/patterns")
    def api_patterns() -> dict[str, Any]:
        return {"patterns": list_patterns()}

    @app.get("/api/live_klines")
    def api_live_klines(
        symbol: str,
        tf: str,
        n: int = Query(default=40, ge=2, le=500),
    ) -> dict[str, Any]:
        tf = normalize_tf(tf)
        symbol = str(symbol or "").upper().strip()
        if not re.fullmatch(r"[A-Z0-9]{4,24}", symbol):
            raise HTTPException(400, "无效 symbol")
        try:
            client = BinanceClient()
            candles = fetch_last_closed_klines(client, symbol, tf, n, timeout=8.0)
        except Exception as exc:
            raise HTTPException(502, f"live klines 失败: {exc}") from exc
        if len(candles) < 2:
            raise HTTPException(404, f"没有 {symbol} {tf} 现价 K 线")
        window = candles[-n:] if len(candles) >= n else candles
        out = []
        for b in window:
            ts = int(b.ts)
            out.append(
                {
                    "ts": ts,
                    "time": ts // 1000,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                    "is_closed": bool(b.is_closed),
                }
            )
        return {
            "asset": symbol[:-4] if symbol.endswith("USDT") and len(symbol) > 4 else symbol,
            "tf": tf,
            "venue": "binance",
            "native_symbol": symbol,
            "interval_ms": TF_MS[tf],
            "bars": out,
            "live": True,
        }

    @app.get("/api/match/presets")
    def api_match_presets() -> dict[str, Any]:
        return {"presets": MATCH_PRESETS}

    @app.post("/api/match")
    def api_match(req: MatchRequest) -> dict[str, Any]:
        tf = normalize_tf(req.tf)
        close_only = bool(req.pattern) or req.path is not None
        try:
            wc, ws, wv = _weights_from_request(req, close_only=close_only)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        conn = _conn()
        try:
            if req.pattern:
                try:
                    resolve_pattern(req.pattern)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                bundle = match_pattern(
                    conn,
                    tf,
                    req.pattern,
                    n=req.n,
                    live=bool(req.live),
                    w_close=wc,
                    w_shape=ws,
                    w_volume=wv,
                )
            elif req.path is not None:
                arr = np.asarray(req.path, dtype=np.float64)
                if not np.isfinite(arr).all():
                    raise HTTPException(400, "path 含非有限值")
                bundle = match_drawn(conn, tf, arr, n=req.n, w_close=wc, w_shape=ws, w_volume=wv)
            else:
                if not (req.asset or "").strip():
                    raise HTTPException(400, "需要 asset 或 path 或 pattern")
                if primary_of(conn, req.asset, tf) is None:
                    conn.close()
                    _try_ensure(req.asset, tf)
                    conn = _conn()
                bundle = match_query(
                    conn,
                    req.asset,
                    tf,
                    n=req.n,
                    start_ts=req.start_ts,
                    end_ts=req.end_ts,
                    w_close=wc,
                    w_shape=ws,
                    w_volume=wv,
                )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            conn.close()
        return {
            "query": bundle.query,
            "query_z": bundle.query_z,
            "resonance": [h.as_dict() for h in bundle.resonance],
            "history": [h.as_dict() for h in bundle.history],
            "forward": bundle.forward,
        }

    @app.get("/api/templates")
    def api_templates() -> dict[str, Any]:
        with _TEMPLATES_LOCK:
            return {"templates": _read_templates()}

    @app.post("/api/templates")
    def api_templates_save(req: TemplateIn) -> dict[str, Any]:
        tf = normalize_tf(req.tf)
        arr = np.asarray(req.path, dtype=np.float64)
        if not np.isfinite(arr).all():
            raise HTTPException(400, "path 含非有限值")
        try:
            path = resample_path(arr, req.n) if arr.size != req.n else arr.astype(np.float64, copy=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        name = (req.name or "").strip() or f"手绘 {tf} N={req.n}"
        if len(name) > 64:
            name = name[:64]
        rec = {
            "id": uuid.uuid4().hex,
            "name": name,
            "tf": tf,
            "n": int(req.n),
            "path": [float(x) for x in path],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with _TEMPLATES_LOCK:
            items = _read_templates()
            if len(items) >= _TEMPLATES_CAP:
                raise HTTPException(400, f"最多保存 {_TEMPLATES_CAP} 条波形")
            items.append(rec)
            _write_templates(items)
        return rec

    @app.delete("/api/templates/{tid}")
    def api_templates_delete(tid: str) -> dict[str, Any]:
        if not _TEMPLATE_ID_RE.fullmatch(tid):
            raise HTTPException(400, "无效 id")
        with _TEMPLATES_LOCK:
            items = _read_templates()
            kept = [it for it in items if str(it.get("id")) != tid]
            if len(kept) == len(items):
                raise HTTPException(404, "模板不存在")
            _write_templates(kept)
        return {"ok": True, "id": tid}

    dist = _web_dist()
    if dist.is_dir():
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            if full_path.startswith("api"):
                raise HTTPException(404, "not found")
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            index = dist / "index.html"
            if not index.is_file():
                raise HTTPException(404, "frontend not built")
            return FileResponse(index)

    if boot_sync:
        t = threading.Thread(target=_boot_sync, name="boot-sync", daemon=True)
        t.start()
    return app


def _ingest_process_running() -> bool:
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if "kline_match ingest" in cmd or "kline_match.cli ingest" in cmd:
            return True
    return False


def _boot_sync() -> None:
    if ingest_is_locked() or _ingest_process_running():
        BOOT["state"] = "idle"
        BOOT["message"] = "外部 ingest 进行中，跳过启动同步"
        return
    conn = _conn()
    try:
        n = count_closed_bars(conn)
        mode = "full" if n == 0 else "incremental"
        BOOT.update(
            {
                "state": "ingesting" if mode == "full" else "syncing",
                "message": f"{mode} 宇宙数据",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
            }
        )
        report = run_ingest(conn, mode=mode)
        BOOT["errors"] = len(report.errors)
        BOOT["message"] = f"完成 {len(report.ok)} 项，失败 {len(report.errors)}"
    except Exception as exc:
        BOOT["state"] = "error"
        BOOT["message"] = str(exc)
        BOOT["finished_at"] = datetime.now(timezone.utc).isoformat()
        return
    finally:
        conn.close()
    BOOT["state"] = "idle"
    BOOT["finished_at"] = datetime.now(timezone.utc).isoformat()


def run_server(host: str = "0.0.0.0", port: int = 18765, boot_sync: bool = True) -> None:
    import uvicorn

    app = create_app(boot_sync=boot_sync)
    uvicorn.run(app, host=host, port=port, log_level="info")
