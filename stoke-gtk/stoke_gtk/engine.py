"""The conversation with `stoke serve`, and everything the window shows.

Two engine processes: one as the user for looking at drives and images,
one under pkexec, started the first time a drive is actually written and
kept for the next one so the password is asked once per session. The
protocol is stoke's PROTOCOL.md: JSON, one object per line, every reply
and streamed event echoing the request's id.
"""

import json
import os
import pwd

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

from .i18n import _, fmt  # noqa: E402

DEFAULT_ENGINE = "/usr/bin/stoke"

# The variables the engine reads to run from a source tree, plus the ones
# gettext uses to pick a language. A child started directly inherits them;
# pkexec strips the environment, so for the privileged engine they are
# carried across on the command line.
PASS_THROUGH_ENV = ("STOKE_LIB", "STOKE_PAYLOAD", "STOKE_LOCALE_DIR", "STOKE_BACKEND",
                    "LANGUAGE", "LC_ALL", "LANG")


def engine_path():
    # The engine can be pointed at a checkout while it is being worked on.
    # The installed path is what the polkit policy names, so it is also what
    # pkexec authorises without a generic "run this program as root" prompt.
    return os.environ.get("STOKE_ENGINE") or DEFAULT_ENGINE


def pass_through_env():
    return [name + "=" + os.environ[name] for name in PASS_THROUGH_ENV if name in os.environ]


def human_size(size):
    """Same rounding as the engine's size_human, so the number next to a
    drive matches the number in its display string."""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return "%d %s" % (int(value), units[unit])
    return ("%.1f %s" if value < 10 else "%.0f %s") % (value, units[unit])


def current_user():
    """The login name, which is what Rufus offers as the Windows account name."""
    name = os.environ.get("USER")
    if name:
        return name
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return ""


class Engine(GObject.Object):
    """One `stoke serve` process and the JSON-lines conversation with it.

    A handler is kept per request id and fed each message until the terminal
    one (a result, an error, or a `done` event). Requests sent before the
    engine's `hello` line are queued, which is what lets the window ask for
    a write the moment START is pressed while pkexec is still showing a
    password prompt.
    """

    __gsignals__ = {
        "hello": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Empty reason means it left because we asked.
        "stopped": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Anything the engine printed outside the protocol: tracebacks,
        # pkexec complaints. Shown in the log so a failure is never silent.
        "stderr-text": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, privileged):
        super().__init__()
        self.privileged = privileged
        self.hello_message = {}
        self._proc = None
        self._stdin = None
        self._handlers = {}
        self._queued = []
        self._next_id = 1
        self._ready = False
        self._dead = False
        self._quitting = False
        self._err_tail = ""
        self._stdout_eof = False
        self._exit = None

    @property
    def alive(self):
        # A dead engine is replaced, not restarted, so the owner can keep its
        # own reference simple.
        return self._proc is not None and not self._dead

    @property
    def ready(self):
        return self._ready

    def start(self):
        # Already root (a development session run with sudo) means pkexec
        # would only add a prompt with nothing behind it.
        via_pkexec = self.privileged and os.geteuid() != 0
        if via_pkexec:
            argv = ["pkexec"]
            env = pass_through_env()
            if env:
                argv += ["env"] + env
            argv += [engine_path(), "serve"]
        else:
            argv = [engine_path(), "serve"]
        flags = (Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE
                 | Gio.SubprocessFlags.STDERR_PIPE)
        try:
            self._proc = Gio.Subprocess.new(argv, flags)
        except GLib.Error:
            # Reported from the main loop rather than inline, so a caller
            # never sees the failure before request() has returned.
            self._err_tail = fmt(_("%1 could not be run"), argv[0])
            self._proc = False
            self._stdout_eof = True
            GLib.idle_add(self._finished, -1, False)
            return
        self._stdin = self._proc.get_stdin_pipe()
        self._read(self._proc.get_stdout_pipe(), self._on_stdout_line, bytearray())
        self._read(self._proc.get_stderr_pipe(), self._on_stderr_line, bytearray())
        self._proc.wait_async(None, self._on_wait)

    def request(self, req, handler):
        rid = self._next_id
        self._next_id += 1
        req = dict(req, id=rid)
        line = json.dumps(req) + "\n"
        if self._dead:
            GLib.idle_add(lambda: handler({"error": _("The engine is not running.")}) and False)
            return rid
        self._handlers[rid] = handler
        if self._proc is None:
            self.start()
        if self._ready:
            self._send(line)
        else:
            self._queued.append(line)
        return rid

    def shutdown(self):
        """Sends `quit` and closes stdin. Closing stdin is the one way to stop
        the privileged engine: it runs as root under pkexec, so a signal from
        the user's window would simply be refused."""
        if not self._proc or self._dead:
            return
        self._quitting = True
        if self._ready:
            self._send('{"id":0,"cmd":"quit"}\n')
        try:
            self._stdin.close(None)
        except GLib.Error:
            pass

    # ---- process plumbing ----------------------------------------------

    def _send(self, line):
        try:
            self._stdin.write_all(line.encode("utf-8"), None)
        except GLib.Error:
            # The pipe is gone; the exit is on its way and will fail the
            # handlers with a proper reason.
            pass

    def _read(self, stream, on_line, buf):
        # Chunks rather than DataInputStream lines: a read of zero bytes is
        # the one unambiguous end-of-file, which the line reader's empty
        # string is not.
        stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, self._on_chunk, (stream, on_line, buf))

    def _on_chunk(self, stream, result, data):
        stream, on_line, buf = data
        try:
            chunk = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            chunk = b""
        if not chunk:
            if buf:
                on_line(bytes(buf).decode("utf-8", errors="replace"))
            on_line(None)
            return
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(buf[:nl + 1])
            del buf[:nl + 1]
            on_line(line.decode("utf-8", errors="replace"))
        self._read(stream, on_line, buf)

    def _on_stdout_line(self, line):
        if line is None:
            self._stdout_eof = True
            self._maybe_finish()
            return
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            # Not protocol: something the engine or pkexec printed. Into the
            # log rather than dropped, since it is usually the interesting
            # part of a failure.
            self.emit("stderr-text", line + "\n")
            return
        self._deliver(msg)

    def _on_stderr_line(self, line):
        if line is None:
            return
        # Kept for the error message when the engine fails before saying
        # hello; the whole thing goes to the log as it arrives.
        self._err_tail = (self._err_tail + line)[-2000:]
        self.emit("stderr-text", line)

    def _deliver(self, msg):
        if msg.get("event") == "hello":
            self.hello_message = msg
            self._ready = True
            self.emit("hello", msg)
            queued, self._queued = self._queued, []
            for line in queued:
                self._send(line)
            return
        rid = msg.get("id")
        if not isinstance(rid, int):
            # A reply to nothing we sent ("bad json"). Worth seeing, never fatal.
            self.emit("stderr-text", json.dumps(msg) + "\n")
            return
        handler = self._handlers.get(rid)
        if handler is None:
            return
        terminal = "result" in msg or "error" in msg or msg.get("event") == "done"
        if terminal:
            del self._handlers[rid]
        handler(msg)

    def _on_wait(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc.get_if_exited():
            self._exit = (proc.get_exit_status(), False)
        else:
            self._exit = (-1, True)
        # The last lines may still be sitting in the pipe: the exit is
        # reported once stdout has been read to its end. A second or so is
        # the fallback for a pipe held open by something the engine left
        # behind.
        self._maybe_finish()
        GLib.timeout_add(1500, self._force_finish)

    def _force_finish(self):
        self._stdout_eof = True
        self._maybe_finish()
        return False

    def _maybe_finish(self):
        if self._exit is None or not self._stdout_eof:
            return
        code, crashed = self._exit
        self._finished(code, crashed)

    def _finished(self, code, crashed):
        if self._dead:
            return False
        self._dead = True
        detail = self._err_tail.strip()
        reason = ""
        if self._quitting:
            pass  # Left because we told it to.
        elif not self._ready and self.privileged and code in (126, 127):
            # pkexec's own exit codes: the prompt was dismissed or the
            # password refused. Not an engine crash, and the message must not
            # say so.
            reason = _("Authentication was cancelled.")
        elif not self._ready:
            reason = (fmt(_("The engine could not be started: %1"), detail) if detail
                      else fmt(_("The engine could not be started (exit code %1)."), code))
        elif crashed:
            reason = _("The engine crashed.")
        else:
            reason = fmt(_("The engine stopped unexpectedly (exit code %1)."), code)
        # Owners hear first, so that a handler which reacts by starting the
        # job again is handed a fresh engine rather than this one.
        self.emit("stopped", reason)
        self._fail_all(reason or _("The engine has quit."))
        return False

    def _fail_all(self, reason):
        pending, self._handlers = self._handlers, {}
        self._queued = []
        for handler in pending.values():
            handler({"error": reason})


class Backend(GObject.Object):
    """Everything the window shows comes from stoke through here."""

    __gsignals__ = {
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "image-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "running-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "hash-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "progress-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # The text appended since the last flush.
        "log-appended": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "hello-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "clusters-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._user = None
        self._root = None
        self._query = None
        self.devices = []
        self.list_usb_hdd = False
        self._devices_pending = False
        self._devices_stale = False
        self.image = {}
        self.probing = False
        self.running = False
        # A download runs through the unprivileged engine and drives the same
        # progress plumbing as a write, so `running` is shared; this tells the
        # two apart for cancelling.
        self.download_active = False
        self.hashing = False
        self.hashes = {}
        self.hash_progress = 0.0
        self.status = ""
        self.overall = 0.0
        # -1 when the current phase has no measurable progress.
        self.phase_progress = -1.0
        self.phase_message = ""
        self.log = ""
        # The last failure, in the engine's own words. Cleared by the next action.
        self.error = ""
        self.succeeded = False
        self.windows_options = []
        self.windows_defaults = []
        self.engine_version = ""
        self.cluster_choices = []
        self.cluster_default = 0
        self._cluster_request = 0
        self._log_pending = ""
        self._log_flush = 0
        self._watch_devices()

    # ---- engines -------------------------------------------------------

    def _make_engine(self, privileged):
        engine = Engine(privileged)
        engine.connect("stderr-text", lambda _e, text: self._append_log(text))
        engine.connect("hello", self._on_hello, privileged)
        engine.connect("stopped", self._on_stopped, privileged)
        return engine

    def _on_hello(self, engine, hello, privileged):
        # Both engines say the same thing; whichever speaks first fills the
        # fields the Windows dialog needs.
        if not self.engine_version or not privileged:
            self.windows_options = list(hello.get("windows_options") or [])
            self.windows_defaults = list(hello.get("windows_defaults") or [])
            self.engine_version = str(hello.get("version") or "")
            self.emit("hello-changed")
        self._append_log(fmt(_("%1 %2 engine started%3\n"), hello.get("app", ""),
                             hello.get("version", ""), _(" (root)") if hello.get("root") else ""))

    def _on_stopped(self, engine, reason, privileged):
        # A job that was running reports the reason as its own failure; this
        # is for an engine that died while nothing was asked of it.
        if reason and not self.running and not self.probing and not self.hashing:
            self._append_log(reason + "\n")
        # References are dropped so the next action starts a fresh process
        # rather than talking to a corpse.
        if privileged and self._root is engine:
            self._root = None
        elif self._query is engine:
            self._query = None
        elif not privileged and self._user is engine:
            self._user = None

    def user_engine(self):
        if self._user is None:
            self._user = self._make_engine(False)
        return self._user

    def query_engine(self):
        # A second, unprivileged engine kept apart from `user_engine` for the
        # download dialog's version/release/edition/language/link lookups. The
        # window polls the first engine for drives every couple of seconds; a
        # languages or links fetch takes long enough on the network that one of
        # those polls would otherwise arrive in the middle of it, and the
        # engine tags a threaded reply with the id of the request it saw last,
        # not the one that asked. On an engine nothing else talks to, the id
        # stays the fetch's own and the reply comes back to the right handler.
        if self._query is None:
            self._query = self._make_engine(False)
        return self._query

    def root_engine(self):
        # Already root: one process can do everything, and a second one would
        # ask for nothing and add nothing.
        if os.geteuid() == 0:
            return self.user_engine()
        # An engine that can write without root (the udisks2 backend, in a
        # Flatpak or AppImage) says so in its hello; nothing to elevate then,
        # polkit asks through udisks2 when the drive is opened.
        if self.user_engine().hello_message.get("can_write"):
            return self.user_engine()
        if self._root is None:
            self._root = self._make_engine(True)
        return self._root

    def shutdown(self):
        # A write that is still going is cancelled rather than left running
        # in a process whose window has gone.
        if self.running and self._root is not None:
            self._root.request({"cmd": "cancel"}, lambda msg: None)
        if self._root is not None:
            self._root.shutdown()
        if self._user is not None:
            self._user.shutdown()
        if self._query is not None:
            self._query.shutdown()

    # ---- devices -------------------------------------------------------

    def _watch_devices(self):
        # The volume monitor says when a stick is plugged or pulled; the list
        # is refreshed a moment later so the burst of events a single stick
        # produces becomes one refresh. The two-second poll behind it covers
        # a session without a working monitor (no gvfs, a headless test).
        self._debounce = 0
        try:
            self._monitor = Gio.VolumeMonitor.get()
            for signal in ("drive-connected", "drive-disconnected", "volume-added",
                           "volume-removed", "mount-added", "mount-removed"):
                self._monitor.connect(signal, self._on_volume_event)
        except Exception:
            self._monitor = None
        GLib.timeout_add(2000, self._poll_devices)

    def _on_volume_event(self, *_args):
        if self._debounce:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(700, self._debounced_refresh)

    def _debounced_refresh(self):
        self._debounce = 0
        self.refresh_devices()
        return False

    def _poll_devices(self):
        self.refresh_devices()
        return True

    def set_list_usb_hdd(self, on):
        if self.list_usb_hdd == on:
            return
        self.list_usb_hdd = on
        self.refresh_devices()

    def refresh_devices(self):
        # Not while writing: the job itself makes the disk appear and vanish,
        # and the combo the user is not allowed to touch would flicker with
        # it. One refresh is owed when the job ends.
        if self.running or self._devices_pending:
            self._devices_stale = True
            return
        self._devices_pending = True
        self._devices_stale = False

        def on_reply(msg):
            self._devices_pending = False
            if "result" in msg:
                self.devices = list(msg["result"] or [])
                self.emit("devices-changed")
            elif "error" in msg:
                self._fail(str(msg["error"]))
            if self._devices_stale:
                self.refresh_devices()

        self.user_engine().request({"cmd": "devices", "usb_hdd": self.list_usb_hdd}, on_reply)

    # ---- images --------------------------------------------------------

    def probe(self, path):
        self.image = {}
        self.hashes = {}
        self.probing = True
        self.error = ""
        self.succeeded = False
        self.emit("hash-changed")
        self.emit("image-changed")
        self._set_status(_("Scanning image…"))
        self._append_log(fmt(_("Scanning image: %1\n"), path))

        def on_message(msg):
            event = msg.get("event")
            if event == "log":
                self._append_log(str(msg.get("text", "")) + "\n")
                return
            if "result" in msg:
                self.image = dict(msg["result"] or {})
                self.probing = False
                self.emit("image-changed")
                self._set_status(_("READY"))
                for warning in self.image.get("warnings") or []:
                    self._append_log(fmt(_("Warning: %1\n"), warning))
                return
            if event == "done" or "error" in msg:
                self.probing = False
                self.emit("image-changed")
                self._fail(str(msg.get("error") or _("The image could not be read.")))

        self.user_engine().request({"cmd": "probe", "path": path}, on_message)

    def clear_image(self):
        self.image = {}
        self.hashes = {}
        self.probing = False
        self.emit("hash-changed")
        self.emit("image-changed")

    def hash(self, path):
        if self.hashing:
            return
        self.hashing = True
        self.hashes = {}
        self.hash_progress = 0.0
        self.emit("hash-changed")

        def on_message(msg):
            event = msg.get("event")
            if event == "progress":
                value = msg.get("value")
                self.hash_progress = float(value) if isinstance(value, (int, float)) else 0.0
                self.emit("hash-changed")
                return
            if event == "log":
                self._append_log(str(msg.get("text", "")) + "\n")
                return
            if "result" in msg:
                self.hashes = dict(msg["result"] or {})
                self.hash_progress = 1.0
                self.hashing = False
                self.emit("hash-changed")
                return
            if event == "done" or "error" in msg:
                self.hashing = False
                self.emit("hash-changed")
                if not msg.get("cancelled"):
                    self._fail(str(msg.get("error") or _("Checksums could not be computed.")))

        self.user_engine().request({"cmd": "hash", "path": path}, on_message)

    def clusters(self, fs, size):
        # Requests overtake each other when the user flicks through file
        # systems; only the answer to the latest question is kept.
        self._cluster_request += 1
        seq = self._cluster_request

        def on_reply(msg):
            if seq != self._cluster_request or "result" not in msg:
                return
            result = msg["result"] or {}
            self.cluster_default = int(result.get("default") or 0)
            self.cluster_choices = [int(c) for c in result.get("choices") or []]
            self.emit("clusters-changed")

        self.user_engine().request({"cmd": "clusters", "fs": fs, "size": int(size)}, on_reply)

    # ---- writing -------------------------------------------------------

    def start(self, job):
        if self.running:
            return
        self.running = True
        self.error = ""
        self.succeeded = False
        self.overall = 0.0
        self.phase_progress = -1.0
        self.phase_message = ""
        self.emit("running-changed")
        self._set_status(_("Starting…"))
        self._append_log("\n" + fmt(_("Writing %1\n"), job.get("device", "")))

        def finished():
            self.running = False
            self.emit("running-changed")
            if self._devices_stale:
                self.refresh_devices()

        def on_message(msg):
            event = msg.get("event")
            if event == "log":
                self._append_log(str(msg.get("text", "")) + "\n")
            elif event == "status":
                self._set_status(str(msg.get("text", "")))
            elif event == "progress":
                value = msg.get("value")
                self.phase_progress = float(value) if isinstance(value, (int, float)) else -1.0
                self.phase_message = str(msg.get("message") or "")
                self.emit("progress-changed")
            elif event == "overall":
                value = msg.get("value")
                self.overall = float(value) if isinstance(value, (int, float)) else 0.0
                self.emit("progress-changed")
            elif event == "done":
                self.phase_progress = -1.0
                self.phase_message = ""
                if msg.get("ok"):
                    self.overall = 1.0
                    self.succeeded = True
                    self._set_status(_("READY"))
                    self._append_log(_("Done.\n"))
                elif msg.get("cancelled"):
                    self.overall = 0.0
                    self._fail(_("Cancelled."))
                else:
                    self._fail(str(msg.get("error") or _("The write failed.")))
                    trace = msg.get("trace")
                    if trace:
                        self._append_log(str(trace))
                finished()
            elif "error" in msg:
                # Refused outright, or the engine went away before it could
                # answer -- the authentication prompt being dismissed lands
                # here too.
                self.overall = 0.0
                self._fail(str(msg["error"]))
                finished()

        self.root_engine().request({"cmd": "write", "job": job}, on_message)

    def download(self, url, dest, on_done=None):
        """Fetch an ISO through the unprivileged engine. The progress is shown
        in the main window the same way a write's is (`running` shared), so
        START becomes CANCEL and the bar follows the `download` phase. On
        success `on_done` is handed the finished file's path."""
        if self.running:
            return
        self.running = True
        self.download_active = True
        self.error = ""
        self.succeeded = False
        self.overall = 0.0
        self.phase_progress = -1.0
        self.phase_message = ""
        self.emit("running-changed")
        self._set_status(_("Downloading…"))
        self._append_log("\n" + fmt(_("Downloading %1\n"), url))

        def finished():
            self.running = False
            self.download_active = False
            self.emit("running-changed")
            if self._devices_stale:
                self.refresh_devices()

        def on_message(msg):
            event = msg.get("event")
            if event == "log":
                self._append_log(str(msg.get("text", "")) + "\n")
            elif event == "progress":
                value = msg.get("value")
                if isinstance(value, (int, float)):
                    self.phase_progress = float(value)
                    # No separate `overall` event for a download; the phase
                    # value drives the bar directly.
                    self.overall = float(value)
                else:
                    self.phase_progress = -1.0
                    self.overall = 0.0
                self.phase_message = str(msg.get("message") or "")
                self.emit("progress-changed")
            elif event == "done":
                self.phase_progress = -1.0
                self.phase_message = ""
                if msg.get("ok"):
                    self.overall = 1.0
                    self.succeeded = True
                    self._set_status(_("READY"))
                    self._append_log(fmt(_("Downloaded to %1\n"), msg.get("path", "")))
                    path = msg.get("path")
                    finished()
                    if on_done and path:
                        on_done(str(path))
                    return
                if msg.get("cancelled"):
                    self.overall = 0.0
                    self._fail(_("Cancelled."))
                else:
                    self._fail(str(msg.get("error") or _("The download failed.")))
                finished()
            elif "error" in msg:
                self.overall = 0.0
                self._fail(str(msg["error"]))
                finished()

        self.user_engine().request({"cmd": "win_download", "url": url, "dest": dest}, on_message)

    def cancel(self):
        # Whichever engine holds the job: a write runs as root, a checksum or
        # a download as the user. The engine answers with "cancelling" and then
        # the job's own `done`, so nothing needs to be tracked here.
        req = {"cmd": "cancel"}
        if self.running and self.download_active and self._user is not None:
            self._user.request(req, lambda msg: None)
        elif self.running and self._root is not None:
            self._root.request(req, lambda msg: None)
        elif (self.running or self.hashing) and self._user is not None:
            self._user.request(req, lambda msg: None)
        if self.running:
            self._set_status(_("Cancelling…"))

    # ---- log and status ------------------------------------------------

    def note(self, text):
        """A line from the window for the log pane: the defaults it applied,
        the job it is about to send. Rufus logs the same, and it is what
        makes a report from a user readable."""
        self._append_log(text + "\n")

    def clear_error(self):
        if not self.error:
            return
        self.error = ""
        self.emit("progress-changed")

    def _append_log(self, text):
        self.log += text
        self._log_pending += text
        if os.environ.get("STOKE_GTK_DEBUG"):
            print(text, end="" if text.endswith("\n") else "\n", flush=True)
        # Coalesced: a copy phase can log many lines a second, and rebuilding
        # the log view for each of them is work the progress bar would rather
        # have. Four updates a second is faster than anyone reads.
        if not self._log_flush:
            self._log_flush = GLib.timeout_add(250, self._flush_log)

    def _flush_log(self):
        self._log_flush = 0
        chunk, self._log_pending = self._log_pending, ""
        if chunk:
            self.emit("log-appended", chunk)
        return False

    def _set_status(self, text):
        self.status = text
        self.emit("progress-changed")

    def _fail(self, text):
        self.error = text
        self.status = text
        self._append_log(fmt(_("Error: %1\n"), text))
        self.emit("progress-changed")


# ---- remembered choices ---------------------------------------------------

def _settings_path():
    return os.path.join(GLib.get_user_config_dir(), "stoke-gtk", "settings.json")


def load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        # Forgetting the answers is a nuisance, not a failure of the write.
        pass


def saved_windows_choices():
    """What the Windows dialog was last answered with. `saved` is False the
    first time, when the engine's defaults apply instead."""
    data = load_settings()
    options = data.get("windows_options")
    return {
        "saved": isinstance(options, list),
        "options": [str(o) for o in options] if isinstance(options, list) else [],
        "username": str(data.get("windows_username") or ""),
    }


def remember_windows_choices(options, username):
    data = load_settings()
    data["windows_options"] = list(options)
    data["windows_username"] = username
    save_settings(data)
