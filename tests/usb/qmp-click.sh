#!/usr/bin/env bash
# Click at screen pixel coordinates in a running QEMU (absolute tablet), then
# screenshot after a pause.   tests/usb/qmp-click.sh out/qmp-x.sock 1052 667 out/after.png [wait]
set -euo pipefail
python3 - "${1:?sock}" "${2:?x}" "${3:?y}" <<'PY'
import json, socket, sys, time
sock, x, y = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
W, H = int(__import__("os").environ.get("STOKE_SCREEN_W", 1280)), int(__import__("os").environ.get("STOKE_SCREEN_H", 800))
s = socket.socket(socket.AF_UNIX); s.connect(sock); f = s.makefile("rw")
def cmd(name, **args):
    f.write(json.dumps({"execute": name, "arguments": args} if args else {"execute": name}) + "\n"); f.flush()
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r: return r
f.readline(); cmd("qmp_capabilities")
ax, ay = x * 32767 // W, y * 32767 // H
cmd("input-send-event", events=[{"type": "abs", "data": {"axis": "x", "value": ax}}, {"type": "abs", "data": {"axis": "y", "value": ay}}])
time.sleep(0.1)
for down in (True, False):
    cmd("input-send-event", events=[{"type": "btn", "data": {"down": down, "button": "left"}}]); time.sleep(0.12)
PY
sleep "${5:-10}"; "$(dirname "$0")/qmp-shot.sh" "$1" "${4:?png}"
