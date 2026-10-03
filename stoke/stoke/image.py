"""Look at an image and say what it is: the report the window bases every
default on. A port of Rufus's scan pass in ExtractISO()/check_iso_props().
"""

import os
import re

from . import wim as wimmod
from .iso9660 import Iso9660
from .udf import open_image
from .writer import detect_compression, vhd_footer_size
from .util import GB, MB, human_size, UsbError

from .i18n import _

SYSLINUX_CFG = ("isolinux.cfg", "syslinux.cfg", "extlinux.conf", "txt.cfg", "live.cfg")
ISOLINUX_BIN = ("isolinux.bin", "boot.bin")
GRUB_DIRS = ("/boot/grub/i386-pc", "/boot/grub2/i386-pc")
GRUB_CFG = ("grub.cfg", "loopback.cfg")
EFI_BOOTNAMES = ("boot", "grub", "mm")
EFI_ARCH = ("ia32", "x64", "arm", "aa64", "ia64", "riscv64", "loongarch64", "ebc")
WININST = ("install.wim", "install.esd", "install.swm")
MD5SUM = ("md5sum.txt", "MD5SUMS")
PE_DIRS = ("/i386", "/amd64", "/minint")
PE_FILES = ("ntdetect.com", "setupldr.bin", "txtsetup.sif")
OLD_C32 = {"menu.c32": 53500, "vesamenu.c32": 148000}
IMAGE_EXT = (".iso", ".img", ".raw", ".bin", ".vhd", ".gz", ".xz", ".bz2", ".zst", ".zstd", ".vtsi", ".wim", ".esd", ".ffu", ".vhdx")


def syslinux_version(buf):
    """Find 'ISOLINUX 6.04 ...' / 'SYSLINUX x.yy' in a binary. Returns (version, ext) or (None, '')."""
    for m in re.finditer(rb"(?:ISO|SYS)LINUX (\d+)\.(\d+)([^\x00]*)", buf):
        major, minor = int(m.group(1)), int(m.group(2))
        if major > 255 or minor > 255 or major == 0:
            continue
        ext = m.group(3).decode("ascii", "replace")
        ext = ext.split(" Copyright")[0].strip()
        # Arch appends a star, others a date; keep it short and safe
        ext = re.sub(r'[<>:|*?\\/]', "_", ext.rstrip("* "))
        return f"{major}.{minor:02d}", ext
    return None, ""


def grub_version(buf):
    for pat in (rb"GRUB  version ([^\x00]+)", rb"GRUB version ([^\x00]+)"):
        m = re.search(pat, buf)
        if m:
            v = m.group(1).decode("ascii", "replace").strip()
            return v if v and v[0] != "0" else ""
    return ""


def _efi_fat_size(reader, boot):
    """El Torito EFI entries sometimes say 0 sectors; read the FAT header."""
    if boot.sectors:
        return boot.sectors * 512
    try:
        bpb = reader.pread(boot.lba * 2048, 512)
        bps = int.from_bytes(bpb[11:13], "little") or 512
        total = int.from_bytes(bpb[19:21], "little") or int.from_bytes(bpb[32:36], "little")
        return total * bps
    except OSError:
        return 0


def probe(path, log=None):
    """Return the image report as a plain dict (JSON-friendly)."""
    if not os.path.isfile(path):
        raise UsbError(_("%s is not a file") % path)
    size = os.path.getsize(path)
    rep = {
        "path": path, "name": os.path.basename(path), "size": size, "size_human": human_size(size),
        "type": "unknown", "compression": detect_compression(path),
        "is_iso": False, "is_windows_img": False, "is_vhd": False, "is_hybrid": False, "is_bootable_img": False,
        "label": "", "projected_size": 0,
        "is_windows": False, "has_bootmgr": False, "has_bootmgr_efi": False, "has_efi": {}, "efi_boot_files": [],
        "efi_img_path": None, "efi_img_size": 0, "has_efi_img": False,
        "has_grub2": 0, "grub2_version": "", "has_grub4dos": False, "has_syslinux": False, "syslinux_cfgs": [],
        "isolinux_bins": [], "sl_version": None, "sl_version_ext": "", "has_efi_syslinux": False, "has_old_c32": [],
        "has_ldlinux_c32": False, "has_4gb_file": False, "needs_ntfs": False,
        "wininst": [], "win_version": None, "win_arch": "", "win_editions": [], "win_language": "en-US",
        "winpe": [], "uses_minint": False, "has_panther_unattend": False, "has_compatresources_dll": False,
        "uses_casper": False, "uses_live": False, "rh_derivative": False, "has_md5sum": None,
        "disable_iso": False, "has_kolibrios": False, "reactos_path": None, "has_symlinks": False,
        "supports_persistence": False, "warnings": [],
    }
    if rep["compression"]:
        rep["type"] = "compressed"
        rep["is_bootable_img"] = True
        return _recommend(rep)
    if wimmod.is_wim_file(path):
        rep["type"] = "wim"
        rep["is_windows_img"] = True
        try:
            images = wimmod.images_from_file(path)
            rep["win_editions"] = [i.as_dict() for i in images]
            rep["win_version"] = wimmod.windows_version(images)
            rep["win_arch"] = images[0].arch if images else ""
        except (UsbError, ValueError):
            pass
        return _recommend(rep)
    try:
        payload = vhd_footer_size(path)
        if payload:
            rep["type"] = "vhd"
            rep["is_vhd"] = True
            rep["is_bootable_img"] = True
            return _recommend(rep)
    except UsbError as e:
        rep["warnings"].append(str(e))
    try:
        reader = open_image(path)
    except (ValueError, OSError):
        reader = None
    if reader is None:
        # Raw disk image? Look for an MBR signature and a partition entry.
        with open(path, "rb") as f:
            mbr = f.read(512)
        if len(mbr) == 512 and mbr[510:512] == b"\x55\xaa" and any(mbr[0x1BE + 16 * i + 4] for i in range(4)):
            rep["type"] = "raw"
            rep["is_bootable_img"] = True
        return _recommend(rep)
    with reader:
        rep["type"] = "iso"
        rep["is_iso"] = True
        rep["label"] = (reader.label or "").strip()
        rep["is_hybrid"] = reader.is_hybrid()
        rep["is_bootable_img"] = rep["is_hybrid"]
        _scan(reader, rep, log)
    return _recommend(rep)


def _scan(reader, rep, log):
    entries = reader.entries()
    total_blocks = 0
    efi_files = []
    has_efi = {}
    isolinux = []
    cfgs = []
    ldlinux_c32 = False
    for e in entries.values():
        if e.is_dir:
            continue
        name = e.name
        low = name.lower()
        dirname = e.dirname            # '/sources', '' for root
        dlow = dirname.lower()
        if e.is_symlink:
            rep["has_symlinks"] = True
        total_blocks += (e.size + 2047) // 2048
        if low in SYSLINUX_CFG:
            cfgs.append("/" + e.path)
            if low == "syslinux.cfg" and dlow == "/efi/boot":
                rep["has_efi_syslinux"] = True
        if low in ISOLINUX_BIN:
            isolinux.append(e)
        if low == "ldlinux.c32":
            ldlinux_c32 = True
        if low in OLD_C32 and e.size <= OLD_C32[low] and low not in rep["has_old_c32"]:
            rep["has_old_c32"].append(low)
        if dlow in GRUB_DIRS:
            rep["has_grub2"] = GRUB_DIRS.index(dlow) + 1
        if dlow.startswith("/casper"):
            rep["uses_casper"] = True
            if "pop-os" in dlow:
                rep["disable_iso"] = True
        if dlow == "/proxmox":
            rep["disable_iso"] = True
        if dirname == "":
            if low == "bootmgr":
                rep["has_bootmgr"] = True
            elif low == "bootmgr.efi":
                rep["has_bootmgr_efi"] = True
                has_efi["bootmgr"] = True
                efi_files.append({"path": "/" + e.path, "type": "bootmgr", "size": e.size})
            elif low == "grldr":
                rep["has_grub4dos"] = True
            elif low == "kolibri.img":
                rep["has_kolibrios"] = True
            elif low == ".miso":
                rep["disable_iso"] = True
            if name in MD5SUM or low in [m.lower() for m in MD5SUM]:
                rep["has_md5sum"] = name
        if low in ("setupldr.sys", "freeldr.sys") and not rep["reactos_path"]:
            rep["reactos_path"] = "/" + e.path
        if rep["efi_img_path"] is None and low.startswith("efi") and low.endswith(".img") and len(low) >= 7:
            rep["efi_img_path"] = "/" + e.path
            rep["efi_img_size"] = e.size
        if dlow.startswith("/efi/"):
            for k, bn in enumerate(EFI_BOOTNAMES):
                for arch in EFI_ARCH:
                    if low == f"{bn}{arch}.efi":
                        if k == 0:
                            has_efi[arch] = True
                        efi_files.append({"path": "/" + e.path, "type": bn, "arch": arch, "size": e.size})
            if low == "bootx64.efi" and e.size < 256:
                # A broken symlink shipped as a tiny file (Mint 21.x): pull the
                # real loader from the El Torito EFI image instead.
                has_efi["broken_link"] = True
        if dlow.endswith("/sources"):
            if low in WININST:
                rep["wininst"].append({"path": "/" + e.path, "size": e.size, "entry": e.path})
                if e.size >= 4 * GB:
                    rep["has_4gb_file"] = True
        if dlow == "/sources/$oem$/$$/panther" and low == "unattend.xml":
            rep["has_panther_unattend"] = True
        if dlow == "/sources" and low == "compatresources.dll":
            rep["has_compatresources_dll"] = True
        for i, pd in enumerate(PE_DIRS):
            if dlow == pd and low in PE_FILES:
                rep["winpe"].append(f"{pd}/{low}")
        if e.size >= 4 * GB and not (dlow.endswith("/sources") and low in WININST):
            rep["needs_ntfs"] = True
            rep["has_4gb_file"] = True
    rep["syslinux_cfgs"] = cfgs
    rep["isolinux_bins"] = ["/" + e.path for e in isolinux]
    rep["has_syslinux"] = bool(cfgs) and bool(isolinux)
    rep["has_ldlinux_c32"] = ldlinux_c32
    rep["has_efi"] = has_efi
    rep["efi_boot_files"] = efi_files
    rep["projected_size"] = int(total_blocks * 2048 * 1.01)
    rep["is_windows"] = rep["has_bootmgr"] or rep["has_bootmgr_efi"] or bool(rep["wininst"])
    # Rufus reads txtsetup.sif's OsLoadOptions to learn whether the media
    # expects to be booted with /minint (which decides the disk masquerading).
    rep["uses_minint"] = False
    for d in ("i386", "amd64", "minint"):
        e = reader.get(f"{d}/txtsetup.sif")
        if e:
            try:
                txt = reader.read(e, 0, min(e.size, 512 * 1024)).decode("latin-1")
            except OSError:
                continue
            for line in txt.splitlines():
                if line.strip().lower().startswith("osloadoptions"):
                    rep["uses_minint"] = "/minint" in line.lower()
                    break
            break
    rep["supports_persistence"] = rep["uses_casper"]

    # El Torito: note the EFI image even if no /efi/boot tree exists.
    boots = reader.boot_images()
    for b in boots:
        if b.is_efi and not rep["efi_img_path"]:
            rep["efi_img_path"] = f"[BOOT]/efi.img@{b.lba}"
            rep["efi_img_size"] = _efi_fat_size(reader, b)
    rep["has_efi_img"] = bool(rep["efi_img_path"])
    if has_efi.get("broken_link"):
        efi_b = [b for b in boots if b.is_efi]
        if efi_b:
            rep["efi_img_path"] = f"[BOOT]/efi.img@{efi_b[0].lba}"
            rep["efi_img_size"] = _efi_fat_size(reader, efi_b[0])

    # Syslinux version from the first isolinux.bin (they should all agree).
    for e in isolinux:
        try:
            v, ext = syslinux_version(reader.read(e, 0, min(e.size, 64 * 1024)))
        except OSError:
            v, ext = None, ""
        if v:
            if rep["sl_version"] and rep["sl_version"] != v:
                rep["warnings"].append(f"multiple Syslinux versions on the image ({rep['sl_version']} vs {v})")
            rep["sl_version"] = rep["sl_version"] or v
            rep["sl_version_ext"] = rep["sl_version_ext"] or ext
    if rep["has_syslinux"] and not rep["sl_version"]:
        rep["sl_version"] = "6.04" if ldlinux_c32 else "4.07"

    # GRUB version, from core.img if the ISO ships one.
    for d in GRUB_DIRS:
        core = reader.get(d.strip("/") + "/core.img")
        if core:
            rep["grub2_version"] = grub_version(reader.read(core, 0, min(core.size, 512 * 1024)))
            break

    # Debian-style live persistence: boot=live in any config.
    for cfg in cfgs[:8]:
        e = reader.get(cfg.strip("/"))
        if not e:
            continue
        try:
            txt = reader.read(e, 0, min(e.size, 256 * 1024)).decode("utf-8", "replace")
        except OSError:
            continue
        if "boot=live" in txt:
            rep["uses_live"] = True
            rep["supports_persistence"] = True
        if "inst.stage2" in txt:
            rep["rh_derivative"] = True
    for gc in ("boot/grub/grub.cfg", "EFI/BOOT/grub.cfg", "efi/boot/grub.cfg", "boot/grub2/grub.cfg"):
        e = reader.get(gc)
        if e:
            try:
                txt = reader.read(e, 0, min(e.size, 256 * 1024)).decode("utf-8", "replace")
            except OSError:
                continue
            if "inst.stage2" in txt:
                rep["rh_derivative"] = True
            if "boot=live" in txt:
                rep["uses_live"] = True
                rep["supports_persistence"] = True
            if "/casper/" in txt:
                rep["uses_casper"] = True
                rep["supports_persistence"] = True

    # Windows: version, editions, language from the WIMs.
    if rep["wininst"]:
        inst = rep["wininst"][0]
        try:
            images = wimmod.images_from_reader(reader, reader.get(inst["entry"]))
            rep["win_editions"] = [i.as_dict() for i in images]
            rep["win_version"] = wimmod.windows_version(images)
            rep["win_arch"] = images[0].arch if images else ""
        except (UsbError, ValueError, OSError) as ex:
            rep["warnings"].append(f"could not read {inst['path']}: {ex}")
        boot = reader.get("sources/boot.wim")
        if boot:
            try:
                bimgs = wimmod.images_from_reader(reader, boot)
                if bimgs and bimgs[-1].languages:
                    rep["win_language"] = bimgs[-1].default_language or bimgs[-1].languages[0]
                if not rep["win_arch"] and bimgs:
                    rep["win_arch"] = bimgs[0].arch
            except (UsbError, ValueError, OSError):
                pass
    if rep["has_bootmgr_efi"] and not any(k in has_efi for k in EFI_ARCH):
        rep["has_win7_efi"] = True
    else:
        rep["has_win7_efi"] = False


def _recommend(rep):
    """Rufus's defaults: mode, partition scheme, target system, file system, label."""
    efi = any(k in rep["has_efi"] for k in EFI_ARCH) or rep["has_bootmgr_efi"] or bool(rep.get("has_win7_efi"))
    bios = rep["has_bootmgr"] or rep["has_syslinux"] or bool(rep["has_grub2"]) or rep["has_grub4dos"] \
        or bool(rep["winpe"]) or bool(rep["reactos_path"]) or rep["has_kolibrios"]
    rec = {"mode": "dd", "scheme": "mbr", "target": "bios", "fs": "fat32", "label": rep["label"] or "",
           "iso_mode_available": False, "dd_mode_available": rep["is_bootable_img"],
           "boots_uefi": efi, "boots_bios": bios}
    if rep["is_iso"] and (efi or bios) and not rep["disable_iso"]:
        rec["iso_mode_available"] = True
        rec["mode"] = "iso"
    elif rep["is_iso"] and not rep["is_hybrid"] and not rep["disable_iso"]:
        rec["iso_mode_available"] = True
        rec["mode"] = "iso"
    if rep["is_windows"]:
        rec["target"] = "uefi" if efi else "bios"
        rec["scheme"] = "gpt" if efi else "mbr"
        v = rep["win_version"] or {}
        if v.get("major", 0) and v.get("major") < 8:
            rec["scheme"], rec["target"] = "mbr", "bios" if not efi else "uefi"
        rec["fs"] = "ntfs" if rep["has_4gb_file"] else "fat32"
    elif rep["is_iso"]:
        if efi and bios:
            rec["scheme"], rec["target"] = "mbr", "dual"
        elif efi:
            rec["scheme"], rec["target"] = "gpt", "uefi"
        else:
            rec["scheme"], rec["target"] = "mbr", "bios"
        rec["fs"] = "ntfs" if rep["needs_ntfs"] else "fat32"
    rep["recommended"] = rec
    rep["iso_mode_available"] = rec["iso_mode_available"]
    return rep
