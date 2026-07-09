#!/usr/bin/env bash
#
# localplaud smoke test — checks that the API answers.
#
# Usage:
#   scripts/deploy/smoke-test.sh [base-url]     # default: http://localhost:8080
#   scripts/deploy/smoke-test.sh https://plaud.example.com
set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"
BASE_URL="${BASE_URL%/}"
FAILED=0

check() {
  local path="$1" url code
  url="${BASE_URL}${path}"
  # curl prints the http_code even on connection failure (as 000).
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" || true)"
  [ -n "$code" ] || code="000"
  if [ "$code" = "200" ]; then
    printf 'ok    %-40s HTTP %s\n' "$url" "$code"
  else
    printf 'FAIL  %-40s HTTP %s\n' "$url" "$code"
    FAILED=1
  fi
}

check /healthz
check /

if [ "$FAILED" -ne 0 ]; then
  echo "FAIL: localplaud at $BASE_URL is not healthy" >&2
  exit 1
fi
echo "PASS: localplaud at $BASE_URL is healthy"
