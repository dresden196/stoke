# stoke (engine)

Makes bootable USB drives from ISO and disk images, the way Rufus does on
Windows. Works on its own from the command line; `stoke-qt` is the window.

    stoke devices                       # drives it is willing to write
    stoke probe some.iso                # what the image is and how it boots
    sudo stoke write -d /dev/sdb -i some.iso            # defaults from the probe
    sudo stoke write -d /dev/sdb -i win11.iso --scheme gpt --target uefi --fs ntfs \
         --windows-option bypass_requirements --windows-option no_online_account
    sudo stoke write -d /dev/sdb -i ubuntu.iso --persistence 4G
    sudo stoke write -d /dev/sdb -i win11.iso --wintogo 1 --scheme gpt --target uefi --fs ntfs
    sudo stoke write -d /dev/sdb -i image.img.xz --mode dd --verify
    sudo stoke write -d /dev/sdb --boot-type freedos
    sudo stoke write -d /dev/sdb --boot-type grub4dos     # then add a menu.lst

The window (`stoke-qt`) runs `stoke serve` under pkexec and talks
the protocol in `PROTOCOL.md`.

## What it does that a plain `dd` cannot

- Windows install media on NTFS or FAT32, booting on UEFI through the
  UEFI:NTFS loader and on BIOS through Microsoft's own boot code.
- Windows 11 hardware-check bypass written straight into `boot.wim`'s
  registry (hivex + wimlib), the in-place-upgrade wrapper for 24H2, answer
  files for local account / no online account / no telemetry / no BitLocker.
- `install.wim` over 4 GB split for FAT32.
- Windows To Go: `install.wim` applied to NTFS, boot files and a BCD built
  from the media, internal drives set offline.
- Linux ISOs copied as files with config patches (labels, persistence,
  Red Hat `inst.repo`), BIOS boot through GRUB or Syslinux, persistent
  partition for Ubuntu and Debian live media.
- Windows XP-era setup media: the NT loader installed as BOOTMGR/NTLDR and
  patched, txtsetup.sif pointed at the stick, the masquerading MBR making the
  stick disk 1 so Setup installs to the real hard disk. Linux then no longer
  lists the partition (its MBR parser rejects the 0x81 flag); firmware is fine.
- Raw images, fixed VHD, and gzip/xz/bzip2/zstd compressed images, with
  optional read-back verification.

## Layout

| module | job |
|---|---|
| `iso9660.py`, `udf.py` | read ISO images without mounting them (UDF for Windows media) |
| `image.py` | the probe: what boots how, Windows version and editions, recommended settings |
| `layout.py` | partition plan; writes the MBR or GPT itself |
| `bootrec.py`, `bootcode.py` | MBR / volume boot records (byte arrays ported from ms-sys) |
| `blockio.py`, `backend.py` | descriptor-based access to disk and partitions: as root through device nodes (mount(8), a udev rule to keep the desktop away) or through udisks2 over D-Bus with no root |
| `fs.py` | mkfs on a sparse image, copied to the partition by extents; labels |
| `syslinux.py` | Syslinux installed by hand: ldlinux.sys sector map, boot record (payload from `tools/build-syslinux.sh`) |
| `badblocks.py` | destructive write/read pattern scan |
| `extract.py` | file copy with the config fixes |
| `linux.py` | Syslinux modules, GRUB (grub-mkimage + boot.img/core.img placement), persistence, FreeDOS, Grub4DOS |
| `unattend.py`, `windows.py`, `wim.py` | Windows customization and Windows To Go |
| `writer.py` | DD mode |
| `job.py` | the sequence |
| `cli.py` | commands and the `serve` protocol |

`payload/` holds `uefi-ntfs.img`, FreeDOS and the setup wrapper, taken
from Rufus (GPLv3, Pete Batard). `tools/port-ms-sys.py` regenerates
`bootcode.py` from a Rufus checkout.

## Testing

`tests/usb/native.sh` writes a loop-backed disk image on the host and boots
it under BIOS and UEFI in QEMU; that is the quick check. `tests/usb/vm.sh`
boots an Arch-based live ISO in QEMU with an emulated
USB stick and runs the engine inside it, then boots the result under BIOS
and UEFI. See `tests/usb/run.sh`.
