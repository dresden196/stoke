#!/usr/bin/env bash
# Run one Stoke scenario against a loop-backed disk image on this machine,
# then boot the result under BIOS and/or UEFI. Needs root for the write
# (loop devices, mounts) and a kernel with vfat/ntfs3/exfat available; no
# guest VM. Much lighter than vm.sh, and what to reach for first.
#
#   sudo tests/usb/native.sh windows-fat32 ~/Downloads/Win11.iso
#   sudo tests/usb/native.sh live-iso ~/Downloads/archlinux.iso
#   sudo tests/usb/native.sh freedos
#   sudo tests/usb/native.sh ubuntu-iso ~/Downloads/ubuntu.iso
#   sudo tests/usb/native.sh custom /path/img.iso -- --scheme gpt --target uefi --fs ntfs
#
# The disk image is tests/usb/out/native-<scenario>.img (sparse, 32 GB).
# Screenshots land next to it. Set STOKE_NO_BOOT=1 to skip the boot checks,
# STOKE_OUT=<dir> to run two benches side by side (each needs its own QMP socket).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="${STOKE_OUT:-$HERE/out}"
SCN="${1:?scenario}"; IMAGE="${2:-}"; shift $(( $# >= 2 ? 2 : $# ))
[[ "${1:-}" == "--" ]] && shift
[[ $(id -u) == 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
CALLER="${SUDO_USER:-$USER}"

mkdir -p "$OUT"
IMG="$OUT/native-$SCN.img"
rm -f "$IMG"; truncate -s "${STOKE_STICK_SIZE:-32G}" "$IMG"
LOOP=$(losetup -f --show -P "$IMG")
trap 'losetup -d "$LOOP" 2>/dev/null || true; chown "$CALLER" "$OUT"/* 2>/dev/null || true' EXIT
export STOKE_LIB="$ROOT/stoke" STOKE_PAYLOAD="$ROOT/stoke/payload" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/stoke"

case "$SCN" in
    windows-fat32) args=(--image "$IMAGE" --mode iso --scheme mbr --target dual --fs fat32 --windows-option bypass_requirements --windows-option no_online_account) ;;
    windows-uefi)  args=(--image "$IMAGE" --mode iso --scheme gpt --target uefi --fs ntfs --windows-option bypass_requirements --windows-option no_online_account) ;;
    wintogo)       args=(--image "$IMAGE" --mode iso --wintogo 1 --scheme mbr --target dual --fs ntfs --windows-option offline_internal_drives) ;;
    ubuntu-iso)    args=(--image "$IMAGE" --mode iso --scheme mbr --target dual --fs fat32 --persistence 2G --extended-label) ;;
    live-iso)      args=(--image "$IMAGE" --mode iso --scheme mbr --target dual --fs fat32) ;;
    dd)            args=(--image "$IMAGE" --mode dd --verify) ;;
    freedos)       args=(--boot-type freedos --scheme mbr --target bios --fs fat32 --label FREEDOS) ;;
    custom)        args=(--image "$IMAGE" "$@") ;;
    *) echo "unknown scenario $SCN" >&2; exit 1 ;;
esac

echo "== writing $LOOP ($IMG)"
python3 -m stoke write --yes --allow-loop --device "$LOOP" "${args[@]}" 2>&1 | grep -vE "^\s*\["
losetup -d "$LOOP"; trap - EXIT

[[ -n "${STOKE_NO_BOOT:-}" ]] && exit 0
modes="bios uefi"
case "$SCN" in freedos) modes="bios" ;; windows-uefi) modes="uefi" ;; esac
for m in $modes; do
    wait=45; [[ $SCN == windows* ]] && wait=110; [[ $SCN == wintogo ]] && wait=230; [[ $SCN == freedos ]] && wait=20
    echo "== booting under $m"
    STOKE_STICK="$IMG" "$HERE/boot-stick.sh" "$m" "$wait" "$OUT/native-$SCN-$m.png" | tail -1
done
chown "$CALLER" "$OUT"/* 2>/dev/null || true
