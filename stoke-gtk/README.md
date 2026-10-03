# stoke-gtk

The GNOME window for Stoke: GTK 4 and libadwaita, in Python. It is the
same tool as `stoke-qt` (the KDE window) laid out the same way as Rufus,
and it drives the same engine over the same protocol (`stoke/PROTOCOL.md`).
Everything that looks at a disk or an image and every decision about what
will boot is the engine's; this window draws what it reports and hands it
a job.

Two engine processes are used: one as the user, for listing drives and
probing images, and one under `pkexec` (polkit action
`io.github.dresden196.stoke.write`), started the first time START is
pressed and kept for the next write so the password is asked once per
session.

## Running from the tree

    STOKE_ENGINE=$PWD/../stoke/bin/stoke \
    STOKE_LIB=$PWD/../stoke \
    STOKE_PAYLOAD=$PWD/../stoke/payload \
    STOKE_GTK_LIB=$PWD \
    ./bin/stoke-gtk [image.iso]

`STOKE_ENGINE` points at the engine (default `/usr/bin/stoke`);
`STOKE_LIB` and `STOKE_PAYLOAD` are passed through to it, including
across `pkexec`. `STOKE_GTK_DEBUG=1` echoes the log pane to the terminal.
When already root, the privileged engine is started directly.

## Packaging

`makepkg` here builds `stoke-gtk` for Arch Linux; it depends on the
`stoke` engine package, which also carries the shared icon. Translations
go in `po/<lang>/stoke-gtk.po`, from the template `po/stoke-gtk.pot`:

    xgettext --from-code=UTF-8 -k_ -kN_ -kngettext:1,2 -kpgettext:1c,2 \
        -o po/stoke-gtk.pot stoke_gtk/*.py
