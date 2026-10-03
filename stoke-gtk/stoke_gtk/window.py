"""The main window: Rufus's form, top to bottom, over the stoke engine.

Every decision about what will boot is the engine's (the `recommended`
block of a probe report). What lives here is the coupling between the
combos -- GPT boots UEFI only, "BIOS or UEFI" needs MBR, FreeDOS is FAT,
DD mode formats nothing -- and the job object handed over on START.
"""

import json

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

# libadwaita sets button and drop-down text in bold; the KDE window (and
# Rufus) use the regular weight, so the two look alike.
STOKE_CSS = b"""
button label, dropdown label, dropdown > button label, spinbutton text { font-weight: normal; }
"""


def _install_css():
    from gi.repository import Gdk
    provider = Gtk.CssProvider()
    provider.load_from_data(STOKE_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _fix_fractional_text(window):
    """On a display scaled by a non-integer factor (Plasma at 115 %, say)
    GTK renders hinted glyphs on a 2x grid and the compositor resamples
    them, which drops one-pixel stems (T, E, I lose their strokes).
    Unhinted text resamples cleanly, so hinting goes off in that case."""
    try:
        surface = window.get_surface()
        scale = surface.get_scale() if surface is not None else 1.0
    except Exception:
        return
    if abs(scale - round(scale)) < 0.01:
        return
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    try:
        settings.set_property("gtk-font-rendering", Gtk.FontRendering.MANUAL)
    except Exception:
        pass
    settings.set_property("gtk-xft-hinting", 0)
    settings.set_property("gtk-xft-hintstyle", "hintnone")
    settings.set_property("gtk-xft-antialias", 1)

from . import dialogs  # noqa: E402
from .engine import human_size  # noqa: E402
from .i18n import _, fmt, ngettext, pgettext  # noqa: E402

MEGABYTE = 1024 * 1024
LOG_HEIGHT = 220


class ChoiceDrop(Gtk.DropDown):
    """A drop-down over (text, value) pairs. Changing the choices or the
    value from code never reports back as a user change."""

    def __init__(self, on_change):
        super().__init__(hexpand=True)
        self._values = []
        self._texts = []
        self._updating = False
        self._on_change = on_change
        self.set_model(Gtk.StringList())
        self.connect("notify::selected", self._changed)

    def set_choices(self, choices, value):
        self._updating = True
        try:
            texts = [t for t, _v in choices]
            self._values = [v for _t, v in choices]
            # Only replace the model when the entries changed: rebuilding it
            # from inside the drop-down's own selection signal makes GTK abort.
            if texts != self._texts:
                self._texts = texts
                self.set_model(Gtk.StringList.new(texts))
            self._select(value)
        finally:
            self._updating = False

    def set_value(self, value):
        self._updating = True
        try:
            self._select(value)
        finally:
            self._updating = False

    def _select(self, value):
        if value in self._values:
            self.set_selected(self._values.index(value))
        elif self._values:
            self.set_selected(0)

    def get_value(self):
        selected = self.get_selected()
        if 0 <= selected < len(self._values):
            return self._values[selected]
        return None

    def _changed(self, *_args):
        if self._updating:
            return
        # Leave GTK's selection signal before the handler touches widgets
        # (the coupling rules may change this very drop-down's choices).
        value = self.get_value()
        GLib.idle_add(self._deliver, value)

    def _deliver(self, value):
        self._on_change(value)
        return GLib.SOURCE_REMOVE


def heading(text, first=False):
    """A section title with the thin rule under it, the way Rufus draws
    its groups."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=0 if first else 12)
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class("title-4")
    box.append(label)
    box.append(Gtk.Separator())
    return box


def field_label(text):
    return Gtk.Label(label=text, xalign=0, margin_top=6)


def field(text, widget):
    """A small label above its control, the two shown and hidden together."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.append(field_label(text))
    box.append(widget)
    return box


def two_columns(left, right):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
    box.append(left)
    box.append(right)
    return box


def check(text, on_toggled, *args):
    button = Gtk.CheckButton(label=text)
    button.connect("toggled", on_toggled, *args)
    return button


class StokeWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend):
        _install_css()
        super().__init__(application=app, title="Stoke", default_width=740, default_height=860)
        self.connect("realize", _fix_fractional_text)
        self.backend = backend
        self._updating = False

        # ---- the answers ------------------------------------------------
        self.device_path = ""
        self.boot_type = "image"
        self.image_option = ""      # iso | dd | install | wintogo
        self.wintogo_index = 1
        self.scheme = "mbr"
        self.target = "dual"
        self.fs = "fat32"
        self.cluster_size = 0
        self.label = ""
        self.quick_format = True
        self.extended_label = False
        self.bad_blocks = 0
        self.bad_blocks_passes = 1
        self.old_bios_fixes = False
        self.rufus_mbr = False
        self.persistence_mb = 0
        self.persistence_gb = False
        self.advanced_format = False
        self._cluster_key = None
        self._pending_close = False
        self._pulse = 0

        self._build()
        backend.connect("devices-changed", self._on_devices_changed)
        backend.connect("image-changed", self._on_image_changed)
        backend.connect("running-changed", self._on_running_changed)
        backend.connect("hash-changed", lambda *_a: self._sync())
        backend.connect("progress-changed", self._on_progress_changed)
        backend.connect("log-appended", self._on_log_appended)
        backend.connect("clusters-changed", self._on_clusters_changed)
        self.connect("close-request", self._on_close_request)
        self._sync()
        backend.refresh_devices()

    # ---- what the engine told us ----------------------------------------

    @property
    def image(self):
        return self.backend.image

    @property
    def has_image(self):
        return bool(self.image.get("name"))

    @property
    def windows_install(self):
        return self.has_image and bool(self.image.get("is_windows")) and bool(self.image.get("wininst"))

    @property
    def device(self):
        # The drive is remembered by path across refreshes, so a stick
        # appearing or vanishing next to the chosen one does not move the
        # selection.
        for d in self.backend.devices:
            if d.get("device") == self.device_path:
                return d
        return None

    @property
    def dd_mode(self):
        return self.boot_type == "image" and self.image_option == "dd"

    @property
    def wintogo(self):
        return self.boot_type == "image" and self.image_option == "wintogo"

    @property
    def busy(self):
        return self.backend.running or self.backend.probing

    @property
    def editions(self):
        return list(self.image.get("win_editions") or []) if self.has_image else []

    # ---- option lists ---------------------------------------------------

    def boot_types(self):
        # Rufus's order. Index 0 is the image slot; its text becomes the
        # image name once one is chosen.
        return [
            (self.image["name"] if self.has_image else _("Disk or ISO image (Please select)"), "image"),
            (_("Non bootable"), "none"),
            (_("FreeDOS"), "freedos"),
            (_("UEFI:NTFS"), "uefi_ntfs"),
            (_("Syslinux (embedded)"), "syslinux"),
        ]

    def image_options(self):
        if self.boot_type != "image" or not self.has_image:
            return []
        if self.windows_install:
            return [(_("Standard Windows installation"), "install"), (_("Windows To Go"), "wintogo")]
        rec = self.image.get("recommended") or {}
        out = []
        if rec.get("iso_mode_available"):
            out.append((_("ISO Image mode (Recommended)"), "iso"))
        if rec.get("dd_mode_available"):
            out.append((_("DD Image mode"), "dd"))
        return out

    @property
    def image_unsupported(self):
        return self.boot_type == "image" and self.has_image and not self.image_options()

    def targets(self):
        # GPT boots UEFI only; MBR can do anything. "BIOS or UEFI" therefore
        # exists only under MBR, which is the whole coupling rule.
        if self.scheme == "gpt":
            return [(_("UEFI (non CSM)"), "uefi")]
        return [(_("BIOS (or UEFI-CSM)"), "bios"), (_("UEFI (non CSM)"), "uefi"), (_("BIOS or UEFI"), "dual")]

    @property
    def needs_ntfs(self):
        return ((self.has_image and self.boot_type == "image" and bool(self.image.get("needs_ntfs")))
                or self.boot_type == "uefi_ntfs")

    def file_systems(self):
        if self.boot_type == "freedos":
            # FreeDOS is a FAT operating system; anything else would format
            # a drive it cannot read.
            return [("FAT32", "fat32"), ("FAT16", "fat16")]
        out = []
        if not self.needs_ntfs:
            out.append(("FAT32", "fat32"))
        out += [("NTFS", "ntfs"), ("exFAT", "exfat"), ("ext4", "ext4")]
        if self.advanced_format:
            out += [("ext3", "ext3"), ("ext2", "ext2")]
        return out

    def cluster_choices(self):
        out = [(_("Default"), 0)]
        for c in self.backend.cluster_choices:
            suffix = _(" (Default)") if c == self.backend.cluster_default else ""
            out.append((cluster_text(c) + suffix, c))
        return out

    @property
    def persistence_max_mb(self):
        # Space left on the drive once the image is on it, with the same
        # margin Rufus keeps for the boot partitions and the file system's
        # own tables.
        device = self.device
        if device is None or not self.has_image:
            return 0
        projected = self.image.get("projected_size") or self.image.get("size") or 0
        spare = int(device.get("size") or 0) - int(projected) - 512 * MEGABYTE
        return max(0, spare // MEGABYTE)

    @property
    def persistence_available(self):
        return (self.boot_type == "image" and self.has_image
                and bool(self.image.get("supports_persistence")) and not self.dd_mode)

    @property
    def can_start(self):
        return (self.device is not None and not self.busy and not self.backend.hashing
                and (self.boot_type != "image" or (self.has_image and not self.image_unsupported)))

    # ---- the form ---------------------------------------------------------

    def _build(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu_model = Gio.Menu()
        menu_model.append(_("About Stoke"), "app.about")
        menu_model.append(_("Quit"), "app.quit")
        menu = Gtk.MenuButton(icon_name="open-menu-symbolic", primary=True, menu_model=menu_model)
        header.pack_end(menu)
        view.add_top_bar(header)

        # Rufus's form: a label over each control, full width, top to
        # bottom, with the status and the buttons pinned under it.
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=12, margin_bottom=6, margin_start=12, margin_end=12)
        scroller.set_child(form)
        view.set_content(scroller)

        # ================= Drive Properties =================
        form.append(heading(_("Drive Properties"), first=True))

        form.append(field_label(_("Device")))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.device_drop = ChoiceDrop(self._on_device_selected)
        row.append(self.device_drop)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Refresh the device list"))
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_a: self.backend.refresh_devices())
        row.append(refresh)
        self.refresh_button = refresh
        form.append(row)

        form.append(field_label(_("Boot selection")))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.boot_drop = ChoiceDrop(self._on_boot_type_selected)
        row.append(self.boot_drop)
        self.hash_button = Gtk.Button(label="#", valign=Gtk.Align.CENTER, tooltip_text=_("Compute image checksums"))
        self.hash_button.add_css_class("flat")
        self.hash_button.connect("clicked", self._on_hash_clicked)
        row.append(self.hash_button)
        # Rufus's SELECT button carries a dropdown arrow offering Download; the
        # main button keeps its old job of opening the file chooser.
        boot_actions = Gio.SimpleActionGroup()
        select_action = Gio.SimpleAction.new("select", None)
        select_action.connect("activate", lambda *_a: self._on_select_clicked())
        boot_actions.add_action(select_action)
        download_action = Gio.SimpleAction.new("download", None)
        download_action.connect("activate", lambda *_a: self._on_download_clicked())
        boot_actions.add_action(download_action)
        self.insert_action_group("boot", boot_actions)
        boot_menu = Gio.Menu()
        boot_menu.append(pgettext("@action:button", "Select"), "boot.select")
        boot_menu.append(pgettext("@action:button opens the Download ISO dialog", "Download"), "boot.download")
        self.select_button = Adw.SplitButton(label=pgettext("@action:button", "SELECT"),
                                             valign=Gtk.Align.CENTER, menu_model=boot_menu)
        self.select_button.connect("clicked", self._on_select_clicked)
        row.append(self.select_button)
        form.append(row)

        self.unsupported_label = Gtk.Label(
            label=_("This image cannot be written: it is neither a bootable ISO nor a disk image."),
            xalign=0, wrap=True, visible=False, margin_top=6)
        self.unsupported_label.add_css_class("error")
        form.append(self.unsupported_label)

        self.image_option_drop = ChoiceDrop(self._on_image_option_selected)
        self.image_option_box = field(_("Image option"), self.image_option_drop)
        form.append(self.image_option_box)
        self.edition_drop = ChoiceDrop(self._on_edition_selected)
        self.edition_box = field(_("Windows edition"), self.edition_drop)
        form.append(self.edition_box)

        # Slider, spin box and unit, over the same number of megabytes.
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.persistence_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                           adjustment=Gtk.Adjustment(lower=0, upper=0, step_increment=1,
                                                                     page_increment=1024))
        self.persistence_scale.set_draw_value(False)
        self.persistence_scale.set_hexpand(True)
        self.persistence_scale.connect("value-changed", self._on_persistence_slid)
        row.append(self.persistence_scale)
        self.persistence_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(lower=0, upper=0, step_increment=1,
                                                                          page_increment=1024))
        self.persistence_spin.set_width_chars(6)
        self.persistence_spin.set_numeric(True)
        self.persistence_spin.connect("value-changed", self._on_persistence_spun)
        row.append(self.persistence_spin)
        self.persistence_unit = Gtk.DropDown.new_from_strings(["MB", "GB"])
        self.persistence_unit.connect("notify::selected", self._on_persistence_unit)
        row.append(self.persistence_unit)
        self.persistence_row = row
        self.persistence_caption = Gtk.Label(label=_("0 (No persistence)"), xalign=0)
        self.persistence_caption.add_css_class("caption")
        self.persistence_caption.add_css_class("dim-label")
        self.persistence_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.persistence_box.append(field_label(_("Persistent partition size")))
        self.persistence_box.append(row)
        self.persistence_box.append(self.persistence_caption)
        form.append(self.persistence_box)

        self.scheme_drop = ChoiceDrop(self._on_scheme_selected)
        self.target_drop = ChoiceDrop(self._on_target_selected)
        form.append(two_columns(field(_("Partition scheme"), self.scheme_drop),
                                field(_("Target system"), self.target_drop)))

        self.advanced_drive = Gtk.Expander(label=_("Show advanced drive properties"), margin_top=6)
        self.advanced_drive.connect("notify::expanded", self._on_advanced_drive_toggled)
        self.advanced_drive_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6)
        self.usb_hdd_check = check(_("List USB Hard Drives"), self._on_check, "usb_hdd")
        self.advanced_drive_box.append(self.usb_hdd_check)
        self.old_bios_check = check(_("Add fixes for old BIOSes (extra partition, align, etc.)"),
                                    self._on_check, "old_bios_fixes")
        self.advanced_drive_box.append(self.old_bios_check)
        self.rufus_mbr_check = check(_("Use masquerading MBR (BIOS ID 0x81)"), self._on_check, "rufus_mbr")
        self.advanced_drive_box.append(self.rufus_mbr_check)
        self.advanced_drive.set_child(self.advanced_drive_box)
        form.append(self.advanced_drive)

        # ================= Format Options =================
        form.append(heading(_("Format Options")))

        self.label_entry = Gtk.Entry(hexpand=True)
        self.label_entry.connect("notify::text", self._on_label_changed)
        form.append(field(_("Volume label"), self.label_entry))

        self.fs_drop = ChoiceDrop(self._on_fs_selected)
        self.cluster_drop = ChoiceDrop(self._on_cluster_selected)
        form.append(two_columns(field(_("File system"), self.fs_drop),
                                field(_("Cluster size"), self.cluster_drop)))

        self.advanced_format_expander = Gtk.Expander(label=_("Show advanced format options"), margin_top=6)
        self.advanced_format_expander.connect("notify::expanded", self._on_advanced_format_toggled)
        self.advanced_format_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6)
        self.quick_check = check(_("Quick format"), self._on_check, "quick_format")
        self.quick_check.set_active(True)
        self.advanced_format_box.append(self.quick_check)
        self.extended_check = check(_("Create extended label and icon files"), self._on_check, "extended_label")
        self.advanced_format_box.append(self.extended_check)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.bad_blocks_check = Gtk.CheckButton(label=_("Check device for bad blocks"))
        self.bad_blocks_check.connect("toggled", self._on_bad_blocks_toggled)
        row.append(self.bad_blocks_check)
        # A plural form per count, built once rather than inline so it can
        # be translated.
        self.passes_drop = Gtk.DropDown.new_from_strings(
            [fmt(ngettext("%1 pass", "%1 passes", n), n) for n in (1, 2, 3, 4)])
        self.passes_drop.set_valign(Gtk.Align.CENTER)
        self.passes_drop.connect("notify::selected", self._on_passes_selected)
        row.append(self.passes_drop)
        self.advanced_format_box.append(row)
        self.advanced_format_expander.set_child(self.advanced_format_box)
        form.append(self.advanced_format_expander)

        # ================= Status =================
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                         margin_top=0, margin_bottom=12, margin_start=12, margin_end=12)
        bottom.append(heading(_("Status")))
        self.status_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, xalign=0.5, margin_top=6)
        self.status_label.add_css_class("heading")
        bottom.append(self.status_label)
        self.progress = Gtk.ProgressBar()
        bottom.append(self.progress)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=6)
        self.log_button = Gtk.ToggleButton()
        self.log_button.set_child(Adw.ButtonContent(
            icon_name="utilities-terminal-symbolic",
            label=pgettext("@action:button toggles the log pane", "Log")))
        self.log_button.connect("toggled", self._on_log_toggled)
        actions.append(self.log_button)
        self.size_label = Gtk.Label(hexpand=True, xalign=1)
        self.size_label.add_css_class("dim-label")
        actions.append(self.size_label)
        self.start_button = Gtk.Button(label=pgettext("@action:button", "START"), width_request=100)
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._on_start_clicked)
        actions.append(self.start_button)
        close = Gtk.Button(label=pgettext("@action:button", "CLOSE"), width_request=100)
        close.connect("clicked", lambda *_a: self.close())
        actions.append(close)
        bottom.append(actions)

        # ================= Log =================
        self.log_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        log_scroller = Gtk.ScrolledWindow(height_request=LOG_HEIGHT, margin_top=6)
        log_scroller.add_css_class("card")
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     left_margin=6, right_margin=6, top_margin=6, bottom_margin=6)
        self.log_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.log_buffer = self.log_view.get_buffer()
        self._log_end = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self._placeholder_shown = True
        self.log_buffer.set_text(_("Nothing logged yet."))
        log_scroller.set_child(self.log_view)
        self.log_revealer.set_child(log_scroller)
        bottom.append(self.log_revealer)

        view.add_bottom_bar(bottom)
        self.set_content(view)

    # ---- pushing state into the widgets ---------------------------------

    def _sync(self):
        """Every widget follows the answers; the answers never follow a
        widget except through its handler."""
        self._updating = True
        try:
            backend = self.backend
            busy = self.busy
            devices = backend.devices
            if devices:
                self.device_drop.set_choices([(str(d.get("display") or d.get("device")), d.get("device"))
                                              for d in devices], self.device_path)
            else:
                self.device_drop.set_choices([(_("No device found"), None)], None)
            self.device_drop.set_sensitive(not busy and bool(devices))
            self.refresh_button.set_sensitive(not busy)

            self.boot_drop.set_choices(self.boot_types(), self.boot_type)
            self.boot_drop.set_sensitive(not busy)
            self.hash_button.set_sensitive(self.has_image and not busy)
            self.select_button.set_sensitive(not busy)
            self.unsupported_label.set_visible(self.image_unsupported)

            options = self.image_options()
            self.image_option_box.set_visible(self.boot_type == "image" and self.has_image and bool(options))
            self.image_option_drop.set_choices(options, self.image_option)
            self.image_option_drop.set_sensitive(not busy)

            editions = self.editions
            self.edition_box.set_visible(self.wintogo and bool(editions))
            self.edition_drop.set_choices([(str(e.get("name", "")), e.get("index")) for e in editions],
                                          self.wintogo_index)
            self.edition_drop.set_sensitive(not busy)

            available = self.persistence_available
            max_mb = self.persistence_max_mb
            mb = min(self.persistence_mb, max_mb)
            self.persistence_box.set_visible(available)
            self.persistence_row.set_sensitive(not busy and max_mb > 0)
            self.persistence_scale.set_range(0, max_mb)
            self.persistence_scale.set_value(mb)
            if self.persistence_gb:
                self.persistence_spin.set_range(0, max_mb // 1024)
                self.persistence_spin.set_increments(1, 1)
                self.persistence_spin.set_value(mb // 1024)
            else:
                self.persistence_spin.set_range(0, max_mb)
                self.persistence_spin.set_increments(1, 1024)
                self.persistence_spin.set_value(mb)
            self.persistence_unit.set_selected(1 if self.persistence_gb else 0)
            self.persistence_caption.set_text(
                fmt(_("%1 persistent partition"), human_size(self.persistence_mb * MEGABYTE))
                if self.persistence_mb > 0 else _("0 (No persistence)"))

            self.scheme_drop.set_choices([("MBR", "mbr"), ("GPT", "gpt")], self.scheme)
            self.scheme_drop.set_sensitive(not busy)
            self.target_drop.set_choices(self.targets(), self.target)
            self.target_drop.set_sensitive(not busy)

            self.advanced_drive_box.set_sensitive(not busy)
            self.usb_hdd_check.set_active(backend.list_usb_hdd)
            mbr_bios = self.scheme == "mbr" and self.target != "uefi"
            self.old_bios_check.set_active(self.old_bios_fixes)
            self.old_bios_check.set_sensitive(mbr_bios)
            self.rufus_mbr_check.set_active(self.rufus_mbr)
            self.rufus_mbr_check.set_sensitive(mbr_bios)

            # DD writes the image's own partition table and file systems;
            # there is nothing here to label or format.
            formatting = not busy and not self.dd_mode
            if self.label_entry.get_text() != self.label:
                self.label_entry.set_text(self.label)
            self.label_entry.set_sensitive(formatting)
            self.fs_drop.set_choices(self.file_systems(), self.fs)
            self.fs_drop.set_sensitive(formatting)
            self.cluster_drop.set_choices(self.cluster_choices(), self.cluster_size)
            self.cluster_drop.set_sensitive(formatting)
            self.advanced_format_box.set_sensitive(formatting)
            self.quick_check.set_active(self.quick_format)
            self.extended_check.set_active(self.extended_label)
            self.bad_blocks_check.set_active(self.bad_blocks > 0)
            self.passes_drop.set_selected(max(0, self.bad_blocks_passes - 1))
            self.passes_drop.set_sensitive(self.bad_blocks > 0)

            self.size_label.set_text(human_size(self.device.get("size", 0)) if self.device else "")
            self.start_button.set_label(pgettext("@action:button", "CANCEL") if backend.running
                                        else pgettext("@action:button", "START"))
            self.start_button.set_sensitive(backend.running or self.can_start)
            if backend.running:
                self.start_button.remove_css_class("suggested-action")
                self.start_button.add_css_class("destructive-action")
            else:
                self.start_button.remove_css_class("destructive-action")
                self.start_button.add_css_class("suggested-action")
            self._sync_status()
        finally:
            self._updating = False

    def _sync_status(self):
        backend = self.backend
        if backend.error:
            text = backend.error
        else:
            text = backend.status
            if backend.running:
                if backend.phase_progress >= 0:
                    text += " %d%%" % round(backend.phase_progress * 100)
                if backend.phase_message:
                    text += " (" + backend.phase_message + ")"
        self.status_label.set_text(text)
        if backend.error:
            self.status_label.add_css_class("error")
        else:
            self.status_label.remove_css_class("error")
        indeterminate = backend.running and backend.overall <= 0 and backend.phase_progress < 0
        if indeterminate:
            if not self._pulse:
                self._pulse = GLib.timeout_add(120, self._pulse_progress)
        else:
            if self._pulse:
                GLib.source_remove(self._pulse)
                self._pulse = 0
            self.progress.set_fraction(max(0.0, min(1.0, backend.overall)))

    def _pulse_progress(self):
        self.progress.pulse()
        return True

    # ---- rules --------------------------------------------------------------

    def _settle(self):
        """Re-applies the coupling rules after an answer changed, then
        redraws."""
        if not any(v == self.fs for _t, v in self.file_systems()):
            self.fs = self.file_systems()[0][1]
        options = self.image_options()
        if self.image_option and not any(v == self.image_option for _t, v in options):
            self.image_option = options[0][1] if options else ""
        if self.cluster_size and self.cluster_size not in self.backend.cluster_choices:
            self.cluster_size = 0
        self._refresh_clusters()
        self._sync()

    def _refresh_clusters(self):
        # Asked only when the answer can differ. The device list is rebuilt
        # on every refresh, and re-asking on each one would throw away a
        # cluster size the user had just picked.
        device = self.device
        key = (self.fs, device.get("size", 0) if device else 0)
        if key == self._cluster_key:
            return
        self._cluster_key = key
        self.backend.clusters(self.fs, key[1])

    def _apply_defaults(self):
        """The engine's recommendation for the image, or Rufus's fixed
        defaults for the other boot selections. Applied whenever the
        selection changes, since a stick made for the previous image is the
        wrong stick."""
        self.persistence_mb = 0
        backend = self.backend
        if self.boot_type == "image":
            if not self.has_image:
                return
            image = self.image
            rec = image.get("recommended") or {}
            if self.windows_install:
                self.image_option = "install"
            elif rec.get("mode") == "dd" and rec.get("dd_mode_available"):
                self.image_option = "dd"
            elif rec.get("iso_mode_available"):
                self.image_option = "iso"
            elif rec.get("dd_mode_available"):
                self.image_option = "dd"
            else:
                self.image_option = ""
            editions = self.editions
            self.wintogo_index = int(editions[0].get("index", 1)) if editions else 1
            self.scheme = rec.get("scheme") or "mbr"
            self.target = rec.get("target") or ("uefi" if self.scheme == "gpt" else "dual")
            self.fs = rec.get("fs") or "fat32"
            self.label = rec.get("label") or image.get("label") or ""
            win = image.get("win_version") or {}
            backend.note("Image: %s (%s, %s%s%s)" % (
                image.get("name"), image.get("size_human"), image.get("type"),
                (", Windows %s build %s" % (win.get("major", ""), win.get("build", ""))
                 if image.get("is_windows") else ""),
                ", hybrid" if image.get("is_hybrid") else ""))
            backend.note("Defaults: %s / %s / %s / %s / label '%s'%s%s%s" % (
                self.image_option or "unsupported", self.scheme.upper(), self.target, self.fs.upper(),
                self.label,
                " / persistence available" if image.get("supports_persistence") else "",
                " / needs NTFS" if image.get("needs_ntfs") else "",
                " / %d Windows editions" % len(editions) if editions else ""))
        else:
            self.image_option = ""
            device = self.device
            self.label = str(device.get("label") or "") if device else ""
            if self.boot_type == "none":
                self.scheme, self.target, self.fs = "mbr", "dual", "fat32"
            elif self.boot_type == "freedos":
                self.scheme, self.target, self.fs = "mbr", "bios", "fat32"
                self.label = self.label or "FREEDOS"
            elif self.boot_type == "uefi_ntfs":
                self.scheme, self.target, self.fs = "gpt", "uefi", "ntfs"
            elif self.boot_type == "syslinux":
                self.scheme, self.target, self.fs = "mbr", "bios", "fat32"

    def build_job(self):
        job = {
            "device": self.device.get("device"),
            "boot_type": self.boot_type,
            "scheme": self.scheme,
            "target": self.target,
            "fs": self.fs,
            "cluster_size": self.cluster_size,
            "label": self.label,
            "quick_format": self.quick_format,
            "bad_blocks": self.bad_blocks,
            "extended_label": self.extended_label,
            "old_bios_fixes": self.old_bios_fixes,
            "rufus_mbr": self.rufus_mbr,
            "persistence_size": self.persistence_mb * MEGABYTE if self.persistence_available else 0,
            "windows_options": [],
            "username": "",
            "edition_index": 1,
            "verify": False,
            "zero_full": False,
        }
        if self.boot_type == "image":
            job["image"] = self.image.get("path")
            job["mode"] = "dd" if self.dd_mode else "iso"
            job["wintogo"] = self.wintogo
            job["wintogo_index"] = self.wintogo_index
        return job

    # ---- user actions -------------------------------------------------------

    def select_image(self, path):
        if not path:
            return
        self.boot_type = "image"
        self.backend.probe(path)
        self._sync()

    def _on_select_clicked(self, *_args):
        dialog = Gtk.FileDialog(title=_("Select an image"))
        patterns = "*.iso *.img *.raw *.bin *.vhd *.wim *.esd *.gz *.xz *.bz2 *.zst"
        images = Gtk.FileFilter(name=fmt(_("Disk images (%1)"), patterns))
        for pattern in patterns.split():
            images.add_pattern(pattern)
            images.add_pattern(pattern.upper())
        everything = Gtk.FileFilter(name=fmt(_("All files (%1)"), "*"))
        everything.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(images)
        store.append(everything)
        dialog.set_filters(store)
        dialog.set_default_filter(images)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            f = dialog.open_finish(result)
        except GLib.Error:
            # Dismissed: nothing chosen, nothing to say.
            return
        if f is not None and f.get_path():
            self.select_image(f.get_path())

    def _on_download_clicked(self, *_args):
        if self.busy:
            return
        dialogs.DownloadDialog(self.backend, self._start_download).present(self)

    def _start_download(self, url, dest):
        # The download runs through the same (unprivileged) engine as a probe,
        # and shows its progress in the main window like a write. When it
        # finishes the file becomes the boot selection, exactly as if it had
        # been picked from disk.
        self.backend.clear_error()
        self.backend.download(url, dest, self.select_image)

    def _on_hash_clicked(self, *_args):
        if not self.has_image:
            return
        dialogs.ChecksumDialog(self.backend, self.image.get("path"), self.image.get("name")).present(self)

    def _on_start_clicked(self, *_args):
        if self.backend.running:
            self.backend.cancel()
            return
        if not self.can_start:
            return
        self.backend.clear_error()
        job = self.build_job()
        self.backend.note("Job: " + json.dumps(job))
        if self.windows_install and not self.dd_mode:
            dialogs.WindowsOptionsDialog(self.backend, job, self.editions, self.wintogo,
                                         self._confirm).present(self)
        else:
            self._confirm(job)

    def _confirm(self, job):
        device = self.device
        if device is None:
            return
        dialogs.confirm_write(self, str(device.get("display") or device.get("device")),
                              human_size(device.get("size", 0)), lambda: self.backend.start(job))

    def _on_close_request(self, *_args):
        if self.backend.running:
            def cancel_and_close():
                # The window leaves once the engine has confirmed the
                # cancellation, not before.
                self._pending_close = True
                self.backend.cancel()
            dialogs.confirm_close(self, cancel_and_close)
            return True
        return False

    # ---- widget handlers ----------------------------------------------------

    def _on_device_selected(self, value):
        if self._updating or value is None:
            return
        self.device_path = value
        self._on_device_changed()

    def _on_device_changed(self):
        if self.boot_type != "image" and self.device and not self.label:
            self.label = str(self.device.get("label") or "")
        self._settle()

    def _on_boot_type_selected(self, value):
        if self._updating or value is None:
            return
        self.boot_type = value
        self._apply_defaults()
        self._settle()

    def _on_image_option_selected(self, value):
        if self._updating or value is None:
            return
        self.image_option = value
        self._settle()

    def _on_edition_selected(self, value):
        if self._updating or value is None:
            return
        self.wintogo_index = int(value)

    def _on_persistence_slid(self, *_args):
        if self._updating:
            return
        self.persistence_mb = int(round(self.persistence_scale.get_value()))
        self._sync()

    def _on_persistence_spun(self, *_args):
        if self._updating:
            return
        value = int(self.persistence_spin.get_value())
        self.persistence_mb = value * 1024 if self.persistence_gb else value
        self._sync()

    def _on_persistence_unit(self, *_args):
        if self._updating:
            return
        self.persistence_gb = self.persistence_unit.get_selected() == 1
        if self.persistence_gb:
            self.persistence_mb = (self.persistence_mb // 1024) * 1024
        self._sync()

    def _on_scheme_selected(self, value):
        if self._updating or value is None:
            return
        self.scheme = value
        if self.scheme == "gpt":
            self.target = "uefi"
        self._settle()

    def _on_target_selected(self, value):
        if self._updating or value is None:
            return
        self.target = value
        if self.target == "dual":
            self.scheme = "mbr"
        self._settle()

    def _on_fs_selected(self, value):
        if self._updating or value is None:
            return
        self.fs = value
        self._settle()

    def _on_cluster_selected(self, value):
        if self._updating or value is None:
            return
        self.cluster_size = int(value)

    def _on_label_changed(self, *_args):
        if self._updating:
            return
        self.label = self.label_entry.get_text()

    def _on_check(self, button, name):
        if self._updating:
            return
        on = button.get_active()
        if name == "usb_hdd":
            self.backend.set_list_usb_hdd(on)
        else:
            setattr(self, name, on)

    def _on_bad_blocks_toggled(self, *_args):
        if self._updating:
            return
        self.bad_blocks = self.bad_blocks_passes if self.bad_blocks_check.get_active() else 0
        self._sync()

    def _on_passes_selected(self, *_args):
        if self._updating:
            return
        self.bad_blocks_passes = self.passes_drop.get_selected() + 1
        if self.bad_blocks:
            self.bad_blocks = self.bad_blocks_passes

    def _on_advanced_drive_toggled(self, expander, _pspec):
        expander.set_label(_("Hide advanced drive properties") if expander.get_expanded()
                           else _("Show advanced drive properties"))

    def _on_advanced_format_toggled(self, expander, _pspec):
        self.advanced_format = expander.get_expanded()
        expander.set_label(_("Hide advanced format options") if self.advanced_format
                           else _("Show advanced format options"))
        self._settle()

    def _on_log_toggled(self, button):
        show = button.get_active()
        self.log_revealer.set_reveal_child(show)
        # The log opens below the form the way Rufus's does: the window
        # grows rather than the form scrolling out of view.
        width, height = self.get_default_size()
        if self.get_mapped():
            width, height = self.get_width(), self.get_height()
        self.set_default_size(width, height + (LOG_HEIGHT if show else -LOG_HEIGHT))

    # ---- backend signals ----------------------------------------------------

    def _on_devices_changed(self, *_args):
        # Keep the choice if the drive is still there; otherwise the first
        # drive, which for one stick is the only sensible answer.
        if self.device is None:
            devices = self.backend.devices
            self.device_path = devices[0].get("device", "") if devices else ""
            self._on_device_changed()
        else:
            self._sync()

    def _on_image_changed(self, *_args):
        if self.has_image:
            self._apply_defaults()
        self._settle()

    def _on_clusters_changed(self, *_args):
        if self.cluster_size not in self.backend.cluster_choices:
            self.cluster_size = 0
        self._sync()

    def _on_running_changed(self, *_args):
        self._sync()
        if not self.backend.running and self._pending_close:
            self.destroy()

    def _on_progress_changed(self, *_args):
        self._sync_status()
        # Errors and READY also change what START may do.
        self.start_button.set_sensitive(self.backend.running or self.can_start)

    def _on_log_appended(self, _backend, chunk):
        if self._placeholder_shown:
            self.log_buffer.set_text("")
            self._placeholder_shown = False
        self.log_buffer.insert(self.log_buffer.get_end_iter(), chunk)
        # Follows the newest line, which is where the news is.
        self.log_view.scroll_to_mark(self._log_end, 0.0, False, 0.0, 1.0)


def cluster_text(size):
    if size >= MEGABYTE:
        return "%d MB" % (size // MEGABYTE)
    if size >= 1024:
        return "%d KB" % (size // 1024)
    return "%d bytes" % size

