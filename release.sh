#!/usr/bin/env bash
# Build the Stoke packages from this tree into dist/, for a distribution
# to import into its repository. The packages carry Stoke's own version
# (the PKGBUILD pkgver, matching the git tag), not the consumer's.
#
#   ./release.sh            -> dist/stoke-<ver>-any.pkg.tar.zst, dist/stoke-qt-<ver>-x86_64.pkg.tar.zst,
#                              dist/stoke-gtk-<ver>-any.pkg.tar.zst
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$ROOT/dist"; mkdir -p "$DIST"
for p in stoke stoke-qt stoke-gtk; do
    (cd "$ROOT/$p" && rm -rf src pkg && PKGDEST="$DIST" makepkg -f --noconfirm -s && rm -rf src pkg)
done
rm -f "$DIST"/*-debug-*.pkg.tar.zst
ls -1 "$DIST"
