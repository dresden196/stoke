#!/usr/bin/env bash
# Drive stoke through its paces inside the test VM, then boot what it
# made. Each scenario writes the emulated stick, then the stick image is
# booted on the host under BIOS and/or UEFI and a screenshot is taken.
#
#   tests/usb/run.sh                    all scenarios
#   tests/usb/run.sh windows-uefi       one scenario
#
# Needs tests/usb/vm.sh running and tests/usb/guest-setup.sh done. The images
# are named relative to STOKE_ISOS (default ~/Downloads): WIN_ISO (a Windows
# 10/11 ISO), UBUNTU_ISO; the live ISO the guest booted from is used for the
# live-iso scenario.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export STOKE_VM="${STOKE_VM:-usb}"
RUN="$HERE/guest-run.sh"
OUT="$HERE/out"
ISOS_DIR="${STOKE_ISOS:-$HOME/Downloads}"
WIN_ISO="${WIN_ISO:-$(ls "$ISOS_DIR"/*[Ww]in*.iso "$ISOS_DIR"/X2*-*.iso 2>/dev/null | head -1 | xargs -r basename)}"
UBUNTU_ISO="${UBUNTU_ISO:-$(ls "$ISOS_DIR"/ubuntu-*.iso 2>/dev/null | head -1 | xargs -r basename)}"
LIVE_ISO="$(basename "${STOKE_LIVE_ISO:?set STOKE_LIVE_ISO}")"
PASS=0; FAIL=0

engine() {
    # $1 = extra args for `write`; runs as root in the guest, JSON progress
    # reduced to the log lines and the final result.
    STOKE_GUEST_TIMEOUT=3600 "$RUN" "
        cd /mnt/stoke/stoke &&
        PYTHONDONTWRITEBYTECODE=1 STOKE_PAYLOAD=/mnt/stoke/stoke/payload TMPDIR=/var/tmp/stoke \
        python3 -m stoke --json write --yes --temp-dir /var/tmp/stoke $1 2>&1 |
        python3 -c 'import sys,json
for l in sys.stdin:
    try: e=json.loads(l)
    except Exception: print(l.rstrip()); continue
    if e.get(\"event\")==\"log\": print(\"  \"+e[\"text\"])
    elif e.get(\"event\")==\"status\": print(\"==> \"+e[\"text\"])
    elif e.get(\"event\")==\"done\": print(\"DONE \"+json.dumps(e))
'
    "
}

stick_dev() {
    "$RUN" 'lsblk -dno NAME,SERIAL | awk "\$2==\"STOKETEST01\"{print \"/dev/\"\$1}"' | tr -d '[:space:]'
}

check() {  # name, condition-result(0/1)
    if [[ "$2" == 0 ]]; then echo "  PASS  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi
}

layout() {
    "$RUN" "sfdisk -l $1 2>/dev/null; echo; blkid $1* 2>/dev/null"
}

scenario() {
    local name="$1"; shift
    echo; echo "=================== $name ==================="
    local dev; dev=$(stick_dev)
    [[ -n "$dev" ]] || { echo "no test stick in guest" >&2; exit 1; }
    local out
    out=$(engine "--device $dev $*") || true
    echo "$out" | tail -40
    if echo "$out" | grep -q 'DONE {"event": "done", "ok": true'; then check "$name: engine finished" 0; else check "$name: engine finished" 1; return 1; fi
    layout "$dev"
}

boot_check() {  # name mode wait
    local name="$1" mode="$2" wait="$3"
    local shot="$OUT/stick-$name-$mode.png"
    if "$HERE/boot-stick.sh" "$mode" "$wait" "$shot" >/dev/null 2>&1; then
        check "$name: boots under $mode (screenshot $shot)" 0
    else
        check "$name: boots under $mode" 1
    fi
}

want="${1:-all}"
run_scn() { [[ "$want" == all || "$want" == "$1" ]]; }

if run_scn ubuntu-iso; then
    scenario ubuntu-iso --image "/mnt/isos/$UBUNTU_ISO" --mode iso --scheme mbr --target dual --fs fat32 --persistence 2G --extended-label && {
        "$RUN" 'mkdir -p /mnt/t && mount /dev/disk/by-id/usb-*STOKETEST01*-part1 /mnt/t 2>/dev/null && ls /mnt/t | head -20 && cat /mnt/t/boot/grub/grub.cfg | head -20; ls /mnt/t/boot/grub/i386-pc | head -3; umount /mnt/t' || true
        boot_check ubuntu-iso bios 45
        boot_check ubuntu-iso uefi 60
    }
fi
if run_scn live-iso; then
    scenario live-iso --image "/mnt/live/$LIVE_ISO" --mode iso --scheme mbr --target dual --fs fat32 && {
        boot_check live-iso bios 45
        boot_check live-iso uefi 60
    }
fi
if run_scn ubuntu-dd; then
    scenario ubuntu-dd --image "/mnt/isos/$UBUNTU_ISO" --mode dd --verify && boot_check ubuntu-dd uefi 60
fi
if run_scn windows-uefi; then
    scenario windows-uefi --image "/mnt/isos/$WIN_ISO" --mode iso --scheme gpt --target uefi --fs ntfs \
        --windows-option bypass_requirements --windows-option no_online_account --windows-option set_user --username tester \
        --windows-option no_data_collection --windows-option disable_bitlocker --windows-option duplicate_locale && {
        "$RUN" 'mkdir -p /mnt/t && mount -t ntfs3 /dev/disk/by-id/usb-*STOKETEST01*-part1 /mnt/t 2>/dev/null || ntfs-3g /dev/disk/by-id/usb-*STOKETEST01*-part1 /mnt/t; ls /mnt/t; ls -la /mnt/t/sources/appraiserres* /mnt/t/setup.* /mnt/t/sources/\$OEM\$/\$\$/Panther/ 2>&1; wimdir /mnt/t/sources/boot.wim 2 | grep -i "^/autounattend"; umount /mnt/t' || true
        boot_check windows-uefi uefi 120
    }
fi
if run_scn windows-fat32; then
    scenario windows-fat32 --image "/mnt/isos/$WIN_ISO" --mode iso --scheme mbr --target dual --fs fat32 \
        --windows-option bypass_requirements --windows-option no_online_account && {
        "$RUN" 'mkdir -p /mnt/t && mount /dev/disk/by-id/usb-*STOKETEST01*-part1 /mnt/t; ls -la /mnt/t/sources/install*.swm /mnt/t/sources/install.wim 2>&1; umount /mnt/t' || true
        boot_check windows-fat32 bios 90
        boot_check windows-fat32 uefi 120
    }
fi
if run_scn freedos; then
    scenario freedos --boot-type freedos --scheme mbr --target bios --fs fat32 --label FREEDOS && boot_check freedos bios 20
fi
if run_scn wintogo; then
    scenario wintogo --image "/mnt/isos/$WIN_ISO" --mode iso --wintogo 1 --scheme mbr --target dual --fs ntfs \
        --windows-option offline_internal_drives --windows-option set_user --username tester --windows-option no_online_account && {
        boot_check wintogo uefi 240
        boot_check wintogo bios 240
    }
fi

echo; echo "passed: $PASS  failed: $FAIL"
[[ $FAIL == 0 ]]
