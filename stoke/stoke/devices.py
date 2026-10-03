"""Find the disks we are willing to write to.

Rufus's rule is the right one: show removable USB drives, hide everything
else unless the user explicitly asks for USB hard drives. Nothing that holds
the running system is ever offered, whatever the flags say.
"""

import os
import re

from .util import human_size

SYS_BLOCK = "/sys/block"


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _mounts():
    """{ '/dev/sda1': ['/mnt/x', ...] } from mountinfo, plus swap devices."""
    out = {}
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                if "-" not in parts:
                    continue
                dash = parts.index("-")
                mnt = parts[4].replace("\\040", " ")
                src = parts[dash + 2]
                if src.startswith("/dev/"):
                    out.setdefault(os.path.realpath(src), []).append(mnt)
    except OSError:
        pass
    try:
        with open("/proc/swaps") as f:
            for line in f.readlines()[1:]:
                src = line.split()[0]
                if src.startswith("/dev/"):
                    out.setdefault(os.path.realpath(src), []).append("[swap]")
    except OSError:
        pass
    return out


def _udev_props(devnode):
    """Properties from `udevadm info`, without needing pyudev at import time."""
    props = {}
    try:
        import subprocess
        r = subprocess.run(["udevadm", "info", "--query=property", "--name", devnode],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
    except Exception:
        pass
    return props


def _system_disks(mounts):
    """Disks that hold /, /boot, /home, swap or anything under /usr: never targets."""
    protected = set()
    for dev, mps in mounts.items():
        for mp in mps:
            if mp in ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[swap]") \
                    or mp.startswith("/usr/") or mp.startswith("/var/lib"):
                protected.add(dev)
    disks = set()
    for dev in protected:
        name = os.path.basename(dev)
        # partition -> parent disk
        for disk in os.listdir(SYS_BLOCK):
            if name == disk or os.path.isdir(os.path.join(SYS_BLOCK, disk, name)):
                disks.add(disk)
        # dm/md: resolve slaves
        slaves = os.path.join(SYS_BLOCK, name, "slaves")
        if os.path.isdir(slaves):
            for s in os.listdir(slaves):
                s = re.sub(r"p?\d+$", "", s) if s.startswith(("nvme", "mmcblk")) else re.sub(r"\d+$", "", s)
                disks.add(s)
    return disks


def _partitions(disk, mounts):
    parts = []
    base = os.path.join(SYS_BLOCK, disk)
    for entry in sorted(os.listdir(base)):
        if not entry.startswith(disk):
            continue
        pdir = os.path.join(base, entry)
        if not os.path.exists(os.path.join(pdir, "partition")):
            continue
        node = "/dev/" + entry
        props = _udev_props(node)
        parts.append({
            "device": node,
            "number": int(_read(os.path.join(pdir, "partition"), "0") or 0),
            "start": int(_read(os.path.join(pdir, "start"), "0") or 0) * 512,
            "size": int(_read(os.path.join(pdir, "size"), "0") or 0) * 512,
            "fstype": props.get("ID_FS_TYPE", ""),
            "label": props.get("ID_FS_LABEL", ""),
            "mountpoints": mounts.get(os.path.realpath(node), []),
        })
    return parts


def list_disks_sysfs(include_usb_hdd=False, include_all=False, include_loop=False):
    """Return a list of candidate target disks, safest first (from sysfs
    and udev; what the native backend uses).

    include_usb_hdd: also list non-removable USB drives (Rufus's
                     "List USB Hard Drives").
    include_all:     list internal SATA/NVMe/virtio disks too. Only for
                     testing; the window never sets this.
    include_loop:    list loop devices (testing on a laptop with no stick).
    """
    mounts = _mounts()
    system = _system_disks(mounts)
    disks = []
    for name in sorted(os.listdir(SYS_BLOCK)):
        if name.startswith(("ram", "zram", "dm-", "md", "nbd", "sr", "fd")):
            continue
        if name.startswith("loop") and not include_loop:
            continue
        base = os.path.join(SYS_BLOCK, name)
        size = int(_read(os.path.join(base, "size"), "0") or 0) * 512
        if size == 0:
            continue
        node = "/dev/" + name
        props = _udev_props(node)
        removable = _read(os.path.join(base, "removable")) == "1"
        bus = props.get("ID_BUS", "")
        is_usb = bus == "usb" or "usb" in props.get("DEVPATH", "").split("/") or \
            props.get("ID_USB_DRIVER", "") in ("usb-storage", "uas")
        is_loop = name.startswith("loop")
        is_mmc = name.startswith("mmcblk")
        if name in system:
            continue
        if is_usb and removable:
            kind = "usb"
        elif is_usb:
            kind = "usb-hdd"
            if not include_usb_hdd and not include_all:
                continue
        elif is_mmc or removable:
            kind = "card" if is_mmc else "removable"
        elif is_loop:
            kind = "loop"
        else:
            kind = "internal"
            if not include_all:
                continue
        vendor = props.get("ID_VENDOR", _read(os.path.join(base, "device/vendor"))).strip()
        model = props.get("ID_MODEL", _read(os.path.join(base, "device/model"))).strip()
        model = model.replace("_", " ")
        vendor = vendor.replace("_", " ")
        if is_loop:
            backing = _read(os.path.join(base, "loop/backing_file"))
            model = "Loop device " + os.path.basename(backing) if backing else "Loop device"
        label = ""
        parts = _partitions(name, mounts)
        for p in parts:
            if p["label"]:
                label = p["label"]
                break
        display = " ".join(x for x in (vendor, model) if x) or name
        disks.append({
            "device": node,
            "name": name,
            "size": size,
            "size_human": human_size(size, binary=False),
            "sector_size": int(_read(os.path.join(base, "queue/logical_block_size"), "512") or 512),
            "physical_sector_size": int(_read(os.path.join(base, "queue/physical_block_size"), "512") or 512),
            "removable": removable,
            "kind": kind,
            "vendor": vendor,
            "model": model,
            "serial": props.get("ID_SERIAL_SHORT", ""),
            "label": label,
            "display": f"{label or display} ({human_size(size, binary=False)}) [{name}]",
            "partition_table": props.get("ID_PART_TABLE_TYPE", ""),
            "partitions": parts,
            "mounted": any(p["mountpoints"] for p in parts) or bool(mounts.get(node)),
        })
    order = {"usb": 0, "card": 1, "removable": 2, "usb-hdd": 3, "loop": 4, "internal": 5}
    disks.sort(key=lambda d: (order.get(d["kind"], 9), d["name"]))
    return disks


def is_system_disk_sysfs(device):
    """True when the device (or its parent disk) holds the running system."""
    name = os.path.basename(os.path.realpath(device))
    disks = _system_disks(_mounts())
    if name in disks:
        return True
    for d in disks:
        if os.path.isdir(os.path.join(SYS_BLOCK, d, name)):
            return True
    return False


def list_disks(include_usb_hdd=False, include_all=False, include_loop=False):
    """Candidate target disks from whichever backend this process uses."""
    from .backend import get_backend
    return get_backend().list_disks(include_usb_hdd=include_usb_hdd, include_all=include_all, include_loop=include_loop)


def is_system_disk(device):
    from .backend import get_backend
    return get_backend().is_system_disk(device)


def find_disk(device, **kw):
    from .backend import get_backend
    return get_backend().find_disk(device)
