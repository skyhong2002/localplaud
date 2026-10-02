#!/usr/bin/env bash
# Serve the localplaud Web App from a worktree against a synthetic demo library.
#
#   scripts/serve_demo.sh <port> [checkout] [demo-dir] [state-dir]
#
# <checkout> defaults to the repository containing this script; its src/ goes
# first on PYTHONPATH because a shared venv's editable install may point at a
# different checkout. The demo library (built by scripts/seed_demo.py) is
# treated as read-only: its database is copied to <state-dir> (default
# /tmp/lp-demo-<port>) on every start unless KEEP=1, so edits made while testing
# never leak between users of the same demo. Audio is shared by absolute path.
# Plaud OAuth token paths and API base are redirected, so nothing reaches Plaud.
set -euo pipefail

port="${1:?usage: serve_demo.sh <port> [checkout] [demo-dir] [state-dir]}"
here="$(cd "${2:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
demo="${3:-/tmp/lp-tools/demo}"
state="${4:-/tmp/lp-demo-${port}}"
python="${PYTHON:-$here/.venv/bin/python}"
if [[ ! -x "$python" ]]; then
  echo "Set PYTHON to the project's virtualenv Python executable." >&2
  exit 1
fi

mkdir -p "$state"
if [[ "${KEEP:-0}" != "1" || ! -f "$state/localplaud.db" ]]; then
  rm -f "$state"/localplaud.db*
  # sqlite3's online backup produces a consistent copy even if WAL is present.
  "$python" - "$demo/localplaud.db" "$state/localplaud.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
fi

cd "$state"
export PYTHONPATH="$here/src"
export LOCALPLAUD_CONFIG="$state/no-config.toml"
export LOCALPLAUD_STORE__DATABASE_URL="sqlite:///$state/localplaud.db"
export LOCALPLAUD_POLLER__DOWNLOAD_DIR="$demo/audio"
export LOCALPLAUD_POLLER__ENABLED=false
export LOCALPLAUD_API__HOST=127.0.0.1
export LOCALPLAUD_API__PORT="$port"
export LOCALPLAUD_API__LOGIN_PASSWORD=
export LOCALPLAUD_API__SESSION_SECRET=
export LOCALPLAUD_API__AUTH_TOKEN=
# Never use the operator's real Plaud OAuth tokens (~/.plaud) or reach Plaud.
export LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH="$state/no-plaud-tokens.json"
export LOCALPLAUD_PLAUD__OFFICIAL__API_BASE=http://127.0.0.1:9
export LOCALPLAUD_PLAUD__MCP__TOKENS_PATH="$state/no-plaud-tokens-mcp.json"
export LOCALPLAUD_PLAUD__MCP__COMMAND=false
exec "$python" -m localplaud.cli serve
