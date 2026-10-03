"""A read-only UDF reader, enough for Microsoft's install media.

Windows ISOs are "UDF bridge" discs: the ISO 9660 tree is a stub and the
real files live only in UDF. This handles UDF 1.02-2.01 with plain
(type 1) partition maps, short and long allocation descriptors, embedded
data, and extended file entries. Sparable, virtual and metadata partitions
(rewritable media, Blu-ray) are not needed for optical images.
"""

import os
import struct
from datetime import datetime, timezone

from .iso9660 import IsoEntry, Iso9660, SECTOR

TAG_PVD, TAG_AVDP, TAG_PD, TAG_LVD, TAG_TERM = 1, 2, 5, 6, 8
TAG_FSD, TAG_FID, TAG_FE, TAG_EFE = 256, 257, 261, 266


def _dstring(b):
    if not b:
        return ""
    comp = b[0]
    body = b[1:]
    if comp == 16:
        return body.decode("utf-16-be", "replace")
    if comp == 8:
        return body.decode("latin-1", "replace")
    return body.decode("utf-8", "replace")


def _timestamp(b):
    try:
        tz_type = struct.unpack_from("<H", b, 0)[0]
        year, mo, d, h, mi, s = struct.unpack_from("<HBBBBB", b, 2)
        tz = tz_type & 0x0FFF
        if tz & 0x800:
            tz -= 0x1000
        return datetime(year, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() - tz * 60
    except (ValueError, struct.error):
        return 0.0


class Udf:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        self.size = os.fstat(self.fd).st_size
        self.block = SECTOR
        self.label = ""
        self.partitions = {}      # number -> start lba
        self._maps = []           # partition ref -> partition number
        self._entries = None
        self._root_icb = None
        self._iso = None
        self._read_volume()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self._iso:
            self._iso.close()
            self._iso = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def pread(self, offset, length):
        return os.pread(self.fd, length, offset)

    @staticmethod
    def has_udf(path):
        try:
            with open(path, "rb") as f:
                f.seek(256 * SECTOR)
                tag = f.read(16)
            return len(tag) == 16 and struct.unpack_from("<H", tag, 0)[0] == TAG_AVDP
        except OSError:
            return False

    # ------------------------------------------------------------ volume
    def _read_volume(self):
        avdp = self.pread(256 * self.block, self.block)
        if struct.unpack_from("<H", avdp, 0)[0] != TAG_AVDP:
            raise ValueError("no UDF anchor")
        vds_len, vds_loc = struct.unpack_from("<II", avdp, 16)
        fsd_ad = None
        for i in range(vds_len // self.block):
            d = self.pread((vds_loc + i) * self.block, self.block)
            tag = struct.unpack_from("<H", d, 0)[0]
            if tag == TAG_PVD:
                self.label = _dstring(d[24:24 + 32].rstrip(b"\0")).rstrip() if d[24] else ""
                # Volume identifier is a 32-byte dstring; last byte is length.
                ln = d[24 + 31]
                self.label = _dstring(d[24:24 + ln]) if 0 < ln < 32 else self.label
            elif tag == TAG_PD:
                num = struct.unpack_from("<H", d, 22)[0]
                start, length = struct.unpack_from("<II", d, 188)
                self.partitions[num] = start
            elif tag == TAG_LVD:
                self.block = struct.unpack_from("<I", d, 212)[0] or self.block
                fsd_ad = d[248:264]
                n_maps = struct.unpack_from("<I", d, 268)[0]
                pos = 440
                for _map in range(n_maps):
                    mtype, mlen = d[pos], d[pos + 1]
                    if mtype == 1:
                        self._maps.append(struct.unpack_from("<H", d, pos + 4)[0])
                    else:
                        self._maps.append(None)
                    pos += mlen or 6
            elif tag == TAG_TERM:
                break
        if fsd_ad is None:
            raise ValueError("no UDF logical volume descriptor")
        length, lb, pref = struct.unpack_from("<IIH", fsd_ad, 0)
        fsd = self.pread(self._abs(lb, pref), max(length, self.block))
        if struct.unpack_from("<H", fsd, 0)[0] != TAG_FSD:
            raise ValueError("bad UDF file set descriptor")
        self._root_icb = fsd[400:416]

    def _abs(self, lb, pref):
        pnum = self._maps[pref] if pref < len(self._maps) else pref
        start = self.partitions.get(pnum)
        if start is None:
            raise ValueError("UDF partition map references an unknown partition")
        return (start + lb) * self.block

    # ------------------------------------------------------------ ICB / extents
    def _file_entry(self, icb):
        length, lb, pref = struct.unpack_from("<IIH", icb, 0)
        base = self._abs(lb, pref)
        d = self.pread(base, max(self.block, min(length, 64 * self.block)))
        tag = struct.unpack_from("<H", d, 0)[0]
        if tag == TAG_FE:
            ftype = d[16 + 11]
            flags = struct.unpack_from("<H", d, 16 + 18)[0]
            info_len = struct.unpack_from("<Q", d, 56)[0]
            mtime = _timestamp(d[84:96])
            l_ea, l_ad = struct.unpack_from("<II", d, 168)
            ad_off = 176 + l_ea
        elif tag == TAG_EFE:
            ftype = d[16 + 11]
            flags = struct.unpack_from("<H", d, 16 + 18)[0]
            info_len = struct.unpack_from("<Q", d, 56)[0]
            mtime = _timestamp(d[96:108])
            l_ea, l_ad = struct.unpack_from("<II", d, 208)
            ad_off = 216 + l_ea
        else:
            raise ValueError(f"unexpected UDF tag {tag} for file entry")
        ad_type = flags & 7
        ads = d[ad_off:ad_off + l_ad]
        extents = []       # [(absolute byte offset, length)]
        embedded = None
        if ad_type == 3:
            embedded = ads[:info_len]
        elif ad_type == 0:
            for i in range(0, len(ads) - 7, 8):
                ln, pos = struct.unpack_from("<II", ads, i)
                etype, ln = ln >> 30, ln & 0x3FFFFFFF
                if ln == 0:
                    break
                if etype == 0:
                    extents.append((self._abs(pos, pref), ln))
                elif etype == 3:
                    # continuation of allocation descriptors
                    more = self.pread(self._abs(pos, pref), ln)
                    ads = ads[:i] + more
                    i -= 8
        elif ad_type == 1:
            for i in range(0, len(ads) - 15, 16):
                ln, pos, p2 = struct.unpack_from("<IIH", ads, i)
                etype, ln = ln >> 30, ln & 0x3FFFFFFF
                if ln == 0:
                    break
                if etype == 0:
                    extents.append((self._abs(pos, p2), ln))
        return ftype, info_len, mtime, extents, embedded

    def _read_extents(self, extents, embedded, length):
        if embedded is not None:
            return embedded[:length]
        out = bytearray()
        for off, ln in extents:
            out += self.pread(off, ln)
            if len(out) >= length:
                break
        return bytes(out[:length])

    # ------------------------------------------------------------ tree
    def entries(self):
        if self._entries is None:
            self._entries = {}
            self._walk(self._root_icb, "", set())
        return self._entries

    def _walk(self, icb, prefix, seen):
        key = bytes(icb[:10])
        if key in seen:
            return
        seen.add(key)
        ftype, info_len, mtime, extents, embedded = self._file_entry(icb)
        data = self._read_extents(extents, embedded, info_len)
        pos = 0
        while pos + 38 <= len(data):
            if struct.unpack_from("<H", data, pos)[0] != TAG_FID:
                break
            chars = data[pos + 18]
            l_fi = data[pos + 19]
            child_icb = data[pos + 20:pos + 36]
            l_iu = struct.unpack_from("<H", data, pos + 36)[0]
            name = _dstring(data[pos + 38 + l_iu:pos + 38 + l_iu + l_fi])
            total = 38 + l_iu + l_fi
            pos += (total + 3) & ~3
            if chars & 0x08 or chars & 0x04:   # parent, deleted
                continue
            if not name:
                continue
            path = f"{prefix}/{name}" if prefix else name
            is_dir = bool(chars & 0x02)
            try:
                cftype, csize, cmtime, cext, cemb = self._file_entry(child_icb)
            except ValueError:
                continue
            entry = IsoEntry(path, name, 0 if is_dir else csize, is_dir,
                             [(off // SECTOR, ln) for off, ln in cext] if cemb is None else [],
                             cmtime)
            if cemb is not None and not is_dir:
                entry.extents = []
                entry.mode = 0
                entry._embedded = cemb  # type: ignore[attr-defined]
            if cftype == 12:
                entry.is_symlink = True
                entry.link_target = ""
            self._entries[path] = entry
            if is_dir:
                self._walk(child_icb, path, seen)

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
        emb = getattr(entry, "_embedded", None)
        if emb is not None:
            return bytes(emb[offset:offset + length])
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
        return None if entry.is_symlink else entry

    def extract(self, entry, dest, chunk=4 * 1024 * 1024, progress=None, cancel=None):
        emb = getattr(entry, "_embedded", None)
        written = 0
        with open(dest, "wb") as out:
            if emb is not None:
                out.write(emb)
                written = len(emb)
            else:
                left_total = entry.size
                for lba, elen in entry.extents:
                    off = lba * SECTOR
                    left = min(elen, left_total)
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
                        left_total -= len(buf)
                        written += len(buf)
                        if progress:
                            progress(len(buf))
        if entry.mtime:
            try:
                os.utime(dest, (entry.mtime, entry.mtime))
            except OSError:
                pass
        return written

    # ------------------------------------------------------------ via ISO9660
    def _iso9660(self):
        if self._iso is None:
            self._iso = Iso9660(self.path)
        return self._iso

    @property
    def iso_label(self):
        try:
            return self._iso9660().label
        except ValueError:
            return ""

    def boot_images(self):
        try:
            return self._iso9660().boot_images()
        except ValueError:
            return []

    def is_hybrid(self):
        try:
            return self._iso9660().is_hybrid()
        except ValueError:
            return False


def open_image(path):
    """Pick the tree with the files in it: UDF when it exists and is richer
    than the ISO 9660 view (Windows), otherwise ISO 9660 (everything else)."""
    iso = Iso9660(path)
    if Udf.has_udf(path):
        try:
            udf = Udf(path)
            n_udf = len(udf.entries())
            if n_udf > len(iso.entries()):
                iso.close()
                # Keep the ISO 9660 label: it is what setup/isolinux see.
                udf.label = udf.iso_label or udf.label
                return udf
            udf.close()
        except (ValueError, struct.error, OSError):
            pass
    return iso
