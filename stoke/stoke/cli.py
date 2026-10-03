"""Command line and the JSON-lines service the window talks to."""

import argparse
import json
import os
import sys
import threading
import uuid

from . import __version__, APP_NAME, devices, fs as fsmod, hashing, image, job as jobmod, unattend as ua, windl
from .util import Emitter, CancelToken, UsbError, Cancelled, human_size

from .i18n import _


def _need_writer():
    from .backend import get_backend
    if not get_backend().can_write:
        raise UsbError(_("Writing a drive needs root (pkexec or sudo) or the udisks2 backend"))


def cmd_devices(args):
    disks = devices.list_disks(include_usb_hdd=args.usb_hdd, include_all=args.all, include_loop=args.loop)
    if args.json:
        print(json.dumps(disks, indent=None if args.compact else 2))
        return 0
    if not disks:
        print("No removable drives found." + ("" if args.usb_hdd else " (--usb-hdd lists USB hard drives)"))
        return 0
    for d in disks:
        mark = "*" if d["mounted"] else " "
        print(f"{mark} {d['device']:<12} {d['size_human']:>9}  {d['kind']:<8} {d['vendor']} {d['model']}"
              + (f"  [{d['label']}]" if d["label"] else ""))
    return 0


def cmd_probe(args):
    rep = image.probe(args.image, log=(None if args.json else lambda s: print("  " + s, file=sys.stderr)))
    if args.json:
        print(json.dumps(rep, indent=None if args.compact else 2, default=str))
        return 0
    r = rep["recommended"]
    print(f"{rep['name']}: {rep['type']} {rep['size_human']}, label '{rep['label']}'")
    if rep["is_windows"]:
        v = rep["win_version"] or {}
        print(f"  Windows {v.get('major', '?')} build {v.get('build', '?')} {rep['win_arch']} ({rep['win_language']})")
        for e in rep["win_editions"]:
            print(f"    [{e['index']}] {e['name']}")
    print(f"  boots: {'UEFI ' if r['boots_uefi'] else ''}{'BIOS' if r['boots_bios'] else ''}"
          f"{'  (hybrid: DD mode available)' if rep['is_hybrid'] else ''}")
    flags = [k for k in ("has_syslinux", "has_grub2", "has_bootmgr", "has_bootmgr_efi", "has_4gb_file", "needs_ntfs",
                         "uses_casper", "uses_live", "rh_derivative", "disable_iso", "supports_persistence") if rep.get(k)]
    print("  " + ", ".join(flags))
    print(f"  recommended: {r['mode']} mode, {r['scheme'].upper()} / {r['target']}, {r['fs'].upper()}")
    for w in rep["warnings"]:
        print("  WARNING: " + w)
    return 0


def cmd_hash(args):
    em = Emitter(json_mode=args.json, verbose=True)
    res = hashing.hash_file(args.image, args.algorithms or hashing.ALGORITHMS, emitter=em)
    if args.json:
        print(json.dumps(res))
    else:
        for k, v in res.items():
            print(f"{k:<8} {v}")
    return 0


def _job_from_args(args):
    if args.job:
        with open(args.job) as f:
            j = json.load(f)
    else:
        j = {}
    for key in ("device", "image", "boot_type", "mode", "scheme", "target", "fs", "label", "username", "temp_dir"):
        v = getattr(args, key, None)
        if v is not None:
            j[key] = v
    if args.cluster_size is not None:
        j["cluster_size"] = args.cluster_size
    if args.persistence is not None:
        j["persistence_size"] = _parse_size(args.persistence)
    if args.wintogo is not None:
        j["wintogo"] = True
        j["wintogo_index"] = args.wintogo
    if args.edition is not None:
        j["edition_index"] = args.edition
    if args.windows_option:
        j["windows_options"] = args.windows_option
    if args.no_quick:
        j["quick_format"] = False
    if args.bad_blocks is not None:
        j["bad_blocks"] = args.bad_blocks
    for flag in ("extended_label", "old_bios_fixes", "rufus_mbr", "verify", "zero_full", "allow_internal", "allow_loop"):
        if getattr(args, flag, False):
            j[flag] = True
    return j


def _parse_size(s):
    s = str(s).strip().upper()
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    if s.endswith("B") and s[:-1] and s[-2] in mult:
        return int(float(s[:-2]) * mult[s[-2]])
    return int(s)


def cmd_write(args):
    _need_writer()
    j = _job_from_args(args)
    if not args.json and not args.yes:
        d = devices.find_disk(j.get("device", ""))
        if d is None:
            raise UsbError(_("%s is not a disk") % j.get('device'))
        print(f"About to ERASE {d['device']}: {d['display']}")
        if d["mounted"]:
            print("  (it has mounted partitions; they will be unmounted)")
        ans = input("Type the device name to confirm: ").strip()
        if ans != d["device"] and ans != os.path.basename(d["device"]):
            print("Aborted.")
            return 1
    em = Emitter(json_mode=args.json, verbose=not args.quiet)
    cancel = CancelToken()
    try:
        res = jobmod.run_job(j, em, cancel)
        em.event(event="done", ok=True, **{k: v for k, v in res.items() if k != "ok"})
        if not args.json:
            print("Done.")
        return 0
    except Cancelled:
        em.event(event="done", ok=False, cancelled=True)
        return 2
    except UsbError as e:
        em.event(event="done", ok=False, error=str(e))
        if not args.json:
            print("ERROR: " + str(e), file=sys.stderr)
        return 1


def cmd_download(args):
    """List Windows/UEFI Shell ISOs or download one (a Python port of Fido)."""
    from . import windl
    vlist = windl.versions()
    if args.list or not args.win:
        print("Windows versions (--win):")
        for v in vlist:
            print("  " + v["name"])
        return 0
    vi = next((v["index"] for v in vlist if args.win.lower() in v["name"].lower()), None)
    if vi is None:
        print("Unknown version. Use --list."); return 1
    rels = windl.releases(vi)
    ri = 0
    if args.rel and args.rel.lower() != "latest":
        ri = next((r["index"] for r in rels if r["label"].lower().startswith(args.rel.lower())), None)
        if ri is None:
            print("Releases:"); [print("  " + r["label"]) for r in rels]; return 1
    eds = windl.editions(vi, ri)
    ed = eds[0]
    if args.edition:
        ed = next((e for e in eds if args.edition.lower() in e["name"].lower()), None)
        if ed is None:
            print("Editions:"); [print("  " + e["name"]) for e in eds]; return 1
    em = Emitter(json_mode=False, verbose=not args.quiet)
    langs = windl.languages(vi, ed, log=em.log)
    langlist = langs["languages"]
    if args.lang and args.lang.lower() != "list":
        lang = next((l for l in langlist if args.lang.lower() in l["display"].lower()), None)
    else:
        if args.lang:
            print("Languages:"); [print("  " + l["display"]) for l in langlist]; return 0
        lang = next((l for l in langlist if l["display"] == "English"), langlist[0])
    if lang is None:
        print("Unknown language. Use --lang List."); return 1
    links = windl.download_links(vi, ri, ed, lang, log=em.log, sessions=langs["_sessions"])
    link = links[0]
    if args.arch:
        link = next((l for l in links if l["arch"].lower() == args.arch.lower()), link)
    if args.url_only:
        print(link["url"]); return 0
    dest = args.output or windl.filename_for(link["url"])
    windl.download(link["url"], dest, emitter=em, cancel=CancelToken(), log=em.log)
    print("Saved " + dest)
    return 0


# ----------------------------------------------------------------- serve

class TaggedEmitter(Emitter):
    """Emitter that stamps every event with the request id."""

    def __init__(self, rid, stream):
        super().__init__(json_mode=True, stream=stream)
        self.rid = rid

    def _write(self, obj):
        obj = dict(obj)
        obj.setdefault("id", self.rid)
        super()._write(obj)


def cmd_serve(args):
    """Read JSON requests on stdin, one per line; answer on stdout.

    Requests:  {"id": n, "cmd": "devices"|"probe"|"write"|"hash"|"cancel"|"clusters"|"ping"|"quit", ...}
    Responses: {"id": n, "result": ...} or {"id": n, "error": "..."}; long jobs
    stream {"id": n, "event": "log"|"status"|"progress"|"overall"|"done", ...}.
    """
    out = sys.stdout
    lock = threading.Lock()

    def send(obj):
        with lock:
            out.write(json.dumps(obj, default=str) + "\n")
            out.flush()

    from .backend import get_backend
    try:
        be = get_backend()
        backend_name, can_write = be.name, be.can_write
    except UsbError as e:
        backend_name, can_write = "none", False
        print(f"backend: {e}", file=sys.stderr)
    send({"event": "hello", "app": APP_NAME, "version": __version__, "root": os.geteuid() == 0,
          "backend": backend_name, "can_write": can_write,
          "windows_options": list(ua.ALL_OPTIONS), "windows_defaults": sorted(ua.DEFAULT_OPTIONS)})
    current = {"thread": None, "cancel": None, "id": None}
    dl_sessions = {}
    threads = []

    def worker(rid, fn):
        try:
            res = fn()
            send({"id": rid, "result": res})
        except Cancelled:
            send({"id": rid, "event": "done", "ok": False, "cancelled": True})
        except UsbError as e:
            send({"id": rid, "event": "done", "ok": False, "error": str(e)})
        except Exception as e:  # keep the service alive; the window shows the text
            import traceback
            send({"id": rid, "event": "done", "ok": False, "error": f"internal error: {e}", "trace": traceback.format_exc()})
        finally:
            current["thread"] = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            send({"error": "bad json"})
            continue
        rid = req.get("id")
        cmd = req.get("cmd")
        try:
            if cmd == "ping":
                send({"id": rid, "result": "pong"})
            elif cmd == "quit":
                if current["cancel"]:
                    current["cancel"].cancel()
                for t in threads:
                    if t.is_alive():
                        t.join()
                send({"id": rid, "result": "bye"})
                return 0
            elif cmd == "devices":
                send({"id": rid, "result": devices.list_disks(include_usb_hdd=bool(req.get("usb_hdd")),
                                                              include_all=bool(req.get("all")), include_loop=bool(req.get("loop")))})
            elif cmd == "clusters":
                default, choices = fsmod.default_cluster_sizes(req.get("fs", "fat32"), int(req.get("size", 0)))
                send({"id": rid, "result": {"default": default, "choices": choices}})
            elif cmd == "probe":
                path = req.get("path", "")
                em = TaggedEmitter(rid, out)
                t = threading.Thread(target=worker, args=(rid, lambda: image.probe(path, log=em.log)), daemon=True)
                threads.append(t)
                t.start()
            elif cmd == "hash":
                if current["thread"] is not None and current["thread"].is_alive():
                    send({"id": rid, "error": "a job is already running"})
                    continue
                path = req.get("path", "")
                algs = req.get("algorithms") or list(hashing.ALGORITHMS)
                em = TaggedEmitter(rid, out)
                cancel = CancelToken()
                current.update(cancel=cancel, id=rid)
                t = threading.Thread(target=worker, args=(rid, lambda: hashing.hash_file(path, algs, emitter=em, cancel=cancel)), daemon=True)
                current["thread"] = t
                threads.append(t)
                t.start()
            elif cmd == "write":
                if current["thread"] is not None and current["thread"].is_alive():
                    send({"id": rid, "error": "a job is already running"})
                    continue
                if not can_write:
                    send({"id": rid, "event": "done", "ok": False, "error": "writing needs root or the udisks2 backend"})
                    continue
                em = TaggedEmitter(rid, out)
                cancel = CancelToken()
                current.update(cancel=cancel, id=rid)

                def do_write(em=em, cancel=cancel, jobd=req.get("job") or {}, rid=rid):
                    res = jobmod.run_job(jobd, em, cancel)
                    send({"id": rid, "event": "done", "ok": True, **{k: v for k, v in res.items() if k != "ok"}})
                    return None
                t = threading.Thread(target=worker, args=(rid, do_write), daemon=True)
                current["thread"] = t
                threads.append(t)
                t.start()
            elif cmd == "win_versions":
                send({"id": rid, "result": windl.versions()})
            elif cmd == "win_releases":
                send({"id": rid, "result": windl.releases(int(req.get("version", 0)))})
            elif cmd == "win_editions":
                send({"id": rid, "result": windl.editions(int(req.get("version", 0)), int(req.get("release", 0)),
                                                          locale=req.get("locale", "en-US"))})
            elif cmd == "win_languages":
                em = TaggedEmitter(rid, out)
                cancel = CancelToken()
                current.update(cancel=cancel, id=rid)

                def do_langs(em=em, cancel=cancel, r=req, rid=rid):
                    ed = {"ids": r.get("edition_ids") or []}
                    res = windl.languages(int(r.get("version", 0)), ed, locale=r.get("locale", "en-US"),
                                          log=em.log, cancel=cancel)
                    token = uuid.uuid4().hex
                    dl_sessions[token] = res["_sessions"]
                    send({"id": rid, "result": {"token": token, "languages": res["languages"]}})
                    return None
                t = threading.Thread(target=worker, args=(rid, do_langs), daemon=True)
                threads.append(t)
                t.start()
            elif cmd == "win_links":
                em = TaggedEmitter(rid, out)
                cancel = CancelToken()

                def do_links(em=em, cancel=cancel, r=req, rid=rid):
                    sessions = dl_sessions.get(r.get("token"))
                    ed = {"ids": r.get("edition_ids") or []}
                    lang = {"data": r.get("language_data") or []}
                    res = windl.download_links(int(r.get("version", 0)), int(r.get("release", 0)), ed, lang,
                                               log=em.log, cancel=cancel, sessions=sessions)
                    send({"id": rid, "result": res})
                    return None
                t = threading.Thread(target=worker, args=(rid, do_links), daemon=True)
                threads.append(t)
                t.start()
            elif cmd == "win_download":
                if current["thread"] is not None and current["thread"].is_alive():
                    send({"id": rid, "error": "a job is already running"})
                    continue
                em = TaggedEmitter(rid, out)
                cancel = CancelToken()
                current.update(cancel=cancel, id=rid)

                def do_dl(em=em, cancel=cancel, r=req, rid=rid):
                    res = windl.download(r.get("url", ""), r.get("dest", ""), emitter=em, cancel=cancel, log=em.log)
                    send({"id": rid, "event": "done", "ok": True, **res})
                    return None
                t = threading.Thread(target=worker, args=(rid, do_dl), daemon=True)
                current["thread"] = t
                threads.append(t)
                t.start()
            elif cmd == "cancel":
                if current["cancel"]:
                    current["cancel"].cancel()
                send({"id": rid, "result": "cancelling"})
            else:
                send({"id": rid, "error": f"unknown command {cmd}"})
        except UsbError as e:
            send({"id": rid, "error": str(e)})
    # stdin closed: let running work finish before we go away.
    for t in threads:
        if t.is_alive():
            t.join()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="stoke", description=f"{APP_NAME} {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--compact", action="store_true", help="single-line JSON")
    p.add_argument("--backend", choices=("auto", "native", "udisks"),
                   help="how to reach the drive: device nodes as root (native) or udisks2 over D-Bus (udisks)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("devices", help="list drives that can be written")
    s.add_argument("--usb-hdd", action="store_true", help="also list non-removable USB drives")
    s.add_argument("--all", action="store_true", help="list internal drives too (dangerous)")
    s.add_argument("--loop", action="store_true", help="list loop devices (testing)")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("probe", help="analyze an image")
    s.add_argument("image")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("hash", help="compute checksums of an image")
    s.add_argument("image")
    s.add_argument("-a", "--algorithms", nargs="*", choices=hashing.ALGORITHMS)
    s.set_defaults(fn=cmd_hash)

    s = sub.add_parser("write", help="create a drive")
    s.add_argument("--job", help="job description JSON file (command line flags override it)")
    s.add_argument("--device", "-d")
    s.add_argument("--image", "-i")
    s.add_argument("--boot-type", dest="boot_type", choices=jobmod.BOOT_TYPES)
    s.add_argument("--mode", choices=("iso", "dd"))
    s.add_argument("--scheme", choices=("mbr", "gpt"))
    s.add_argument("--target", choices=("bios", "uefi", "dual"))
    s.add_argument("--fs", choices=jobmod.FS_TYPES)
    s.add_argument("--cluster-size", dest="cluster_size", type=int)
    s.add_argument("--label", "-l")
    s.add_argument("--persistence", help="persistent partition size, e.g. 4G")
    s.add_argument("--wintogo", type=int, metavar="INDEX", help="Windows To Go with this install.wim index")
    s.add_argument("--edition", type=int, help="edition index for silent install")
    s.add_argument("--windows-option", dest="windows_option", action="append", choices=ua.ALL_OPTIONS,
                   help="Windows customization (repeatable)")
    s.add_argument("--username")
    s.add_argument("--no-quick", dest="no_quick", action="store_true", help="full format (zero the partition first)")
    s.add_argument("--bad-blocks", dest="bad_blocks", type=int, choices=(0, 1, 2, 3, 4), help="destructive bad block passes")
    s.add_argument("--extended-label", dest="extended_label", action="store_true", help="write autorun.inf")
    s.add_argument("--old-bios-fixes", dest="old_bios_fixes", action="store_true")
    s.add_argument("--rufus-mbr", dest="rufus_mbr", action="store_true", help="masquerading MBR (BIOS ID 0x81)")
    s.add_argument("--verify", action="store_true", help="read back after DD write")
    s.add_argument("--zero", dest="zero_full", action="store_true", help="non-bootable: zero the whole drive")
    s.add_argument("--allow-internal", dest="allow_internal", action="store_true")
    s.add_argument("--allow-loop", dest="allow_loop", action="store_true")
    s.add_argument("--temp-dir", dest="temp_dir")
    s.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(fn=cmd_write)

    s = sub.add_parser("download", help="download a Windows or UEFI Shell ISO from Microsoft (Fido port)")
    s.add_argument("--win", help='Windows version, e.g. "Windows 11"')
    s.add_argument("--rel", help="release label prefix, or 'Latest'")
    s.add_argument("--edition", help="edition name substring")
    s.add_argument("--lang", help="language name substring, or 'List'")
    s.add_argument("--arch", choices=("x86", "x64", "ARM64"), help="architecture")
    s.add_argument("-o", "--output", help="save path (default: the ISO's own name)")
    s.add_argument("--url-only", dest="url_only", action="store_true", help="print the link, do not download")
    s.add_argument("--list", action="store_true", help="list Windows versions")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("serve", help="JSON-lines service on stdin/stdout (used by the window)")
    s.set_defaults(fn=cmd_serve)

    argv = list(sys.argv[1:] if argv is None else argv)
    for flag in ("--json", "--compact"):
        if flag in argv:
            argv.remove(flag)
            argv.insert(0, flag)
    args = p.parse_args(argv)
    if args.backend and args.backend != "auto":
        os.environ["STOKE_BACKEND"] = args.backend
    try:
        return args.fn(args)
    except UsbError as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print("ERROR: " + str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
