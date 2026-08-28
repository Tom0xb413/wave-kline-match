#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export KLINE_MATCH_ROOT="$ROOT"
PORT="${PORT:-18765}"
HOST="${HOST:-127.0.0.1}"
LOGDIR="$ROOT/logs"
BIN="$ROOT/bin"
VENV="$ROOT/.venv"
mkdir -p "$LOGDIR" "$BIN"

is_up() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] || return 1
  local pid
  pid="$(cat "$pidfile")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

if ! is_up "$LOGDIR/serve.pid"; then
  nohup "$VENV/bin/python" -m kline_match serve --host "$HOST" --port "$PORT" > "$LOGDIR/serve.log" 2>&1 &
  echo $! > "$LOGDIR/serve.pid"
  echo "started serve pid $(cat "$LOGDIR/serve.pid")"
else
  echo "serve already running pid $(cat "$LOGDIR/serve.pid")"
fi

for i in $(seq 1 40); do
  if curl -fsS "http://${HOST}:${PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if ! is_up "$LOGDIR/cloudflared.pid"; then
  : > "$LOGDIR/cloudflared.log"
  nohup "$BIN/cloudflared" tunnel --url "http://${HOST}:${PORT}" > "$LOGDIR/cloudflared.log" 2>&1 &
  echo $! > "$LOGDIR/cloudflared.pid"
  echo "started cloudflared pid $(cat "$LOGDIR/cloudflared.pid")"
else
  echo "cloudflared already running pid $(cat "$LOGDIR/cloudflared.pid")"
fi

URL=""
for i in $(seq 1 60); do
  URL="$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOGDIR/cloudflared.log" | tail -n 1 || true)"
  if [[ -n "$URL" ]]; then
    break
  fi
  sleep 0.5
done

if [[ -z "$URL" ]]; then
  echo "ERROR: could not parse trycloudflare.com URL from $LOGDIR/cloudflared.log" >&2
  exit 1
fi

printf '%s\n' "$URL" > "$ROOT/PUBLIC_URL.txt"
echo "PUBLIC_URL=$URL"
echo "serve_pid=$(cat "$LOGDIR/serve.pid")"
echo "cloudflared_pid=$(cat "$LOGDIR/cloudflared.pid")"
