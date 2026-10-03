# stoke engine protocol

The window runs the engine as `pkexec /usr/bin/stoke serve` (root is
needed to write a drive through the device nodes) and talks JSON, one
object per line, over its stdin/stdout. Unprivileged commands (`devices`,
`probe`, `hash`) also work without pkexec: `stoke serve` as the user.

On start the engine prints one line:

    {"event":"hello","app":"Stoke","version":"0.1.0","root":true,
     "backend":"native","can_write":true,
     "windows_options":[...all option names...],"windows_defaults":[...]}

`backend` is how this engine reaches drives: `native` (device nodes, needs
root) or `udisks` (udisks2 over D-Bus, no root; polkit asks when the drive
is opened). `can_write` says whether `write` will be accepted. A window
should ask an unprivileged engine first and only start a `pkexec` one when
`can_write` is false; inside a Flatpak or AppImage the engine picks the
udisks backend by itself, and `STOKE_BACKEND=native|udisks` forces one.

## Requests

Every request carries an `id` (any JSON value; echoed back) and a `cmd`.

| cmd        | fields                                   | reply |
|------------|------------------------------------------|-------|
| `ping`     |                                          | `{"id","result":"pong"}` |
| `devices`  | `usb_hdd` (bool), `all` (bool), `loop` (bool) | `{"id","result":[disk...]}` |
| `clusters` | `fs`, `size` (bytes)                     | `{"id","result":{"default":N,"choices":[...]}}` |
| `probe`    | `path`                                   | streams `log` events, then `{"id","result":report}` or `{"id","event":"done","ok":false,"error":...}` |
| `hash`     | `path`, `algorithms` ([md5,sha1,sha256,sha512]) | `progress` events (phase `hash`), then `{"id","result":{"md5":...}}` |
| `write`    | `job` (object, see below)                | streams events; ends with `{"id","event":"done","ok":true,"label":...}` or `{"id","event":"done","ok":false,"error":"...","cancelled":true?}` |
| `cancel`   |                                          | cancels the running write/hash/download |
| `win_versions` |                                      | `{"id","result":[{"index","name","kind"}]}` (Windows/UEFI Shell ISO list) |
| `win_releases` | `version` (index)                    | `{"id","result":[{"index","label"}]}` |
| `win_editions` | `version`, `release`, `locale`       | `{"id","result":[{"index","name","ids"}]}` |
| `win_languages` | `version`, `edition_ids`, `locale`  | streams `log`; then `{"id","result":{"token","languages":[{"name","display","data"}]}}` (talks to Microsoft) |
| `win_links` | `version`, `release`, `edition_ids`, `token`, `language_data` | streams `log`; then `{"id","result":[{"arch","url"}]}` |
| `win_download` | `url`, `dest`                       | `progress` events (phase `download`), then `{"id","event":"done","ok":true,"path","size"}` |
| `quit`     |                                          | `{"id","result":"bye"}` and exit |

Only one `write` or `hash` runs at a time. A second one is answered with
`{"id","error":"a job is already running"}`.

## Events (all carry the request `id`)

- `{"event":"log","text":"..."}` — one line for the log pane (Rufus's log window).
- `{"event":"status","text":"..."}` — the current step, for the status line.
- `{"event":"progress","phase":"copy","value":0.42,"message":"1.2 GB / 3.0 GB"}` — `value` is 0..1 or null (indeterminate).
  Phases: `scan`, `badblocks`, `partition`, `format`, `bootrec`, `copy`, `patch`, `finalize`, `write` (DD), `verify`, `hash`, `apply` (Windows To Go).
- `{"event":"overall","value":0.37}` — the whole job, 0..1, for the main progress bar.
- `{"event":"done","ok":bool,...}` — terminal event of `write`.

## Disk object (`devices`)

    {"device":"/dev/sdb","name":"sdb","size":31029460992,"size_human":"31 GB",
     "sector_size":512,"physical_sector_size":512,"removable":true,
     "kind":"usb"|"usb-hdd"|"card"|"removable"|"loop"|"internal",
     "vendor":"SanDisk","model":"Ultra","serial":"...","label":"UBUNTU",
     "display":"UBUNTU (31 GB) [sdb]","partition_table":"gpt"|"dos"|"",
     "partitions":[{"device":"/dev/sdb1","number":1,"start":..,"size":..,"fstype":"vfat","label":"..","mountpoints":["/run/media/..."]}],
     "mounted":true}

Disks that hold the running system are never listed. `kind` other than
`usb`/`card`/`removable` only appears when asked for.

## Image report (`probe`)

Keys the window needs:

- `name`, `size`, `size_human`, `type` (`iso`|`raw`|`vhd`|`compressed`|`wim`|`unknown`), `label`
- `is_iso`, `is_hybrid` (DD possible), `is_windows`, `wininst` (list), `win_version` `{major,minor,build,revision}`,
  `win_arch`, `win_editions` `[{index,name,edition,arch,version,languages}]`, `win_language`
- `has_4gb_file`, `needs_ntfs`, `supports_persistence`, `disable_iso`, `iso_mode_available`
- `recommended`: `{"mode":"iso"|"dd","scheme":"mbr"|"gpt","target":"bios"|"uefi"|"dual","fs":"fat32"|"ntfs",
   "label":"...","iso_mode_available":bool,"dd_mode_available":bool,"boots_uefi":bool,"boots_bios":bool}`
- `warnings` (list of strings)

## Job object (`write`)

    {"device":"/dev/sdb",
     "boot_type":"image"|"none"|"freedos"|"syslinux"|"grub2"|"grub4dos"|"uefi_ntfs",
     "image":"/path/to.iso",              # boot_type image
     "mode":"iso"|"dd",                   # image option
     "wintogo":false, "wintogo_index":1,  # Windows To Go with this install.wim index
     "scheme":"mbr"|"gpt",
     "target":"bios"|"uefi"|"dual",       # dual = "BIOS or UEFI"
     "fs":"fat32"|"fat16"|"ntfs"|"exfat"|"ext2"|"ext3"|"ext4",
     "cluster_size":0,                    # bytes, 0 = default
     "label":"UBUNTU",
     "quick_format":true,
     "bad_blocks":0,                      # 0..4 destructive passes
     "extended_label":false,              # autorun.inf
     "old_bios_fixes":false,
     "rufus_mbr":false,                   # masquerading MBR
     "persistence_size":0,                # bytes
     "windows_options":["bypass_requirements","no_online_account",...],
     "username":"", "edition_index":1,
     "verify":false,                      # DD readback
     "zero_full":false}                   # non bootable: zero whole drive

Windows option names: `bypass_requirements`, `no_online_account`,
`no_data_collection`, `offline_internal_drives`, `duplicate_locale`,
`set_user` (with `username`), `disable_bitlocker`, `force_s_mode`,
`use_ms2023_bootloaders`, `apply_skusipolicy`, `silent_install` (with
`edition_index`), `qol_enhancements`. Defaults: the first, second and fourth.

Rules the window should enforce before sending (the engine checks too):
GPT requires target uefi; `dual` target requires MBR; Windows images with
a file over 4 GB default to NTFS (FAT32 is allowed, install.wim is split);
`needs_ntfs` forbids FAT; persistence only when `supports_persistence`;
Windows To Go needs `fs` ntfs. On MBR the boot files live on the NTFS
partition and UEFI:NTFS chains into them; on GPT the engine adds an ESP and
an MSR and the store names partitions by GUID, like bcdboot.

## Exit / errors

`pkexec` exits 126/127 when authentication is cancelled; the window should
say so rather than "engine crashed".
