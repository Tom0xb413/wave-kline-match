"""周期常量与时间换算。

只支持 1H / 4H / 12H / 1D。匹配时周期必须完全相同，禁止跨周期。
"""

from __future__ import annotations

from datetime import datetime, timezone

TIMEFRAMES: tuple[str, ...] = ("1H", "4H", "12H", "1D")

TF_MS: dict[str, int] = {
    "1H": 3_600_000,
    "4H": 14_400_000,
    "12H": 43_200_000,
    "1D": 86_400_000,
}

_ALIASES: dict[str, str] = {
    "1h": "1H",
    "4h": "4H",
    "12h": "12H",
    "1d": "1D",
    "h": "1H",
    "d": "1D",
    "1H": "1H",
    "4H": "4H",
    "12H": "12H",
    "1D": "1D",
}


def normalize_tf(value: str) -> str:
    """把用户输入规范成 1H/4H/12H/1D。"""
    key = str(value).strip()
    tf = _ALIASES.get(key) or _ALIASES.get(key.upper()) or _ALIASES.get(key.lower())
    if tf not in TF_MS:
        raise ValueError(f"不支持的周期 {value!r}，可选: {', '.join(TIMEFRAMES)}")
    return tf


def tf_ms(tf: str) -> int:
    return TF_MS[normalize_tf(tf)]


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ms_to_utc(ms: int) -> str:
    """毫秒时间戳 → UTC ``YYYY-MM-DD HH:MM:SS``。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_depth(spec: str | None, now: int) -> int | None:
    """深度配置转起始毫秒。``full`` 表示不截断（返回 None）。"""
    if spec is None:
        return None
    text = str(spec).strip().lower()
    if text in {"full", "all", "max"}:
        return None
    if text.endswith("y") and text[:-1].isdigit():
        years = int(text[:-1])
        return now - years * 365 * 24 * 3600 * 1000
    if text.endswith("d") and text[:-1].isdigit():
        days = int(text[:-1])
        return now - days * 24 * 3600 * 1000
    raise ValueError(f"无法解析历史深度: {spec!r}")
