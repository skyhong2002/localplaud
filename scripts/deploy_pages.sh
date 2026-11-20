#!/usr/bin/env bash
# Publish a built site directory (from scripts/build_pages.sh) to the gh-pages
# branch as a single fresh commit, so the demo snapshot never bloats history.
#
#   scripts/deploy_pages.sh <site-dir>
set -euo pipefail
site="$(cd "${1:?usage: deploy_pages.sh <site-dir>}" && pwd)"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

remote="$(git -C "$here" remote get-url origin)"
git -C "$work" init -q -b gh-pages
cp -R "$site"/. "$work"/
git -C "$work" add -A
git -C "$work" -c user.name="$(git -C "$here" config user.name)" \
  -c user.email="$(git -C "$here" config user.email)" \
  commit -q -m "Publish site from $(git -C "$here" rev-parse --short HEAD)"
git -C "$work" push -q -f "$remote" gh-pages
echo "Pushed $(du -sh "$site" | cut -f1) to gh-pages"
