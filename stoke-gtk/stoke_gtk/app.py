"""The application object: one window, an image given on the command line."""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .engine import Backend  # noqa: E402
from .i18n import _  # noqa: E402
from .window import StokeWindow  # noqa: E402


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name("Stoke")
        self.backend = None
        self.window = None
        self.connect("shutdown", self._on_shutdown)

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name("stoke")
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_a: self._request_quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _window(self):
        if self.window is None:
            self.backend = Backend()
            self.window = StokeWindow(self, self.backend)
        return self.window

    def do_activate(self):
        self._window().present()

    def do_open(self, files, n_files, hint):
        # An image opened with the writer from a file manager. Handed to the
        # window as the boot selection, and nothing more: opening a file must
        # never start writing a drive.
        window = self._window()
        window.present()
        for f in files:
            path = f.get_path()
            if path and os.path.isfile(path):
                window.select_image(os.path.abspath(path))
                break

    def _request_quit(self):
        if self.window is not None:
            self.window.close()
        else:
            self.quit()

    def _on_shutdown(self, *_args):
        if self.backend is not None:
            self.backend.shutdown()

    def _on_about(self, *_args):
        dialog = Adw.AboutDialog(
            application_name="Stoke",
            application_icon="stoke",
            developer_name="Dresden Wildey",
            version=__version__,
            website="https://github.com/dresden196/stoke",
            issue_url="https://github.com/dresden196/stoke/issues",
            license_type=Gtk.License.GPL_3_0,
            comments=_("Write bootable USB drives"),
        )
        if self.backend is not None and self.backend.engine_version:
            dialog.set_debug_info("stoke engine " + self.backend.engine_version)
        dialog.present(self.window)
