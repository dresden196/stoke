"""Raw image writing ("DD mode"), with transparent decompression.

Accepts .iso/.img/.raw/.bin, fixed .vhd (raw with a 512-byte footer), and
any of those wrapped in gzip, bzip2, xz or zstd. The whole image goes to
the start of the disk, then the drive is flushed and the kernel is asked to
re-read the new partition table.
"""

import bz2
import gzip
import lzma
import os
import struct
import subprocess

from .util import UsbError, Cancelled, MB, human_size, sync_device, run

from .i18n import _

MAGIC = [
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
]


def detect_compression(path):
    with open(path, "rb") as f:
        head = f.read(8)
    for magic, name in MAGIC:
        if head.startswith(magic):
            return name
    return None


def vhd_footer_size(path):
    """A fixed VHD is a raw image followed by a 512-byte 'conectix' footer;
    return the payload size or 0 when this is not one."""
    size = os.path.getsize(path)
    if size < 512:
        return 0
    with open(path, "rb") as f:
        f.seek(size - 512)
        foot = f.read(512)
    if foot[:8] != b"conectix":
        return 0
    disk_type = struct.unpack(">I", foot[60:64])[0]
    if disk_type != 2:   # 2 = fixed; dynamic/differencing need conversion
        raise UsbError(_("only fixed-size VHD images can be written; convert dynamic VHD/VHDX with qemu-img first"))
    return size - 512


def open_stream(path):
    """Return (file-like object yielding raw bytes, expected size or None)."""
    comp = detect_compression(path)
    if comp == "gzip":
        f = gzip.open(path, "rb")
        # gzip stores the uncompressed size mod 2^32 in the last 4 bytes; only
        # trust it for images under 4 GB.
        with open(path, "rb") as raw:
            raw.seek(-4, os.SEEK_END)
            isize = struct.unpack("<I", raw.read(4))[0]
        return f, (isize if isize and os.path.getsize(path) < 4 * 1024 * MB else None)
    if comp == "bzip2":
        return bz2.open(path, "rb"), None
    if comp == "xz":
        return lzma.open(path, "rb"), None
    if comp == "zstd":
        p = subprocess.Popen(["zstd", "-dc", "--", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        size = None
        try:
            r = subprocess.run(["zstd", "-l", "--", path], capture_output=True, text=True)
            for line in r.stdout.splitlines():
                parts = line.split()
                # "Decompressed" column, when the frame header carries it
                if len(parts) >= 4 and parts[0].isdigit() and "MiB" in line or "GiB" in line:
                    pass
        except Exception:
            pass
        return p.stdout, size
    size = vhd_footer_size(path) or os.path.getsize(path)
    return open(path, "rb"), size


def write_image(image_path, disk, backend=None, emitter=None, cancel=None, log=None, chunk=4 * MB, verify=False):
    """DD mode: the image, decompressed on the fly, straight onto the
    disk (a BlockTarget)."""
    disk_size = disk.size
    src, expected = open_stream(image_path)
    if expected and expected > disk_size:
        raise UsbError(_("image is %s but the drive holds only %s") % (human_size(expected), human_size(disk_size)))
    if log:
        log(f"Writing {os.path.basename(image_path)}" + (f" ({human_size(expected)})" if expected else "") + f" to {disk.path}")
    written = 0
    limit = expected if expected else None
    try:
        while True:
            if cancel:
                cancel.check()
            want = chunk if limit is None else min(chunk, limit - written)
            if want <= 0:
                break
            buf = src.read(want)
            if not buf:
                break
            # Pad the final block to the sector size: the kernel refuses a
            # partial-sector write on a block device.
            if len(buf) % 512:
                buf = buf + bytes(512 - len(buf) % 512)
            if written + len(buf) > disk_size:
                raise UsbError(_("image is larger than the drive"))
            disk.pwrite(buf, written)
            written += len(buf)
            if emitter:
                emitter.progress("write", (written / expected) if expected else None,
                                 f"{human_size(written)} written")
        disk.fsync()
    finally:
        try:
            src.close()
        except Exception:
            pass
    if log:
        log(f"Wrote {human_size(written)}")
    if verify and not detect_compression(image_path):
        _verify(image_path, disk, backend, written, emitter, cancel, log)
    sync_device(disk)
    if backend:
        backend.rescan(disk, (), log)
    return written


def _verify(image_path, disk, backend, length, emitter, cancel, log):
    """Read back and compare, the way balenaEtcher does. Drops the page
    cache first so we compare against the flash, not RAM."""
    if backend:
        backend.drop_cache(disk)
    chunk = 4 * MB
    done = 0
    with open(image_path, "rb") as a:
        while done < length:
            if cancel:
                cancel.check()
            n = min(chunk, length - done)
            x = a.read(n)
            y = disk.pread(n, done)
            if x != y[:len(x)]:
                raise UsbError(_("verification failed at offset %s: the drive does not hold what was written") % done)
            done += n
            if emitter:
                emitter.progress("verify", done / length)
    if log:
        log("Verification passed")


def zero_drive(disk, emitter=None, cancel=None, log=None, full=True):
    """Erase: full zero of the drive, or just the first and last 4 MB."""
    disk_size = disk.size
    zero = bytes(4 * MB)
    if not full:
        disk.pwrite(zero[:min(len(zero), disk_size)], 0)
        disk.pwrite(zero[:min(len(zero), disk_size)], max(0, disk_size - len(zero)))
    else:
        done = 0
        while done < disk_size:
            if cancel:
                cancel.check()
            n = min(len(zero), disk_size - done)
            disk.pwrite(zero[:n], done)
            done += n
            if emitter:
                emitter.progress("write", done / disk_size, f"{human_size(done)} zeroed")
    disk.fsync()
    sync_device(disk)
    if log:
        log("Drive zeroed" if full else "Partition table and signatures cleared")
