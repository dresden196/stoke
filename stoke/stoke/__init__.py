"""Stoke: a bootable USB writer for Linux.

Everything that touches a disk lives here. The window (stoke-qt) only
draws what this package reports and hands it a job to run.

The design and most of the domain knowledge come from Rufus
(https://github.com/pbatard/rufus, GPLv3, Pete Batard): what makes a Windows
ISO boot from NTFS on UEFI, which registry keys skip the Windows 11 hardware
checks, how a persistent Ubuntu stick is laid out. The Windows plumbing was
left behind: on Linux a disk is a file and a partition is a file, so the
parts of Rufus that fight drive letters and VDS have no equivalent here.
"""

__version__ = "0.3.0"
APP_NAME = "Stoke"
# Where the payload (uefi-ntfs.img, FreeDOS, the setup wrapper) is installed.
# Overridden by STOKE_PAYLOAD for running from the source tree.
DEFAULT_PAYLOAD_DIR = "/usr/share/stoke"
