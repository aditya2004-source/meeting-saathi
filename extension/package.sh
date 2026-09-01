#!/usr/bin/env bash
# Build the shareable extension ZIP. Ships only the files Chrome loads + the
# setup guide — no node_modules, no tests, no package.json.
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(grep -oE '"version": *"[^"]+"' manifest.json | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
OUT="../dist/meeting-saathi-v${VERSION}.zip"
mkdir -p ../dist
rm -f "$OUT"

zip -r "$OUT" \
  manifest.json \
  background.js content_script.js offscreen.js offscreen.html \
  popup.html popup.js settings.html settings.js \
  dashboard.html dashboard.js dashboard.css \
  permissions.html permissions.js \
  lib/*.js vendor/*.js \
  icon16.png icon48.png icon128.png \
  SETUP-GUIDE.md \
  -x '*/.*' >/dev/null

echo "built $OUT"
unzip -l "$OUT" | tail -n +4 | head -n -2 | awk '{print "  " $4}'
echo
echo "size: $(du -h "$OUT" | cut -f1)"
