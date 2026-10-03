#!/usr/bin/env bash
# Rebuild the Syslinux payload (stoke/payload/syslinux) from source.
#
# The 6.04-pre1 release tarball is too old: its gfxboot.c32 cannot load the
# kernel of Ubuntu 20.04 ("live: file not found"), and .c32 modules must
# match ldlinux.sys exactly, so everything comes from one build. This is
# the git snapshot Arch Linux packages, with Arch's build fixes.
#
#   tools/build-syslinux.sh            (needs git, nasm, gcc, make, python)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMIT=05ac953c23f90b2328d393f7eecde96e41aed067
REVERT=458a54133ecdf1685c02294d812cb562fe7bf4c3
PATCHES=(0002-gfxboot-menu-label 0017-single-load-segment 0016-strip-gnu-property
         0018-prevent-pow-optimization 0025-reproducible-build
         0005-Workaround-multiple-definition-of-symbol-errors
         0006-Replace-builtin-strlen-that-appears-to-get-optimized
         0026-add-missing-include 0027-use-correct-type-for-size)
ARCH_RAW=https://gitlab.archlinux.org/archlinux/packaging/packages/syslinux/-/raw/main
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
git clone -q https://repo.or.cz/syslinux.git
cd syslinux
git checkout -q "$COMMIT"
git revert -n "$REVERT"
for p in "${PATCHES[@]}"; do
    curl -sSL -o "../$p.patch" "$ARCH_RAW/$p.patch"
    patch -p1 -s < "../$p.patch"
done
truncate --size 0 mk/devel.mk
export LDFLAGS="${LDFLAGS:-}--no-dynamic-linker" EXTRA_CFLAGS=-fno-PIE
make -j"$(nproc)" PYTHON=python bios > build.log 2>&1 || { tail -30 build.log; exit 1; }
OUT="$ROOT/stoke/payload/syslinux"
rm -rf "$OUT"; mkdir -p "$OUT"
cp bios/core/ldlinux.sys bios/core/ldlinux.bss "$OUT/"
find bios/com32 -name '*.c32' -exec cp {} "$OUT/" \;
cp COPYING "$OUT/COPYING"
git describe --long | sed 's/^syslinux-//;s/\([^-]*-g\)/r\1/;s/-/./g' > "$OUT/VERSION"
echo "Syslinux $(cat "$OUT/VERSION"): $(ls "$OUT" | wc -l) files in $OUT"
