"""Syslinux, installed by hand: ldlinux.sys written into the volume, its
sector map patched in, the volume boot record replaced. This is what
syslinux's own installer (libinstaller/syslxmod.c) does, ported so it
works through a udisks2 descriptor with no extlinux or FIBMAP.

Sector numbers are 512-byte units relative to the partition, as the boot
sector expects (it adds the BPB's hidden sectors itself).
"""

import fcntl
import os
import struct

from .util import UsbError, payload_dir

from .i18n import _

LDLINUX_MAGIC = 0x3EB202FE
ADV_MAGIC1 = 0x5A2D2FA5
ADV_MAGIC2 = 0xA3041767
ADV_MAGIC3 = 0xDD28BF64
ADV_SIZE = 512
SECTOR = 512

FS_IOC_FIEMAP = 0xC020660B
FAT_IOCTL_SET_ATTRIBUTES = 0x40047211
ATTR_RO, ATTR_HIDDEN, ATTR_SYS = 0x01, 0x02, 0x04


def lib_dir():
    """Where ldlinux.sys, ldlinux.bss and the .c32 modules live: the
    payload (STOKE_SYSLINUX overrides), else the host's syslinux."""
    for d in (os.environ.get("STOKE_SYSLINUX"), os.path.join(payload_dir(), "syslinux"), "/usr/lib/syslinux/bios"):
        if d and os.path.isfile(os.path.join(d, "ldlinux.sys")) and os.path.isfile(os.path.join(d, "ldlinux.bss")):
            return d
    raise UsbError(_("Syslinux payload (ldlinux.sys) is missing"))


def version():
    try:
        with open(os.path.join(lib_dir(), "VERSION")) as f:
            return f.read().strip()
    except (OSError, UsbError):
        return "6.04"


def make_adv():
    adv = bytearray(ADV_SIZE)
    struct.pack_into("<I", adv, 0, ADV_MAGIC1)
    csum = ADV_MAGIC2
    for i in range(8, ADV_SIZE - 4, 4):
        csum = (csum - struct.unpack_from("<I", adv, i)[0]) & 0xFFFFFFFF
    struct.pack_into("<I", adv, 4, csum)
    struct.pack_into("<I", adv, ADV_SIZE - 4, ADV_MAGIC3)
    return bytes(adv) * 2


def generate_extents(sectors, nptrs):
    """Coalesce a sector list into (lba, len) extents the way syslxmod.c
    does: contiguous, under 64 KB, not crossing a 64 KB segment boundary
    of the load address (ldlinux.sys loads at 0x8000)."""
    ex = []
    addr = 0x8000
    base = addr
    lba = 0
    ln = 0
    for sect in sectors:
        if ln:
            xbytes = (ln + 1) * SECTOR
            if sect == lba + ln and xbytes < 65536 and ((addr ^ (base + xbytes - 1)) & 0xFFFF0000) == 0:
                ln += 1
                addr += SECTOR
                continue
            ex.append((lba, ln))
        base = addr
        lba = sect
        ln = 1
        addr += SECTOR
    if ln:
        ex.append((lba, ln))
    if len(ex) > nptrs:
        raise UsbError(_("ldlinux.sys is too fragmented on this volume"))
    return ex


def patch(image, bootsect, sectors, subdir=None):
    """Patch the ldlinux.sys image and the boot-sector template with the
    file's sector map. Returns (patched image bytes, patched bootsect)."""
    image = bytearray(image)
    bootsect = bytearray(bootsect)
    nsect = ((len(image) + SECTOR - 1) // SECTOR) + 2
    if len(sectors) < nsect:
        raise UsbError(_("ldlinux.sys sector map is too short (%d < %d)") % (len(sectors), nsect))
    pa = None
    for off in range(0, len(image) - 4, 4):
        if struct.unpack_from("<I", image, off)[0] == LDLINUX_MAGIC:
            pa = off
            break
    if pa is None:
        raise UsbError(_("ldlinux.sys has no patch area"))
    (magic, instance, data_sectors, adv_sectors, dwords, checksum, maxtransfer, epaoffset) = \
        struct.unpack_from("<IIHHIIHH", image, pa)
    (advptroffset, diroffset, dirlen, subvoloffset, subvollen, secptroffset, secptrcnt,
     sect1ptr0, sect1ptr1, raidpatch) = struct.unpack_from("<HHHHHHHHHH", image, epaoffset)

    struct.pack_into("<I", bootsect, sect1ptr0, sectors[0] & 0xFFFFFFFF)
    struct.pack_into("<I", bootsect, sect1ptr1, sectors[0] >> 32)

    dw = len(image) >> 2
    struct.pack_into("<HHI", image, pa + 8, nsect - 2, 2, dw)
    for i, (lba, ln) in enumerate(generate_extents(sectors[1:nsect - 2], secptrcnt)):
        struct.pack_into("<QH", image, secptroffset + 10 * i, lba, ln)
    struct.pack_into("<QQ", image, advptroffset, sectors[nsect - 2], sectors[nsect - 1])
    if subdir:
        s = subdir.encode() + b"\0"
        if len(s) > dirlen:
            raise UsbError(_("Syslinux directory name too long: %s") % subdir)
        image[diroffset:diroffset + len(s)] = s
    struct.pack_into("<I", image, pa + 16, 0)
    csum = LDLINUX_MAGIC
    for i in range(dw):
        csum = (csum - struct.unpack_from("<I", image, i * 4)[0]) & 0xFFFFFFFF
    struct.pack_into("<I", image, pa + 16, csum)
    return bytes(image), bytes(bootsect)


# ------------------------------------------------------------ sector maps

def fiemap_sectors(path):
    """Partition-relative 512-byte sectors of a file, from FIEMAP (ext, ntfs3)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
        size = os.fstat(fd).st_size
        n = 256
        buf = bytearray(struct.pack("QQIIII", 0, 0xFFFFFFFFFFFFFFFF, 1, 0, n, 0) + bytes(56 * n))
        fcntl.ioctl(fd, FS_IOC_FIEMAP, buf)
        mapped = struct.unpack_from("QQIIII", buf)[3]
        sectors = []
        for i in range(mapped):
            logical, physical, length, _r1, _r2, flags = struct.unpack_from("QQQQQI", buf, 32 + 56 * i)
            if flags & 0x00000800:      # FIEMAP_EXTENT_UNKNOWN / not mapped
                raise UsbError(_("ldlinux.sys has unmapped extents"))
            for s in range(length // SECTOR):
                sectors.append(physical // SECTOR + s)
        want = (size + SECTOR - 1) // SECTOR
        return sectors[:max(want, len(sectors))]
    except OSError as e:
        raise UsbError(_("cannot map ldlinux.sys (%s); this file system driver has no FIEMAP") % e.strerror)
    finally:
        os.close(fd)


class _Fat:
    """Enough FAT12/16/32 to find a file's cluster chain by reading the
    partition directly."""

    def __init__(self, part):
        self.part = part
        bs = part.pread(512, 0)
        self.bps = struct.unpack_from("<H", bs, 0x0B)[0]
        self.spc = bs[0x0D]
        rsvd = struct.unpack_from("<H", bs, 0x0E)[0]
        nfats = bs[0x10]
        rootents = struct.unpack_from("<H", bs, 0x11)[0]
        total = struct.unpack_from("<H", bs, 0x13)[0] or struct.unpack_from("<I", bs, 0x20)[0]
        fatsz = struct.unpack_from("<H", bs, 0x16)[0] or struct.unpack_from("<I", bs, 0x24)[0]
        if not (self.bps and self.spc and fatsz and total):
            raise UsbError(_("not a FAT volume"))
        self.fat_off = rsvd * self.bps
        self.fat_len = fatsz * self.bps
        root_dir_sectors = (rootents * 32 + self.bps - 1) // self.bps
        self.root_off = (rsvd + nfats * fatsz) * self.bps
        self.root_len = root_dir_sectors * self.bps
        self.first_data = rsvd + nfats * fatsz + root_dir_sectors
        clusters = (total - self.first_data) // self.spc
        self.type = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
        self.root_cluster = struct.unpack_from("<I", bs, 0x2C)[0] if self.type == 32 else 0
        self._cache = {}

    def _fat_bytes(self, off, n):
        page = off // 4096
        out = b""
        while len(out) < n:
            if page not in self._cache:
                self._cache[page] = self.part.pread(4096, self.fat_off + page * 4096)
            start = (off + len(out)) - page * 4096
            out += self._cache[page][start:start + (n - len(out))]
            page += 1
        return out

    def next_cluster(self, c):
        if self.type == 32:
            v = struct.unpack("<I", self._fat_bytes(c * 4, 4))[0] & 0x0FFFFFFF
            return None if v >= 0x0FFFFFF8 or v < 2 else v
        if self.type == 16:
            v = struct.unpack("<H", self._fat_bytes(c * 2, 2))[0]
            return None if v >= 0xFFF8 or v < 2 else v
        v = struct.unpack("<H", self._fat_bytes(c + c // 2, 2))[0]
        v = (v >> 4) if (c & 1) else (v & 0xFFF)
        return None if v >= 0xFF8 or v < 2 else v

    def chain(self, c):
        out = []
        seen = set()
        while c is not None and c not in seen and len(out) < 1 << 20:
            seen.add(c)
            out.append(c)
            c = self.next_cluster(c)
        return out

    def cluster_offset(self, c):
        return (self.first_data + (c - 2) * self.spc) * self.bps

    def entries(self, cluster):
        """(name, attr, first_cluster, size) for a directory: the fixed
        root region on FAT12/16 when cluster is 0, else a cluster chain."""
        if cluster == 0 and self.type != 32:
            regions = [(self.root_off, self.root_len)]
        else:
            regions = [(self.cluster_offset(c), self.spc * self.bps) for c in self.chain(cluster or self.root_cluster)]
        lfn = {}
        for off, ln in regions:
            data = self.part.pread(ln, off)
            for i in range(0, len(data), 32):
                e = data[i:i + 32]
                if e[0] == 0:
                    return
                if e[0] == 0xE5:
                    lfn = {}
                    continue
                attr = e[11]
                if attr == 0x0F:
                    seq = e[0] & 0x1F
                    lfn[seq] = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "replace")
                    continue
                if attr & 0x08:
                    lfn = {}
                    continue
                name = "".join(lfn[k] for k in sorted(lfn)).split("\0", 1)[0] if lfn else ""
                lfn = {}
                if not name:
                    base = e[0:8].decode("ascii", "replace").rstrip()
                    ext = e[8:11].decode("ascii", "replace").rstrip()
                    if e[0] == 0x05:
                        base = "\xe5" + base[1:]
                    name = base + ("." + ext if ext else "")
                first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16 if self.type == 32 else 0)
                yield name, attr, first, struct.unpack_from("<I", e, 28)[0]

    def file_sectors(self, path):
        cluster = 0
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        for i, comp in enumerate(parts):
            found = None
            for name, attr, first, size in self.entries(cluster):
                if name.lower() == comp.lower():
                    found = (attr, first, size)
                    break
            if found is None:
                raise UsbError(_("%s not found on the FAT volume") % path)
            attr, first, size = found
            if i < len(parts) - 1:
                if not attr & 0x10:
                    raise UsbError(_("%s is not a directory") % comp)
                cluster = first
        sectors = []
        per_cluster = self.spc * self.bps // SECTOR
        for c in self.chain(first):
            base = self.cluster_offset(c) // SECTOR
            sectors.extend(range(base, base + per_cluster))
        return sectors[:max((size + SECTOR - 1) // SECTOR, 0)] or sectors


# ------------------------------------------------------------ install

def install(part, mount_dir, cfg_dir, fs, log=None):
    """Put ldlinux.sys (+ ldlinux.c32) into /cfg_dir on the mounted volume
    and write the Syslinux boot record. `part` is the partition's
    BlockTarget, `mount_dir` where it is mounted, `fs` fat16|fat32|ntfs|ext*."""
    lib = lib_dir()
    with open(os.path.join(lib, "ldlinux.sys"), "rb") as f:
        image = f.read()
    with open(os.path.join(lib, "ldlinux.bss"), "rb") as f:
        bss = f.read()
    target_dir = os.path.join(mount_dir, cfg_dir) if cfg_dir else mount_dir
    os.makedirs(target_dir, exist_ok=True)
    sys_path = os.path.join(target_dir, "ldlinux.sys")
    # Rewrite from scratch so the file is laid out fresh (and not a leftover
    # from the image with the wrong version).
    if os.path.exists(sys_path):
        _clear_fat_attrs(sys_path)
        os.remove(sys_path)
    data = image + make_adv()
    fd = os.open(sys_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_SYNC, 0o444)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.sync()
    rel = "/" + (cfg_dir.strip("/") + "/" if cfg_dir else "") + "ldlinux.sys"
    if fs in ("fat16", "fat32"):
        sectors = _Fat(part).file_sectors(rel)
    else:
        sectors = fiemap_sectors(sys_path)
    subdir = "/" + cfg_dir.strip("/") + "/" if cfg_dir else None
    patched, bootsect = patch(image, bss, sectors, subdir)
    # Back into the file in place: same sectors, so the map stays right.
    fd = os.open(sys_path, os.O_WRONLY | os.O_SYNC)
    try:
        os.pwrite(fd, patched, 0)
        os.fsync(fd)
        if fs in ("fat16", "fat32"):
            try:
                fcntl.ioctl(fd, FAT_IOCTL_SET_ATTRIBUTES, struct.pack("I", ATTR_RO | ATTR_HIDDEN | ATTR_SYS))
            except OSError:
                pass
    finally:
        os.close(fd)
    os.sync()
    # ldlinux.c32 must match ldlinux.sys exactly.
    with open(os.path.join(lib, "ldlinux.c32"), "rb") as f:
        c32 = f.read()
    with open(os.path.join(target_dir, "ldlinux.c32"), "wb") as f:
        f.write(c32)
    os.sync()

    # Volume boot record: head + code from the template, BPB from mkfs.
    cur = bytearray(part.pread(512, 0))
    if fs in ("fat16", "fat32"):
        cur[0:11] = bootsect[0:11]
        cur[0x5A:0x1FE] = bootsect[0x5A:0x1FE]
    elif fs == "ntfs":
        cur[0:3] = bootsect[0:3]
        cur[0x54:0x1FE] = bootsect[0x54:0x1FE]
    else:
        cur[0:512] = bootsect[0:512]
    part.pwrite(bytes(cur), 0)
    part.fsync()
    if log:
        log(f"Installed Syslinux {version()} ({len(sectors)} sectors mapped) to /{cfg_dir}" if cfg_dir
            else f"Installed Syslinux {version()} ({len(sectors)} sectors mapped)")


def _clear_fat_attrs(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, FAT_IOCTL_SET_ATTRIBUTES, struct.pack("I", 0))
        finally:
            os.close(fd)
    except OSError:
        pass
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
