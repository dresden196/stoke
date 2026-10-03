"""The write job: Rufus's FormatThread() in the order that works.

Nothing here is clever. The value is in the order and in the conditions,
each of which exists because some firmware or some installer needs it.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time

from . import badblocks, bootrec, devices, extract, fs as fsmod, image, layout, linux, unattend as ua, windows, wim as wimmod, writer
from .backend import get_backend
from .udf import open_image
from .util import (UsbError, Cancelled, human_size, payload_path, sync_device, default_temp_dir, GB, MB, KB)

from .i18n import _

BOOT_TYPES = ("image", "none", "freedos", "syslinux", "grub2", "grub4dos", "uefi_ntfs")
FS_TYPES = ("fat16", "fat32", "ntfs", "exfat", "ext2", "ext3", "ext4")

# Text for the "you booted UEFI-only media in BIOS mode"
# message MBR. Colour codes as in Rufus's msg.S: \0N sets the attribute.
PROTECTIVE_MESSAGE = (
    "\x07             \x70\xc9" + "\xcd" * 48 + "\xbb \x07\r\n"
    "             \x70\xba" + " " * 48 + "\xba \x07\r\n"
    "             \x70\xba   \x74ERROR: BIOS/LEGACY BOOT OF UEFI-ONLY MEDIA\x70   \xba \x07\r\n"
    "             \x70\xba" + " " * 48 + "\xba \x07\r\n"
    "             \x70\xc8" + "\xcd" * 48 + "\xbc \x07\r\n\r\n"
    "             This drive was created by Stoke.\r\n\r\n"
    "             It can boot in \x04UEFI mode only\x07 but you are trying to\r\n"
    "             boot it in BIOS/Legacy mode. THIS WILL NOT WORK!\r\n\r\n"
    "             To remove this message you need to do \x02ONE\x07 of the following:\r\n"
    "             o If this computer supports UEFI, go to your UEFI settings\r\n"
    "               and lower or disable the priority of \x09CSM/Legacy mode\x07.\r\n"
    "             o \x02OR\x07 Recreate the drive and use:\r\n"
    "               * \x09Partition scheme\x07 -> \x09MBR\x07.\r\n"
    "               * \x09Target system\x07 -> \x09BIOS (...)\x07\r\n"
    "             o \x02OR\x07 Erase the whole drive by selecting:\r\n"
    "               * \x09Boot Type\x07 -> \x09Non bootable\x07\r\n\r\n"
    "             Note: You may also see this message if you installed a new\r\n"
    "             OS and your computer is unable to boot that OS in UEFI mode.\r\n"
)


class Progress:
    """Maps per-phase progress onto one overall bar."""

    def __init__(self, emitter):
        self.emitter = emitter
        self.ranges = {}
        self.current = None

    def plan(self, phases):
        """phases: list of (name, weight)."""
        total = sum(w for _, w in phases) or 1
        pos = 0.0
        self.ranges = {}
        for name, w in phases:
            self.ranges[name] = (pos / total, (pos + w) / total)
            pos += w

    def phase(self, name, status=None):
        self.current = name
        if status:
            self.emitter.status(status)
        self.update(None)

    def update(self, value, message=None):
        lo, hi = self.ranges.get(self.current, (0.0, 1.0))
        overall = None if value is None else lo + (hi - lo) * max(0.0, min(1.0, value))
        self.emitter.progress(self.current or "work", value, message)
        if overall is not None:
            self.emitter.event(event="overall", value=overall)

    # emitter-compatible shim for modules that take an `emitter`
    def progress(self, phase, value, message=None):
        self.update(value, message)

    def status(self, text):
        self.emitter.status(text)

    def log(self, text):
        self.emitter.log(text)

    def event(self, **kw):
        self.emitter.event(**kw)


def normalize(job):
    """Fill defaults and validate the job dictionary."""
    j = dict(job)
    j.setdefault("boot_type", "image" if j.get("image") else "none")
    if j["boot_type"] not in BOOT_TYPES:
        raise UsbError(_("unknown boot type %s") % j['boot_type'])
    j.setdefault("mode", "iso")
    j.setdefault("wintogo", False)
    j.setdefault("wintogo_index", 1)
    j.setdefault("scheme", "mbr")
    j.setdefault("target", "bios")
    j.setdefault("fs", "fat32")
    j.setdefault("cluster_size", 0)
    j.setdefault("label", "")
    j.setdefault("quick_format", True)
    j.setdefault("bad_blocks", 0)
    j.setdefault("extended_label", False)
    j.setdefault("old_bios_fixes", False)
    j.setdefault("rufus_mbr", False)
    j.setdefault("persistence_size", 0)
    j.setdefault("windows_options", [])
    j.setdefault("username", "")
    j.setdefault("edition_index", 1)
    j.setdefault("verify", False)
    j.setdefault("zero_full", False)
    j.setdefault("allow_internal", False)
    j.setdefault("allow_loop", False)
    j.setdefault("temp_dir", None)
    if not j["temp_dir"]:
        j["temp_dir"] = default_temp_dir()
    if j["scheme"] not in ("mbr", "gpt"):
        raise UsbError(_("scheme must be mbr or gpt"))
    if j["target"] not in ("bios", "uefi", "dual"):
        raise UsbError(_("target must be bios, uefi or dual"))
    if j["fs"] not in FS_TYPES:
        raise UsbError(_("unsupported file system %s") % j['fs'])
    if j["boot_type"] == "image" and not j.get("image"):
        raise UsbError(_("no image given"))
    if not j.get("device"):
        raise UsbError(_("no device given"))
    if j["scheme"] == "gpt" and j["target"] == "bios":
        raise UsbError(_("GPT needs a UEFI target"))
    if j["boot_type"] == "uefi_ntfs" and j["fs"] not in ("ntfs", "exfat"):
        raise UsbError(_("UEFI:NTFS needs an NTFS or exFAT main partition"))
    return j


def run_job(job, emitter, cancel=None):
    j = normalize(job)
    prog = Progress(emitter)
    log = emitter.log
    backend = get_backend()
    dev = os.path.realpath(j["device"])
    disk_info = devices.find_disk(dev)
    if disk_info is None:
        if devices.is_system_disk(dev):
            raise UsbError(_("%s holds the running system; refusing to write it") % dev)
        raise UsbError(_("%s is not a disk") % dev)
    if disk_info["kind"] == "internal" and not j["allow_internal"]:
        raise UsbError(_("%s is an internal drive; refusing to write it") % dev)
    if disk_info["kind"] == "loop" and not j["allow_loop"]:
        raise UsbError(_("%s is a loop device; refusing to write it") % dev)
    disk_size, sector = disk_info["size"], disk_info["sector_size"]
    log(f"Device: {disk_info['display']} ({dev}), {human_size(disk_size)}, {sector}-byte sectors, {disk_info['kind']}; backend {backend.name}")
    if disk_info["kind"] == "internal":
        log("WARNING: writing an internal drive because allow_internal was set")

    report = None
    reader = None
    if j["boot_type"] == "image":
        prog.phase("scan", _("Scanning image..."))
        report = image.probe(j["image"], log=log)
        log(f"Image: {report['name']} ({report['size_human']}), label '{report['label']}'")
        if j["mode"] == "dd" and not report["is_bootable_img"] and report["is_iso"]:
            raise UsbError(_("this ISO is not a hybrid image and cannot be written in DD mode"))
        if j["mode"] == "iso" and not report["is_iso"]:
            j["mode"] = "dd"
        if j["wintogo"] and not report["wininst"]:
            raise UsbError(_("Windows To Go needs an image with sources/install.wim"))
        if j["wintogo"] and j["fs"] != "ntfs":
            raise UsbError(_("Windows To Go needs NTFS"))
        if j["mode"] == "iso" and report["needs_ntfs"] and j["fs"] in ("fat16", "fat32"):
            raise UsbError(_("this image has files over 4 GB that cannot be split; use NTFS or exFAT"))
        if j["persistence_size"] and not report["supports_persistence"]:
            raise UsbError(_("this image does not support a persistent partition"))
        if j["mode"] == "iso" and j["fs"] in ("fat16", "fat32") and report["has_4gb_file"] and not report["wininst"]:
            raise UsbError(_("this image has files over 4 GB; FAT32 cannot hold them, use NTFS or exFAT"))
        if j["mode"] == "iso" and report["projected_size"] > disk_size:
            raise UsbError(_("the image needs %s but the drive has %s") % (human_size(report['projected_size']), human_size(disk_size)))

    dd_mode = j["boot_type"] == "image" and j["mode"] == "dd"
    bootable = j["boot_type"] != "none"
    is_windows = bool(report and report["is_windows"])
    efi_bootable = bool(report and (report["recommended"]["boots_uefi"]))
    needs_masquerading = bool(report and report["winpe"] and not report["uses_minint"])

    extras = set()
    if not dd_mode:
        if j["persistence_size"]:
            extras.add("persistence")
        wintogo_esp = j["boot_type"] == "image" and j["wintogo"] and j["scheme"] == "gpt"
        if wintogo_esp:
            # Windows running from GPT wants a real ESP (its system partition;
            # without one sysprep fails 0xC0000452) and, per Microsoft, an MSR.
            # On MBR the active NTFS partition is the system partition and
            # UEFI:NTFS chains into it instead.
            extras.update({"esp", "msr"})
        elif (j["boot_type"] == "image" and efi_bootable and j["fs"] in ("ntfs", "exfat")) or j["boot_type"] == "uefi_ntfs":
            extras.add("uefi_ntfs")
            if j["boot_type"] == "image" and report["has_bootmgr"] and not j["wintogo"] and j["target"] == "bios":
                extras.discard("uefi_ntfs")
        if j["old_bios_fixes"] and j["scheme"] == "mbr":
            extras.add("compat")
    plan_phases = []
    if j["bad_blocks"]:
        plan_phases.append(("badblocks", 25))
    if dd_mode:
        plan_phases += [("write", 70), ("verify", 20 if j["verify"] else 0), ("finalize", 2)]
    elif j["boot_type"] == "none" and j["zero_full"]:
        plan_phases += [("write", 95), ("finalize", 2)]
    else:
        plan_phases += [("partition", 2), ("format", 4 if j["quick_format"] else 20), ("bootrec", 1),
                        ("copy", 70 if j["boot_type"] == "image" else 3), ("patch", 8 if is_windows else 1), ("finalize", 3)]
    prog.plan(plan_phases)

    main_mount = None
    mounted = []            # (partition target, mount dir) still mounted
    parts = []
    disk = None
    tmp_root = None
    with backend.inhibit(dev):
        try:
            backend.unmount_all(dev, log)
            backend.check_exclusive(dev)
            disk = backend.open_disk(dev)
            log(f"Current MBR: {bootrec.describe_mbr(disk)}")
            cur = layout.read_layout(disk)
            if cur:
                log(f"Current partition table: {cur.get('label', '?')} with {len(cur.get('partitions', []))} partition(s)")

            if j["bad_blocks"]:
                prog.phase("badblocks", _("Checking for bad blocks..."))
                badblocks.check(disk, backend, j["bad_blocks"], prog, cancel, log)
                # The destructive test wrote patterns over the whole drive: clear again.
                layout.wipe_disk_signatures(disk, log)

            if dd_mode:
                prog.phase("write", _("Writing image..."))
                writer.write_image(j["image"], disk, backend, emitter=prog, cancel=cancel, log=log, verify=j["verify"])
                prog.phase("finalize", _("Finalizing..."))
                sync_device(disk)
                return {"ok": True}
            if j["boot_type"] == "none" and j["zero_full"]:
                prog.phase("write", _("Zeroing drive..."))
                writer.zero_drive(disk, emitter=prog, cancel=cancel, log=log, full=True)
                return {"ok": True}

            tmp_root = tempfile.mkdtemp(prefix="stoke-job-", dir=j["temp_dir"])

            # ---- partition
            prog.phase("partition", _("Creating partition table..."))
            layout.wipe_disk_signatures(disk, log)
            cluster = j["cluster_size"] or 0
            parts = layout.plan(disk_size, sector, j["scheme"], j["fs"], bootable, extras,
                                persistence_size=j["persistence_size"], old_bios_fixes=j["old_bios_fixes"],
                                cluster_size=cluster, uefi_ntfs_size=os.path.getsize(payload_path("uefi-ntfs.img")))
            for p in parts:
                log(f"● Creating {p.name} (offset: {p.offset}, size: {human_size(p.size)})")
            layout.clear_partition_starts(disk, parts)
            layout.apply(disk, backend, parts, j["scheme"], mbr_uefi_marker=(j["scheme"] == "mbr" and j["target"] == "uefi"), log=log)
            by_role = {p.role: p for p in parts}
            main = by_role["main"]
            if cancel:
                cancel.check()

            if "uefi_ntfs" in by_role:
                p = by_role["uefi_ntfs"]
                log("Writing UEFI:NTFS data...")
                with open(payload_path("uefi-ntfs.img"), "rb") as f:
                    # The image comes labelled RUFUS_BOOT; the silent-install
                    # answer file refers to this partition by label.
                    data = fsmod.fat_relabel(f.read(), "STOKE_BOOT")
                p.target.pwrite(data, 0)
                p.target.fsync()

            # ---- format
            prog.phase("format", _("Formatting..."))
            if "persistence" in by_role:
                p = by_role["persistence"]
                kind = "casper" if report and report["uses_casper"] else "live"
                log(f"Using {'Ubuntu' if kind == 'casper' else 'Debian'}-like method to enable persistence")
                fsmod.mkfs(p.target, "ext4", "casper-rw" if kind == "casper" else "persistence", quick=True, log=log, cancel=cancel,
                           temp_dir=tmp_root, populate=linux.persistence_populate(kind, tmp_root))
            if "esp" in by_role:
                # No label on purpose: a labelled ESP has cost people hours (Rufus's words).
                fsmod.mkfs(by_role["esp"].target, "fat32", "", cluster_size=1024 if sector <= 1024 else 4096,
                           quick=True, sector_size=sector, log=log, cancel=cancel, temp_dir=tmp_root,
                           hidden_sectors=by_role["esp"].offset // sector)
            label = j["label"] or (report["label"] if report else "") or ""
            if j["fs"] not in ("ext2", "ext3", "ext4"):
                label = fsmod.valid_label(label, j["fs"], disk_size)
            # Windows To Go without a device node: the NTFS image is filled by
            # wimlib in temporary space and copied over afterwards.
            wtg_image = None
            wtg_via_image = bool(j["boot_type"] == "image" and j["wintogo"] and not backend.has_device_nodes)
            wtg_image = fsmod.mkfs(main.target, j["fs"], label, cluster_size=cluster, quick=j["quick_format"], sector_size=sector,
                                   size=main.size, emitter=prog, cancel=cancel, log=log, hidden_sectors=main.offset // sector,
                                   temp_dir=j["temp_dir"] if wtg_via_image else tmp_root, keep_image=wtg_via_image)
            usb_label = fsmod.read_label(main.target, j["fs"]) or label
            if cancel:
                cancel.check()

            # ---- boot records
            prog.phase("bootrec", _("Writing boot records..."))
            is_reactos = j["boot_type"] == "image" and bool(report["reactos_path"]) and not report["has_syslinux"]
            uses_syslinux = j["boot_type"] == "syslinux" or is_reactos or (
                j["boot_type"] == "image" and report["has_syslinux"] and not (is_windows and j["target"] == "dual"))
            uses_grub4dos = not uses_syslinux and (j["boot_type"] == "grub4dos" or (j["boot_type"] == "image" and report["has_grub4dos"]))
            uses_grub2 = not uses_syslinux and not uses_grub4dos and (
                j["boot_type"] == "grub2" or (j["boot_type"] == "image" and report["has_grub2"]))
            mbr_kind = _mbr_kind(j, report, bootable, is_windows, uses_syslinux, uses_grub2, needs_masquerading, uses_grub4dos)
            # The masquerading flag (0x81) makes Linux reject the whole table
            # and drop the partition nodes, so the job runs with 0x80 and
            # sets 0x81 as its very last act, after everything is unmounted.
            masquerade_later = bool(needs_masquerading and bootable and j["target"] != "uefi" and j["scheme"] == "mbr")
            if j["scheme"] == "mbr":
                bootrec.fix_mbr_entries(disk, parts.index(main), j["fs"],
                                        0x80 if (bootable and j["target"] != "uefi") else None, log)
            if mbr_kind:
                bootrec.write_mbr_code(disk, mbr_kind, log)
            if mbr_kind == "msg":
                bootrec.write_sbr(disk, PROTECTIVE_MESSAGE.encode("cp437", "replace") + b"\x00", 17 * KB, parts[0].offset, log)
            pbr_variant = None
            if bootable and j["target"] != "uefi" and not uses_syslinux and not uses_grub2 and j["boot_type"] != "uefi_ntfs":
                if j["boot_type"] == "freedos":
                    pbr_variant = "fd"
                elif report and report["has_kolibrios"] and j["fs"] == "fat32":
                    pbr_variant = "kos"
                elif report and report["has_bootmgr"]:
                    pbr_variant = "pe"
                elif report and report["winpe"]:
                    pbr_variant = "nt"
                else:
                    pbr_variant = "std"
            if pbr_variant and j["fs"] in ("fat16", "fat32", "ntfs") and not j["wintogo"]:
                bootrec.write_pbr(main.target, j["fs"], pbr_variant, log,
                                  drive_id=0x81 if needs_masquerading and mbr_kind == "rufus" else 0x80)

            # ---- content
            prog.phase("copy", _("Copying files...") if j["boot_type"] == "image" else _("Preparing volume..."))
            modified = []
            if j["boot_type"] == "image" and j["wintogo"]:
                _windows_to_go_apply(j, report, main, wtg_image, backend, prog, cancel, log, tmp_root)
                wtg_image = None
                main_mount = backend.mount(main.target, j["fs"], "main", log)
                mounted.append((main.target, main_mount))
                esp = None
                if "esp" in by_role:
                    e = by_role["esp"]
                    esp = {"mount": backend.mount(e.target, "fat32", "esp", log),
                           "partition_guid": e.uuid, "disk_guid": e.disk_guid, "main_partition_guid": main.uuid}
                    mounted.append((e.target, esp["mount"]))
                    if not all(esp.values()):
                        raise UsbError(_("could not read the partition GUIDs back from the new table"))
                try:
                    _windows_to_go_setup(j, report, main_mount, prog, cancel, log, tmp_root, esp)
                finally:
                    if esp:
                        backend.unmount(by_role["esp"].target, esp["mount"], log)
                        mounted.remove((by_role["esp"].target, esp["mount"]))
                if pbr_variant and j["fs"] == "ntfs" and j["target"] != "uefi":
                    backend.unmount(main.target, main_mount, log)
                    mounted.remove((main.target, main_mount))
                    bootrec.write_pbr(main.target, "ntfs", "pe", log)
                    main_mount = backend.mount(main.target, "ntfs", "main", log)
                    mounted.append((main.target, main_mount))
            else:
                main_mount = backend.mount(main.target, j["fs"], "main", log)
                mounted.append((main.target, main_mount))
                if j["boot_type"] == "image":
                    reader = open_image(j["image"])
                    with reader:
                        modified, split = extract.extract_iso(reader, report, main_mount, j["fs"], usb_label, emitter=prog, cancel=cancel,
                                                              log=log, persistence=bool(j["persistence_size"]), temp_dir=j["temp_dir"])
                        modified += split
                        if report["has_kolibrios"]:
                            e = reader.get("HD_Load/USB_Boot/MTLD_F32")
                            if e:
                                reader.extract(e, os.path.join(main_mount, "MTLD_F32"))
                elif j["boot_type"] == "freedos":
                    linux.install_freedos(main_mount, log)
                elif j["boot_type"] == "syslinux":
                    pass

            # ---- boot loaders
            if bootable and j["target"] != "uefi" and not j["wintogo"]:
                if uses_syslinux:
                    linux.install_syslinux(main.target, main_mount, report or {"syslinux_cfgs": []}, j["fs"], log,
                                           embedded=(j["boot_type"] == "syslinux"),
                                           reactos_path=(report["reactos_path"] if is_reactos else None))
                elif uses_grub4dos:
                    linux.install_grub4dos(disk, main_mount, parts[0].offset, from_image=(j["boot_type"] == "image"), log=log)
                elif uses_grub2:
                    linux.install_grub2(disk, main.number, j["scheme"], j["fs"], parts[0].offset, main_mount, report, log)
            if j["boot_type"] == "image" and is_windows and not j["wintogo"]:
                if j["target"] in ("uefi", "dual") and report.get("has_win7_efi"):
                    inst = windows._find(main_mount, *report["wininst"][0]["path"].strip("/").split("/"))
                    if inst:
                        windows.setup_win7_efi(main_mount, inst, log, cancel)

            if j["boot_type"] == "image" and report["winpe"] and j["target"] != "uefi":
                # XP-era media: no bootmgr, so it is not "Windows" above.
                windows.setup_winpe(main_mount, report, log)

            # ---- Windows customization
            prog.phase("patch", _("Applying customization...") if is_windows else _("Finalizing files..."))
            if j["boot_type"] == "image" and is_windows and j["windows_options"]:
                modified += windows.apply_customization(main_mount, report, j["windows_options"], j["username"],
                                                        j["edition_index"], wintogo=j["wintogo"], log=log, emitter=prog,
                                                        cancel=cancel, temp_dir=j["temp_dir"])
            if j["boot_type"] == "image" and report["has_md5sum"] and modified and not j["wintogo"]:
                extract.update_md5sum(main_mount, report["has_md5sum"], modified, log)
            if j["extended_label"] and j["fs"] in ("fat16", "fat32", "exfat", "ntfs"):
                extract.write_autorun(main_mount, j["label"] or label, log)

            prog.phase("finalize", _("Finalizing..."))
            backend.unmount(main.target, main_mount, log)
            mounted.remove((main.target, main_mount))
            main_mount = None
            sync_device(disk)
            if masquerade_later:
                bootrec.fix_mbr_entries(disk, parts.index(main), None, 0x81, log)
                sync_device(disk)
                log("Note: with the masquerading MBR Linux will no longer show the partition; the BIOS does not mind")
            for p in parts:
                if p.target:
                    p.target.close()
            backend.rescan(disk, (), log, reopen=False)
            log("Done.")
            return {"ok": True, "label": usb_label}
        finally:
            for target, mdir in list(mounted):
                try:
                    backend.unmount(target, mdir, log)
                except Exception:
                    pass
            for p in parts:
                if p.target:
                    p.target.close()
            if disk is not None:
                sync_device(disk)
                disk.close()
            if tmp_root:
                shutil.rmtree(tmp_root, ignore_errors=True)


def _mbr_kind(j, report, bootable, is_windows, uses_syslinux, uses_grub2, needs_masquerading, uses_grub4dos=False):
    if j["scheme"] == "gpt":
        return "msg" if bootable else "zero"
    if j["boot_type"] == "image" and is_windows and j["target"] == "dual":
        return "rufus" if (needs_masquerading or j["rufus_mbr"]) else "win7"
    if not bootable or j["target"] == "uefi":
        return "zero"
    if uses_syslinux:
        return "syslinux"
    if uses_grub2:
        return None   # grub-install writes boot.img + core.img itself
    if uses_grub4dos:
        return "grub4dos"
    if j["boot_type"] == "image" and report and report["has_kolibrios"] and j["fs"] in ("fat16", "fat32"):
        return "kolibri"
    if needs_masquerading or j["rufus_mbr"]:
        return "rufus"
    return "win7"


def _windows_to_go_apply(j, report, main, wtg_image, backend, prog, cancel, log, tmp_root):
    """Extract install.wim to temp space and apply it to the NTFS volume:
    onto its device node, or into the NTFS image that is then copied to
    the partition when this backend has no nodes to offer wimlib."""
    inst = report["wininst"][0]
    tmpdir = tempfile.mkdtemp(prefix="stoke-wtg-", dir=j["temp_dir"])
    try:
        need = inst["size"] + 64 * MB
        if wtg_image:
            need += int(report["win_editions"][0].get("total_bytes") or 0) or 3 * inst["size"]
        free = shutil.disk_usage(tmpdir).free
        if free < need:
            raise UsbError(_("Windows To Go needs %s of temporary space in %s; "
                             "only %s is free (set TMPDIR to a larger location)")
                           % (human_size(need), tmpdir, human_size(free)))
        prog.status(_("Extracting install image..."))
        with open_image(j["image"]) as reader:
            e = reader.get(inst["entry"])
            wim_tmp = os.path.join(tmpdir, e.name)
            done = [0]

            def cb(n):
                done[0] += n
                prog.update(min(0.3, 0.3 * done[0] / e.size), f"{human_size(done[0])} extracted")
            reader.extract(e, wim_tmp, progress=cb, cancel=cancel)
        index = j["wintogo_index"] or 1
        names = {i["index"]: i["name"] for i in report["win_editions"]}
        log(f"Windows To Go: edition {index} ({names.get(index, '?')})")
        if wtg_image:
            windows.apply_windows_to_go(wim_tmp, index, wtg_image, log=log, emitter=prog, cancel=cancel)
            os.remove(wim_tmp)
            prog.status(_("Copying Windows to the drive..."))
            log(f"Copying the applied NTFS image to {main.device}")
            fsmod.write_image(wtg_image, main.target, prog, cancel, phase="copy")
        else:
            node = backend.device_node(main.target)
            windows.apply_windows_to_go(wim_tmp, index, node, log=log, emitter=prog, cancel=cancel)
    finally:
        if wtg_image:
            try:
                os.remove(wtg_image)
            except OSError:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def _windows_to_go_setup(j, report, main_mount, prog, cancel, log, tmp_root, esp=None):
    """Boot files and BCD for the applied volume (mounted at main_mount)."""
    bcd = {}
    with open_image(j["image"]) as reader:
        for key, path in (("efi", "efi/microsoft/boot/bcd"), ("bios", "boot/bcd")):
            be = reader.get(path)
            if be:
                bcd[key] = os.path.join(tmp_root, f"BCD_{key}")
                reader.extract(be, bcd[key])
    if "efi" not in bcd or "bios" not in bcd:
        raise UsbError(_("image has no BCD template to build the boot store from"))
    windows.setup_windows_to_go(None, report, main_mount, j["target"], bcd, set(j["windows_options"]),
                                log=log, emitter=prog, cancel=cancel, temp_dir=j["temp_dir"], esp=esp)
