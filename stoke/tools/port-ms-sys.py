#!/usr/bin/env python3
"""Regenerate stoke/bootcode.py from a Rufus checkout's src/ms-sys/inc.

    tools/port-ms-sys.py ~/src/rufus/src/ms-sys/inc
"""
import pathlib
import re
import sys

WANTED = ["mbr_rufus", "mbr_win7", "mbr_msg_rufus", "mbr_zero", "mbr_syslinux", "mbr_gpt_syslinux", "mbr_grub2", "mbr_grub",
          "br_ntfs_0x0", "br_ntfs_0x54", "br_fat32_0x0", "br_fat32_0x52", "br_fat32_0x3f0",
          "br_fat32nt_0x52", "br_fat32nt_0x3f0", "br_fat32nt_0x1800", "br_fat32pe_0x52", "br_fat32pe_0x3f0", "br_fat32pe_0x1800",
          "br_fat32fd_0x52", "br_fat32fd_0x3f0", "br_fat32kos_0x52", "br_fat32ros_0x52", "br_fat32ros_0x3f0", "br_fat32ros_0x1c00", "br_fat16ros_0x3e", "mbr_reactos", "mbr_kolibri", "br_fat16_0x0", "br_fat16_0x3e", "br_fat16fd_0x3e", "br_fat12_0x0", "br_fat12_0x3e"]

inc = pathlib.Path(sys.argv[1])
out = ["# Boot record byte arrays ported from ms-sys (GPLv2+, Henrik Carlqvist,",
       "# Pete Batard). Each name is <kind>_<hex offset>; offsets are where the",
       "# bytes go within the sector(s). Do not edit by hand: regenerate from",
       "# rufus/src/ms-sys/inc with tools/port-ms-sys.py.", "", ""]
for name in WANTED:
    src = (inc / (name + ".h")).read_text(errors="replace")
    for m in re.finditer(r"unsigned char\s+(\w+)\[\]\s*=\s*\{(.*?)\};", src, re.S):
        vals = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{1,2})", m.group(2))]
        # The FreeDOS/NT/PE variants reuse the plain identifier (br_fat32_0x52)
        # and only differ by header file, so name by file where the file
        # carries the offset.
        ident = name.upper() if "_0x" in name else m.group(1).upper()
        out.append(f"{ident} = bytes([")
        for i in range(0, len(vals), 12):
            out.append("    " + ", ".join(f"0x{v:02x}" for v in vals[i:i + 12]) + ",")
        out += ["])", f"assert len({ident}) == {len(vals)}", ""]
dest = pathlib.Path(__file__).resolve().parent.parent / "stoke" / "bootcode.py"
dest.write_text("\n".join(out) + "\n")
print("wrote", dest)
