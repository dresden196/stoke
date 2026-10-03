"""How the engine reaches the drive: as root through the device nodes, or
unprivileged through udisks2.

The native backend is what the packaged engine uses under pkexec: it opens
/dev/sdX itself, mounts with mount(8), hides the drive from the desktop with
a udev rule. The udisks backend is for the Flatpak and the AppImage, where
there is no root and no device node: udisks2 hands over a descriptor for the
disk and each partition (polkit asks once), rescans the table and mounts
the fresh volumes under /run/media. Everything between the two, partition
tables, boot records, file systems, boot loaders, is written by the engine
through those descriptors and is the same code either way.
"""

import contextlib
import fcntl
import os
import shutil
import subprocess
import time

from .blockio import BlockTarget, BLKRRPART, BLKFLSBUF
from .util import run, UsbError, human_size

from .i18n import _

_backend = None


def get_backend():
    """The backend for this process: STOKE_BACKEND=native|udisks, else
    udisks inside a Flatpak or AppImage, else native."""
    global _backend
    if _backend is None:
        want = os.environ.get("STOKE_BACKEND", "auto")
        if want == "auto":
            # Root, or the packaged engine under pkexec: device nodes. A
            # sandbox (Flatpak, AppImage) has no root and no nodes: udisks2.
            sandboxed = os.path.exists("/.flatpak-info") or bool(os.environ.get("APPIMAGE"))
            want = "udisks" if (sandboxed and os.geteuid() != 0) else "native"
        if want == "native":
            _backend = NativeBackend()
        elif want == "udisks":
            _backend = UdisksBackend()
        else:
            raise UsbError(_("unknown backend %s") % want)
    return _backend


def set_backend(name):
    global _backend
    _backend = None
    os.environ["STOKE_BACKEND"] = name
    return get_backend()


def partition_node(dev, number):
    base = os.path.basename(dev)
    sep = "p" if base[-1].isdigit() else ""
    return f"/dev/{base}{sep}{number}"


def _sysfs(name, *rel):
    try:
        with open(os.path.join("/sys/class/block", name, *rel)) as f:
            return f.read().strip()
    except OSError:
        return ""


def sector_size_of(dev):
    return int(_sysfs(os.path.basename(dev), "queue/logical_block_size") or 512)


def _wait(predicate, timeout, step=0.1):
    end = time.monotonic() + timeout
    while True:
        r = predicate()
        if r:
            return r
        if time.monotonic() > end:
            return None
        time.sleep(step)


class Backend:
    name = "?"
    can_write = False
    #: True when partitions have device nodes we may open by path (so tools
    #: like wimapply can be pointed at them).
    has_device_nodes = False

    def list_disks(self, include_usb_hdd=False, include_all=False, include_loop=False):
        raise NotImplementedError

    def find_disk(self, dev):
        dev = os.path.realpath(dev)
        for d in self.list_disks(include_usb_hdd=True, include_all=True, include_loop=True):
            if os.path.realpath(d["device"]) == dev:
                return d
        return None

    def is_system_disk(self, dev):
        raise NotImplementedError

    # --- access
    def open_disk(self, dev):
        raise NotImplementedError

    def check_exclusive(self, dev):
        """Raise if anything (a mount, another writer) holds the disk."""
        raise NotImplementedError

    def open_partition(self, disk, number):
        raise NotImplementedError

    def device_node(self, target):
        return target.path if self.has_device_nodes else None

    # --- kernel/desktop coordination
    def unmount_all(self, dev, log=None):
        raise NotImplementedError

    @contextlib.contextmanager
    def inhibit(self, dev):
        yield

    def rescan(self, disk, expected_numbers=(), log=None, reopen=True):
        """Make the kernel (and the desktop) read the new partition table
        and wait until every expected partition exists. With reopen=False
        the disk may be left closed (the job is done with it)."""
        raise NotImplementedError

    def settle(self):
        pass

    def drop_cache(self, target):
        """Best effort: make the next read of the target hit the medium."""
        try:
            os.posix_fadvise(target.fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass

    # --- file systems
    def mount(self, part, fs, tag="main", log=None):
        raise NotImplementedError

    def unmount(self, part, mount_dir, log=None):
        raise NotImplementedError


# ------------------------------------------------------------ native

MOUNT_BASE = "/run/stoke"
RULES_DIR = "/run/udev/rules.d"


def _mounts_from_proc():
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


class NativeBackend(Backend):
    name = "native"
    has_device_nodes = True

    def __init__(self):
        self.can_write = os.geteuid() == 0

    def list_disks(self, include_usb_hdd=False, include_all=False, include_loop=False):
        from . import devices
        return devices.list_disks_sysfs(include_usb_hdd, include_all, include_loop)

    def is_system_disk(self, dev):
        from . import devices
        return devices.is_system_disk_sysfs(dev)

    def open_disk(self, dev):
        try:
            fd = os.open(dev, os.O_RDWR)
        except OSError as e:
            raise UsbError(_("cannot open %s: %s") % (dev, e.strerror))
        return BlockTarget(dev, fd, sector_size=sector_size_of(dev))

    def check_exclusive(self, dev):
        try:
            fd = os.open(dev, os.O_RDWR | os.O_EXCL)
        except OSError as e:
            raise UsbError(_("%s is in use (%s); close anything using it and try again") % (dev, e.strerror))
        os.close(fd)

    def open_partition(self, disk, number):
        node = partition_node(disk.path, number)
        ok = _wait(lambda: os.path.exists(node), 10)
        if not ok:
            raise UsbError(_("%s did not appear after partitioning") % node)
        fd = os.open(node, os.O_RDWR)
        return BlockTarget(node, fd, sector_size=disk.sector_size, number=number)

    def unmount_all(self, dev, log=None):
        name = os.path.basename(os.path.realpath(dev))
        targets = set()
        for src, mps in _mounts_from_proc().items():
            if os.path.basename(src).startswith(name):
                for mp in mps:
                    if mp == "[swap]":
                        raise UsbError(_("%s is active swap; deactivate it first") % src)
                    targets.add((src, mp))
        for src, mp in sorted(targets, key=lambda t: -len(t[1])):
            if log:
                log(f"Unmounting {src} from {mp}")
            r = subprocess.run(["umount", mp], capture_output=True, text=True)
            if r.returncode != 0:
                subprocess.run(["umount", "-l", mp], capture_output=True, text=True)

    def _reload_udev(self, dev):
        subprocess.run(["udevadm", "control", "--reload"], check=False)
        subprocess.run(["udevadm", "trigger", "--action=change", "--subsystem-match=block",
                        "--sysname-match", os.path.basename(dev) + "*"], check=False)
        self.settle()

    @contextlib.contextmanager
    def inhibit(self, dev):
        """Hide the drive from udisks (and so from the desktop's automount)
        while we write it."""
        name = os.path.basename(os.path.realpath(dev))
        rule = os.path.join(RULES_DIR, f"89-stoke-{name}.rules")
        try:
            os.makedirs(RULES_DIR, exist_ok=True)
            with open(rule, "w") as f:
                f.write(f'KERNEL=="{name}*", ENV{{UDISKS_IGNORE}}="1", ENV{{UDISKS_AUTO}}="0", '
                        f'ENV{{UDISKS_PRESENTATION_HIDE}}="1"\n')
            self._reload_udev(dev)
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                os.remove(rule)
            except OSError:
                pass
            self._reload_udev(dev)

    def rescan(self, disk, expected_numbers=(), log=None, reopen=True):
        disk.fsync()
        for _try in range(20):
            try:
                fcntl.ioctl(disk.fd, BLKRRPART)
                break
            except OSError as e:
                if e.errno != 16:   # EBUSY: a stale mount/probe still holds a partition
                    break
                time.sleep(0.25)
        self.settle()
        for n in expected_numbers:
            node = partition_node(disk.path, n)
            if not _wait(lambda: os.path.exists(node), 10):
                raise UsbError(_("%s did not appear after partitioning") % node)

    def settle(self):
        subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)

    def drop_cache(self, target):
        try:
            fcntl.ioctl(target.fd, BLKFLSBUF)
        except OSError:
            pass
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("1\n")
        except OSError:
            pass

    def mount(self, part, fs, tag="main", log=None):
        node = part.path if hasattr(part, "path") else part
        mount_dir = os.path.join(MOUNT_BASE, os.path.basename(node) + "-" + tag)
        os.makedirs(mount_dir, exist_ok=True)
        attempts = []
        if fs in ("fat16", "fat32"):
            # No `flush`: it syncs after every close, and an XP tree is thousands
            # of small files; the job syncs the whole drive before unmounting.
            attempts.append(["mount", "-t", "vfat", "-o", "rw,umask=000,shortname=mixed,utf8=1", node, mount_dir])
        elif fs == "exfat":
            attempts.append(["mount", "-t", "exfat", "-o", "rw,umask=000", node, mount_dir])
        elif fs == "ntfs":
            attempts.append(["mount", "-t", "ntfs3", "-o", "rw,windows_names,force", node, mount_dir])
            attempts.append(["ntfs-3g", "-o", "rw,windows_names,big_writes", node, mount_dir])
            attempts.append(["mount", "-t", "ntfs-3g", "-o", "rw,windows_names", node, mount_dir])
        elif fs.startswith("ext"):
            attempts.append(["mount", "-t", fs, "-o", "rw", node, mount_dir])
        else:
            attempts.append(["mount", node, mount_dir])
        errors = []
        for cmd in attempts:
            if not shutil.which(cmd[0]):
                continue
            r = run(cmd, check=False, log=log)
            if r.returncode == 0:
                return mount_dir
            errors.append((r.stderr or r.stdout).strip())
        raise UsbError(_("could not mount %s (%s): %s") % (node, fs, errors[-1] if errors else _("no mount helper")))

    def unmount(self, part, mount_dir, log=None):
        os.sync()
        for _try in range(10):
            r = subprocess.run(["umount", mount_dir], capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(0.5)
        else:
            subprocess.run(["umount", "-l", mount_dir], capture_output=True, text=True)
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass


# ------------------------------------------------------------ udisks2

UDISKS = "org.freedesktop.UDisks2"
UD_BLOCK = UDISKS + ".Block"
UD_PART = UDISKS + ".Partition"
UD_PTABLE = UDISKS + ".PartitionTable"
UD_FS = UDISKS + ".Filesystem"
UD_DRIVE = UDISKS + ".Drive"
UD_LOOP = UDISKS + ".Loop"


def _cstr(v):
    """udisks passes device names as NUL-terminated byte arrays."""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).split(b"\0", 1)[0].decode(errors="replace")
    if isinstance(v, list):
        return bytes(v).split(b"\0", 1)[0].decode(errors="replace")
    return str(v)


class UdisksBackend(Backend):
    name = "udisks"
    has_device_nodes = False

    def __init__(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as e:
            raise UsbError(_("the udisks backend needs python-gobject (%s)") % e)
        self.Gio, self.GLib = Gio, GLib
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.mgr = self._proxy("/org/freedesktop/UDisks2", "org.freedesktop.DBus.ObjectManager")
            self.objects()
        except GLib.Error as e:
            raise UsbError(_("udisks2 is not reachable on the system bus: %s") % e.message)
        self.can_write = True
        self._mounted = {}      # object path -> mount dir we asked for

    # --- D-Bus plumbing
    def _proxy(self, path, iface):
        return self.Gio.DBusProxy.new_sync(self.bus, self.Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
                                           | self.Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS,
                                           None, UDISKS, path, iface, None)

    #: udisks answers slowly while the kernel is still flushing a stick after
    #: a big copy; the default 25 s D-Bus timeout is not enough then.
    CALL_TIMEOUT_MS = 300000

    def _call(self, path, iface, method, sig, args, fds=False):
        proxy = self._proxy(path, iface)
        params = self.GLib.Variant(sig, args) if sig else None
        try:
            if fds:
                res, fdlist = proxy.call_with_unix_fd_list_sync(method, params, self.Gio.DBusCallFlags.NONE,
                                                                 self.CALL_TIMEOUT_MS, None, None)
                return res.unpack(), fdlist
            return proxy.call_sync(method, params, self.Gio.DBusCallFlags.NONE, self.CALL_TIMEOUT_MS, None).unpack()
        except self.GLib.Error as e:
            msg = e.message
            if "NotAuthorized" in msg or "Not authorized" in msg or "dismissed" in msg.lower():
                raise UsbError(_("not authorised to write %s (the authentication was cancelled)") % path.rsplit("/", 1)[-1])
            raise UsbError(f"udisks2 {method}: {msg}")

    def objects(self):
        return self.mgr.call_sync("GetManagedObjects", None, self.Gio.DBusCallFlags.NONE, -1, None).unpack()[0]

    def _block(self, devnode, objs=None):
        objs = objs if objs is not None else self.objects()
        for path, ifaces in objs.items():
            b = ifaces.get(UD_BLOCK)
            if b and _cstr(b.get("Device")) == devnode:
                return path, ifaces
        return None, None

    def _path_for(self, devnode, timeout=0):
        r = _wait(lambda: self._block(devnode)[0], timeout) if timeout else self._block(devnode)[0]
        if not r:
            raise UsbError(_("udisks2 does not know %s") % devnode)
        return r

    def _open(self, devnode, mode, flags=0, timeout=0):
        path = self._path_for(devnode, timeout)
        opts = {"flags": self.GLib.Variant("i", flags)} if flags else {}
        (idx,), fdlist = self._call(path, UD_BLOCK, "OpenDevice", "(sa{sv})", (mode, opts), fds=True)
        return fdlist.get(idx)

    # --- listing
    def list_disks(self, include_usb_hdd=False, include_all=False, include_loop=False):
        objs = self.objects()
        mounts = {}
        for path, ifaces in objs.items():
            fs = ifaces.get(UD_FS)
            b = ifaces.get(UD_BLOCK)
            if fs and b:
                mps = [_cstr(m) for m in fs.get("MountPoints") or []]
                if mps:
                    mounts[_cstr(b["Device"])] = mps
        disks = []
        for path, ifaces in objs.items():
            b = ifaces.get(UD_BLOCK)
            if not b or UD_PART in ifaces:
                continue
            node = _cstr(b["Device"])
            name = os.path.basename(node)
            if name.startswith(("ram", "zram", "dm-", "md", "nbd", "sr", "fd")):
                continue
            is_loop = UD_LOOP in ifaces or name.startswith("loop")
            if is_loop and not include_loop:
                continue
            size = int(b.get("Size") or 0)
            if size == 0:
                continue
            drive = objs.get(b.get("Drive") or "", {}).get(UD_DRIVE, {}) if b.get("Drive") not in (None, "/") else {}
            removable = bool(drive.get("Removable") or drive.get("MediaRemovable"))
            bus = drive.get("ConnectionBus", "") or ""
            is_usb = bus == "usb"
            # UDISKS_IGNORE hides a device from file managers; a udev rule that
            # turns off automount for USB sticks sets it too, and udisks still
            # opens and mounts those on request. Honour it only for fixed disks.
            if b.get("HintIgnore") and not (removable or is_usb):
                continue
            is_mmc = name.startswith("mmcblk") or bus == "sdio"
            system = bool(b.get("HintSystem")) and not removable and not is_loop
            parts = []
            label = ""
            for ppath, pif in objs.items():
                pb = pif.get(UD_BLOCK)
                pp = pif.get(UD_PART)
                if not pb or not pp or pp.get("Table") != path:
                    continue
                pnode = _cstr(pb["Device"])
                mps = mounts.get(pnode, [])
                if any(m in ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var") or m.startswith(("/usr/", "/var/lib")) for m in mps):
                    system = True
                parts.append({
                    "device": pnode, "number": int(pp.get("Number") or 0),
                    "start": int(pp.get("Offset") or 0), "size": int(pp.get("Size") or 0),
                    "fstype": b and pb.get("IdType", "") or "", "label": pb.get("IdLabel", "") or "",
                    "mountpoints": mps,
                })
                if not label and pb.get("IdLabel"):
                    label = pb.get("IdLabel")
            parts.sort(key=lambda p: p["number"])
            if system:
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
            vendor = (drive.get("Vendor") or "").strip().replace("_", " ")
            model = (drive.get("Model") or "").strip().replace("_", " ")
            if is_loop:
                backing = _cstr(ifaces.get(UD_LOOP, {}).get("BackingFile") or b"")
                model = "Loop device " + os.path.basename(backing) if backing else "Loop device"
            display = " ".join(x for x in (vendor, model) if x) or name
            sector = int(_sysfs(name, "queue/logical_block_size") or 512)
            disks.append({
                "device": node, "name": name, "size": size,
                "size_human": human_size(size, binary=False),
                "sector_size": sector,
                "physical_sector_size": int(_sysfs(name, "queue/physical_block_size") or sector),
                "removable": removable, "kind": kind, "vendor": vendor, "model": model,
                "serial": drive.get("Serial", "") or "", "label": label,
                "display": f"{label or display} ({human_size(size, binary=False)}) [{name}]",
                "partition_table": (ifaces.get(UD_PTABLE) or {}).get("Type", "") or "",
                "partitions": parts,
                "mounted": any(p["mountpoints"] for p in parts) or bool(mounts.get(node)),
                "readonly": bool(b.get("ReadOnly")),
            })
        order = {"usb": 0, "card": 1, "removable": 2, "usb-hdd": 3, "loop": 4, "internal": 5}
        disks.sort(key=lambda d: (order.get(d["kind"], 9), d["name"]))
        return disks

    def is_system_disk(self, dev):
        objs = self.objects()
        path, ifaces = self._block(dev, objs)
        if not path:
            return False
        b = ifaces.get(UD_BLOCK, {})
        drive = objs.get(b.get("Drive") or "", {}).get(UD_DRIVE, {}) if b.get("Drive") not in (None, "/") else {}
        if b.get("HintSystem") and not (drive.get("Removable") or drive.get("MediaRemovable")):
            return True
        for ppath, pif in objs.items():
            pp = pif.get(UD_PART)
            fs = pif.get(UD_FS)
            if pp and fs and pp.get("Table") == path:
                for m in fs.get("MountPoints") or []:
                    m = _cstr(m)
                    if m in ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var") or m.startswith(("/usr/", "/var/lib")):
                        return True
        return False

    # --- access
    def open_disk(self, dev):
        fd = self._open(dev, "rw")
        return BlockTarget(dev, fd, sector_size=sector_size_of(dev))

    def check_exclusive(self, dev):
        try:
            fd = self._open(dev, "rw", os.O_EXCL)
        except UsbError as e:
            if "busy" in str(e).lower():
                raise UsbError(_("%s is in use (%s); close anything using it and try again") % (dev, "busy"))
            raise
        os.close(fd)

    def open_partition(self, disk, number):
        node = partition_node(disk.path, number)
        fd = self._open(node, "rw", timeout=10)
        return BlockTarget(node, fd, sector_size=disk.sector_size, number=number)

    def unmount_all(self, dev, log=None):
        objs = self.objects()
        for path, ifaces in objs.items():
            b = ifaces.get(UD_BLOCK)
            fs = ifaces.get(UD_FS)
            if not b or not fs:
                continue
            node = _cstr(b["Device"])
            if node != dev and not node.startswith(dev):
                continue
            mps = [_cstr(m) for m in fs.get("MountPoints") or []]
            if not mps:
                continue
            if log:
                log(f"Unmounting {node} from {mps[0]}")
            self._call(path, UD_FS, "Unmount", "(a{sv})", ({"force": self.GLib.Variant("b", True)},))
        for path, ifaces in objs.items():
            b = ifaces.get(UD_BLOCK)
            if b and _cstr(b["Device"]).startswith(dev) and (ifaces.get(UD_BLOCK) or {}).get("IdType") == "swap":
                raise UsbError(_("%s is active swap; deactivate it first") % _cstr(b["Device"]))

    def rescan(self, disk, expected_numbers=(), log=None, reopen=True):
        # Closing the last writer makes udev re-read the table (its inotify
        # watch on the node); Rescan asks udisks to refresh on top of that.
        # The disk is reopened afterwards unless this is the last step.
        disk.fsync()
        disk.close()
        path = self._path_for(disk.path)
        self._call(path, UD_BLOCK, "Rescan", "(a{sv})", ({},))
        for n in expected_numbers:
            node = partition_node(disk.path, n)
            if not self._path_for(node, timeout=15):
                raise UsbError(_("%s did not appear after partitioning") % node)
        if reopen:
            disk.fd = self._open(disk.path, "rw")

    def refresh(self, target):
        """After writing a file system through the descriptor: have udisks
        re-probe the partition so its Filesystem interface appears."""
        target.fsync()
        path = self._path_for(target.path)
        self._call(path, UD_BLOCK, "Rescan", "(a{sv})", ({},))

    def _fs_ready(self, node):
        path, ifaces = self._block(node)
        return path if (ifaces and UD_FS in ifaces) else None

    def mount(self, part, fs, tag="main", log=None):
        node = part.path
        self.refresh(part)
        path = _wait(lambda: self._fs_ready(node), 15)
        if not path:
            raise UsbError(_("udisks2 did not recognise the new file system on %s") % node)
        opts = {"vfat": "umask=000,shortname=mixed,utf8=1", "exfat": "umask=000",
                "ntfs": "windows_names"}.get({"fat16": "vfat", "fat32": "vfat"}.get(fs, fs), "")
        args = {"options": self.GLib.Variant("s", opts)} if opts else {}
        try:
            (mp,) = self._call(path, UD_FS, "Mount", "(a{sv})", (args,))
        except UsbError as e:
            if "AlreadyMounted" not in str(e) and "already mounted" not in str(e).lower():
                raise
            # The desktop got there first; use its mount point.
            _p, ifaces = self._block(node)
            mps = [_cstr(m) for m in (ifaces.get(UD_FS) or {}).get("MountPoints") or []]
            if not mps:
                raise
            mp = mps[0]
        if log:
            log(f"Mounted {node} at {mp}")
        self._mounted[node] = mp
        return mp

    def unmount(self, part, mount_dir, log=None):
        node = part.path if hasattr(part, "path") else part
        os.sync()
        path, ifaces = self._block(node)
        if not path or not ifaces or UD_FS not in ifaces:
            return
        for _try in range(10):
            try:
                self._call(path, UD_FS, "Unmount", "(a{sv})", ({},))
                break
            except UsbError as e:
                if "NotMounted" in str(e):
                    break
                time.sleep(0.5)
        else:
            self._call(path, UD_FS, "Unmount", "(a{sv})", ({"force": self.GLib.Variant("b", True)},))
        self._mounted.pop(node, None)
