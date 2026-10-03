"""Boot loaders for non-Windows media, plus persistence and FreeDOS.

Like Rufus, Stoke carries its own ldlinux.sys and writes Syslinux by hand
(syslinux.py), and builds GRUB's core.img with grub-mkimage and places it
itself; nothing here needs a device node, only the partition's descriptor
and the mounted volume.
"""

import os
import shutil
import subprocess

from . import syslinux as syslinuxmod
from .util import run, UsbError, payload_path, require_tool, which

from .i18n import _

# Modules a 6.x ldlinux.sys needs beside the ones the ISO already ships.
SYSLINUX_CORE_MODULES = ("ldlinux.c32", "libcom32.c32", "libutil.c32", "libmenu.c32", "libgpl.c32")


def grub_dir():
    """GRUB's i386-pc modules: STOKE_GRUB_DIR (the bundled copy in a
    sandbox) or the host's."""
    for d in (os.environ.get("STOKE_GRUB_DIR"), "/usr/lib/grub/i386-pc", "/usr/lib/grub2/i386-pc"):
        if d and os.path.isfile(os.path.join(d, "boot.img")) and os.path.isfile(os.path.join(d, "normal.mod")):
            return d
    raise UsbError(_("GRUB's i386-pc modules are not available (install grub)"))


def syslinux_cfg_dir(report):
    """The directory holding the config Syslinux should load, without the
    leading slash. isolinux.cfg wins over syslinux.cfg over extlinux.conf."""
    cfgs = report.get("syslinux_cfgs") or []
    for want in ("isolinux.cfg", "syslinux.cfg", "extlinux.conf"):
        for c in cfgs:
            if os.path.basename(c).lower() == want:
                return os.path.dirname(c).strip("/")
    return os.path.dirname(cfgs[0]).strip("/") if cfgs else ""


def install_syslinux(part, mount_dir, report, fs, log=None, embedded=False, reactos_path=None):
    """Install ldlinux.sys into the config directory and write Syslinux's
    volume boot record. `part` is the partition's BlockTarget, mounted at
    mount_dir. FAT, NTFS (kernel ntfs3) and ext.

    reactos_path: a ReactOS image has no Syslinux of its own; FreeLoader is
    started as a multiboot kernel through mboot.c32 from a one-entry config."""
    lib = syslinuxmod.lib_dir()
    cfg_dir = "" if (embedded or reactos_path) else syslinux_cfg_dir(report)
    target_dir = os.path.join(mount_dir, cfg_dir) if cfg_dir else mount_dir
    os.makedirs(target_dir, exist_ok=True)
    if reactos_path:
        for m in ("mboot.c32", "libcom32.c32"):
            shutil.copy2(os.path.join(lib, m), os.path.join(mount_dir, m))
        with open(os.path.join(mount_dir, "syslinux.cfg"), "w") as f:
            f.write(f"DEFAULT ReactOS\nLABEL ReactOS\n  KERNEL mboot.c32\n  APPEND {reactos_path}\n")
        if log:
            log(f"Setting up ReactOS: mboot.c32 chain to {reactos_path}")

    # Syslinux only reads syslinux.cfg; an ISO that has isolinux.cfg alone
    # gets a one-line syslinux.cfg that chains to it.
    names = {n.lower() for n in os.listdir(target_dir)}
    if "syslinux.cfg" not in names:
        if "isolinux.cfg" in names:
            with open(os.path.join(target_dir, "syslinux.cfg"), "w") as f:
                f.write("DEFAULT loadconfig\n\nLABEL loadconfig\n  CONFIG isolinux.cfg\n"
                        f"  APPEND /{cfg_dir}/\n" if cfg_dir else
                        "DEFAULT loadconfig\n\nLABEL loadconfig\n  CONFIG isolinux.cfg\n")
            if log:
                log(f"Created {cfg_dir}/syslinux.cfg chaining to isolinux.cfg")
        elif "extlinux.conf" in names:
            shutil.copy2(os.path.join(target_dir, "extlinux.conf"), os.path.join(target_dir, "syslinux.cfg"))

    # The .c32 modules must match ldlinux.sys exactly, so every module the
    # image brought is replaced with the one from our Syslinux.
    replaced = 0
    wanted = set(SYSLINUX_CORE_MODULES)
    for scan_dir in {target_dir, mount_dir}:
        for n in os.listdir(scan_dir):
            if n.lower().endswith(".c32"):
                wanted.add(n.lower())
    for n in sorted(wanted):
        src = os.path.join(lib, n)
        if not os.path.isfile(src):
            continue
        for d in {target_dir, mount_dir} if n in SYSLINUX_CORE_MODULES else {target_dir}:
            existing = [x for x in os.listdir(d) if x.lower() == n]
            dst = os.path.join(d, existing[0] if existing else n)
            if existing or n in SYSLINUX_CORE_MODULES:
                shutil.copy2(src, dst)
                replaced += 1
    if log:
        log(f"Syslinux {syslinuxmod.version()}: updated {replaced} modules under /{cfg_dir}")
    os.sync()
    syslinuxmod.install(part, mount_dir, cfg_dir, fs, log)
    os.sync()


def syslinux_version():
    return syslinuxmod.version()


GRUB_FS_MODULE = {"fat16": "fat", "fat32": "fat", "ntfs": "ntfs", "exfat": "exfat",
                  "ext2": "ext2", "ext3": "ext2", "ext4": "ext2"}


def install_grub2(disk, main_number, scheme, fs, first_partition_offset, mount_dir, report, log=None):
    """BIOS GRUB: boot.img into the MBR (partition table kept), core.img
    into the gap after it, the modules under /boot/grub/i386-pc on the
    stick. What grub-install + grub-bios-setup do, without a device node.
    Images that keep their config in /boot/grub2 get a stub."""
    from .bootrec import write_sbr
    require_tool("grub-mkimage", "grub")
    gdir = grub_dir()
    boot_dir = os.path.join(mount_dir, "boot")
    grub_home = os.path.join(boot_dir, "grub")
    mod_dir = os.path.join(grub_home, "i386-pc")
    os.makedirs(mod_dir, exist_ok=True)
    n = 0
    for name in os.listdir(gdir):
        if name.endswith((".mod", ".lst")) or name in ("boot.img", "modinfo.sh"):
            shutil.copyfile(os.path.join(gdir, name), os.path.join(mod_dir, name))
            n += 1
    for font in ("/usr/share/grub/unicode.pf2", os.path.join(gdir, "..", "..", "..", "share", "grub", "unicode.pf2"),
                 os.path.join(os.environ.get("STOKE_GRUB_DIR") or "", "unicode.pf2")):
        if font and os.path.isfile(font):
            os.makedirs(os.path.join(grub_home, "fonts"), exist_ok=True)
            shutil.copyfile(font, os.path.join(grub_home, "fonts", "unicode.pf2"))
            break
    prefix = f"(,{'msdos' if scheme == 'mbr' else 'gpt'}{main_number})/boot/grub"
    core = os.path.join(mod_dir, "core.img")
    run(["grub-mkimage", "-O", "i386-pc", "-d", gdir, "-p", prefix, "-o", core,
         "biosdisk", "part_msdos", "part_gpt", GRUB_FS_MODULE.get(fs, "fat")], log=log)
    with open(core, "rb") as f:
        core_bytes = f.read()
    with open(os.path.join(gdir, "boot.img"), "rb") as f:
        boot = bytearray(f.read()[:0x1B8])
    # As grub-bios-setup does: no floppy drive check (jumps straight on),
    # kernel sector 1 and "boot drive = whatever the BIOS says" are
    # boot.img's defaults.
    boot[0x66:0x68] = b"\x90\x90"
    disk.pwrite(bytes(boot), 0)
    disk.fsync()
    write_sbr(disk, core_bytes, 512, first_partition_offset, log)

    cfg = os.path.join(grub_home, "grub.cfg")
    alt = os.path.join(boot_dir, "grub2", "grub.cfg")
    if not os.path.exists(cfg) and os.path.exists(alt):
        with open(cfg, "w") as f:
            f.write("set prefix=($root)/boot/grub2\nconfigfile /boot/grub2/grub.cfg\n")
        if log:
            log("Created /boot/grub/grub.cfg stub for /boot/grub2")
    elif not os.path.exists(cfg):
        # Some images only carry an EFI grub.cfg; point BIOS GRUB at it.
        for cand in ("EFI/BOOT/grub.cfg", "efi/boot/grub.cfg", "EFI/boot/grub.cfg", "boot/grub/loopback.cfg"):
            p = os.path.join(mount_dir, cand)
            if os.path.exists(p):
                with open(cfg, "w") as f:
                    f.write(f"search --no-floppy --set=root --file /{cand}\nconfigfile /{cand}\n")
                if log:
                    log(f"Created /boot/grub/grub.cfg chaining to /{cand}")
                break
    if log:
        log(f"Installed GRUB {grub_version()} for BIOS boot ({n} module files, core.img {len(core_bytes)} bytes)")
    os.sync()


def grub_version():
    try:
        r = subprocess.run(["grub-mkimage", "--version"], capture_output=True, text=True)
        return r.stdout.strip().split()[-1]
    except Exception:
        return "?"


def persistence_populate(kind, tmp_dir):
    """Files the persistence volume must carry: Debian-style persistence
    needs a persistence.conf; casper only needs the label. Returns a
    directory for mke2fs -d, or None."""
    if kind != "live":
        return None
    d = os.path.join(tmp_dir, "persistence-root")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "persistence.conf"), "w") as f:
        f.write("/ union\n")
    return d


FREEDOS_ROOT = ("KERNEL.SYS", "COMMAND.COM")


def install_freedos(mount_dir, log=None, keyboard=None, codepage=None):
    """Copy the FreeDOS kernel and shell, locale files into LOCALE\\, and
    an AUTOEXEC.BAT that sets the keyboard, like Rufus's SetDOSLocale."""
    src = payload_path("freedos")
    locale_dir = os.path.join(mount_dir, "LOCALE")
    os.makedirs(locale_dir, exist_ok=True)
    for name in sorted(os.listdir(src)):
        dst = os.path.join(mount_dir if name in FREEDOS_ROOT else locale_dir, name)
        shutil.copy2(os.path.join(src, name), dst)
    kb = keyboard or _dos_keyboard()
    cp = codepage or 437
    with open(os.path.join(mount_dir, "AUTOEXEC.BAT"), "w", newline="\r\n") as f:
        f.write("@echo off\nset PATH=.\\;\\;\\LOCALE\n")
        f.write("display con=(ega,,1)\n")
        f.write(f"mode con codepage prepare=(({cp}) \\LOCALE\\ega.cpx)\n")
        f.write(f"mode con codepage select={cp}\n")
        if kb and kb != "us":
            f.write(f"keyb {kb},,\\LOCALE\\keyboard.sys\n")
    with open(os.path.join(mount_dir, "CONFIG.SYS"), "w", newline="\r\n") as f:
        f.write("")
    if log:
        log("Installed FreeDOS 1.4 (KERNEL.SYS, COMMAND.COM, LOCALE\\)")


def _dos_keyboard():
    """xkb layout name -> FreeDOS keyb id, for the common layouts."""
    try:
        r = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=5)
        import re
        m = re.search(r"X11 Layout:\s*(\S+)", r.stdout)
        layout = m.group(1).split(",")[0] if m else "us"
    except Exception:
        layout = "us"
    return {"us": "us", "gb": "uk", "de": "gr", "fr": "fr", "es": "sp", "it": "it", "pt": "po", "br": "br",
            "nl": "nl", "be": "be", "ch": "sg", "se": "sv", "no": "no", "dk": "dk", "fi": "su", "pl": "pl",
            "cz": "cz", "hu": "hu", "ru": "ru", "tr": "tr", "gr": "gk", "ca": "cf", "latam": "la"}.get(layout, "us")


def install_grub4dos(disk, mount_dir, first_partition_offset, from_image, log=None):
    """Grub4DOS: its MBR code in sector 0 (written by the caller), the rest
    of grldr.mbr right after it, and grldr in the root unless the image
    brought its own. Rufus does the same from the same 0.4.6a release."""
    from .bootrec import write_sbr
    with open(payload_path("grub4dos", "grldr.mbr"), "rb") as f:
        mbr_rest = f.read()[512:]
    write_sbr(disk, mbr_rest, 512, first_partition_offset, log)
    if not from_image:
        shutil.copy2(payload_path("grub4dos", "grldr"), os.path.join(mount_dir, "grldr"))
        if log:
            log("Installed grldr (Grub4DOS 0.4.6a); add a menu.lst to the root of the drive")
