"""Partition layout: the rules from Rufus's CreatePartition(), written by hand.

The order and sizes are not arbitrary. A UEFI:NTFS partition must be a plain
data partition on GPT, because Windows Setup refuses to install when the
media carries a second ESP. An ESP for Windows To Go goes first because
Microsoft says so and some firmware agrees. Everything is aligned to 1 MB
unless the user asked for old-BIOS fixes, which aligns to a fake track.
"""

import os
import struct
import time
import uuid
import zlib

from .util import MB, KB, align_up, align_down, UsbError, read_at, write_at

from .i18n import _

# GPT type GUIDs
GPT_MS_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
GPT_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GPT_MSR = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
GPT_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
# Bit 63 of the GPT attributes: "no drive letter" for Windows.
GPT_ATTR_NO_DRIVE_LETTER = 63

# MBR partition types
MBR_FAT16_LBA = 0x0E
MBR_FAT32_LBA = 0x0C
MBR_NTFS = 0x07          # also exFAT and UDF
MBR_LINUX = 0x83
MBR_ESP = 0xEF
MBR_EXTRA_COMPAT = 0xEA  # Rufus's "BIOS Compatibility" filler partition

# Rufus writes this MBR disk id when the drive was made MBR+UEFI, so the
# scheme can be recognised again later.
MBR_UEFI_MARKER = 0x49464555  # "UEFI"

ESP_SIZE = 260 * MB
MSR_SIZE = 128 * MB
MAX_PARTITIONS = 16


class Partition:
    def __init__(self, name, role):
        self.name = name        # GPT name / log label
        self.role = role        # main | esp | msr | uefi_ntfs | persistence | compat
        self.offset = 0
        self.size = 0
        self.mbr_type = 0
        self.gpt_type = GPT_MS_DATA
        self.bootable = False
        self.attrs = []
        self.device = None      # /dev/sdX<n> once created (a name; see .target)
        self.target = None      # BlockTarget once created
        self.number = 0
        self.uuid = None        # GPT unique partition GUID (upper-case string)
        self.disk_guid = None

    def __repr__(self):
        return f"<{self.name} @{self.offset} +{self.size}>"


def plan(disk_size, sector_size, scheme, fs, bootable=True, extras=(), persistence_size=0,
         old_bios_fixes=False, cluster_size=0, uefi_ntfs_size=1 * MB, write_as_esp=False):
    """Return a list of Partition in disk order. `extras` is a set of roles
    among {'uefi_ntfs','esp','msr','persistence','compat'}."""
    extras = set(extras)
    if scheme not in ("mbr", "gpt"):
        raise UsbError(_("unknown partition scheme %s") % scheme)
    cluster = cluster_size or 512
    # Linux has no real geometry for USB; BIOSes assume 63 sectors/track.
    bytes_per_track = 63 * sector_size
    parts = []

    if scheme == "gpt" or not old_bios_fixes:
        first = 1 * MB
    else:
        # Align to a cylinder that is itself aligned to the cluster, doubled so
        # GRUB2's core.img still fits in the gap (Rufus does the same).
        first = align_up(bytes_per_track, cluster) * 2

    offset = first
    if "esp" in extras and scheme == "gpt":
        p = Partition("EFI System Partition", "esp")
        p.offset, p.size = offset, ESP_SIZE
        p.gpt_type = GPT_ESP
        parts.append(p)
        offset = align_up(offset + p.size, bytes_per_track)
        if cluster % sector_size == 0:
            offset = align_down(offset, cluster)
        extras.discard("esp")
    if "msr" in extras:
        if scheme != "gpt":
            raise UsbError(_("an MSR partition needs GPT"))
        p = Partition("Microsoft Reserved Partition", "msr")
        p.offset, p.size = offset, MSR_SIZE
        p.gpt_type = GPT_MSR
        parts.append(p)
        offset = align_up(offset + p.size, bytes_per_track)
        if cluster % sector_size == 0:
            offset = align_down(offset, cluster)
        extras.discard("msr")

    main = Partition("EFI System Partition" if write_as_esp else "Main Data Partition", "main")
    main.offset = offset
    main.bootable = bootable
    parts.append(main)
    tail = []
    if "persistence" in extras:
        if persistence_size <= 0:
            raise UsbError(_("persistence requested with no size"))
        p = Partition("Linux Persistence", "persistence")
        p.size = align_up(persistence_size, bytes_per_track)
        p.gpt_type = GPT_LINUX
        p.mbr_type = MBR_LINUX
        tail.append(p)
    if "esp" in extras:
        p = Partition("EFI System Partition", "esp")
        p.size = align_up(ESP_SIZE, bytes_per_track)
        p.gpt_type = GPT_ESP
        p.mbr_type = MBR_ESP
        tail.append(p)
    elif "uefi_ntfs" in extras:
        p = Partition("UEFI:NTFS", "uefi_ntfs")
        p.size = align_up(uefi_ntfs_size, bytes_per_track)
        # Deliberately a data partition on GPT (see module docstring) and
        # hidden from Windows.
        p.gpt_type = GPT_MS_DATA
        p.attrs = [GPT_ATTR_NO_DRIVE_LETTER]
        p.mbr_type = MBR_ESP
        tail.append(p)
    elif "compat" in extras:
        p = Partition("BIOS Compatibility", "compat")
        p.size = bytes_per_track
        p.mbr_type = MBR_EXTRA_COMPAT
        tail.append(p)
    parts.extend(tail)
    if len(parts) > MAX_PARTITIONS:
        raise UsbError(_("too many partitions"))

    # Extra partitions are packed at the end of the disk, track-aligned.
    last = disk_size
    if scheme == "gpt":
        last -= 33 * sector_size
    for p in reversed(tail):
        if p.size >= last:
            raise UsbError(_("%s does not fit on this drive") % p.name)
        p.offset = align_down(last - p.size, bytes_per_track)
        last = p.offset
    if last <= main.offset:
        raise UsbError(_("drive is too small for this layout"))
    main.size = align_down(last - main.offset, bytes_per_track)
    if cluster % sector_size == 0:
        main.size = align_down(main.size, cluster)
    if main.size <= 0:
        raise UsbError(_("drive is too small for this layout"))

    main.mbr_type = {
        "fat16": MBR_FAT16_LBA, "fat32": MBR_FAT32_LBA,
        "ntfs": MBR_NTFS, "exfat": MBR_NTFS, "udf": MBR_NTFS,
        "ext2": MBR_LINUX, "ext3": MBR_LINUX, "ext4": MBR_LINUX,
    }.get(fs)
    if main.mbr_type is None:
        raise UsbError(_("unsupported file system %s") % fs)
    if write_as_esp:
        main.mbr_type = MBR_ESP
        main.gpt_type = GPT_ESP
    return parts



# ------------------------------------------------------------- tables

def _chs(lba, heads=255, spt=63):
    """CHS bytes for a 255/63 geometry, the one BIOSes assume. The kernel's
    fake USB geometry (64/32) would make NT-era boot sectors, which read by
    CHS, read the wrong sectors."""
    per_cyl = heads * spt
    if lba >= 1024 * per_cyl:
        return bytes([0xFE, 0xFF, 0xFF])
    c, rem = divmod(lba, per_cyl)
    h, s = divmod(rem, spt)
    return bytes([h, ((c >> 2) & 0xC0) | (s + 1), c & 0xFF])


def build_mbr(parts, sector_size, disk_id=None, existing=None):
    """The 512-byte MBR for `parts`: boot code kept from `existing` (or
    zero), a disk id, LBA-typed entries with 255/63 CHS fields."""
    mbr = bytearray(existing[:0x1B8]) if existing else bytearray(0x1B8)
    mbr += bytes(512 - len(mbr))
    struct.pack_into("<I", mbr, 0x1B8, disk_id & 0xFFFFFFFF)
    mbr[0x1BC:0x1BE] = b"\x00\x00"
    if len(parts) > 4:
        raise UsbError(_("too many partitions for an MBR table"))
    for i, p in enumerate(parts):
        e = 0x1BE + 16 * i
        start = p.offset // sector_size
        size = p.size // sector_size
        if start > 0xFFFFFFFF or size > 0xFFFFFFFF:
            raise UsbError(_("drive too large for an MBR table; use GPT"))
        mbr[e] = 0x80 if p.bootable else 0x00
        mbr[e + 1:e + 4] = _chs(start)
        mbr[e + 4] = p.mbr_type
        mbr[e + 5:e + 8] = _chs(start + size - 1)
        struct.pack_into("<II", mbr, e + 8, start, size)
    mbr[0x1FE:0x200] = b"\x55\xaa"
    return bytes(mbr)


def _guid_bytes(s):
    return uuid.UUID(s).bytes_le


def build_gpt(parts, disk_size, sector_size, disk_guid=None):
    """Protective MBR, primary header + entries, backup entries + header.
    Returns (mbr, primary_bytes_at_lba1, backup_bytes, backup_offset)."""
    total = disk_size // sector_size
    entries = 128
    entry_size = 128
    table_bytes = entries * entry_size
    table_sectors = (table_bytes + sector_size - 1) // sector_size
    first_usable = 34 if sector_size == 512 else 2 + table_sectors
    last_usable = total - 2 - table_sectors
    disk_guid = disk_guid or str(uuid.uuid4()).upper()

    table = bytearray(table_bytes)
    for i, p in enumerate(parts):
        e = i * entry_size
        p.uuid = p.uuid or str(uuid.uuid4()).upper()
        table[e:e + 16] = _guid_bytes(p.gpt_type)
        table[e + 16:e + 32] = _guid_bytes(p.uuid)
        start = p.offset // sector_size
        end = (p.offset + p.size) // sector_size - 1
        if start < first_usable or end > last_usable:
            raise UsbError(_("%s does not fit inside the GPT usable area") % p.name)
        struct.pack_into("<QQ", table, e + 32, start, end)
        attrs = 0
        for bit in p.attrs:
            attrs |= 1 << bit
        struct.pack_into("<Q", table, e + 48, attrs)
        name = p.name.encode("utf-16-le")[:72]
        table[e + 56:e + 56 + len(name)] = name
    table_crc = zlib.crc32(bytes(table)) & 0xFFFFFFFF

    def header(my_lba, alt_lba, table_lba):
        h = bytearray(sector_size)
        h[0:8] = b"EFI PART"
        struct.pack_into("<I", h, 8, 0x00010000)
        struct.pack_into("<I", h, 12, 92)
        struct.pack_into("<I", h, 16, 0)     # header crc, filled below
        struct.pack_into("<I", h, 20, 0)
        struct.pack_into("<QQQQ", h, 24, my_lba, alt_lba, first_usable, last_usable)
        h[56:72] = _guid_bytes(disk_guid)
        struct.pack_into("<QIII", h, 72, table_lba, entries, entry_size, table_crc)
        crc = zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF
        struct.pack_into("<I", h, 16, crc)
        return bytes(h)

    backup_table_lba = total - 1 - table_sectors
    primary = header(1, total - 1, 2) + bytes(table) + bytes(table_sectors * sector_size - table_bytes)
    backup = bytes(table) + bytes(table_sectors * sector_size - table_bytes) + header(total - 1, 1, backup_table_lba)

    # Protective MBR: one 0xEE entry covering the disk (capped at 32 bits).
    mbr = bytearray(512)
    e = 0x1BE
    mbr[e] = 0x00
    mbr[e + 1:e + 4] = bytes([0x00, 0x02, 0x00])
    mbr[e + 4] = 0xEE
    mbr[e + 5:e + 8] = bytes([0xFF, 0xFF, 0xFF])
    struct.pack_into("<II", mbr, e + 8, 1, min(total - 1, 0xFFFFFFFF))
    mbr[0x1FE:0x200] = b"\x55\xaa"
    return bytes(mbr), primary, backup, backup_table_lba * sector_size, disk_guid


def describe(parts, scheme, sector_size):
    lines = [f"label: {'dos' if scheme == 'mbr' else 'gpt'}"]
    for p in parts:
        lines.append(f"{p.name}: start={p.offset // sector_size} size={p.size // sector_size} "
                     + (f"type={p.mbr_type:02x}" if scheme == "mbr" else f"type={p.gpt_type}")
                     + (" bootable" if p.bootable and scheme == "mbr" else ""))
    return lines


def clear_partition_starts(disk, parts):
    """Zero the first sectors of each partition so stale superblocks can't
    confuse the kernel, udev, or the desktop."""
    zero = bytes(min(128 * KB, disk.sector_size * 256))
    for p in parts:
        n = min(len(zero), p.size)
        disk.pwrite(zero[:n], p.offset)
    disk.fsync()


def wipe_disk_signatures(disk, log=None):
    """Rufus's ClearMBRGPT: zero the first and last MB, MBR and both GPTs,
    and the places other file systems keep their superblocks."""
    zero = bytes(1 * MB)
    disk.pwrite(zero, 0)
    disk.pwrite(zero[:min(1 * MB, disk.size)], max(0, disk.size - 1 * MB))
    disk.fsync()


def apply(disk, backend, parts, scheme, mbr_uefi_marker=False, log=None):
    """Write the partition table through `disk` (a BlockTarget), have the
    kernel re-read it, and open a BlockTarget for each partition."""
    sector = disk.sector_size
    if log:
        for line in describe(parts, scheme, sector):
            log("  table: " + line)
    if scheme == "mbr":
        sig = MBR_UEFI_MARKER if mbr_uefi_marker else ((int(time.time() * 1000) & 0xFFFFFFFF) or 1)
        disk.pwrite(build_mbr(parts, sector, sig), 0)
        # No stale GPT may survive behind an MBR: firmware prefers it.
        disk.pwrite(bytes(sector), sector)
    else:
        mbr, primary, backup, backup_off, guid = build_gpt(parts, disk.size, sector)
        disk.pwrite(mbr, 0)
        disk.pwrite(primary, sector)
        disk.pwrite(backup, backup_off)
        if log:
            log(f"  disk GUID {guid}")
        for p in parts:
            p.disk_guid = guid
    disk.fsync()
    for i, p in enumerate(parts, 1):
        p.number = i
        p.device = partition_device(disk.path, i)
    backend.rescan(disk, [p.number for p in parts], log)
    for p in parts:
        p.target = backend.open_partition(disk, p.number)
        if abs(p.target.size - p.size) > sector:
            raise UsbError(_("%s came back with size %s, expected %s") % (p.device, p.target.size, p.size))
    return parts


def partition_device(dev, number):
    base = os.path.basename(os.path.realpath(dev))
    sep = "p" if base[-1].isdigit() else ""
    return f"/dev/{base}{sep}{number}"


def read_layout(disk):
    """What is on the disk now, for the log: {'label': 'dos'|'gpt',
    'id': ..., 'partitions': [{'node', 'start', 'size', 'type', 'uuid'}]}."""
    try:
        mbr = read_at(disk, 0, 512)
    except OSError:
        return None
    if mbr[0x1FE:0x200] != b"\x55\xaa":
        return None
    sector = disk.sector_size
    if mbr[0x1C2] == 0xEE:
        hdr = read_at(disk, sector, sector)
        if hdr[:8] != b"EFI PART":
            return {"label": "gpt", "id": None, "partitions": []}
        table_lba, n, esz = struct.unpack_from("<QII", hdr, 72)
        guid = str(uuid.UUID(bytes_le=bytes(hdr[56:72]))).upper()
        table = read_at(disk, table_lba * sector, n * esz)
        parts = []
        for i in range(n):
            e = table[i * esz:(i + 1) * esz]
            if e[:16] == bytes(16):
                continue
            start, end = struct.unpack_from("<QQ", e, 32)
            parts.append({"node": partition_device(disk.path, i + 1), "start": start * sector,
                          "size": (end - start + 1) * sector,
                          "type": str(uuid.UUID(bytes_le=bytes(e[:16]))).upper(),
                          "uuid": str(uuid.UUID(bytes_le=bytes(e[16:32]))).upper()})
        return {"label": "gpt", "id": guid, "partitions": parts}
    parts = []
    for i in range(4):
        e = 0x1BE + 16 * i
        if mbr[e + 4] == 0:
            continue
        start, size = struct.unpack_from("<II", mbr, e + 8)
        parts.append({"node": partition_device(disk.path, i + 1), "start": start * sector, "size": size * sector,
                      "type": f"{mbr[e + 4]:02x}", "bootable": mbr[e] == 0x80})
    return {"label": "dos", "id": f"0x{struct.unpack_from('<I', mbr, 0x1B8)[0]:08x}", "partitions": parts}
