#!/usr/bin/env bash
# Prepare the live guest started by tests/usb/vm.sh: mount the shares,
# install the engine's dependencies, give it scratch space.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export STOKE_VM="${STOKE_VM:-usb}"
RUN="$HERE/guest-run.sh"

echo ">> waiting for the guest agent"
for _ in $(seq 1 120); do
    if "$RUN" 'true' >/dev/null 2>&1; then break; fi
    sleep 2
done
"$RUN" 'true' >/dev/null || { echo "guest agent never answered" >&2; exit 1; }

STOKE_GUEST_TIMEOUT=900 "$RUN" '
set -e
mkdir -p /mnt/stoke /mnt/isos /mnt/live /var/tmp/stoke
mountpoint -q /mnt/stoke || mount -t 9p -o trans=virtio,version=9p2000.L,msize=262144 stoke /mnt/stoke
mountpoint -q /mnt/isos || mount -t 9p -o trans=virtio,version=9p2000.L,msize=262144 isos /mnt/isos
mountpoint -q /mnt/live || mount -t 9p -o trans=virtio,version=9p2000.L,msize=262144 live /mnt/live
if ! mountpoint -q /var/tmp/stoke; then
    blkid /dev/vda >/dev/null 2>&1 || mkfs.ext4 -q -F /dev/vda
    mount /dev/vda /var/tmp/stoke
fi
missing=""
for p in python python-pyudev util-linux dosfstools ntfs-3g exfatprogs e2fsprogs syslinux grub parted wimlib hivex; do
    pacman -Q $p >/dev/null 2>&1 || missing="$missing $p"
done
if [ -n "$missing" ]; then
    # Only the official repos: a distribution live image may list its own
    # repo whose key is not trusted this early in the boot, and one unusable
    # repo makes -Sy fail as a whole.
    printf "[options]\nArchitecture = auto\nSigLevel = Required DatabaseOptional\n[core]\nInclude = /etc/pacman.d/mirrorlist\n[extra]\nInclude = /etc/pacman.d/mirrorlist\n" > /tmp/pacman-arch.conf
    for i in 1 2 3; do pacman --config /tmp/pacman-arch.conf -Sy --noconfirm --needed $missing && break; sleep 5; done
fi
lsblk -o NAME,SIZE,TRAN,RM,MODEL | grep -E "NAME|sd"
echo "guest ready"
'
