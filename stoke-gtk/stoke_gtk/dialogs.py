"""The dialogs: the Windows options, the checksums, and the last warning
before a drive is erased."""

from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .engine import current_user, remember_windows_choices, saved_windows_choices  # noqa: E402
from .i18n import _, fmt, pgettext  # noqa: E402


def iso_name_from_url(url):
    """The file name to offer in the save dialog: the URL's last path segment,
    unquoted, or a plain fallback. The engine names the file the same way, but
    exposes no helper to the window, so it is derived here."""
    try:
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        if name:
            return name
    except Exception:
        pass
    return "windows.iso"

# Rufus's "Windows User Experience" dialog: what to patch into the installer
# before it runs. The option names are the engine's; only the labels live
# here.
WINDOWS_OPTIONS = [
    ("bypass_requirements", _("Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0")),
    ("no_online_account", _("Remove requirement for an online Microsoft account")),
    ("set_user", _("Create a local account with username:")),
    ("duplicate_locale", _("Set regional options to the same values as this user's")),
    ("no_data_collection", _("Disable data collection (Skip privacy questions)")),
    ("disable_bitlocker", _("Disable BitLocker automatic device encryption")),
    ("offline_internal_drives", _("Set internal drives offline")),
    ("qol_enhancements", _("Apply Windows quality-of-life defaults (no OneDrive/Outlook/Copilot/ads)")),
    ("silent_install", _("Silent unattended installation (wipes disk 0!)")),
    ("use_ms2023_bootloaders", _("Use 'Windows UEFI CA 2023' signed bootloaders")),
]

HASH_ALGORITHMS = [("md5", "MD5"), ("sha1", "SHA1"), ("sha256", "SHA256"), ("sha512", "SHA512")]


def confirm_write(parent, device_text, size_text, on_ok):
    """The last thing between the user and an erased drive."""
    body = (fmt(_("Device: %1\nSize: %2"), device_text, size_text) if size_text
            else fmt(_("Device: %1"), device_text))
    dialog = Adw.AlertDialog(
        heading=fmt(_("WARNING: ALL DATA ON DEVICE '%1' WILL BE DESTROYED."), device_text),
        body=body + "\n\n" + _("To continue with this operation, click OK. To quit click CANCEL."),
    )
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("OK"))
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog, response):
        if response == "ok":
            on_ok()

    dialog.connect("response", on_response)
    dialog.present(parent)


def confirm_close(parent, on_yes):
    dialog = Adw.AlertDialog(
        heading=_("Write in progress"),
        body=_("A drive is still being written. Cancel it and close?"),
    )
    dialog.add_response("no", _("No"))
    dialog.add_response("yes", _("Yes"))
    dialog.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("no")
    dialog.set_close_response("no")

    def on_response(_dialog, response):
        if response == "yes":
            on_yes()

    dialog.connect("response", on_response)
    dialog.present(parent)


class WindowsOptionsDialog(Adw.Dialog):
    """Asks how the Windows installer should be customised and hands the
    completed job to `on_done`."""

    def __init__(self, backend, job, editions, wintogo, on_done):
        super().__init__(title=_("Windows User Experience"), content_width=520)
        self._job = job
        self._editions = list(editions or [])
        self._on_done = on_done
        self._switches = {}

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.close())
        header.pack_start(cancel)
        ok = Gtk.Button(label=_("OK"))
        ok.add_css_class("suggested-action")
        ok.connect("clicked", self._accept)
        header.pack_end(ok)
        view.add_top_bar(header)
        self.set_default_widget(ok)

        group = Adw.PreferencesGroup(description=_("Customize the Windows installation:"))
        # Only what this engine says it understands, so an option it would
        # reject is never offered.
        known = backend.windows_options or [k for k, _l in WINDOWS_OPTIONS]
        saved = saved_windows_choices()
        on = list(saved["options"]) if saved["saved"] else list(backend.windows_defaults)
        # Taking internal drives offline is what keeps a Windows To Go stick
        # from mounting, or worse, using the host's own Windows.
        if wintogo and "offline_internal_drives" not in on:
            on.append("offline_internal_drives")

        self._username = None
        self._edition = None
        for key, label in WINDOWS_OPTIONS:
            if key not in known:
                continue
            if key == "set_user":
                row = Adw.ExpanderRow(title=label, show_enable_switch=True, enable_expansion=key in on)
                self._username = Adw.EntryRow(title=_("Username"), text=saved["username"] or current_user())
                row.add_row(self._username)
                self._switches[key] = row
            elif key == "silent_install" and self._editions:
                row = Adw.ExpanderRow(title=label, show_enable_switch=True, enable_expansion=key in on)
                self._edition = Adw.ComboRow(title=_("Windows edition"))
                self._edition.set_model(Gtk.StringList.new([str(e.get("name", "")) for e in self._editions]))
                row.add_row(self._edition)
                self._switches[key] = row
            else:
                row = Adw.SwitchRow(title=label, active=key in on)
                self._switches[key] = row
            group.add(row)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      max_content_height=640)
        clamp = Adw.Clamp(margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        clamp.set_child(group)
        scroller.set_child(clamp)
        view.set_content(scroller)
        self.set_child(view)

    def _chosen(self):
        chosen = []
        for key, row in self._switches.items():
            active = row.get_enable_expansion() if isinstance(row, Adw.ExpanderRow) else row.get_active()
            if active:
                chosen.append(key)
        return chosen

    def _accept(self, *_args):
        chosen = self._chosen()
        username = self._username.get_text().strip() if self._username is not None else ""
        if not username:
            chosen = [k for k in chosen if k != "set_user"]
        remember_windows_choices(chosen, username)

        job = dict(self._job)
        job["windows_options"] = chosen
        job["username"] = username
        index = 1
        if self._edition is not None:
            selected = self._edition.get_selected()
            if 0 <= selected < len(self._editions):
                index = int(self._editions[selected].get("index", 1))
        job["edition_index"] = index
        self.close()
        self._on_done(job)


class ChecksumDialog(Adw.Dialog):
    """Rufus's "#" button: the image's checksums, computed by the engine while
    a progress bar shows the file being read."""

    def __init__(self, backend, path, name):
        super().__init__(title=_("Checksums"), content_width=560)
        self._backend = backend

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18, margin_start=18, margin_end=18)
        title = Gtk.Label(label=name, ellipsize=Pango.EllipsizeMode.MIDDLE, xalign=0)
        title.add_css_class("heading")
        box.append(title)
        self._bar = Gtk.ProgressBar()
        box.append(self._bar)

        grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        self._entries = {}
        for row, (key, label) in enumerate(HASH_ALGORITHMS):
            grid.attach(Gtk.Label(label=label, xalign=0, width_chars=7), 0, row, 1, 1)
            entry = Gtk.Entry(editable=False, hexpand=True, can_focus=True)
            entry.add_css_class("monospace")
            grid.attach(entry, 1, row, 1, 1)
            self._entries[key] = entry
        box.append(grid)
        view.set_content(box)
        self.set_child(view)

        self._handler = backend.connect("hash-changed", self._update)
        self.connect("closed", self._on_closed)
        backend.hash(path)
        self._update()

    def _update(self, *_args):
        backend = self._backend
        self._bar.set_visible(backend.hashing)
        self._bar.set_fraction(max(0.0, min(1.0, backend.hash_progress)))
        for key, entry in self._entries.items():
            entry.set_text(str(backend.hashes.get(key) or ""))
            entry.set_placeholder_text(_("Computing…") if backend.hashing else "")

    def _on_closed(self, *_args):
        self._backend.disconnect(self._handler)
        # Closing while the file is still being read stops the read; a
        # checksum nobody is waiting for is only disk traffic.
        if self._backend.hashing:
            self._backend.cancel()


class DownloadDialog(Adw.Dialog):
    """Rufus's "Download" button: pick a Windows or UEFI Shell ISO from
    Microsoft (and GitHub) by version, release, edition, language and
    architecture, then hand the chosen link and a save path to `on_download`.

    Version, release and edition are answered from the engine's local tables
    and cascade instantly. The language list and the download links are fetched
    over the network, so those steps show a spinner and disable the form. A
    generation counter discards a cascade the user has already moved past.

    Laid out like the main window (and the KDE dialog): Rufus's form, a small
    label over each full-width drop-down, the buttons at the bottom right."""

    LOCALE = "en-US"

    def __init__(self, backend, on_download):
        super().__init__(title=_("Download ISO"), content_width=520)
        # window.py imports this module at load time, so the form helpers are
        # fetched here, once the window module is complete, rather than at
        # the top of the file.
        from .window import ChoiceDrop, field  # noqa: PLC0415
        self._backend = backend
        self._on_download = on_download
        self._gen = 0
        self._busy = False
        self._versions = []
        self._releases = []
        self._editions = []
        self._languages = []
        self._token = None
        self._links = []

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=6, margin_bottom=12, margin_start=12, margin_end=12)
        self._version_row = ChoiceDrop(self._on_version_changed)
        self._release_row = ChoiceDrop(self._on_release_changed)
        self._edition_row = ChoiceDrop(self._on_edition_changed)
        self._language_row = ChoiceDrop(self._on_language_changed)
        self._arch_row = ChoiceDrop(lambda _value: None)
        for label, drop in ((_("Version"), self._version_row), (_("Release"), self._release_row),
                            (_("Edition"), self._edition_row), (_("Language"), self._language_row),
                            (_("Architecture"), self._arch_row)):
            form.append(field(label, drop))

        self._spinner = Gtk.Spinner()
        self._status = Gtk.Label(xalign=0, wrap=True)
        self._status.add_css_class("dim-label")
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=12)
        status_row.append(self._spinner)
        status_row.append(self._status)
        form.append(status_row)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=12,
                          halign=Gtk.Align.END)
        cancel = Gtk.Button(label=_("Cancel"), width_request=100)
        cancel.connect("clicked", lambda *_a: self.close())
        actions.append(cancel)
        self._download_button = Gtk.Button(label=pgettext("@action:button", "Download"), width_request=100)
        self._download_button.add_css_class("suggested-action")
        self._download_button.connect("clicked", self._on_download_clicked)
        actions.append(self._download_button)
        form.append(actions)
        self.set_default_widget(self._download_button)

        view.set_content(form)
        self.set_child(view)
        self._set_busy(False)
        self._load_versions()

    # ---- drop-down helpers ----------------------------------------------

    def _fill(self, drop, texts, selected=0):
        # The choices are the list indexes: what every engine call takes.
        drop.set_choices([(text, i) for i, text in enumerate(texts)], selected)

    def _selected(self, drop):
        value = drop.get_value()
        return value if value is not None else -1

    # ---- busy state -----------------------------------------------------

    def _set_busy(self, on, text=""):
        self._busy = on
        if on:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._spinner.set_visible(on)
        self._status.set_text(text)
        for row in (self._version_row, self._release_row, self._edition_row,
                    self._language_row, self._arch_row):
            row.set_sensitive(not on)
        self._refresh_download_button()

    def _refresh_download_button(self):
        # Download needs a fetched language list (its token and data feed the
        # links call); nothing to download before then.
        self._download_button.set_sensitive(
            not self._busy and bool(self._languages) and self._token is not None)

    def _request(self, req, handler):
        # A dedicated engine, not the drive-polling one: see Backend.query_engine.
        self._backend.query_engine().request(req, handler)

    def _log(self, msg):
        text = msg.get("text")
        if text:
            self._backend.note(str(text))

    # ---- the cascade ----------------------------------------------------

    def _load_versions(self):
        def on_reply(msg):
            if "result" not in msg:
                if "error" in msg:
                    self._set_busy(False, str(msg["error"]))
                return
            self._versions = list(msg["result"] or [])
            self._fill(self._version_row, [str(v.get("name", "")) for v in self._versions])
            if self._versions:
                self._load_releases()
        self._request({"cmd": "win_versions"}, on_reply)

    def _load_releases(self):
        version = self._selected(self._version_row)
        if version < 0:
            return
        self._gen += 1
        gen = self._gen
        self._reset_below("release")

        def on_reply(msg):
            if gen != self._gen or "result" not in msg:
                return
            self._releases = list(msg["result"] or [])
            self._fill(self._release_row, [str(r.get("label", "")) for r in self._releases])
            self._load_editions(gen)
        self._request({"cmd": "win_releases", "version": version}, on_reply)

    def _load_editions(self, gen=None):
        if gen is None:
            self._gen += 1
            gen = self._gen
        version = self._selected(self._version_row)
        release = self._selected(self._release_row)
        if version < 0 or release < 0:
            return
        self._reset_below("edition")

        def on_reply(msg):
            if gen != self._gen or "result" not in msg:
                return
            self._editions = list(msg["result"] or [])
            self._fill(self._edition_row, [str(e.get("name", "")) for e in self._editions])
            self._load_languages(gen)
        self._request({"cmd": "win_editions", "version": version, "release": release,
                       "locale": self.LOCALE}, on_reply)

    def _load_languages(self, gen=None):
        if gen is None:
            self._gen += 1
            gen = self._gen
        version = self._selected(self._version_row)
        edition = self._selected(self._edition_row)
        if version < 0 or edition < 0:
            return
        self._reset_below("language")
        edition_ids = list(self._editions[edition].get("ids") or [])
        self._set_busy(True, _("Fetching languages from Microsoft…"))

        def on_reply(msg):
            if gen != self._gen:
                return
            event = msg.get("event")
            if event == "log":
                self._log(msg)
                return
            if "result" in msg:
                result = msg["result"] or {}
                self._token = result.get("token")
                self._languages = list(result.get("languages") or [])
                self._fill(self._language_row,
                           [str(la.get("display") or la.get("name") or "") for la in self._languages])
                self._set_busy(False, "" if self._languages else _("No languages were returned."))
                return
            if event == "done" or "error" in msg:
                self._languages = []
                self._token = None
                self._set_busy(False, fmt(_("Could not fetch languages: %1"),
                                          msg.get("error") or _("network error")))
        self._request({"cmd": "win_languages", "version": version,
                       "edition_ids": edition_ids, "locale": self.LOCALE}, on_reply)

    def _reset_below(self, level):
        # Clear every choice downstream of the one that changed, so a stale
        # language or link is never carried into a new selection.
        order = ["version", "release", "edition", "language", "link"]
        below = order[order.index(level) + 1:]
        if "language" in below:
            self._languages = []
            self._token = None
            self._fill(self._language_row, [])
        if "link" in below:
            self._links = []
            self._fill(self._arch_row, [])
        self._refresh_download_button()

    # ---- user changes ---------------------------------------------------

    def _on_version_changed(self, _value):
        self._load_releases()

    def _on_release_changed(self, _value):
        self._load_editions()

    def _on_edition_changed(self, _value):
        self._load_languages()

    def _on_language_changed(self, _value):
        # A new language means the previous links no longer apply, but the
        # language list itself stays.
        self._gen += 1
        self._reset_below("language")

    # ---- links and download --------------------------------------------

    def _on_download_clicked(self, *_args):
        if self._busy or not self._languages or self._token is None:
            return
        if not self._links:
            self._load_links()
            return
        arch = self._selected(self._arch_row)
        if 0 <= arch < len(self._links):
            self._choose_dest(str(self._links[arch].get("url") or ""))

    def _load_links(self):
        self._gen += 1
        gen = self._gen
        version = self._selected(self._version_row)
        release = self._selected(self._release_row)
        edition = self._selected(self._edition_row)
        language = self._selected(self._language_row)
        if min(version, release, edition, language) < 0:
            return
        edition_ids = list(self._editions[edition].get("ids") or [])
        language_data = list(self._languages[language].get("data") or [])
        self._set_busy(True, _("Fetching download links…"))

        def on_reply(msg):
            if gen != self._gen:
                return
            event = msg.get("event")
            if event == "log":
                self._log(msg)
                return
            if "result" in msg:
                self._links = list(msg["result"] or [])
                self._fill(self._arch_row, [str(li.get("arch") or "") for li in self._links])
                self._set_busy(False, "" if self._links else _("No download links were returned."))
                # One architecture is the common case (and UEFI Shell always):
                # go straight to the save dialog rather than make the user pick.
                if len(self._links) == 1:
                    self._choose_dest(str(self._links[0].get("url") or ""))
                return
            if event == "done" or "error" in msg:
                self._links = []
                self._set_busy(False, fmt(_("Could not fetch links: %1"),
                                          msg.get("error") or _("network error")))
        self._request({"cmd": "win_links", "version": version, "release": release,
                       "edition_ids": edition_ids, "token": self._token,
                       "language_data": language_data}, on_reply)

    def _choose_dest(self, url):
        if not url:
            return
        dialog = Gtk.FileDialog(title=_("Save ISO"))
        dialog.set_initial_name(iso_name_from_url(url))
        dialog.save(self.get_root(), None, lambda d, r: self._on_dest_chosen(d, r, url))

    def _on_dest_chosen(self, dialog, result, url):
        try:
            f = dialog.save_finish(result)
        except GLib.Error:
            # Dismissed: leave the download dialog open for another try.
            return
        if f is None or not f.get_path():
            return
        dest = f.get_path()
        self.close()
        self._on_download(url, dest)

