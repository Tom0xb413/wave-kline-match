from __future__ import annotations

import pytest

from kline_match.sectors import clear_adhoc_overlay


@pytest.fixture(autouse=True)
def _clear_adhoc_overlay():
    clear_adhoc_overlay()
    yield
    clear_adhoc_overlay()
