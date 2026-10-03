#!/usr/bin/env bash
# Cancel and error paths: a write cancelled mid-copy, and a write that fails
# for lack of temporary space, must both leave nothing behind: no mounts
# under /run/stoke, no udev inhibit rule, the loop device free.
#
#   sudo tests/usb/test-cancel.sh ~/Downloads/ubuntu.iso ~/Downloads/win11.iso
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"; OUT="$HERE/out"
LINUX_ISO="${1:?linux iso}"; WIN_ISO="${2:?windows iso}"
[[ $(id -u) == 0 ]] || { echo "run as root" >&2; exit 1; }
export STOKE_LIB="$ROOT/stoke" STOKE_PAYLOAD="$ROOT/stoke/payload" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/stoke"
PASS=0; FAIL=0
check() { if [[ "$2" == 0 ]]; then echo "  PASS  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
clean_state() {  # 0 when nothing is left behind for $1 (loop name)
    local n="$1" rc=0
    grep -q "/run/stoke/$n" /proc/self/mountinfo && { echo "    leftover mount under /run/stoke/$n"; rc=1; }
    ls /run/udev/rules.d/89-stoke-"$n"* >/dev/null 2>&1 && { echo "    leftover udev rule"; rc=1; }
    ls -d /run/stoke/"$n"-* >/dev/null 2>&1 && { echo "    leftover mount dir"; rc=1; }
    return $rc
}
mkdir -p "$OUT"
IMG="$OUT/native-cancel.img"; rm -f "$IMG"; truncate -s 8G "$IMG"
LOOP=$(losetup -f --show -P "$IMG"); LN=$(basename "$LOOP")
trap 'losetup -d "$LOOP" 2>/dev/null; umount /mnt/stoke-tiny 2>/dev/null; rmdir /mnt/stoke-tiny 2>/dev/null' EXIT

echo "== cancel during copy"
# Drive the serve protocol: send the write, wait for copy progress, cancel.
RESULT=$(python3 - "$LOOP" "$LINUX_ISO" "$ROOT/stoke/bin/stoke" <<'PY'
import json, subprocess, sys, time
loop, iso, engine = sys.argv[1:4]
p = subprocess.Popen([engine, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
p.stdout.readline()  # hello
send({"id": 1, "cmd": "write", "job": {"device": loop, "image": iso, "mode": "iso", "scheme": "mbr", "target": "dual",
                                       "fs": "fat32", "allow_loop": True}})
copying = False; sent_cancel = False; outcome = None; t0 = time.time()
while time.time() - t0 < 300:
    line = p.stdout.readline()
    if not line: break
    e = json.loads(line)
    if e.get("event") == "progress" and e.get("phase") == "copy" and (e.get("value") or 0) > 0.05 and not sent_cancel:
        send({"id": 2, "cmd": "cancel"}); sent_cancel = True
    if e.get("event") == "done":
        outcome = e; break
send({"id": 3, "cmd": "quit"}); p.wait(timeout=30)
print(json.dumps({"sent_cancel": sent_cancel, "outcome": outcome}))
PY
)
echo "  $RESULT"
echo "$RESULT" | grep -q '"sent_cancel": true' ; check "cancel was sent during the copy phase" $?
echo "$RESULT" | grep -q '"cancelled": true' ; check "engine reported the job as cancelled" $?
sleep 1; clean_state "$LN"; check "nothing left behind after cancel" $?

echo "== not enough temporary space (install.wim split on FAT32)"
mkdir -p /mnt/stoke-tiny && mount -t tmpfs -o size=64M tmpfs /mnt/stoke-tiny
OUTPUT=$(python3 -m stoke --json write --yes --allow-loop --device "$LOOP" --image "$WIN_ISO" --mode iso --scheme mbr --target dual --fs fat32 --temp-dir /mnt/stoke-tiny 2>&1 | tail -1)
echo "  ${OUTPUT:0:200}"
echo "$OUTPUT" | grep -q '"ok": false' && echo "$OUTPUT" | grep -qi "temporary space" ; check "engine failed with the temporary-space error" $?
sleep 1; clean_state "$LN"; check "nothing left behind after the error" $?
umount /mnt/stoke-tiny

echo "== refuses an internal disk and an unknown device"
# The engine exits 1 on refusal, so capture first: under pipefail a grep on
# the pipeline would report the engine's status, not the match.
SYSDISK=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null | head -1)
MSG=$(python3 -m stoke --json write --yes --device "/dev/${SYSDISK:-nvme0n1}" --boot-type none 2>&1); grep -q "running system" <<<"$MSG"; check "system disk refused" $?
MSG=$(python3 -m stoke --json write --yes --device /dev/sdzz --boot-type none 2>&1); grep -q "not a disk" <<<"$MSG"; check "unknown device refused" $?

echo; echo "passed: $PASS  failed: $FAIL"; [[ $FAIL == 0 ]]
