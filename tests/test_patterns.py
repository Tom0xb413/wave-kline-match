"""Cup-and-handle canonical path and light structural gate."""

from __future__ import annotations

import numpy as np
import pytest

from kline_match.match import mass_pearson, zscore
from kline_match.patterns import (
    cup_handle_path,
    list_patterns,
    passes_cup_handle_gate,
    resolve_pattern,
)


def test_cup_handle_path_length_and_shape() -> None:
    p = cup_handle_path(40)
    assert p.shape == (40,)
    assert np.isfinite(p).all()
    cup_n = int(40 * 0.70)
    bottom = int(np.argmin(p[:cup_n]))
    assert bottom < int(40 * 0.72)
    lo, hi = float(p.min()), float(p.max())
    mid = (lo + hi) / 2.0
    assert p[-1] > mid
    # cup bottoms in the first 70%, not a V spike at an endpoint
    assert 2 <= bottom <= cup_n - 2
    # handle is shallow: last 28% stays in upper half
    handle = p[int(40 * 0.72) :]
    assert float(handle.min()) >= lo + 0.5 * (hi - lo)


def test_cup_handle_self_pearson_is_one() -> None:
    p = cup_handle_path(40)
    r = mass_pearson(p, p)
    assert r.shape == (1,)
    assert r[0] == pytest.approx(1.0, abs=1e-8)
    # z-score of the path against itself via corrcoef
    z = zscore(p)
    assert np.corrcoef(z, z)[0, 1] == pytest.approx(1.0, abs=1e-8)


def test_list_patterns_includes_cup_handle() -> None:
    items = list_patterns()
    ids = [p["id"] for p in items]
    assert "cup_handle" in ids
    spec = next(p for p in items if p["id"] == "cup_handle")
    assert spec["name_zh"] == "杯柄"
    assert spec["suggested_n"] == 40
    assert resolve_pattern("cup_handle").id == "cup_handle"
    with pytest.raises(ValueError, match="未知形态"):
        resolve_pattern("not_a_pattern")


def test_canonical_path_passes_gate() -> None:
    p = cup_handle_path(40)
    assert passes_cup_handle_gate(p)
    assert passes_cup_handle_gate(zscore(p))


def test_handle_collapse_fails_gate() -> None:
    p = cup_handle_path(40)
    bad = p.copy()
    bad[-3:] = p.min() - 0.4
    assert not passes_cup_handle_gate(bad)
