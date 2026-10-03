"""A small ISO 9660 reader: Joliet and Rock Ridge names, multi-extent files,
El Torito boot catalog, and random access into any file.

Why not loop-mount? Because probing an image should not need root, and
because reading the XML index at the tail of a 4 GB install.wim must not
mean extracting 4 GB first. Rufus uses libcdio for the same reasons.
"""

import os
import struct
from datetime import datetime, timezone

SECTOR = 2048
JOLIET_NAME_LIMIT = 64


class IsoEntry:
    __slots__ = ("path", "name", "size", "is_dir", "is_symlink", "link_target",
                 "extents", "mtime", "mode")

    def __init__(self, path, name, size, is_dir, extents, mtime, mode=0):
        self.path = path            # 'sources/boot.wim' -- no leading slash
        self.name = name
        self.size = size
        self.is_dir = is_dir
        self.is_symlink = False
        self.link_target = None
        self.extents = extents      # [(lba, length_in_bytes), ...]
        self.mtime = mtime
        self.mode = mode

    @property
    def dirname(self):
        i = self.path.rfind("/")
        return "/" + self.path[:i] if i >= 0 else ""

    def __repr__(self):
        return f"<IsoEntry {self.path} {self.size}>"


class BootImage:
    __slots__ = ("platform", "media", "lba", "sectors", "bootable")

    def __init__(self, platform, media, lba, sectors, bootable):
        self.platform, self.media, self.lba, self.sectors, self.bootable = platform, media, lba, sectors, bootable

    @property
    def is_efi(self):
        return self.platform == 0xEF

    @property
    def size(self):
        return self.sectors * 512


def _iso_date(b):
    try:
        y, mo, d, h, mi, s = b[0] + 1900, b[1], b[2], b[3], b[4], b[5]
        tz = struct.unpack("b", b[6:7])[0] * 15 * 60
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() - tz
    except (ValueError, IndexError):
        return 0.0


class Iso9660:
    def __init__(self, path, use_joliet=True, use_rockridge=True):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        self.size = os.fstat(self.fd).st_size
        self.label = ""
        self.joliet = False
        self.rockridge = False
        self._entries = None
        self._pvd_root = None
        self._svd_root = None
        self._use_joliet = use_joliet
        self._use_rr = use_rockridge
        self._read_descriptors()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------------------ raw io
    def pread(self, offset, length):
        return os.pread(self.fd, length, offset)

    def _sector(self, lba, count=1):
        return self.pread(lba * SECTOR, count * SECTOR)

    # ------------------------------------------------------------ descriptors
    def _read_descriptors(self):
        lba = 16
        self.boot_catalog_lba = None
        while lba < 64:
            vd = self._sector(lba)
            if vd[1:6] != b"CD001":
                if lba == 16:
                    raise ValueError("not an ISO 9660 image")
                break
            t = vd[0]
            if t == 1:
                self.label = vd[40:72].decode("ascii", "replace").rstrip()
                self.block_size = struct.unpack_from("<H", vd, 128)[0] or SECTOR
                self._pvd_root = vd[156:190]
            elif t == 2 and vd[88:91] in (b"%/@", b"%/C", b"%/E"):
                self._svd_root = vd[156:190]
            elif t == 0 and vd[7:30].rstrip(b"\0") == b"EL TORITO SPECIFICATION":
                self.boot_catalog_lba = struct.unpack_from("<I", vd, 0x47)[0]
            elif t == 255:
                break
            lba += 1
        if self._pvd_root is None:
            raise ValueError("no primary volume descriptor")
        self.joliet = self._svd_root is not None and self._use_joliet

    # ------------------------------------------------------------ directory walk
    def _parse_record(self, rec, joliet):
        ext_len = rec[1]
        lba = struct.unpack_from("<I", rec, 2)[0] + ext_len
        length = struct.unpack_from("<I", rec, 10)[0]
        mtime = _iso_date(rec[18:25])
        flags = rec[25]
        name_len = rec[32]
        raw = rec[33:33 + name_len]
        if raw in (b"\x00", b"\x01"):
            name = "." if raw == b"\x00" else ".."
        elif joliet:
            name = raw.decode("utf-16-be", "replace")
        else:
            name = raw.decode("ascii", "replace")
            if ";" in name:
                name = name[:name.index(";")]
            if name.endswith(".") and len(name) > 1:
                name = name[:-1]
        su_start = 33 + name_len + (0 if (name_len % 2) else 1)
        su = rec[su_start:] if su_start < len(rec) else b""
        return name, lba, length, flags, mtime, su

    def _rock_ridge(self, su, depth=0):
        """Return (name, symlink_target, mode) from a system-use area."""
        name_parts, link_parts, mode = [], [], 0
        has_link = False
        i = 0
        while i + 4 <= len(su):
            sig, ln, ver = su[i:i + 2], su[i + 2], su[i + 3]
            if ln < 4:
                break
            data = su[i + 4:i + ln]
            if sig == b"NM":
                flags = data[0] if data else 0
                if flags & 0x06:  # current / parent
                    pass
                else:
                    name_parts.append(data[1:].decode("utf-8", "replace"))
                if not (flags & 1):
                    pass
            elif sig == b"SL":
                has_link = True
                j = 1
                comps = []
                while j + 2 <= len(data):
                    cflags, clen = data[j], data[j + 1]
                    content = data[j + 2:j + 2 + clen]
                    if cflags & 0x02:
                        comps.append(".")
                    elif cflags & 0x04:
                        comps.append("..")
                    elif cflags & 0x08:
                        comps.append("")  # root
                    else:
                        comps.append(content.decode("utf-8", "replace"))
                    j += 2 + clen
                link_parts.append("/".join(comps) if comps != [""] else "/")
            elif sig == b"PX" and len(data) >= 4:
                mode = struct.unpack_from("<I", data, 0)[0]
            elif sig == b"CE" and len(data) >= 24 and depth < 4:
                clba = struct.unpack_from("<I", data, 0)[0]
                coff = struct.unpack_from("<I", data, 8)[0]
                clen2 = struct.unpack_from("<I", data, 16)[0]
                cont = self.pread(clba * SECTOR + coff, clen2)
                n2, l2, m2 = self._rock_ridge(cont, depth + 1)
                if n2:
                    name_parts.append(n2)
                if l2:
                    link_parts.append(l2)
                mode = mode or m2
            i += ln
        name = "".join(name_parts) if name_parts else None
        link = "".join(link_parts) if has_link else None
        return name, link, mode

    def _read_dir(self, lba, length, joliet):
        data = self.pread(lba * SECTOR, length)
        pos = 0
        out = []
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = ((pos // SECTOR) + 1) * SECTOR
                continue
            rec = data[pos:pos + ln]
            out.append(self._parse_record(rec, joliet))
            pos += ln
        return out

    def entries(self):
        if self._entries is None:
            self._entries = {}
            root = self._svd_root if self.joliet else self._pvd_root
            _, lba, length, _, _, _ = self._parse_record(root, self.joliet)
            self._walk(lba, length, "", self.joliet, set())
        return self._entries

    def _walk(self, lba, length, prefix, joliet, seen):
        if lba in seen:
            return
        seen.add(lba)
        recs = self._read_dir(lba, length, joliet)
        pending = None  # multi-extent accumulation
        for name, elba, elen, flags, mtime, su in recs:
            if name in (".", ".."):
                continue
            rr_name = link = None
            mode = 0
            if self._use_rr and not joliet and su:
                rr_name, link, mode = self._rock_ridge(su)
                if rr_name:
                    name = rr_name
                    self.rockridge = True
            elif joliet and self._use_rr and len(name) >= JOLIET_NAME_LIMIT and not (flags & 0x02):
                # Joliet cuts names at 64 characters (103 with -joliet-long);
                # the Rock Ridge tree has the real one, and md5sum.txt uses it.
                full = self._rr_names_by_lba().get(elba)
                if full and len(full) > len(name):
                    name = full
            is_dir = bool(flags & 0x02)
            path = f"{prefix}/{name}" if prefix else name
            if pending and pending.name == name and not is_dir:
                pending.extents.append((elba, elen))
                pending.size += elen
                entry = pending
            else:
                entry = IsoEntry(path, name, 0 if is_dir else elen, is_dir, [(elba, elen)], mtime, mode)
                if link:
                    entry.is_symlink = True
                    entry.link_target = link
                self._entries[path] = entry
            pending = entry if (flags & 0x80) and not is_dir else None
            if is_dir:
                self._walk(elba, elen, path, joliet, seen)

    def _rr_names_by_lba(self):
        """{extent lba: Rock Ridge name} for every file in the ISO 9660 tree,
        built once, only when a Joliet name looks truncated."""
        if getattr(self, "_rr_map", None) is None:
            self._rr_map = {}
            if self._pvd_root is not None:
                _, lba, length, _, _, _ = self._parse_record(self._pvd_root, False)
                self._rr_map_walk(lba, length, set())
        return self._rr_map

    def _rr_map_walk(self, lba, length, seen):
        if lba in seen:
            return
        seen.add(lba)
        for name, elba, elen, flags, mtime, su in self._read_dir(lba, length, False):
            if name in (".", ".."):
                continue
            if su:
                rr_name, _link, _mode = self._rock_ridge(su)
                if rr_name:
                    name = rr_name
            if flags & 0x02:
                self._rr_map_walk(elba, elen, seen)
            else:
                self._rr_map.setdefault(elba, name)

    def get(self, path):
        path = path.strip("/")
        e = self.entries().get(path)
        if e is None:
            low = path.lower()
            for k, v in self.entries().items():
                if k.lower() == low:
                    return v
        return e

    def files(self):
        return [e for e in self.entries().values() if not e.is_dir]

    # ------------------------------------------------------------ file access
    def read(self, entry, offset=0, length=None):
        if length is None:
            length = entry.size - offset
        out = bytearray()
        remaining = length
        pos = 0
        for lba, elen in entry.extents:
            if remaining <= 0:
                break
            if offset >= pos + elen:
                pos += elen
                continue
            start = offset - pos if offset > pos else 0
            n = min(elen - start, remaining)
            out += self.pread(lba * SECTOR + start, n)
            remaining -= n
            offset += n
            pos += elen
        return bytes(out)

    def resolve_link(self, entry):
        """Follow a Rock Ridge symlink to its target entry, or None."""
        if not entry.is_symlink:
            return entry
        target = entry.link_target or ""
        base = entry.dirname.strip("/")
        if target.startswith("/"):
            path = target.strip("/")
        else:
            parts = (base.split("/") if base else [])
            for comp in target.split("/"):
                if comp == "..":
                    if parts:
                        parts.pop()
                elif comp and comp != ".":
                    parts.append(comp)
            path = "/".join(parts)
        t = self.entries().get(path)
        if t is not None and t.is_symlink and t is not entry:
            return self.resolve_link(t)
        return t

    def extract(self, entry, dest, chunk=4 * 1024 * 1024, progress=None, cancel=None):
        """Copy a file out of the image. Returns bytes written."""
        written = 0
        with open(dest, "wb") as out:
            for lba, elen in entry.extents:
                off = lba * SECTOR
                left = elen
                while left > 0:
                    if cancel:
                        cancel.check()
                    n = min(chunk, left)
                    buf = self.pread(off, n)
                    if not buf:
                        break
                    out.write(buf)
                    off += len(buf)
                    left -= len(buf)
                    written += len(buf)
                    if progress:
                        progress(len(buf))
        if entry.mtime:
            try:
                os.utime(dest, (entry.mtime, entry.mtime))
            except OSError:
                pass
        return written

    # ------------------------------------------------------------ boot info
    def boot_images(self):
        """El Torito entries: the BIOS boot image and the EFI one (efi.img)."""
        if self.boot_catalog_lba is None:
            return []
        cat = self._sector(self.boot_catalog_lba)
        images = []
        if cat[0] != 1:
            return images
        platform = cat[1]
        pos = 0x20
        while pos + 0x20 <= len(cat):
            ent = cat[pos:pos + 0x20]
            if ent[0] in (0x88, 0x00) and pos == 0x20 or (ent[0] in (0x88, 0x00) and images):
                media = ent[1] & 0x0F
                sectors = struct.unpack_from("<H", ent, 6)[0]
                lba = struct.unpack_from("<I", ent, 8)[0]
                if lba:
                    images.append(BootImage(platform, media, lba, sectors, ent[0] == 0x88))
            elif ent[0] in (0x90, 0x91):  # section header
                platform = ent[1]
            elif ent[0] == 0 and not images:
                break
            pos += 0x20
        return images

    def is_hybrid(self):
        """True if sector 0 carries an MBR (isohybrid) or a GPT follows it,
        meaning the image can simply be written raw to the drive."""
        mbr = self.pread(0, 512)
        if mbr[0x1FE:0x200] != b"\x55\xaa":
            return False
        for i in range(4):
            e = mbr[0x1BE + 16 * i:0x1BE + 16 * (i + 1)]
            if e[4] != 0:
                return True
        return False
