#!/usr/bin/env bash
set -euo pipefail

APP_PORT="${APP_PORT:-8093}"
SCAN_SLEEP="${SCAN_SLEEP:-120}"
SCAN_INITIAL_DELAY="${SCAN_INITIAL_DELAY:-30}"
DB_PATH="${DB_PATH:-/data/brloh.db}"

mkdir -p /data

export APP_PORT
export DB_PATH

python - <<'PY'
import os, re
from pathlib import Path

app = Path("/app/app.py")
src = app.read_text(encoding="utf-8")
src = re.sub(
    r'DB_PATH = Path\(os\.getenv\("DB_PATH", "[^"]*"\)\)',
    'DB_PATH = Path(os.getenv("DB_PATH", "/data/brloh.db"))',
    src,
    count=1,
)
app.write_text(src, encoding="utf-8")
print("Runtime patch OK: DB_PATH -> /data/brloh.db")
PY

scan_loop() {
  echo "[scan-loop] initial delay ${SCAN_INITIAL_DELAY}s"
  sleep "${SCAN_INITIAL_DELAY}"
  while true; do
    echo "[scan-loop] start $(date '+%F %T')"
    curl -sS -X POST "http://127.0.0.1:${APP_PORT}/api/scan" >/tmp/brloh-scan-response.json || true
    echo "[scan-loop] end   $(date '+%F %T')"
    sleep "${SCAN_SLEEP}"
  done
}

python /app/app.py &
APP_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/health" >/dev/null 2>&1; then
    echo "[entrypoint] app is healthy"
    break
  fi
  sleep 1
done

scan_loop &
SCAN_PID=$!

term_handler() {
  kill "${SCAN_PID}" 2>/dev/null || true
  kill "${APP_PID}" 2>/dev/null || true
  wait "${APP_PID}" 2>/dev/null || true
  wait "${SCAN_PID}" 2>/dev/null || true
}

trap term_handler TERM INT
wait "${APP_PID}"
