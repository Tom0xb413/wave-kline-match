#!/usr/bin/env bash
# Hourly incremental sync into the live WAVE DB (closed bars only).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export KLINE_MATCH_ROOT="$ROOT"
exec "$ROOT/.venv/bin/python" -m kline_match sync
