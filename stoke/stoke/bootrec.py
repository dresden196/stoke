"""Master and partition boot records.

The byte arrays in bootcode.py are the ones ms-sys and Rufus write. mkfs.fat
and mkntfs both leave a "this is not a bootable disk" stub in the volume boot
record, so for anything that boots in BIOS mode we replace it: the NTFS and
FAT32 records here load BOOTMGR, the FreeDOS ones load KERNEL.SYS.
"""

import os
import struct

from . import bootcode as bc
from .util import UsbError, read_at, write_at, KB

from .i18n import _

MBR_KINDS = {
    "rufus": bc.MBR_RUFUS_0X0,          # masquerades the USB as 0x81 when asked
    "win7": bc.MBR_WIN7_0X0,
    "zero": bc.MBR_ZERO_0X0,
    "syslinux": bc.MBR_SYSLINUX_0X0,
    "syslinux_gpt": bc.MBR_GPT_SYSLINUX_0X0,
    "grub2": bc.MBR_GRUB2_0X0,
    "grub4dos": bc.MBR_GRUB_0X0,
    "msg": bc.MBR_MSG_RUFUS_0X0,        # prints the text stored after the GPT
    "kolibri": bc.MBR_KOLIBRI_0X0,
    "reactos": bc.MBR_REACTOS_0X0,
}


def _sector_size(dev):
    if hasattr(dev, "sector_size"):
        return dev.sector_size
    try:
        with open(f"/sys/class/block/{os.path.basename(os.path.realpath(dev))}/queue/logical_block_size") as f:
            return int(f.read().strip())
    except OSError:
        return 512


def _write_bootmarks(dev, base, sector_size):
    """0x55AA at the end of every 512-byte block within the (maybe 4K) sector."""
    pos = 0x1FE
    while pos < sector_size:
        write_at(dev, base + pos, b"\x55\xaa")
        pos += 0x200


def write_mbr_code(dev, kind, log=None):
    """Replace the boot code in sector 0, keeping the disk id and partition table."""
    if kind not in MBR_KINDS:
        raise UsbError(_("unknown MBR type %s") % kind)
    code = MBR_KINDS[kind]
    if len(code) > 0x1B8:
        # mbr_zero is 446 bytes: it covers the disk signature too, on purpose.
        pass
    write_at(dev, 0, code)
    _write_bootmarks(dev, 0, _sector_size(dev))
    if log:
        log(f"Using {kind} MBR")


def fix_mbr_entries(dev, main_index, fs=None, active_id=None, log=None):
    """Rufus's post-format MBR corrections: LBA types for FAT, and the boot
    indicator (0x80, or 0x81 to masquerade for WinPE)."""
    mbr = bytearray(read_at(dev, 0, 512))
    entry = 0x1BE + 16 * main_index
    ptype = mbr[entry + 4]
    if fs == "fat16":
        if ptype in (0x04, 0x06):
            mbr[entry + 4] = 0x0E
    elif fs == "fat32":
        if ptype == 0x0B:
            mbr[entry + 4] = 0x0C
    if active_id is not None:
        for i in range(4):
            mbr[0x1BE + 16 * i] = 0x00
        mbr[entry] = active_id
        if log:
            log(f"Set bootable USB partition as 0x{active_id:02X}")
    write_at(dev, 0, bytes(mbr))


def fix_mbr_chs(dev, sector_size=512, heads=255, spt=63, log=None):
    """Rewrite the CHS fields of every MBR entry for a 255/63 geometry, the
    one BIOSes assume. util-linux fills them from the kernel's fake USB
    geometry (64/32), and NT-era boot sectors, which read by CHS, then
    address the wrong sectors."""
    mbr = bytearray(read_at(dev, 0, 512))
    if mbr[0x1FE:0x200] != b"\x55\xaa":
        return
    per_cyl = heads * spt

    def chs(lba):
        if lba >= 1024 * per_cyl:
            return bytes([0xFE, 0xFF, 0xFF])
        c, rem = divmod(lba, per_cyl)
        h, s = divmod(rem, spt)
        return bytes([h, ((c >> 2) & 0xC0) | (s + 1), c & 0xFF])

    changed = False
    for i in range(4):
        e = 0x1BE + 16 * i
        if mbr[e + 4] == 0:
            continue
        start, size = struct.unpack_from("<II", mbr, e + 8)
        new = mbr[:e + 1] + chs(start) + mbr[e + 4:e + 5] + chs(start + size - 1) + mbr[e + 8:]
        if new != mbr:
            mbr = bytearray(new)
            changed = True
    if changed:
        write_at(dev, 0, bytes(mbr))
        if log:
            log(f"Set CHS fields for a {heads}/{spt} geometry")


def write_sbr(dev, data, offset, first_partition_offset, log=None):
    """Secondary boot record between the MBR and the first partition:
    GRUB2's core.img, or the text for the protective-message MBR."""
    if offset + len(data) > first_partition_offset:
        raise UsbError(_("not enough space before the first partition for the boot loader "
                         "(uncheck 'Add fixes for old BIOSes')"))
    write_at(dev, offset, data)
    if log:
        log(f"Wrote {len(data)} byte secondary boot record at offset {offset}")


# ---------------------------------------------------------------- PBRs

def _is_fat32(sector):
    return sector[0x52:0x5A] == b"FAT32   "


def _is_fat16(sector):
    return sector[0x36:0x3E] in (b"FAT16   ", b"FAT12   ")


def _is_ntfs(sector):
    return sector[0x03:0x0B] == b"NTFS    "


def write_pbr(part_dev, fs, variant="std", log=None, drive_id=0x80):
    """variant: 'std' (Windows 9x/plain), 'nt' (NTLDR), 'pe' (BOOTMGR),
    'fd' (FreeDOS). NTFS always loads BOOTMGR.

    drive_id is what the boot sector's BPB says the BIOS drive is, and
    the Microsoft boot sectors trust it over the DL they are handed. Under
    the masquerading MBR (0x80 and 0x81 swapped) it must be 0x81 so the
    boot sector still reads the stick."""
    sector_size = _sector_size(part_dev)
    boot = read_at(part_dev, 0, max(512, sector_size))
    if fs == "ntfs":
        if not _is_ntfs(boot):
            raise UsbError(_("new volume does not have an NTFS boot sector"))
        write_at(part_dev, 0x0, bc.BR_NTFS_0X0)
        write_at(part_dev, 0x54, bc.BR_NTFS_0X54)
        if log:
            log("Wrote NTFS (BOOTMGR) partition boot record")
        return
    if fs == "fat32":
        if not _is_fat32(boot):
            raise UsbError(_("new volume does not have a FAT32 boot sector"))
        tables = {
            "std": (bc.BR_FAT32_0X52, bc.BR_FAT32_0X3F0, None),
            "nt": (bc.BR_FAT32NT_0X52, bc.BR_FAT32NT_0X3F0, bc.BR_FAT32NT_0X1800),
            "pe": (bc.BR_FAT32PE_0X52, bc.BR_FAT32PE_0X3F0, bc.BR_FAT32PE_0X1800),
            "fd": (bc.BR_FAT32FD_0X52, bc.BR_FAT32FD_0X3F0, None),
            "kos": (bc.BR_FAT32KOS_0X52, None, None),
        }
        c52, c3f0, c1800 = tables[variant]
        # Primary at sector 0, backup at sector 6: both must agree.
        backup = struct.unpack_from("<H", boot, 0x32)[0] or 6
        for base in (0, backup * sector_size):
            if base and not _is_fat32(read_at(part_dev, base, 512)):
                if log:
                    log("No FAT32 backup boot sector found; skipping it")
                break
            write_at(part_dev, base + 0x0, bc.BR_FAT32_0X0)
            write_at(part_dev, base + 0x52, c52)
            if c3f0 is not None:
                write_at(part_dev, base + 0x3F0, c3f0)
            if c1800 is not None:
                write_at(part_dev, base + 0x1800, c1800)
            # BIOS drive number: the FAT32 BPB keeps it at 0x40.
            write_at(part_dev, base + 0x40, bytes([drive_id]))
            # Sectors per track and heads as the BIOS will report them (63/255);
            # mkfs.fat copied the kernel's fake USB geometry instead.
            write_at(part_dev, base + 0x18, struct.pack("<HH", 63, 255))
        if log:
            log(f"Wrote FAT32 ({variant}) partition boot record")
        return
    if fs == "fat16":
        if not _is_fat16(boot):
            raise UsbError(_("new volume does not have a FAT16 boot sector"))
        code = bc.BR_FAT16FD_0X3E if variant == "fd" else bc.BR_FAT16_0X3E
        write_at(part_dev, 0x0, bc.BR_FAT16_0X0)
        write_at(part_dev, 0x3E, code)
        write_at(part_dev, 0x24, bytes([drive_id]))  # drive number in the FAT16 BPB
        write_at(part_dev, 0x18, struct.pack("<HH", 63, 255))
        if log:
            log(f"Wrote FAT16 ({variant}) partition boot record")
        return
    if fs in ("ext2", "ext3", "ext4", "exfat"):
        return
    raise UsbError(_("no boot record support for %s") % fs)


def describe_mbr(dev):
    """Best-effort name of what is in sector 0 (for the log)."""
    try:
        mbr = read_at(dev, 0, 512)
    except OSError:
        return "unreadable"
    if mbr[0x1FE:0x200] != b"\x55\xaa":
        return "no boot signature"
    if mbr[:4] == bc.MBR_RUFUS_0X0[:4]:
        return "Rufus"
    if mbr[:len(bc.MBR_MSG_RUFUS_0X0)] == bc.MBR_MSG_RUFUS_0X0:
        return "Rufus protective message"
    if mbr[:0x1B8] == bc.MBR_WIN7_0X0[:0x1B8]:
        return "Windows 7"
    if mbr[:len(bc.MBR_SYSLINUX_0X0)] == bc.MBR_SYSLINUX_0X0:
        return "Syslinux"
    if mbr[:2] == bc.MBR_GRUB2_0X0[:2] and mbr[0x68:0x94] == bc.MBR_GRUB2_0X0[0x68:0x94]:
        return "GRUB 2"
    if mbr[:0x1B8] == bytes(0x1B8):
        return "zeroed"
    if mbr[0x1C2] == 0xEE:
        return "protective (GPT)"
    return "unknown"
