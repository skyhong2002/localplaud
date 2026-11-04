#!/usr/bin/env bash
# Assemble the GitHub Pages site: landing page + read-only static demo.
#
#   scripts/build_pages.sh <out-dir> [demo-origin]
#
# Requires a running demo server (scripts/serve_demo.sh) at demo-origin, default
# http://127.0.0.1:8792, serving ONLY the synthetic library from seed_demo.py.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:?usage: build_pages.sh <out-dir> [demo-origin]}"
origin="${2:-http://127.0.0.1:8792}"
shots="$here/docs/assets/screenshots"
kit="$here/docs/launch/product-hunt"

rm -rf "$out"
mkdir -p "$out/assets"
cp "$here/website/index.html" "$here/website/404.html" "$out/"
touch "$out/.nojekyll"
cp "$here/src/localplaud/api/static/wordmark.svg" "$here/src/localplaud/api/static/favicon.svg" "$out/assets/"
cp "$kit/demo.mp4" "$out/assets/demo.mp4"
cp "$kit/gallery/1-hero.png" "$out/assets/og.png"
for name in hero speakers notes mindmap ask mobile; do
  cp "$shots/$name.webp" "$out/assets/$name.webp"
done
PYTHONUNBUFFERED=1 uv run --no-project --with playwright python "$here/scripts/build_static_demo.py" \
  --origin "$origin" --out "$out" --base /localplaud/demo --pages-root /localplaud
echo "Built $out"
