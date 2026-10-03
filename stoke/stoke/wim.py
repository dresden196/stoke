"""Windows Imaging (.wim/.esd) helpers: reading the XML index from inside an
ISO without extracting the file, and thin wrappers around wimlib-imagex."""

import os
import struct
import subprocess
import xml.etree.ElementTree as ET

from .util import run, UsbError, require_tool

from .i18n import _

WIM_MAGIC = b"MSWIM\x00\x00\x00"
ARCH_NAMES = {0: "x86", 5: "arm", 6: "ia64", 9: "x64", 12: "arm64"}


class WimImage:
    __slots__ = ("index", "name", "description", "arch", "edition", "version", "languages", "default_language",
                 "installation_type", "flags")

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def parse_xml(data):
    """data: the raw XML resource (UTF-16LE with BOM). Returns [WimImage]."""
    text = data.decode("utf-16", "replace") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8", "replace")
    root = ET.fromstring(text.lstrip("﻿"))
    images = []
    for im in root.findall("IMAGE"):
        w = WimImage()
        w.index = int(im.get("INDEX", "0"))
        w.name = im.findtext("NAME") or im.findtext("DISPLAYNAME") or ""
        w.description = im.findtext("DESCRIPTION") or im.findtext("DISPLAYDESCRIPTION") or ""
        w.flags = im.findtext("FLAGS") or ""
        win = im.find("WINDOWS")
        w.arch = w.edition = w.installation_type = ""
        w.version = (0, 0, 0, 0)
        w.languages, w.default_language = [], ""
        if win is not None:
            w.arch = ARCH_NAMES.get(int(win.findtext("ARCH") or -1), win.findtext("ARCH") or "")
            w.edition = win.findtext("EDITIONID") or ""
            w.installation_type = win.findtext("INSTALLATIONTYPE") or ""
            v = win.find("VERSION")
            if v is not None:
                w.version = tuple(int(v.findtext(k) or 0) for k in ("MAJOR", "MINOR", "BUILD", "SPBUILD"))
            langs = win.find("LANGUAGES")
            if langs is not None:
                w.languages = [l.text for l in langs.findall("LANGUAGE") if l.text]
                w.default_language = langs.findtext("DEFAULT") or (w.languages[0] if w.languages else "")
        images.append(w)
    return images


def read_xml_from_reader(reader, entry):
    """Read a WIM's XML index through an ISO/UDF reader without extracting it."""
    hdr = reader.read(entry, 0, 208)
    if hdr[:8] != WIM_MAGIC:
        raise UsbError(_("%s is not a WIM file") % entry.path)
    size = int.from_bytes(hdr[0x48:0x4F], "little")
    offset = struct.unpack_from("<Q", hdr, 0x50)[0]
    if size == 0 or offset + size > entry.size:
        raise UsbError(_("%s has no XML index") % entry.path)
    return reader.read(entry, offset, size)


def images_from_reader(reader, entry):
    return parse_xml(read_xml_from_reader(reader, entry))


def images_from_file(path):
    with open(path, "rb") as f:
        hdr = f.read(208)
        if hdr[:8] != WIM_MAGIC:
            raise UsbError(_("%s is not a WIM file") % path)
        size = int.from_bytes(hdr[0x48:0x4F], "little")
        offset = struct.unpack_from("<Q", hdr, 0x50)[0]
        f.seek(offset)
        return parse_xml(f.read(size))


def windows_version(images, index=0):
    """Rufus's PopulateWindowsVersionFromXml: normalise NT versions to the
    marketing version (6.1 -> 7, 10.0.22000+ -> 11)."""
    if not images:
        return None
    major, minor, build, rev = images[min(index, len(images) - 1)].version
    if major <= 5:
        return None
    if major == 6:
        major, minor = {0: (0, 0), 1: (7, 0), 2: (8, 0), 3: (8, 1), 4: (10, 0)}.get(minor, (0, 0))
        if major == 0:
            return None
    elif major == 10 and build > 20000:
        major = 11
    return {"major": major, "minor": minor, "build": build, "revision": rev}


def is_wim_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(8) == WIM_MAGIC
    except OSError:
        return False


# ----------------------------------------------------------------- wimlib

def _quiet(cmd, log=None, cancel=None):
    require_tool("wimlib-imagex", "wimlib")
    r = run(cmd, check=False, log=log, cancel=cancel)
    if r.returncode != 0:
        lines = [l for l in (r.stderr or r.stdout).splitlines() if l.strip() and not l.startswith("Extracting")]
        raise UsbError(_("%s failed: %s") % (cmd[0], lines[-1] if lines else r.returncode))
    return r


def extract_paths(wim, index, paths, dest_dir, log=None, cancel=None):
    """Extract files from a WIM image to dest_dir (flat, no ACLs)."""
    os.makedirs(dest_dir, exist_ok=True)
    _quiet(["wimextract", wim, str(index)] + list(paths) +
           [f"--dest-dir={dest_dir}", "--no-acls", "--no-attributes", "--no-globs"], log, cancel)


def update(wim, index, commands, log=None, cancel=None):
    """commands: list of wimupdate commands ('add <src> <dest>', 'delete <path>')."""
    require_tool("wimupdate", "wimlib")
    script = "\n".join(commands) + "\n"
    r = run(["wimupdate", wim, str(index)], input=script, check=False, log=log, cancel=cancel)
    if r.returncode != 0:
        raise UsbError(_("wimupdate failed: %s") % ((r.stderr or r.stdout).strip().splitlines() or ["?"])[-1])


def split(wim, dest_swm, part_size_mb=4000, log=None, cancel=None, emitter=None):
    """wimsplit, for FAT32 targets: install.wim -> install.swm, install2.swm ..."""
    require_tool("wimsplit", "wimlib")
    cmd = ["wimsplit", wim, dest_swm, str(part_size_mb)]
    _quiet(cmd, log, cancel)


def apply(wim, index, target, log=None, cancel=None, emitter=None, ntfs_device=False):
    """Apply an image. When target is a block device holding NTFS, wimlib
    writes through libntfs-3g and keeps every Windows attribute and ACL,
    which a kernel mount would drop."""
    require_tool("wimapply", "wimlib")
    cmd = ["wimapply", wim, str(index), target]
    if not ntfs_device:
        cmd.append("--no-acls")
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if cancel:
        cancel.track(proc)
    last = ""
    import re
    try:
        for line in proc.stderr:
            for piece in line.replace("\r", "\n").split("\n"):
                m = re.search(r"\((\d+)%\)", piece)
                if m and emitter:
                    emitter.progress("apply", int(m.group(1)) / 100.0)
                if piece.strip():
                    last = piece.strip()
        proc.wait()
    finally:
        if cancel:
            cancel.untrack(proc)
    if cancel and cancel.cancelled:
        from .util import Cancelled
        raise Cancelled()
    if proc.returncode != 0:
        raise UsbError(_("wimapply failed: %s") % last)
    if log:
        log(f"Applied {os.path.basename(wim)} [{index}] to {target}")


def image_count(wim):
    r = run(["wiminfo", wim], check=False)
    for line in r.stdout.splitlines():
        if line.startswith("Image Count:"):
            return int(line.split(":")[1])
    return 0
