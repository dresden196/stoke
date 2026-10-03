"""Entry point: `stoke-gtk [image]`."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .app import Application  # noqa: E402


def main(argv=None):
    app = Application()
    return app.run(sys.argv if argv is None else argv)
