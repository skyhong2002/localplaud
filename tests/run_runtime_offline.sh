#!/bin/sh
# Usage: LOCALPLAUD_TEST_PYTHON=/path/to/venv/bin/python sh tests/run_runtime_offline.sh [pytest args]
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_python=${LOCALPLAUD_TEST_PYTHON:-python3}
test_root=$(mktemp -d "${TMPDIR:-/tmp}/localplaud-runtime-tests.XXXXXX")
trap 'rm -rf "$test_root"' EXIT HUP INT TERM
cd "$repo"
if [ "$#" -eq 0 ]; then
    set -- -q tests/test_runtime_startup_guard.py tests/test_subprocesses.py \
        tests/test_runtime_admission.py tests/test_daemon_cycle.py
fi
# Drop inherited deployment settings/credentials; never load a production .env.
env -i PATH="$PATH" \
    PYTHONPATH="$repo/tests/runtime_guard:$repo/src:$repo" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    LOCALPLAUD_CONFIG="$test_root/absent-config.toml" \
    LOCALPLAUD_STORE__DATABASE_URL="sqlite:///$test_root/runtime.db" \
    LOCALPLAUD_POLLER__DOWNLOAD_DIR="$test_root/audio" \
    "$test_python" -m pytest -p no:cacheprovider -p tests.runtime_offline "$@"
