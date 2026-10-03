"""Destructive bad-block scan, the way Rufus does it: write a pattern over
the whole drive, read it back, compare. No badblocks(8) needed, so it works
through a udisks2 descriptor as well as on a device node."""

from .util import UsbError, MB, human_size, Cancelled

from .i18n import _

PATTERNS = (0xAA, 0x55, 0xFF, 0x00)


def check(disk, backend, passes, prog=None, cancel=None, log=None, chunk=8 * MB):
    """Run `passes` (1-4) write/read passes over `disk`. Raises UsbError
    when any block read back wrong. Leaves the drive full of the last
    pattern; the caller wipes it."""
    patterns = PATTERNS[:max(1, min(4, passes))]
    size = disk.size
    bad = 0
    first_bad = None
    for i, pat in enumerate(patterns):
        if log:
            log(f"Bad blocks: pass {i + 1}/{len(patterns)}, pattern 0x{pat:02X}")
        block = bytes([pat]) * chunk
        done = 0
        while done < size:
            if cancel:
                cancel.check()
            n = min(chunk, size - done)
            disk.pwrite(block[:n], done)
            done += n
            if prog:
                prog.update((i + 0.5 * done / size) / len(patterns), f"{human_size(done)} written (pass {i + 1})")
        disk.fsync()
        backend.drop_cache(disk)
        done = 0
        while done < size:
            if cancel:
                cancel.check()
            n = min(chunk, size - done)
            buf = disk.pread(n, done)
            if buf != block[:n]:
                # Narrow down to 4 KB blocks for the count.
                for off in range(0, n, 4096):
                    if buf[off:off + 4096] != block[off:off + 4096]:
                        bad += 1
                        if first_bad is None:
                            first_bad = done + off
            done += n
            if prog:
                prog.update((i + 0.5 + 0.5 * done / size) / len(patterns), f"{human_size(done)} verified (pass {i + 1})")
        if bad:
            break
    if cancel and cancel.cancelled:
        raise Cancelled()
    if bad:
        if log:
            log(f"Bad blocks: {bad} bad 4 KB block(s), first at byte {first_bad}")
        raise UsbError(_("bad blocks check found %d bad block(s); this drive should not be trusted") % bad)
    if log:
        log("Bad blocks: check completed, 0 bad blocks found")
