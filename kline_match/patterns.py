"""Canonical close-path templates for 形态快捷匹配.

Paths are noise-free synthetic closes; MASS/Pearson z-score normalizes level
and amplitude. End of a pattern is the buy/POISED point, not the breakout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from kline_match.models import MatchHit

_EPS = 1e-12


@dataclass(frozen=True)
class PatternSpec:
    id: str
    name_zh: str
    suggested_n: int
    path_fn: Callable[[int], np.ndarray]
    query_asset: str = "PATTERN"


def cup_handle_path(n: int) -> np.ndarray:
    """Textbook cup-and-handle close path of length ``n`` (n>=16).

    t in [0, 1]: cup occupies ~0–0.72, handle ~0.72–1.0.
    Cup is a smooth raised-cosine U (left rim 1.0, bottom ~0.62 ≈ 38% depth,
    right rim ~0.98). Handle drifts to ~0.90 then lifts to ~0.93 (shallow,
    upper third). Series ends at the end of the handle — poised for breakout,
    no surge — so 当前共振 names are sitting at the buy point.
    """
    n = int(n)
    if n < 16:
        raise ValueError("杯柄路径至少 16 点")
    t = np.linspace(0.0, 1.0, n)
    cup_end = 0.72
    y = np.empty(n, dtype=np.float64)

    cup_mask = t <= cup_end
    u = np.clip(t[cup_mask] / cup_end, 0.0, 1.0)
    rim = 1.0 + (0.98 - 1.0) * u
    # 1 at both rims, 0 at the bowl: raised cosine, not a V
    bowl = 0.5 * (1.0 + np.cos(2.0 * np.pi * u))
    y[cup_mask] = 0.62 + (rim - 0.62) * bowl

    h_mask = ~cup_mask
    uh = (t[h_mask] - cup_end) / (1.0 - cup_end)
    bottom_u = 0.62
    y_h = np.empty_like(uh)
    down = uh <= bottom_u
    w_down = np.clip(uh[down] / bottom_u, 0.0, 1.0)
    y_h[down] = 0.98 + (0.90 - 0.98) * 0.5 * (1.0 - np.cos(np.pi * w_down))
    up = ~down
    w_up = np.clip((uh[up] - bottom_u) / (1.0 - bottom_u), 0.0, 1.0)
    y_h[up] = 0.90 + (0.93 - 0.90) * 0.5 * (1.0 - np.cos(np.pi * w_up))
    y[h_mask] = y_h
    return y


PATTERNS: dict[str, PatternSpec] = {
    "cup_handle": PatternSpec(
        id="cup_handle",
        name_zh="杯柄",
        suggested_n=40,
        path_fn=cup_handle_path,
        query_asset="CUP_HANDLE",
    ),
}


def resolve_pattern(pattern_id: str) -> PatternSpec:
    key = str(pattern_id or "").strip()
    spec = PATTERNS.get(key)
    if spec is None:
        known = ", ".join(PATTERNS)
        raise ValueError(f"未知形态 {pattern_id!r}。可选: {known}")
    return spec


def pattern_path(pattern_id: str, n: int) -> np.ndarray:
    spec = resolve_pattern(pattern_id)
    n = int(n)
    if n < 16:
        n = spec.suggested_n
    return spec.path_fn(n)


def list_patterns() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in PATTERNS.values():
        path = spec.path_fn(spec.suggested_n)
        out.append(
            {
                "id": spec.id,
                "name_zh": spec.name_zh,
                "suggested_n": spec.suggested_n,
                "path": [float(x) for x in path],
            }
        )
    return out


def passes_cup_handle_gate(y: np.ndarray) -> bool:
    """Light structural checks on a candidate window (raw close or z-score).

    Does not replace Pearson. Failures with very high r are kept by the caller.
    """
    y = np.asarray(y, dtype=np.float64).ravel()
    n = int(y.size)
    if n < 16:
        return False
    cup_n = max(2, int(n * 0.70))
    handle_start = min(n - 1, int(n * 0.72))
    lo = float(np.min(y))
    hi = float(np.max(y))
    span = hi - lo
    if span < _EPS:
        return False
    # Cup bottom (min of first 70%) before the handle region (~last 28%).
    bottom = int(np.argmin(y[:cup_n]))
    if bottom >= handle_start:
        return False
    # Handle should not print a new collapse (global min in the handle).
    if int(np.argmin(y)) >= handle_start:
        return False
    handle = y[handle_start:]
    if handle.size and float(np.min(handle)) < lo + 0.5 * span:
        return False
    # Last close in the upper 45% of the window range.
    if float(y[-1]) < lo + 0.55 * span:
        return False
    return True


def keep_despite_gate(r: float, threshold: float = 0.92) -> bool:
    return float(r) >= float(threshold)


def gate_ok(hit: MatchHit, *, r_keep: float = 0.92) -> bool:
    z = np.asarray(hit.zscore, dtype=np.float64) if hit.zscore else np.empty(0)
    if z.size >= 16 and passes_cup_handle_gate(z):
        return True
    return keep_despite_gate(hit.pearson_r, r_keep)


def filter_resonance(hits: list[MatchHit], *, topk: int = 10, r_keep: float = 0.92) -> list[MatchHit]:
    """Drop structurally-bad resonance windows unless r is very high."""
    kept = [h for h in hits if gate_ok(h, r_keep=r_keep)]
    kept.sort(key=lambda h: (-float(h.pearson_r), h.asset, h.start_ts))
    out = kept[: int(topk)]
    for i, h in enumerate(out, start=1):
        h.rank = i
    return out


def prefer_gated_history(hits: list[MatchHit], *, topk: int = 10, r_keep: float = 0.92) -> list[MatchHit]:
    """Keep Pearson order but demote windows that fail the light gate (unless r high)."""
    passers: list[MatchHit] = []
    failers: list[MatchHit] = []
    for h in hits:
        (passers if gate_ok(h, r_keep=r_keep) else failers).append(h)
    passers.sort(key=lambda h: (-float(h.pearson_r), h.asset, h.start_ts))
    failers.sort(key=lambda h: (-float(h.pearson_r), h.asset, h.start_ts))
    ordered = passers + failers
    out = ordered[: int(topk)]
    for i, h in enumerate(out, start=1):
        h.rank = i
    return out
