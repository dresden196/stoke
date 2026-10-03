"""Copy an ISO's files onto the formatted drive, applying the fixes that
make them boot from a USB stick rather than a CD: Rufus's ExtractISO()
write pass and fix_config().
"""

import hashlib
import os
import re
import shutil
import tempfile

from . import wim as wimmod
from .image import SYSLINUX_CFG, GRUB_CFG, WININST
from .util import GB, MB, UsbError, human_size

from .i18n import _

CFG_TOKENS = ("options", "append", "linux", "linuxefi", "$linux", "search", "for")


def _replace_in_token_data(path, token, src, rep, log=None):
    """For each line whose first word is `token`, replace `src` with `rep`
    in the remainder. Returns True when something changed."""
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return False
    changed = False
    out = []
    tl = token.lower()
    for line in lines:
        stripped = line.lstrip()
        first = stripped.split(None, 1)[0].lower() if stripped.split(None, 1) else ""
        if first == tl and src in line:
            head_len = len(line) - len(stripped) + len(first)
            line = line[:head_len] + line[head_len:].replace(src, rep)
            changed = True
        out.append(line)
    if changed:
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write("".join(out))
    return changed


def fix_config(path, rel_path, report, usb_label, persistence, modified, log=None):
    """rel_path like '/isolinux/isolinux.cfg'. Mirrors Rufus's fix_config()."""
    base = os.path.basename(rel_path).lower()
    dirname = os.path.dirname(rel_path).lower()
    is_syslinux_cfg = base in SYSLINUX_CFG
    is_grub_cfg = base in GRUB_CFG
    is_menu_cfg = base == "menu.cfg"
    is_cfg = base.endswith(".cfg") or is_syslinux_cfg
    is_conf = dirname == "/loader/entries" and base.endswith(".conf")
    changed = False

    if persistence and (is_grub_cfg or is_menu_cfg or is_syslinux_cfg):
        kt = "linux" if is_grub_cfg else "append"
        if _replace_in_token_data(path, kt, "file=/cdrom/preseed", "persistent file=/cdrom/preseed"):
            changed = True
            if is_grub_cfg:
                _replace_in_token_data(path, "linux", "maybe-ubiquity", "")
        elif _replace_in_token_data(path, kt, "boot=casper", "boot=casper persistent"):
            changed = True
        elif _replace_in_token_data(path, "linux", "/casper/vmlinuz", "/casper/vmlinuz persistent"):
            changed = True
        elif _replace_in_token_data(path, "kernel", "/casper/vmlinuz", "/casper/vmlinuz persistent"):
            changed = True
        elif _replace_in_token_data(path, kt, "boot=live", "boot=live persistence"):
            changed = True
        if changed and log:
            log(f"  Added persistence kernel option to {rel_path}")

    if is_cfg or is_conf:
        iso_label = (report.get("label") or "").replace(" ", "\\x20")
        usb = (usb_label or "").replace(" ", "\\x20")
        if iso_label and usb and iso_label != usb:
            patched = False
            for tok in CFG_TOKENS:
                if _replace_in_token_data(path, tok, iso_label, usb):
                    patched = True
            if patched:
                changed = True
                if log:
                    log(f"  Patched {rel_path}: '{iso_label}' -> '{usb}'")
        if report.get("rh_derivative") and "netinst" not in os.path.basename(report.get("path", "")).lower():
            patched = False
            for tok in CFG_TOKENS:
                if _replace_in_token_data(path, tok, "inst.stage2", "inst.repo"):
                    patched = True
            if patched:
                changed = True
                if log:
                    log(f"  Patched {rel_path}: 'inst.stage2' -> 'inst.repo'")

    # Tails and friends: an /EFI/BOOT/isolinux.cfg with no syslinux.cfg next to it.
    if is_syslinux_cfg and dirname == "/efi/boot" and base == "isolinux.cfg" and not report.get("has_efi_syslinux"):
        dst = os.path.join(os.path.dirname(path), "syslinux.cfg")
        if not os.path.exists(dst):
            shutil.copy2(path, dst)
            if log:
                log(f"  Duplicated {rel_path} to syslinux.cfg")

    if is_grub_cfg and report.get("label") and usb_label:
        if _replace_in_token_data(path, "set", f"cd9660:/dev/iso9660/{report['label']}",
                                  f"msdosfs:/dev/msdosfs/{usb_label}"):
            changed = True
    if changed:
        modified.append(rel_path)
    return changed


def _safe_name(name, fs):
    """FAT/exFAT/NTFS cannot hold a few characters that ISO 9660 allows."""
    if fs in ("fat16", "fat32", "exfat", "ntfs"):
        return re.sub(r'[\\:*?"<>|]', "_", name)
    return name


# Windows ISOs name their files in lowercase in the UDF tree (efi/microsoft/
# boot/bcd). Windows does not care, but the EfiFs exFAT driver that
# UEFI:NTFS uses matches names exactly, and bootmgr then cannot find its
# BCD (0xc0000272). On case-insensitive targets the well-known EFI paths are
# written in Microsoft's canonical case, which changes nothing else.
CANONICAL_CASE = {
    "efi": "EFI", "efi/boot": "EFI/Boot", "efi/microsoft": "EFI/Microsoft", "efi/microsoft/boot": "EFI/Microsoft/Boot",
    "efi/microsoft/boot/bcd": "EFI/Microsoft/Boot/BCD", "efi/microsoft/boot/fonts": "EFI/Microsoft/Boot/Fonts",
    "efi/microsoft/boot/resources": "EFI/Microsoft/Boot/Resources", "boot/bcd": "boot/BCD",
}


def _target_parts(path, fs):
    parts = [_safe_name(x, fs) for x in path.split("/")]
    if fs in ("fat16", "fat32", "exfat", "ntfs"):
        for depth in range(1, len(parts) + 1):
            key = "/".join(parts[:depth]).lower()
            if key in CANONICAL_CASE:
                parts[:depth] = CANONICAL_CASE[key].split("/")
    return parts


def extract_iso(reader, report, dest, fs, usb_label, emitter=None, cancel=None, log=None,
                persistence=False, split_wim=True, temp_dir=None):
    """Copy every file. Returns (modified_files, split_wims) for md5 fixing."""
    entries = reader.entries()
    total = sum(e.size for e in entries.values() if not e.is_dir) or 1
    done = 0
    modified = []
    split_done = []
    fat = fs in ("fat16", "fat32")
    can_link = fs in ("ext2", "ext3", "ext4")

    def progress(n):
        nonlocal done
        done += n
        if emitter:
            emitter.progress("copy", min(1.0, done / total), f"{human_size(done)} / {human_size(total)}")

    # Directories first, in path order, so parents exist.
    for path, e in sorted(entries.items(), key=lambda kv: kv[0].lower()):
        if e.is_dir:
            os.makedirs(os.path.join(dest, *_target_parts(path, fs)), exist_ok=True)

    for path, e in sorted(entries.items(), key=lambda kv: kv[0].lower()):
        if e.is_dir:
            continue
        if cancel:
            cancel.check()
        rel = "/" + path
        parts = _target_parts(path, fs)
        target = os.path.join(dest, *parts)
        base = e.name.lower()
        dirname = e.dirname.lower()

        # Our own ldlinux.sys goes in later; the ISO's would be for a different install.
        if dirname == "" and base == "ldlinux.sys":
            if log:
                log(f"Skipping '{e.name}' from the image")
            continue

        if e.is_symlink:
            tgt = reader.resolve_link(e) if hasattr(reader, "resolve_link") else None
            if can_link and e.link_target:
                try:
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(e.link_target, target)
                    continue
                except OSError:
                    pass
            if tgt is None or tgt.is_dir:
                if log:
                    log(f"Skipping symbolic link '{rel}' -> '{e.link_target}'")
                continue
            e = tgt   # copy the target's content in place of the link

        if fat and e.size >= 4 * GB and dirname.endswith("/sources") and base in WININST and split_wim:
            _split_wim(reader, e, target, temp_dir, progress, cancel, log, emitter)
            split_done.append(rel)
            continue
        if fat and e.size >= 4 * GB:
            raise UsbError(_("'%s' is %s: FAT32 cannot hold files over 4 GB. Use NTFS or exFAT.") % (rel, human_size(e.size)))

        try:
            reader.extract(e, target, progress=progress, cancel=cancel)
        except OSError as ex:
            raise UsbError(_("could not write %s: %s") % (rel, ex.strerror))

        if base.endswith(".cfg") or base in SYSLINUX_CFG or (dirname == "/loader/entries" and base.endswith(".conf")):
            fix_config(target, rel, report, usb_label, persistence, modified, log)

    if emitter:
        emitter.progress("copy", 1.0)
    return modified, split_done


def _split_wim(reader, e, target, temp_dir, progress, cancel, log, emitter):
    """install.wim > 4 GB on FAT32: extract to a temp file, wimsplit into
    install.swm + install2.swm..., move the parts in. Windows Setup finds
    them on its own."""
    tmpdir = tempfile.mkdtemp(prefix="stoke-", dir=temp_dir)
    try:
        free = shutil.disk_usage(tmpdir).free
        if free < e.size + 64 * MB:
            raise UsbError(_("splitting %s needs %s of temporary space in "
                             "%s, only %s free. Use NTFS instead of FAT32, "
                             "or set TMPDIR to a larger location.")
                           % (e.name, human_size(e.size), tmpdir, human_size(free)))
        if log:
            log(f"Splitting '{e.name}' ({human_size(e.size)}) for FAT32...")
        tmp_wim = os.path.join(tmpdir, e.name)
        reader.extract(e, tmp_wim, progress=progress, cancel=cancel)
        swm = os.path.splitext(target)[0] + ".swm"
        if emitter:
            emitter.status(_("Splitting install.wim for FAT32..."))
        wimmod.split(tmp_wim, swm, 4000, log=log, cancel=cancel)
        if log:
            parts = sorted(p for p in os.listdir(os.path.dirname(swm)) if p.lower().endswith(".swm"))
            log(f"  Created {', '.join(parts)}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def update_md5sum(dest, md5name, modified, log=None):
    """Rewrite the entries of md5sum.txt for the files we changed, and drop
    the ones we removed/split, so `check disc integrity` still passes."""
    path = os.path.join(dest, md5name)
    if not os.path.isfile(path) or not modified:
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    changed = 0
    out = []
    mods = {m.lstrip("/").lower(): m for m in modified}
    for line in lines:
        m = re.match(r"^([0-9a-fA-F]{32})\s+\*?\.?/?(.+)$", line)
        if not m:
            out.append(line)
            continue
        rel = m.group(2).strip().lower()
        if rel in mods:
            fp = os.path.join(dest, mods[rel].lstrip("/"))
            if os.path.isfile(fp):
                h = hashlib.md5()
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(1 * MB), b""):
                        h.update(chunk)
                out.append(f"{h.hexdigest()}  ./{mods[rel].lstrip('/')}")
                changed += 1
                continue
            else:
                changed += 1
                continue
        out.append(line)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        if log:
            log(f"Updated {changed} entries in {md5name}")


def write_autorun(dest, label, log=None):
    """autorun.inf so Windows shows the full label; Rufus's 'extended label'."""
    try:
        with open(os.path.join(dest, "autorun.inf"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write(f"[autorun]\nlabel={label}\n")
        if log:
            log("Created autorun.inf")
    except OSError:
        pass
