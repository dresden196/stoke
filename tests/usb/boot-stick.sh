#!/usr/bin/env bash
# Boot the stick image written by the test VM, under BIOS or UEFI, and
# take a screenshot after a while. Exits 0 when qemu stayed up.
#
#   tests/usb/boot-stick.sh bios 40 out/shot.png
#   tests/usb/boot-stick.sh uefi 60 out/shot.png
#   STOKE_STICK_PERSIST=1 ...   keep what the guest writes (to read its logs afterwards)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${STOKE_OUT:-$HERE/out}"
MODE="${1:-bios}"
WAIT="${2:-40}"
SHOT="${3:-$OUT/stick-$MODE.png}"
STICK="${STOKE_STICK:-$OUT/usb-stick.img}"
QMP="$OUT/qmp-stickboot.sock"
LOG="$OUT/console-stickboot-$MODE.log"
rm -f "$QMP" "$LOG"

# STOKE_BOOT_BUS=sata attaches the image as a SATA disk instead of a USB
# stick, to tell a guest OS's USB-boot limitation from a bad boot chain.
# STOKE_USB_CTRL=ehci puts the stick on a USB 2.0 controller: XP has no
# xHCI driver, so a stick on USB 3.0 is invisible to its kernel.
if [[ "${STOKE_USB_CTRL:-xhci}" == "ehci" ]]; then
    bus_args=(-device usb-ehci,id=ehci -device usb-storage,bus=ehci.0,drive=stick,removable=on,bootindex=0)
else
    bus_args=(-device qemu-xhci,id=xhci -device usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0)
fi
[[ "${STOKE_BOOT_BUS:-usb}" == "sata" ]] && bus_args=(-device ide-hd,drive=stick,bootindex=0)
# STOKE_EXTRA_DISK=1 adds an empty SATA disk (XP setup media expect one).
extra_args=()
if [[ -n "${STOKE_EXTRA_DISK:-}" ]]; then
    qemu-img create -f qcow2 "$OUT/stick-extra.qcow2" 20G >/dev/null
    extra_args=(-drive file="$OUT/stick-extra.qcow2",if=none,id=hd0,format=qcow2 -device ide-hd,drive=hd0,bootindex=1)
fi
SNAPSHOT=",snapshot=on"
[[ -n "${STOKE_STICK_PERSIST:-}" ]] && SNAPSHOT=""
fw_args=()
if [[ "$MODE" == "uefi" ]]; then
    VARS="$OUT/OVMF_VARS-stickboot.fd"
    cp "${OVMF_DIR:-/usr/share/edk2/x64}/OVMF_VARS.4m.fd" "$VARS"
    fw_args=(-drive if=pflash,format=raw,unit=0,readonly=on,file="${OVMF_DIR:-/usr/share/edk2/x64}/OVMF_CODE.4m.fd"
             -drive if=pflash,format=raw,unit=1,file="$VARS")
fi
qemu-system-x86_64 -enable-kvm -machine "${STOKE_MACHINE:-q35}" -cpu host -smp 4 -m "${STOKE_BOOT_MEM:-3G}" \
    "${fw_args[@]}" \
    -drive file="$STICK",if=none,id=stick,format=raw,file.locking=off$SNAPSHOT \
    "${bus_args[@]}" "${extra_args[@]}" \
    -boot menu=off \
    -netdev user,id=net0 -device e1000,netdev=net0 \
    -qmp "unix:$QMP,server,nowait" \
    -serial "file:$LOG" \
    -display none -device VGA &
QPID=$!
trap 'kill $QPID 2>/dev/null || true' EXIT
sleep "$WAIT"
if ! kill -0 $QPID 2>/dev/null; then echo "qemu exited early" >&2; exit 1; fi
python3 - "$QMP" "$SHOT" <<'PY'
import json, socket, sys
sock, shot = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX); s.connect(sock); f = s.makefile("rw")
def cmd(name, **args):
    f.write(json.dumps({"execute": name, "arguments": args} if args else {"execute": name}) + "\n"); f.flush()
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r: return r
f.readline(); cmd("qmp_capabilities")
print(cmd("screendump", filename=shot, format="png"))
PY
echo "screenshot: $SHOT"
