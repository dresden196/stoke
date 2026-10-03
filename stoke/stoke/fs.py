"""File systems, made the way Rufus makes them: on the partition, by us.

mkfs runs on a sparse image file the size of the partition and only the
blocks it touched are copied to the drive. That keeps one code path for the
root and the udisks2 backends, needs no mkfs on the host when the tools are
bundled, and lets mkfs.fat and mkntfs be told the 255/63 geometry and the
partition start that the BIOS boot sectors expect, which they cannot learn
from a USB stick's fake geometry.
"""

import os
import shutil
import struct
import subprocess
import tempfile

from .util import run, UsbError, MB, KB, GB, human_size, which

from .i18n import _

VALID_LABEL_CHARS_FAT = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !#$%&'()-@^_`{}~")
FS_LABEL_MAX = {"fat16": 11, "fat32": 11, "exfat": 11, "ntfs": 32,
                "ext2": 16, "ext3": 16, "ext4": 16, "udf": 126}


def valid_label(label, fs, disk_size=0):
    """Rufus's ToValidLabel: strip what the file system refuses, upper-case
    FAT labels, and fall back to a size label if what remains is mostly
    underscores."""
    fat = fs in ("fat16", "fat32", "exfat")
    unauthorized = set('*?,;:/\\|+=<>[]"')
    out = []
    for ch in label:
        if fat:
            if ch in unauthorized:
                continue
            if ord(ch) >= 0x80:
                out.append("_")
                continue
        if ch in "\t.":
            out.append("_")
            continue
        out.append(ch.upper() if fat else ch)
    result = "".join(out)
    if fat:
        result = result[:11]
        if result and len(result) < 2 * result.count("_"):
            result = human_size(disk_size, binary=False).replace(".", "_")[:11].upper()
    else:
        result = result[:FS_LABEL_MAX.get(fs, 32)]
    return result


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


# ------------------------------------------------------------ images

def _image_dir(estimate, temp_dir=None):
    """Somewhere with room for `estimate` bytes of file-system metadata:
    the job's temp dir, else the system temp dir, else ~/.cache."""
    from .util import default_temp_dir
    candidates = [temp_dir, default_temp_dir(), tempfile.gettempdir(),
                  os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "stoke")]
    for d in candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            if shutil.disk_usage(d).free > estimate * 1.25 + 64 * MB:
                return d
        except OSError:
            continue
    raise UsbError(_("not enough temporary space (%s needed) to build the file system") % human_size(estimate))


def _metadata_estimate(fs, size, cluster_size):
    if fs in ("fat16", "fat32"):
        cluster = cluster_size or 4096
        return 2 * (size // cluster) * 4 + 4 * MB
    if fs == "ntfs":
        return min(size // 8, 512 * MB) + 16 * MB
    return 64 * MB


def make_image(fs, size, sector_size=512, label="", cluster_size=0, hidden_sectors=0, temp_dir=None,
               log=None, cancel=None, compression=False, populate=None, owner=None):
    """A sparse file holding a fresh `fs` of `size` bytes. `populate` is a
    directory whose contents go into the file system (ext only, via mke2fs
    -d). Returns the path; the caller removes it."""
    d = _image_dir(_metadata_estimate(fs, size, cluster_size), temp_dir)
    fd, path = tempfile.mkstemp(prefix=f"stoke-{fs}-", suffix=".img", dir=d)
    os.close(fd)
    try:
        with open(path, "r+b") as f:
            f.truncate(size)
        if fs in ("fat32", "fat16"):
            cmd = ["mkfs.fat", "-F", "32" if fs == "fat32" else "16", "-I", "-M", "0xf8", "-g", "255/63",
                   "-h", str(hidden_sectors)]
            if label:
                cmd += ["-n", label]
            if cluster_size:
                cmd += ["-s", str(max(1, cluster_size // sector_size))]
            if sector_size != 512:
                cmd += ["-S", str(sector_size)]
            cmd.append(path)
        elif fs == "ntfs":
            # -f is the quick format; the partition was zeroed already when a
            # full format was asked for. -p/-H/-S set the geometry the NTFS
            # boot sector (and BOOTMGR) expects.
            cmd = ["mkntfs", "-F", "-q", "-f", "-s", str(sector_size), "-p", str(hidden_sectors), "-H", "255", "-S", "63"]
            if label:
                cmd += ["-L", label]
            if cluster_size:
                cmd += ["-c", str(cluster_size)]
            if compression:
                cmd.append("-C")
            cmd.append(path)
        elif fs == "exfat":
            cmd = ["mkfs.exfat"]
            if label:
                cmd += ["-L", label]
            if cluster_size:
                cmd += ["-c", str(cluster_size)]
            cmd.append(path)
        elif fs in ("ext2", "ext3", "ext4"):
            cmd = [f"mkfs.{fs}", "-F", "-q"]
            if label:
                cmd += ["-L", label]
            if cluster_size and cluster_size in (1024, 2048, 4096):
                cmd += ["-b", str(cluster_size)]
            # Older live kernels (Debian persistence) choke on metadata_csum_seed
            # and orphan_file; leave the flashy features off, this is a USB stick.
            if fs == "ext4":
                cmd += ["-O", "^metadata_csum_seed,^orphan_file,^64bit"]
            uid, gid = owner if owner else (os.getuid(), os.getgid())
            cmd += ["-E", f"root_owner={uid}:{gid}"]
            if populate:
                cmd += ["-d", populate]
            cmd.append(path)
        else:
            raise UsbError(_("unsupported file system %s") % fs)
        if not which(cmd[0]):
            raise UsbError(_("'%s' is not available on this system%s") % (cmd[0], ""))
        run(cmd, log=log, cancel=cancel)
        return path
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _data_extents(fd, size):
    """[(offset, length)] of the allocated parts of a sparse file; the whole
    file when the file system cannot tell."""
    out = []
    off = 0
    try:
        while off < size:
            try:
                start = os.lseek(fd, off, os.SEEK_DATA)
            except OSError:
                break       # ENXIO: no more data
            end = os.lseek(fd, start, os.SEEK_HOLE)
            out.append((start, end - start))
            off = end
        return out
    except OSError:
        return [(0, size)]


def write_image(path, target, emitter=None, cancel=None, phase="format", chunk=8 * MB):
    """Copy the allocated blocks of a file-system image onto the target."""
    size = os.path.getsize(path)
    if size > target.size:
        raise UsbError(_("file system image (%s) is larger than %s") % (human_size(size), target.path))
    fd = os.open(path, os.O_RDONLY)
    try:
        extents = _data_extents(fd, size)
        total = sum(n for _o, n in extents) or 1
        done = 0
        zero = None
        for off, length in extents:
            pos = off
            end = off + length
            while pos < end:
                if cancel:
                    cancel.check()
                buf = os.pread(fd, min(chunk, end - pos), pos)
                if not buf:
                    break
                if extents == [(0, size)]:
                    # No hole information: skip runs of zeros ourselves.
                    if zero is None or len(zero) != len(buf):
                        zero = bytes(len(buf))
                    if buf == zero:
                        pos += len(buf)
                        done += len(buf)
                        continue
                target.pwrite(buf, pos)
                pos += len(buf)
                done += len(buf)
                if emitter:
                    emitter.progress(phase, done / total)
        target.fsync()
    finally:
        os.close(fd)


def zero_partition(target, size, emitter=None, cancel=None):
    """Full (non-quick) format: overwrite the whole partition with zeros first."""
    chunk = bytes(8 * MB)
    done = 0
    while done < size:
        if cancel:
            cancel.check()
        n = min(len(chunk), size - done)
        target.pwrite(chunk[:n], done)
        done += n
        if emitter:
            emitter.progress("format", done / size)
    target.fsync()


def mkfs(target, fs, label="", cluster_size=0, quick=True, sector_size=512,
         size=0, emitter=None, cancel=None, log=None, compression=False, hidden_sectors=0,
         temp_dir=None, populate=None, keep_image=False):
    """Create the file system on `target` (a partition BlockTarget).
    `cluster_size` in bytes, 0 for the default. With keep_image the image
    is returned instead of being written (the caller fills it first)."""
    size = size or target.size
    if not quick and size and not keep_image:
        if log:
            log(f"Zeroing {human_size(size)} before formatting (full format)")
        zero_partition(target, size, emitter, cancel)
    if emitter:
        emitter.progress("format", None)
    img = make_image(fs, size, sector_size, label, cluster_size, hidden_sectors, temp_dir, log, cancel,
                     compression=compression, populate=populate)
    if keep_image:
        return img
    try:
        write_image(img, target, emitter, cancel)
    finally:
        os.remove(img)
    if log:
        log(f"Formatted {target.path} as {fs.upper()}" + (f" '{label}'" if label else ""))
    return None


# ------------------------------------------------------------ labels

def read_label(target, fs):
    """The label as the file system carries it (FAT from its BPB); None
    when we cannot tell without mounting."""
    try:
        boot = target.pread(512, 0)
    except OSError:
        return None
    if fs == "fat32" and boot[0x52:0x5A].startswith(b"FAT32"):
        return boot[0x47:0x52].decode("ascii", "replace").rstrip()
    if fs == "fat16" and boot[0x36:0x3E].startswith(b"FAT1"):
        return boot[0x2B:0x36].decode("ascii", "replace").rstrip()
    return None


def fat_relabel(image, label):
    """Set the label of a FAT12/16/32 image held in memory: BPB field and
    the root directory's volume-label entry (used for the UEFI:NTFS
    partition image, which comes labelled RUFUS_BOOT)."""
    img = bytearray(image)
    label = valid_label(label, "fat32").ljust(11)[:11].encode("ascii")
    bps = _u16(img, 0x0B)
    spc = img[0x0D]
    rsvd = _u16(img, 0x0E)
    nfats = img[0x10]
    rootents = _u16(img, 0x11)
    fatsz = _u16(img, 0x16) or _u32(img, 0x24)
    is32 = _u16(img, 0x16) == 0
    if is32:
        img[0x47:0x52] = label
        root_off = (rsvd + nfats * fatsz + (_u32(img, 0x2C) - 2) * spc) * bps
        root_len = spc * bps
    else:
        img[0x2B:0x36] = label
        root_off = (rsvd + nfats * fatsz) * bps
        root_len = rootents * 32
    off = root_off
    while off < root_off + root_len:
        e = img[off:off + 32]
        if e[0] == 0:
            break
        if e[0] != 0xE5 and e[11] & 0x08 and e[11] != 0x0F:
            img[off:off + 11] = label
            return bytes(img)
        off += 32
    if off < root_off + root_len:
        entry = bytearray(32)
        entry[0:11] = label
        entry[11] = 0x08
        img[off:off + 32] = entry
    return bytes(img)


def default_cluster_sizes(fs, volume_size):

    """The list Rufus offers, with the default first. Values in bytes."""
    if fs in ("fat32", "fat16"):
        if fs == "fat32":
            if volume_size <= 8 * 1024 * MB:
                default = 4096
            elif volume_size <= 16 * 1024 * MB:
                default = 8192
            elif volume_size <= 32 * 1024 * MB:
                default = 16384
            else:
                default = 32768
        else:
            default = 32768 if volume_size > 1024 * MB else 16384
        choices = [512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
    elif fs == "ntfs":
        default = 4096
        choices = [512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
    elif fs == "exfat":
        default = 32768 if volume_size <= 32 * 1024 * MB else 131072
        choices = [4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576]
    else:
        default = 4096
        choices = [1024, 2048, 4096]
    return default, choices
