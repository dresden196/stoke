"""Byte-addressed access to a disk or one of its partitions.

Everything that writes a boot sector, a partition table or an image goes
through a BlockTarget, so the rest of the engine never cares whether the
descriptor came from opening /dev/sdX as root or from udisks2 handing an
unprivileged process a descriptor over D-Bus.
"""

import fcntl
import os
import struct

from .util import UsbError

from .i18n import _

BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268
BLKRRPART = 0x125F
BLKFLSBUF = 0x1261


def fd_size(fd):
    buf = bytearray(8)
    try:
        fcntl.ioctl(fd, BLKGETSIZE64, buf)
        return struct.unpack("Q", buf)[0]
    except OSError:
        return os.fstat(fd).st_size


def fd_sector_size(fd):
    buf = bytearray(4)
    try:
        fcntl.ioctl(fd, BLKSSZGET, buf)
        return struct.unpack("i", buf)[0] or 512
    except OSError:
        return 512


class BlockTarget:
    """An open descriptor plus what we know about it. `path` is the kernel
    name (/dev/sdb, /dev/sdb1): what the log shows and what a backend uses
    to find the object again; it is not necessarily openable by us."""

    def __init__(self, path, fd, size=None, sector_size=None, number=0):
        self.path = path
        self.fd = fd
        self.number = number            # partition number, 0 for the disk
        self.size = size if size is not None else fd_size(fd)
        self.sector_size = sector_size or fd_sector_size(fd)

    def __repr__(self):
        return f"<BlockTarget {self.path} fd={self.fd} size={self.size}>"

    def pread(self, n, offset):
        out = bytearray()
        while len(out) < n:
            chunk = os.pread(self.fd, n - len(out), offset + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def pwrite(self, data, offset):
        view = memoryview(data)
        done = 0
        while done < len(view):
            n = os.pwrite(self.fd, view[done:], offset + done)
            if n <= 0:
                raise UsbError(_("short write to %s at %s") % (self.path, offset + done))
            done += n
        return done

    def fsync(self):
        if self.fd is not None:
            os.fsync(self.fd)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def is_target(x):
    return isinstance(x, BlockTarget)
