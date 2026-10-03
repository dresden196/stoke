# Testing

Every claim of "boots" below is backed by a screenshot taken from the booted
machine, produced by the scripts in `tests/usb/`.

## Bench

- `tests/usb/native.sh <scenario> [iso]` writes a sparse disk image through a
  loop device on the host and boots it in QEMU under BIOS and UEFI. The quick
  check; needs root and a kernel that can mount the target file systems.
- `tests/usb/vm.sh` boots an Arch-based live ISO (`STOKE_LIVE_ISO`) with an
  emulated USB stick and shares this tree into it; `guest-setup.sh` installs
  the engine's dependencies; `run.sh <scenario>` drives the engine inside the
  guest and boots the result on the host (`boot-stick.sh`).
- `tests/usb/boot-real.sh <vendor:product> uefi|bios|secboot` hands a physical
  stick to QEMU over USB passthrough. `secboot` uses OVMF's Secure Boot build
  with Microsoft's certificates enrolled (`virt-fw-vars --enroll-redhat`).
- `tests/usb/install-vm.sh` lets a written Windows stick install itself onto
  an empty disk with no TPM attached; `test-cancel.sh` covers cancel
  mid-write, running out of temporary space, and refused devices.

Knobs: `STOKE_MACHINE=pc` (pre-q35 chipset), `STOKE_USB_CTRL=ehci` (USB 2.0
controller), `STOKE_EXTRA_DISK=1` (an empty SATA disk), `STOKE_BOOT_MEM`,
`STOKE_STICK_PERSIST=1` (keep the guest's writes, to read its logs after).

## Verified

| Media | Layout | BIOS | UEFI |
|---|---|---|---|
| Ubuntu 24.04 Server, ISO mode, 2 GB persistence | MBR, FAT32 | installer | installer |
| Ubuntu 24.04 Server, DD mode with read-back verify | as image | | installer |
| Windows 11 24H2, NTFS via UEFI:NTFS | GPT | | Setup |
| Windows 11 24H2, exFAT via UEFI:NTFS | GPT | | Setup |
| Windows 11 24H2, FAT32 with split install.swm | MBR | Setup | Setup |
| Windows 11 24H2, silent install, no TPM | GPT | | to the desktop |
| Windows To Go, NTFS | MBR | OOBE | OOBE |
| Windows To Go, NTFS + ESP + MSR | GPT | | OOBE |
| Windows XP SP3 setup media (masquerading MBR, patched setupldr) | MBR, FAT32 | text-mode Setup | |
| Fedora Workstation 44 (config in /boot/grub2) | MBR, FAT32 | GRUB → kernel | Plymouth |
| SakuraOS live (archiso, Syslinux 6.04) | MBR, FAT32 and ext4 | welcome | welcome |

Re-verified on 2026-09-10 after the engine took over partitioning, formatting
and boot-loader placement from sfdisk, extlinux and grub-install (the udisks2
backend shares that code):

| Media | Layout | BIOS | UEFI |
|---|---|---|---|
| FreeDOS 1.4 | MBR, FAT32 | `C:\>` prompt | |
| TinyCore (Syslinux, written natively, through udisks2, and from the Flatpak as a plain user) | MBR, FAT32 | menu | no UEFI loader in that ISO |
| Ubuntu 20.04 Server (isolinux + gfxboot) | MBR, FAT32 | kernel, cloud-init | kernel, cloud-init |
| Ubuntu 24.04 Server (GRUB), 2 GB persistence | MBR, FAT32 + ext4 | casper with persistence | casper with persistence |
| Ubuntu 20.04 Server, DD mode with read-back verify | as image | kernel | |
| Windows 11 24H2, FAT32 with split install.swm | MBR | Setup | Setup |
| Windows 11 24H2, NTFS via UEFI:NTFS | GPT | | Setup |
| Windows XP SP3 setup media | MBR, FAT32 | text-mode Setup | |
| Windows To Go, NTFS | MBR | first boot | first boot |

Real hardware through the **udisks2 backend** (USB DISK 3.0, 31 GB, booted
with `boot-real.sh` over USB passthrough):

| Media | Written by | BIOS | UEFI |
|---|---|---|---|
| Windows 11 24H2, NTFS via UEFI:NTFS, GPT | the Flatpak, as a plain user (polkit prompt) | | Setup, also under enforcing Secure Boot |
| Ubuntu 24.04 Server, 2 GB persistence, MBR/FAT32 | `STOKE_BACKEND=udisks` as root | systemd | systemd |

0.3.0 (the rename to Stoke), run on the SakuraOS build VM with KVM:
FreeDOS, TinyCore, Ubuntu 24.04.5 with persistence (BIOS and UEFI),
Windows 11 26H2 on FAT32 with a split install.swm (BIOS and UEFI) and on
GPT/NTFS via UEFI:NTFS, all to the expected screen; the cancel suite 7 of 7.
The Windows ISO came from Stoke's own downloader with the 26H2 product ids.

ISO downloader (0.2.3, a Fido port): `stoke download` and both windows'
Download dialogs fetched the live language list and a Windows 11 25H2 x64
link from Microsoft, and downloaded the UEFI Shell 2.2 ISO from GitHub,
which each window then adopted as its boot selection.

Packages (0.2.2): `Stoke-qt-*.AppImage` and `Stoke-gtk-*.AppImage` start on
a bare X server and spawn their bundled engine; the two Flatpaks
(`io.github.dresden196.stoke` on the KDE runtime, `.gtk` on the GNOME
runtime) report `backend: udisks, can_write: true` and list the stick.

Known limitation, same as Rufus: a Linux ISO written to **exFAT** reaches
its own GRUB through UEFI:NTFS but the distribution's signed GRUB has no
exFAT driver, so it stops at a `grub>` prompt. Use FAT32 or NTFS for Linux
images.
| FreeDOS 1.4 | MBR, FAT32; FAT16 on a 1 GB drive | `C:\>` | |
| KolibriOS | MBR, FAT32 | desktop | |
| Grub4DOS boot type | MBR, FAT32 | `grub>` | |
| ReactOS 0.4.15 BootCD | MBR, FAT32 | FreeLoader and kernel load, then STOP 0x7B: the image cannot mount a disk partition as its boot device, on USB or SATA alike | |

On physical sticks handed to QEMU over USB passthrough: Windows 11 on NTFS
boots under UEFI and under enforcing Secure Boot; an archiso live image in ISO
mode boots under BIOS and UEFI; an R1Soft recovery CD written through the
window boots under both; the XP stick's contents boot on a `pc` machine with
an EHCI controller (XP has no xHCI driver and rejects q35's ACPI).

Also exercised: the destructive bad-blocks scan, cancel mid-copy, refusal of
the system disk and of unknown devices, and a 64 MB temporary directory
forcing the install.wim split to fail cleanly.
