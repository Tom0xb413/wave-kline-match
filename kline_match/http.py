"""带重试与 429 退避的公共 HTTP GET。"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "kline-match/0.1 (public-market-data; +local-research)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 40.0,
    retries: int = 6,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET JSON。对 429/5xx/网络错误指数退避；其他 4xx 立即失败。"""
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
    hdrs = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise HttpError(f"非 JSON 响应 {url}: {raw[:200]!r}") from exc
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            status = int(exc.code)
            if status in {429, 418} or status >= 500:
                last_err = HttpError(f"HTTP {status} {url} {body[:300]}", status=status, body=body)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = _delay(attempt, retry_after)
                time.sleep(delay)
                continue
            raise HttpError(f"HTTP {status} {url} {body[:500]}", status=status, body=body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            time.sleep(_delay(attempt, None))
            continue
    raise HttpError(f"请求失败（已重试）: {url}: {last_err}") from last_err


def _delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.5, float(retry_after)))
        except ValueError:
            pass
    return min(32.0, (0.4 * (2**attempt)) + random.uniform(0.05, 0.25))
