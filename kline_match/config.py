"""读取 ``config/universe.yaml`` 并定位数据目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kline_match.timeframes import TIMEFRAMES, normalize_tf


def project_root() -> Path:
    """仓库根目录：优先环境变量，其次含 ``config/universe.yaml`` 的祖先路径。"""
    env = os.environ.get("KLINE_MATCH_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent.parent
    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents, here, *here.parents):
        if (cand / "config" / "universe.yaml").exists():
            return cand
    return here


def data_dir(root: Path | None = None) -> Path:
    path = (root or project_root()) / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(root: Path | None = None) -> Path:
    env = os.environ.get("KLINE_MATCH_DB")
    if env:
        p = Path(env)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return data_dir(root) / "kline_match.db"


@dataclass(slots=True)
class VenueSpec:
    venue: str
    native_symbols: list[str]
    primary: bool


@dataclass(slots=True)
class AssetSpec:
    id: str
    klass: str
    venues: list[VenueSpec]


@dataclass(slots=True)
class Universe:
    timeframes: list[str]
    default_n: int
    topk: int
    depths: dict[str, str]
    assets: list[AssetSpec] = field(default_factory=list)

    def asset(self, asset_id: str) -> AssetSpec:
        key = asset_id.upper()
        for item in self.assets:
            if item.id.upper() == key:
                return item
        known = ", ".join(a.id for a in self.assets)
        raise KeyError(f"宇宙中没有资产 {asset_id!r}。已知: {known}")


def _as_symbols(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw]


def load_universe(path: Path | None = None) -> Universe:
    cfg_path = path or (project_root() / "config" / "universe.yaml")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    tfs = [normalize_tf(x) for x in raw.get("timeframes", TIMEFRAMES)]
    depths = {normalize_tf(k): str(v) for k, v in (raw.get("depths") or {}).items()}
    assets: list[AssetSpec] = []
    for item in raw.get("assets") or []:
        venues: list[VenueSpec] = []
        for v in item.get("venues") or []:
            venues.append(
                VenueSpec(
                    venue=str(v["venue"]).lower(),
                    native_symbols=_as_symbols(v.get("native_symbol")),
                    primary=bool(v.get("primary", False)),
                )
            )
        assets.append(
            AssetSpec(
                id=str(item["id"]).upper(),
                klass=str(item.get("class", "crypto")),
                venues=venues,
            )
        )
    return Universe(
        timeframes=tfs,
        default_n=int(raw.get("default_n", 30)),
        topk=int(raw.get("topk", 10)),
        depths=depths,
        assets=assets,
    )
