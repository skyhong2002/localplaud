#!/usr/bin/env bash
#
# Read-only smoke test for the service and, optionally, a signed Android APK.
#
# Usage:
#   scripts/deploy/smoke-test.sh [base-url]     # default: http://localhost:8080
#   scripts/deploy/smoke-test.sh https://plaud.example.com
#   scripts/deploy/smoke-test.sh https://plaud.example.com APK_CERT_SHA256
set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"
BASE_URL="${BASE_URL%/}"
ANDROID_SHA256="${2:-}"
if [[ -n "$ANDROID_SHA256" && "$BASE_URL" != https://* ]]; then
  echo "Android origin verification requires an HTTPS base URL." >&2
  exit 2
fi
scratch="$(mktemp -d)"
trap 'rm -r -- "$scratch"' EXIT
FAILED=0

check() {
  local path="$1" url code accept=text/html
  url="${BASE_URL}${path}"
  if [[ "$path" == /.well-known/assetlinks.json ]]; then accept=application/json; fi
  if code="$(curl -q -sS -H "Accept: $accept" -o "$scratch/body" -D "$scratch/headers" -w '%{http_code}' --max-time 10 "$url")"; then
    case "$path:$code" in
      *:200) printf 'ok    %-40s HTTP %s\n' "$url" "$code"; return ;;
      /:401) printf 'ok    %-40s HTTP %s (authentication required)\n' "$url" "$code"; return ;;
      /:303)
        if grep -qiE '^location: /login([?[:space:]]|$)' "$scratch/headers"; then
          printf 'ok    %-40s HTTP %s (sign-in redirect)\n' "$url" "$code"
          check /login
          return
        fi ;;
    esac
  else
    code="${code:-000} (transfer failed)"
  fi
  printf 'FAIL  %-40s HTTP %s\n' "$url" "$code"
  FAILED=1
}

check /healthz
check /

if [[ -n "$ANDROID_SHA256" ]]; then
  check /.well-known/assetlinks.json
  if ! grep -qiE '^content-type: application/json([;[:space:]]|$)' "$scratch/headers" || ! python3 - "$scratch/body" "$ANDROID_SHA256" <<'PY'
import json
import re
import sys


def normalize(digest):
    return digest.replace(":", "").upper()


expected = normalize(sys.argv[2])
try:
    with open(sys.argv[1], encoding="utf-8") as response:
        statements = json.load(response)
    matched = isinstance(statements, list) and re.fullmatch(r"[0-9A-F]{64}", expected) and any(
        "delegate_permission/common.handle_all_urls" in statement["relation"]
        and (target := statement["target"])["namespace"] == "android_app"
        and target["package_name"] == "org.localplaud.android"
        and expected in map(normalize, target["sha256_cert_fingerprints"])
        for statement in statements
        if isinstance(statement, dict)
        and isinstance(statement.get("relation"), list)
        and isinstance(statement.get("target"), dict)
        and isinstance(statement["target"].get("sha256_cert_fingerprints"), list)
    )
except (AttributeError, KeyError, OSError, TypeError, ValueError):
    matched = False
sys.exit(0 if matched else 1)
PY
  then
    echo 'FAIL: assetlinks.json must be JSON and authorize the supplied APK certificate for org.localplaud.android.' >&2
    FAILED=1
  fi
fi

if [ "$FAILED" -ne 0 ]; then
  echo "FAIL: localplaud at $BASE_URL did not pass the smoke test" >&2
  exit 1
fi
echo "PASS: localplaud at $BASE_URL passed the smoke test"
