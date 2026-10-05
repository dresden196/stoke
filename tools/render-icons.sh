#!/usr/bin/env bash
# Render stoke/icons/stoke.svg to the PNG sizes an icon theme looks for,
# into stoke/icons/hicolor/<size>x<size>/apps/stoke.png. The PNGs are
# committed: every toolkit then shows the same pixels, whatever its SVG
# renderer supports (the mark uses a mask and gradients on strokes).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVG="$ROOT/stoke/icons/stoke.svg"
for s in 16 22 24 32 48 64 96 128 256 512; do
    d="$ROOT/stoke/icons/hicolor/${s}x${s}/apps"
    mkdir -p "$d"
    rsvg-convert -w "$s" -h "$s" "$SVG" -o "$d/stoke.png"
done
echo "rendered $(find "$ROOT/stoke/icons/hicolor" -name stoke.png | wc -l) sizes"
