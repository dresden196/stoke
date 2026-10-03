#!/usr/bin/env bash
# Boot a physical USB stick in QEMU by handing the USB device itself to the
# guest (usb-host), so the firmware sees a real removable drive over a real
# USB stack rather than a disk image. Needs root (detaches the kernel driver
# for the duration) and the stick must not be mounted.
#
#   sudo tests/usb/boot-real.sh 0718:7722 uefi 120 out/real-uefi.png
#   sudo tests/usb/boot-real.sh 0718:7722 bios 60 out/real-bios.png
#   sudo tests/usb/boot-real.sh 0718:7722 secboot 150 out/real-sb.png
# secboot uses the Secure Boot firmware build with out/OVMF_VARS-sb.fd, made by
#   virt-fw-vars --input OVMF_VARS.4m.fd --output out/OVMF_VARS-sb.fd --enroll-redhat --secure-boot
# (Microsoft's KEK and db certificates enrolled, enforcement on).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; OUT="$HERE/out"
IDS="${1:?vendor:product}"; MODE="${2:-uefi}"; WAIT="${3:-90}"; SHOT="${4:-$OUT/real-$MODE.png}"
VID="0x${IDS%%:*}"; PID="0x${IDS##*:}"
QMP="$OUT/qmp-real.sock"; LOG="$OUT/console-real-$MODE.log"; rm -f "$QMP" "$LOG"
OVMF="${OVMF_DIR:-/usr/share/edk2/x64}"
# STOKE_EXTRA_DISK=1 adds an empty SATA disk: XP-era setup media assume a
# target hard disk exists (the masquerading MBR maps the stick to 0x81).
extra_args=()
if [[ -n "${STOKE_EXTRA_DISK:-}" ]]; then
    qemu-img create -f qcow2 "$OUT/real-extra.qcow2" 20G >/dev/null
    extra_args=(-drive file="$OUT/real-extra.qcow2",if=none,id=hd0,format=qcow2 -device ide-hd,drive=hd0,bootindex=1)
fi
# STOKE_USB_CTRL=ehci: a USB 2.0 controller, for guests without xHCI (XP).
# STOKE_MACHINE=pc: the classic chipset, for guests that predate q35.
ctrl_args=(-device qemu-xhci,id=xhci); ctrl_bus=xhci.0
[[ "${STOKE_USB_CTRL:-xhci}" == "ehci" ]] && { ctrl_args=(-device usb-ehci,id=ehci); ctrl_bus=ehci.0; }
fw_args=(); machine="${STOKE_MACHINE:-q35}"
if [[ "$MODE" == "uefi" ]]; then
    VARS="$OUT/OVMF_VARS-real.fd"; cp "$OVMF/OVMF_VARS.4m.fd" "$VARS"
    fw_args=(-drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF/OVMF_CODE.4m.fd" -drive if=pflash,format=raw,unit=1,file="$VARS")
elif [[ "$MODE" == "secboot" ]]; then
    [[ -f "$OUT/OVMF_VARS-sb.fd" ]] || { echo "no out/OVMF_VARS-sb.fd (see header)" >&2; exit 1; }
    VARS="$OUT/OVMF_VARS-sb-run.fd"; cp "$OUT/OVMF_VARS-sb.fd" "$VARS"; machine="${STOKE_MACHINE:-q35},smm=on"
    fw_args=(-global driver=cfi.pflash01,property=secure,value=on
             -drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF/OVMF_CODE.secboot.4m.fd" -drive if=pflash,format=raw,unit=1,file="$VARS")
fi
qemu-system-x86_64 -enable-kvm -machine "$machine" -cpu host -smp 4 -m "${STOKE_BOOT_MEM:-3G}" "${fw_args[@]}" \
    "${ctrl_args[@]}" -device usb-host,bus="${ctrl_bus}",vendorid="$VID",productid="$PID",bootindex=0 "${extra_args[@]}" \
    -netdev user,id=net0 -device e1000,netdev=net0 -qmp "unix:$QMP,server,nowait" \
    -serial "file:$LOG" -display none -device VGA &
QPID=$!; trap 'kill $QPID 2>/dev/null || true' EXIT
sleep "$WAIT"
kill -0 $QPID 2>/dev/null || { echo "qemu exited early" >&2; exit 1; }
"$HERE/qmp-shot.sh" "$QMP" "$SHOT" && echo "screenshot: $SHOT"
chown "${SUDO_USER:-$USER}" "$SHOT" 2>/dev/null || true
