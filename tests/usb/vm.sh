#!/usr/bin/env bash
# Boot an Arch-based live ISO in QEMU with an emulated USB stick, for testing
# Stoke end to end without touching a real drive.
#
# The guest sees: a removable USB mass-storage device backed by
# tests/usb/out/usb-stick.img (raw), this repository at /mnt/stoke (9p,
# read-only), the directory of test ISOs at /mnt/isos (9p, read-only), the
# directory holding the live ISO at /mnt/live, and a scratch virtio disk for
# temporary files. It is driven over the guest agent with guest-run.sh.
#
#   STOKE_LIVE_ISO=~/Downloads/archlinux-x86_64.iso tests/usb/vm.sh
#   tests/usb/vm.sh --stick 8G      a smaller stick (default 32G)
#   tests/usb/vm.sh --reset         recreate the stick and scratch disk
#   tests/usb/vm.sh --gui           show a window
#
# STOKE_ISOS is the directory of images to test with (default ~/Downloads).
# Any live ISO with pacman and python works; guest-setup.sh installs the rest.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/out"
VM="${STOKE_VM:-usb}"
ISOS_DIR="${STOKE_ISOS:-$HOME/Downloads}"
STICK="$OUT/usb-stick.img"
SCRATCH="$OUT/usb-scratch.qcow2"
NVRAM="$OUT/OVMF_VARS-$VM.fd"
QMP_SOCK="$OUT/qmp-$VM.sock"
QGA_SOCK="$OUT/qga-$VM.sock"
OVMF_DIR="${OVMF_DIR:-/usr/share/edk2/x64}"

stick_size=32G
reset=0
gui=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stick) stick_size="$2"; shift ;;
        --reset) reset=1 ;;
        --gui) gui=1 ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
    shift
done

ISO="${STOKE_LIVE_ISO:?set STOKE_LIVE_ISO to an Arch-based live ISO}"
[[ -f "$ISO" ]] || { echo "no such ISO: $ISO" >&2; exit 1; }
LIVE_DIR="$(cd "$(dirname "$ISO")" && pwd)"

mkdir -p "$OUT"
if (( reset )); then rm -f "$STICK" "$SCRATCH"; fi
[[ -f "$STICK" ]] || truncate -s "$stick_size" "$STICK"
[[ -f "$SCRATCH" ]] || qemu-img create -f qcow2 "$SCRATCH" 40G >/dev/null
# Fresh firmware variables every boot: once a scenario has made the stick
# bootable, a remembered boot order would start the stick instead of the ISO.
cp "$OVMF_DIR/OVMF_VARS.4m.fd" "$NVRAM"
rm -f "$QMP_SOCK" "$QGA_SOCK"

display_args=(-display none -device virtio-vga)
(( gui )) && display_args=(-display gtk,show-cursor=on -device virtio-vga)

echo ">> booting $(basename "$ISO") as \"$VM\" with a $stick_size USB stick ($STICK)"
exec qemu-system-x86_64 \
    -enable-kvm -machine q35 -cpu host -smp "${STOKE_VM_CPUS:-6}" -m "${STOKE_VM_MEM:-4G}" \
    -drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF_DIR/OVMF_CODE.4m.fd" \
    -drive if=pflash,format=raw,unit=1,file="$NVRAM" \
    -drive file="$ISO",media=cdrom,readonly=on,if=none,id=cd0 \
    -device ide-cd,drive=cd0,bootindex=0 \
    -drive file="$SCRATCH",if=virtio,format=qcow2 \
    -drive file="$STICK",if=none,id=stick,format=raw,cache=writeback \
    -device qemu-xhci,id=xhci \
    -device usb-storage,bus=xhci.0,drive=stick,removable=on,serial=STOKETEST01 \
    -device usb-tablet \
    -virtfs local,path="$REPO_ROOT",mount_tag=stoke,security_model=none,readonly=on \
    -virtfs local,path="$ISOS_DIR",mount_tag=isos,security_model=none,readonly=on \
    -virtfs local,path="$LIVE_DIR",mount_tag=live,security_model=none,readonly=on \
    -netdev user,id=net0 -device virtio-net,netdev=net0 \
    -qmp "unix:$QMP_SOCK,server,nowait" \
    -chardev "socket,path=$QGA_SOCK,server=on,wait=off,id=qga0" \
    -device virtio-serial -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0 \
    -serial "file:$OUT/console-$VM.log" \
    "${display_args[@]}"
