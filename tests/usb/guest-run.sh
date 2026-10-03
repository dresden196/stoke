#!/usr/bin/env bash
# Run a shell command inside the bench VM (tests/usb/vm.sh) and print its
# output. Goes over the QEMU guest agent, so it needs no network, no SSH and
# no credentials: it works on a live ISO with no configuration at all.
#
#   tests/usb/guest-run.sh 'lsblk'
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:?usage: guest-run.sh <shell command>}"
# Writing a 5 GB image is legitimately slow; a fixed ceiling turns that into
# a failure that looks like the guest is broken.
WAIT="${STOKE_GUEST_TIMEOUT:-60}"

exec python3 - "$HERE/out/qga-${STOKE_VM:-usb}.sock" "$CMD" "$WAIT" <<'PY'
import base64, json, socket, sys, time

sock_path, command = sys.argv[1], sys.argv[2]
wait_for = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
s = socket.socket(socket.AF_UNIX)
s.settimeout(30)
s.connect(sock_path)
f = s.makefile("rwb")


def send(name, **args):
    payload = {"execute": name}
    if args:
        payload["arguments"] = args
    f.write(json.dumps(payload).encode() + b"\n")
    f.flush()


def recv():
    line = f.readline()
    if not line:
        sys.exit("guest agent closed the connection")
    return json.loads(line)


def cmd(name, **args):
    send(name, **args)
    while True:
        msg = recv()
        if "return" in msg or "error" in msg:
            return msg


# A previous client may have left a partial command in the agent's buffer. The
# 0xff byte resets its parser; guest-sync then discards every response queued
# ahead of ours. Both must be raw bytes — 0xff is not valid UTF-8.
token = int(time.time() * 1000) & 0x7FFFFFFF
f.write(b"\xff")
send("guest-sync", id=token)
while recv().get("return") != token:
    pass

r = cmd("guest-exec", path="/bin/bash", arg=["-lc", command], **{"capture-output": True})
if "error" in r:
    sys.exit(r["error"]["desc"])
pid = r["return"]["pid"]

for _ in range(int(wait_for * 2)):
    st = cmd("guest-exec-status", pid=pid)["return"]
    if st.get("exited"):
        break
    time.sleep(0.5)
else:
    sys.exit(f"guest command did not exit within {wait_for:.0f}s")

for stream in ("out-data", "err-data"):
    if st.get(stream):
        sys.stdout.write(base64.b64decode(st[stream]).decode("utf-8", "replace"))
sys.exit(st.get("exitcode", 0))
PY
