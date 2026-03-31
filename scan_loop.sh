#!/usr/bin/env bash
set -u

API_URL="http://127.0.0.1:8093/api/scan"
SLEEP_AFTER_SUCCESS=120
SLEEP_AFTER_ERROR=120

echo "[scan-loop] started at $(date '+%F %T')"

while true; do
  START_TS="$(date +%s)"
  echo "[scan-loop] scan start $(date '+%F %T')"

  HTTP_CODE="$(curl -sS -o /tmp/brloh-scan-response.json -w '%{http_code}' -X POST "$API_URL" || echo '000')"

  END_TS="$(date +%s)"
  DURATION="$((END_TS - START_TS))"

  echo "[scan-loop] scan end $(date '+%F %T') | duration=${DURATION}s | http=${HTTP_CODE}"

  if [ -f /tmp/brloh-scan-response.json ]; then
    echo "[scan-loop] response:"
    cat /tmp/brloh-scan-response.json
    echo
  fi

  if [ "$HTTP_CODE" = "200" ]; then
    echo "[scan-loop] sleeping ${SLEEP_AFTER_SUCCESS}s before next run"
    sleep "$SLEEP_AFTER_SUCCESS"
  else
    echo "[scan-loop] scan failed, sleeping ${SLEEP_AFTER_ERROR}s before retry"
    sleep "$SLEEP_AFTER_ERROR"
  fi
done
