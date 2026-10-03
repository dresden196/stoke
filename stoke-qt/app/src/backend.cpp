#include "backend.h"

#include <KLocalizedString>

#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonValue>
#include <QLoggingCategory>
#include <QSettings>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>

#include <csignal>
#include <pwd.h>
#include <sys/prctl.h>
#include <unistd.h>

// Everything that reaches the log pane also goes here, off unless asked for
// (QT_LOGGING_RULES="stoke.ui.debug=true"), so a run can be watched
// from a terminal without a window.
Q_LOGGING_CATEGORY(lcUsb, "stoke.ui", QtWarningMsg)

namespace {
constexpr auto DefaultEngine = "/usr/bin/stoke";

// The engine can be pointed at a checkout while it is being worked on. The
// installed path is what the polkit policy names, so it is also what pkexec
// authorises without a generic "run this program as root" prompt.
QString enginePath()
{
    const QString override = qEnvironmentVariable("STOKE_ENGINE");
    return override.isEmpty() ? QString::fromLatin1(DefaultEngine) : override;
}

// The variables the engine reads to run from a source tree, plus the ones
// gettext uses to pick a language. A child started directly inherits them;
// pkexec strips the environment, so for the privileged engine they are
// carried across on the command line.
QStringList passThroughEnv()
{
    QStringList out;
    for (const char *name : {"STOKE_LIB", "STOKE_PAYLOAD", "STOKE_LOCALE_DIR", "STOKE_BACKEND",
                              "LANGUAGE", "LC_ALL", "LANG"}) {
        if (qEnvironmentVariableIsSet(name)) {
            out << QString::fromLatin1(name) + QLatin1Char('=') + qEnvironmentVariable(name);
        }
    }
    return out;
}

QString eventOf(const QJsonObject &msg)
{
    return msg.value(QLatin1String("event")).toString();
}
} // namespace

// ------------------------------------------------------------------ Engine

Engine::Engine(bool privileged, QObject *parent)
    : QObject(parent), m_privileged(privileged)
{
}

Engine::~Engine()
{
    shutdown();
}

void Engine::start()
{
    m_proc = new QProcess(this);

    QString program;
    QStringList args;
    // Already root (a development session run with sudo) means pkexec would
    // only add a prompt with nothing behind it.
    const bool viaPkexec = m_privileged && geteuid() != 0;
    if (!viaPkexec) {
        program = enginePath();
        args << QStringLiteral("serve");
    } else {
        program = QStringLiteral("pkexec");
        const QStringList env = passThroughEnv();
        if (!env.isEmpty()) {
            args << QStringLiteral("env") << env;
        }
        args << enginePath() << QStringLiteral("serve");
    }

    connect(m_proc, &QProcess::readyReadStandardOutput, this, &Engine::readStdout);
    connect(m_proc, &QProcess::readyReadStandardError, this, [this] {
        const QByteArray chunk = m_proc->readAllStandardError();
        // Kept for the error message when the engine fails before saying
        // hello; the whole thing goes to the log as it arrives.
        m_errTail += chunk;
        m_errTail = m_errTail.right(2000);
        Q_EMIT stderrText(QString::fromUtf8(chunk));
    });
    connect(m_proc, &QProcess::finished, this, &Engine::finished);
    connect(m_proc, &QProcess::errorOccurred, this, [this](QProcess::ProcessError e) {
        // FailedToStart never reaches finished(), so it is folded in here.
        if (e == QProcess::FailedToStart) {
            m_errTail = i18n("%1 could not be run", m_proc->program()).toUtf8();
            finished(-1, QProcess::CrashExit);
        }
    });
    m_proc->start(program, args);
}

int Engine::request(QJsonObject req, Handler handler)
{
    const int id = m_nextId++;
    req.insert(QLatin1String("id"), id);
    const QByteArray line = QJsonDocument(req).toJson(QJsonDocument::Compact) + '\n';

    if (m_dead) {
        // Answered from the event loop rather than inline, so a caller never
        // sees its own handler run before request() has returned.
        QTimer::singleShot(0, this, [handler] {
            handler(QJsonObject{{QLatin1String("error"), i18n("The engine is not running.")}});
        });
        return id;
    }
    m_handlers.insert(id, std::move(handler));
    if (!m_proc) {
        start();
    }
    if (m_ready) {
        m_proc->write(line);
    } else {
        m_queued << line;
    }
    return id;
}

void Engine::readStdout()
{
    m_buf += m_proc->readAllStandardOutput();
    int nl;
    while ((nl = m_buf.indexOf('\n')) >= 0) {
        const QByteArray line = m_buf.left(nl).trimmed();
        m_buf.remove(0, nl + 1);
        if (line.isEmpty()) {
            continue;
        }
        const QJsonDocument doc = QJsonDocument::fromJson(line);
        if (!doc.isObject()) {
            // Not protocol: something the engine or pkexec printed. Into the
            // log rather than dropped, since it is usually the interesting
            // part of a failure.
            Q_EMIT stderrText(QString::fromUtf8(line) + QLatin1Char('\n'));
            continue;
        }
        deliver(doc.object());
    }
}

void Engine::deliver(const QJsonObject &msg)
{
    if (eventOf(msg) == QLatin1String("hello")) {
        m_hello = msg;
        m_ready = true;
        Q_EMIT helloReceived(msg);
        for (const QByteArray &line : std::as_const(m_queued)) {
            m_proc->write(line);
        }
        m_queued.clear();
        return;
    }
    const QJsonValue idv = msg.value(QLatin1String("id"));
    if (!idv.isDouble()) {
        // A reply to nothing we sent ("bad json"). Worth seeing, never fatal.
        Q_EMIT stderrText(QString::fromUtf8(QJsonDocument(msg).toJson(QJsonDocument::Compact)) + QLatin1Char('\n'));
        return;
    }
    const auto it = m_handlers.find(idv.toInt());
    if (it == m_handlers.end()) {
        return;
    }
    // Copied out first: the handler may issue another request, or be the
    // last message for this id, and either way the hash must not be touched
    // through a live iterator.
    const Handler handler = it.value();
    const bool terminal = msg.contains(QLatin1String("result"))
        || msg.contains(QLatin1String("error"))
        || eventOf(msg) == QLatin1String("done");
    if (terminal) {
        m_handlers.erase(it);
    }
    handler(msg);
}

void Engine::finished(int code, QProcess::ExitStatus status)
{
    if (m_dead) {
        return;
    }
    m_dead = true;

    QString reason;
    const QString detail = QString::fromUtf8(m_errTail).trimmed();
    if (m_quitting) {
        // Left because we told it to.
    } else if (!m_ready && m_privileged && (code == 126 || code == 127)) {
        // pkexec's own exit codes: the prompt was dismissed or the password
        // refused. Not an engine crash, and the message must not say so.
        reason = i18n("Authentication was cancelled.");
    } else if (!m_ready) {
        reason = detail.isEmpty()
            ? i18n("The engine could not be started (exit code %1).", code)
            : i18n("The engine could not be started: %1", detail);
    } else if (status == QProcess::CrashExit) {
        reason = i18n("The engine crashed.");
    } else {
        reason = i18n("The engine stopped unexpectedly (exit code %1).", code);
    }
    // Owners hear first, so that a handler which reacts by starting the job
    // again is handed a fresh engine rather than this one.
    Q_EMIT stopped(reason);
    failAll(reason.isEmpty() ? i18n("The engine has quit.") : reason);
}

void Engine::failAll(const QString &reason)
{
    const QHash<int, Handler> pending = m_handlers;
    m_handlers.clear();
    m_queued.clear();
    for (const Handler &h : pending) {
        h(QJsonObject{{QLatin1String("error"), reason}});
    }
}

void Engine::shutdown()
{
    if (!m_proc || m_dead) {
        return;
    }
    m_quitting = true;
    if (m_ready) {
        m_proc->write("{\"id\":0,\"cmd\":\"quit\"}\n");
    }
    m_proc->closeWriteChannel();
    m_proc->waitForFinished(2000);
}

// ----------------------------------------------------------------- Backend

Backend::Backend(QObject *parent) : QObject(parent)
{
    watchDevices();
}

Backend::~Backend()
{
    // A write that is still going is cancelled rather than left running in a
    // process whose window has gone.
    if (m_running && m_root) {
        m_root->request(QJsonObject{{QLatin1String("cmd"), QLatin1String("cancel")}}, [](const QJsonObject &) {});
    }
    if (m_root) {
        m_root->shutdown();
    }
    if (m_user) {
        m_user->shutdown();
    }
    // Ours and unprivileged, so unlike the engines it can simply be killed.
    if (m_udev && m_udev->state() != QProcess::NotRunning) {
        m_udev->kill();
        m_udev->waitForFinished(500);
    }
}

Engine *Backend::makeEngine(bool privileged)
{
    auto *e = new Engine(privileged, this);
    connect(e, &Engine::stderrText, this, &Backend::appendLog);
    connect(e, &Engine::helloReceived, this, [this, privileged](const QJsonObject &hello) {
        // Both engines say the same thing; whichever speaks first fills the
        // fields the Windows dialog needs.
        if (m_engineVersion.isEmpty() || !privileged) {
            m_windowsOptions.clear();
            for (const QJsonValue &v : hello.value(QLatin1String("windows_options")).toArray()) {
                m_windowsOptions << v.toString();
            }
            m_windowsDefaults.clear();
            for (const QJsonValue &v : hello.value(QLatin1String("windows_defaults")).toArray()) {
                m_windowsDefaults << v.toString();
            }
            m_engineVersion = hello.value(QLatin1String("version")).toString();
            Q_EMIT helloChanged();
        }
        appendLog(i18n("%1 %2 engine started%3\n",
                       hello.value(QLatin1String("app")).toString(),
                       hello.value(QLatin1String("version")).toString(),
                       hello.value(QLatin1String("root")).toBool() ? i18n(" (root)") : QString()));
    });
    connect(e, &Engine::stopped, this, [this, e, privileged](const QString &reason) {
        // A job that was running reports the reason as its own failure; this
        // is for an engine that died while nothing was asked of it.
        if (!reason.isEmpty() && !m_running && !m_probing && !m_hashing) {
            appendLog(reason + QLatin1Char('\n'));
        }
        // Pointers are dropped so the next action starts a fresh process
        // rather than talking to a corpse. The object itself is left for the
        // event loop: this runs from inside its own signal.
        if (privileged && m_root == e) {
            m_root = nullptr;
        } else if (!privileged && m_user == e) {
            m_user = nullptr;
        }
        e->deleteLater();
    });
    return e;
}

Engine *Backend::userEngine()
{
    if (!m_user) {
        m_user = makeEngine(false);
    }
    return m_user;
}

Engine *Backend::rootEngine()
{
    // Already root: one process can do everything, and a second one would
    // ask for nothing and add nothing.
    if (geteuid() == 0) {
        return userEngine();
    }
    // An engine that can write without root (the udisks2 backend, in a
    // Flatpak or AppImage) says so in its hello; then there is nothing to
    // elevate and polkit asks through udisks2 when the drive is opened.
    if (userEngine()->hello().value(QLatin1String("can_write")).toBool()) {
        return userEngine();
    }
    if (!m_root) {
        m_root = makeEngine(true);
    }
    return m_root;
}

void Backend::watchDevices()
{
    // udev says when a stick is plugged or pulled; the list is refreshed a
    // moment later so the burst of partition events a single stick produces
    // becomes one refresh. Polling every couple of seconds is the fallback
    // for a system without udevadm on the path.
    m_udevDebounce = new QTimer(this);
    m_udevDebounce->setSingleShot(true);
    m_udevDebounce->setInterval(700);
    connect(m_udevDebounce, &QTimer::timeout, this, &Backend::refreshDevices);

    m_poll = new QTimer(this);
    m_poll->setInterval(2000);
    connect(m_poll, &QTimer::timeout, this, &Backend::refreshDevices);

    m_udev = new QProcess(this);
    // Dies with the window even when the window is killed rather than
    // closed; a monitor nobody reads would otherwise sit there until its
    // next event hit the closed pipe, which for an idle laptop is never.
    m_udev->setChildProcessModifier([] { prctl(PR_SET_PDEATHSIG, SIGTERM); });
    connect(m_udev, &QProcess::readyReadStandardOutput, this, [this] {
        m_udev->readAllStandardOutput();
        m_udevDebounce->start();
    });
    connect(m_udev, &QProcess::errorOccurred, this, [this] { m_poll->start(); });
    connect(m_udev, &QProcess::finished, this, [this] { m_poll->start(); });
    m_udev->start(QStringLiteral("udevadm"),
                  {QStringLiteral("monitor"), QStringLiteral("--udev"),
                   QStringLiteral("--subsystem-match=block")});
}

void Backend::setListUsbHdd(bool on)
{
    if (m_listUsbHdd == on) {
        return;
    }
    m_listUsbHdd = on;
    Q_EMIT listUsbHddChanged();
    refreshDevices();
}

void Backend::refreshDevices()
{
    // Not while writing: the job itself makes the disk appear and vanish,
    // and the combo the user is not allowed to touch would flicker with it.
    // One refresh is owed when the job ends.
    if (m_running) {
        m_devicesStale = true;
        return;
    }
    if (m_devicesPending) {
        m_devicesStale = true;
        return;
    }
    m_devicesPending = true;
    m_devicesStale = false;
    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("devices")},
                    {QLatin1String("usb_hdd"), m_listUsbHdd}},
        [this](const QJsonObject &msg) {
            m_devicesPending = false;
            if (msg.contains(QLatin1String("result"))) {
                m_devices = msg.value(QLatin1String("result")).toArray().toVariantList();
                Q_EMIT devicesChanged();
            } else if (msg.contains(QLatin1String("error"))) {
                fail(msg.value(QLatin1String("error")).toString());
            }
            if (m_devicesStale) {
                refreshDevices();
            }
        });
}

void Backend::probe(const QString &path)
{
    m_image.clear();
    m_hashes.clear();
    m_probing = true;
    m_error.clear();
    m_succeeded = false;
    Q_EMIT hashChanged();
    Q_EMIT imageChanged();
    setStatus(i18n("Scanning image…"));
    appendLog(i18n("Scanning image: %1\n", path));

    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("probe")},
                    {QLatin1String("path"), path}},
        [this](const QJsonObject &msg) {
            const QString ev = eventOf(msg);
            if (ev == QLatin1String("log")) {
                appendLog(msg.value(QLatin1String("text")).toString() + QLatin1Char('\n'));
                return;
            }
            if (msg.contains(QLatin1String("result"))) {
                m_image = msg.value(QLatin1String("result")).toObject().toVariantMap();
                m_probing = false;
                Q_EMIT imageChanged();
                setStatus(i18n("READY"));
                const QStringList warnings = m_image.value(QLatin1String("warnings")).toStringList();
                for (const QString &w : warnings) {
                    appendLog(i18n("Warning: %1\n", w));
                }
                return;
            }
            if (ev == QLatin1String("done") || msg.contains(QLatin1String("error"))) {
                m_probing = false;
                Q_EMIT imageChanged();
                fail(msg.value(QLatin1String("error")).toString(i18n("The image could not be read.")));
            }
        });
}

void Backend::clearImage()
{
    m_image.clear();
    m_hashes.clear();
    m_probing = false;
    Q_EMIT hashChanged();
    Q_EMIT imageChanged();
}

void Backend::hash(const QString &path)
{
    if (m_hashing) {
        return;
    }
    m_hashing = true;
    m_hashes.clear();
    m_hashProgress = 0;
    Q_EMIT hashChanged();

    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("hash")},
                    {QLatin1String("path"), path}},
        [this](const QJsonObject &msg) {
            const QString ev = eventOf(msg);
            if (ev == QLatin1String("progress")) {
                m_hashProgress = msg.value(QLatin1String("value")).toDouble(0);
                Q_EMIT hashChanged();
                return;
            }
            if (ev == QLatin1String("log")) {
                appendLog(msg.value(QLatin1String("text")).toString() + QLatin1Char('\n'));
                return;
            }
            if (msg.contains(QLatin1String("result"))) {
                m_hashes = msg.value(QLatin1String("result")).toObject().toVariantMap();
                m_hashProgress = 1;
                m_hashing = false;
                Q_EMIT hashChanged();
                return;
            }
            if (ev == QLatin1String("done") || msg.contains(QLatin1String("error"))) {
                m_hashing = false;
                Q_EMIT hashChanged();
                if (!msg.value(QLatin1String("cancelled")).toBool()) {
                    fail(msg.value(QLatin1String("error")).toString(i18n("Checksums could not be computed.")));
                }
            }
        });
}

void Backend::clusters(const QString &fs, double size)
{
    // Requests overtake each other when the user flicks through file
    // systems; only the answer to the latest question is kept.
    const int seq = ++m_clusterRequest;
    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("clusters")},
                    {QLatin1String("fs"), fs},
                    {QLatin1String("size"), static_cast<qint64>(size)}},
        [this, seq](const QJsonObject &msg) {
            if (seq != m_clusterRequest || !msg.contains(QLatin1String("result"))) {
                return;
            }
            const QJsonObject r = msg.value(QLatin1String("result")).toObject();
            m_clusterDefault = r.value(QLatin1String("default")).toInt();
            m_clusterChoices = r.value(QLatin1String("choices")).toArray().toVariantList();
            Q_EMIT clustersChanged();
        });
}

// --------------------------------------------------------- Windows download

void Backend::winVersions()
{
    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("win_versions")}},
        [this](const QJsonObject &msg) {
            if (msg.contains(QLatin1String("result"))) {
                m_winVersions = msg.value(QLatin1String("result")).toArray().toVariantList();
                Q_EMIT winVersionsChanged();
            } else if (msg.contains(QLatin1String("error"))) {
                m_winError = msg.value(QLatin1String("error")).toString();
                Q_EMIT winErrorChanged();
            }
        });
}

void Backend::winReleases(int version)
{
    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("win_releases")},
                    {QLatin1String("version"), version}},
        [this](const QJsonObject &msg) {
            if (msg.contains(QLatin1String("result"))) {
                m_winReleases = msg.value(QLatin1String("result")).toArray().toVariantList();
                Q_EMIT winReleasesChanged();
            } else if (msg.contains(QLatin1String("error"))) {
                m_winError = msg.value(QLatin1String("error")).toString();
                Q_EMIT winErrorChanged();
            }
        });
}

void Backend::winEditions(int version, int release)
{
    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("win_editions")},
                    {QLatin1String("version"), version},
                    {QLatin1String("release"), release},
                    {QLatin1String("locale"), QLatin1String("en-US")}},
        [this](const QJsonObject &msg) {
            if (msg.contains(QLatin1String("result"))) {
                m_winEditions = msg.value(QLatin1String("result")).toArray().toVariantList();
                Q_EMIT winEditionsChanged();
            } else if (msg.contains(QLatin1String("error"))) {
                m_winError = msg.value(QLatin1String("error")).toString();
                Q_EMIT winErrorChanged();
            }
        });
}

void Backend::winLanguages(int version, const QVariantList &editionIds)
{
    m_winBusy = true;
    m_winError.clear();
    Q_EMIT winBusyChanged();
    Q_EMIT winErrorChanged();

    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("win_languages")},
                    {QLatin1String("version"), version},
                    {QLatin1String("edition_ids"), QJsonArray::fromVariantList(editionIds)},
                    {QLatin1String("locale"), QLatin1String("en-US")}},
        [this](const QJsonObject &msg) {
            const QString ev = eventOf(msg);
            if (ev == QLatin1String("log")) {
                appendLog(msg.value(QLatin1String("text")).toString() + QLatin1Char('\n'));
                return;
            }
            if (msg.contains(QLatin1String("result"))) {
                const QJsonObject r = msg.value(QLatin1String("result")).toObject();
                m_winToken = r.value(QLatin1String("token")).toString();
                m_winLanguages = r.value(QLatin1String("languages")).toArray().toVariantList();
                m_winBusy = false;
                Q_EMIT winLanguagesChanged();
                Q_EMIT winBusyChanged();
                return;
            }
            if (ev == QLatin1String("done") || msg.contains(QLatin1String("error"))) {
                m_winBusy = false;
                Q_EMIT winBusyChanged();
                if (!msg.value(QLatin1String("cancelled")).toBool()) {
                    m_winError = msg.value(QLatin1String("error"))
                                     .toString(i18n("The languages could not be fetched."));
                    Q_EMIT winErrorChanged();
                }
            }
        });
}

void Backend::winLinks(int version, int release, const QVariantList &editionIds,
                       const QVariantList &languageData)
{
    m_winBusy = true;
    m_winError.clear();
    Q_EMIT winBusyChanged();
    Q_EMIT winErrorChanged();

    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("win_links")},
                    {QLatin1String("version"), version},
                    {QLatin1String("release"), release},
                    {QLatin1String("edition_ids"), QJsonArray::fromVariantList(editionIds)},
                    {QLatin1String("token"), m_winToken},
                    {QLatin1String("language_data"), QJsonArray::fromVariantList(languageData)}},
        [this](const QJsonObject &msg) {
            const QString ev = eventOf(msg);
            if (ev == QLatin1String("log")) {
                appendLog(msg.value(QLatin1String("text")).toString() + QLatin1Char('\n'));
                return;
            }
            if (msg.contains(QLatin1String("result"))) {
                m_winLinks = msg.value(QLatin1String("result")).toArray().toVariantList();
                m_winBusy = false;
                Q_EMIT winLinksChanged();
                Q_EMIT winBusyChanged();
                return;
            }
            if (ev == QLatin1String("done") || msg.contains(QLatin1String("error"))) {
                m_winBusy = false;
                Q_EMIT winBusyChanged();
                if (!msg.value(QLatin1String("cancelled")).toBool()) {
                    m_winError = msg.value(QLatin1String("error"))
                                     .toString(i18n("The download links could not be fetched."));
                    Q_EMIT winErrorChanged();
                }
            }
        });
}

QUrl Backend::suggestedSaveUrl(const QString &downloadUrl) const
{
    QString path = downloadUrl;
    const int q = path.indexOf(QLatin1Char('?'));
    if (q >= 0) {
        path = path.left(q);
    }
    QString name = path.section(QLatin1Char('/'), -1);
    name = QUrl::fromPercentEncoding(name.toUtf8());
    if (name.isEmpty()) {
        name = QStringLiteral("windows.iso");
    }
    QString dir = QStandardPaths::writableLocation(QStandardPaths::DownloadLocation);
    if (dir.isEmpty()) {
        dir = QDir::homePath();
    }
    return QUrl::fromLocalFile(QDir(dir).filePath(name));
}

void Backend::download(const QString &url, const QString &dest)
{
    if (m_running) {
        return;
    }
    m_running = true;
    m_downloading = true;
    m_error.clear();
    m_succeeded = false;
    m_overall = 0;
    m_phaseProgress = -1;
    m_phaseMessage.clear();
    Q_EMIT runningChanged();
    setStatus(i18n("Downloading…"));
    appendLog(QStringLiteral("\n") + i18n("Downloading %1\n", url));

    // The unprivileged engine: no drive is touched, so no pkexec prompt.
    userEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("win_download")},
                    {QLatin1String("url"), url},
                    {QLatin1String("dest"), dest}},
        [this](const QJsonObject &msg) {
            const QString ev = eventOf(msg);
            if (ev == QLatin1String("log")) {
                appendLog(msg.value(QLatin1String("text")).toString() + QLatin1Char('\n'));
            } else if (ev == QLatin1String("progress")) {
                const QJsonValue v = msg.value(QLatin1String("value"));
                m_phaseProgress = v.isDouble() ? v.toDouble() : -1;
                // The main progress bar reads `overall`; a download has no
                // separate overall event, so its one phase drives it directly.
                m_overall = m_phaseProgress >= 0 ? m_phaseProgress : 0;
                m_phaseMessage = msg.value(QLatin1String("message")).toString();
                Q_EMIT progressChanged();
            } else if (ev == QLatin1String("done")) {
                m_running = false;
                m_downloading = false;
                m_phaseProgress = -1;
                m_phaseMessage.clear();
                if (msg.value(QLatin1String("ok")).toBool()) {
                    m_overall = 1;
                    m_succeeded = true;
                    setStatus(i18n("READY"));
                    appendLog(i18n("Done.\n"));
                    const QString path = msg.value(QLatin1String("path")).toString();
                    Q_EMIT runningChanged();
                    Q_EMIT downloadFinished(path);
                    return;
                }
                if (msg.value(QLatin1String("cancelled")).toBool()) {
                    m_overall = 0;
                    fail(i18n("Cancelled."));
                } else {
                    fail(msg.value(QLatin1String("error")).toString(i18n("The download failed.")));
                }
                Q_EMIT runningChanged();
            } else if (msg.contains(QLatin1String("error"))) {
                m_running = false;
                m_downloading = false;
                m_overall = 0;
                fail(msg.value(QLatin1String("error")).toString());
                Q_EMIT runningChanged();
            }
        });
}

void Backend::start(const QVariantMap &job)
{
    if (m_running) {
        return;
    }
    m_running = true;
    m_error.clear();
    m_succeeded = false;
    m_overall = 0;
    m_phaseProgress = -1;
    m_phaseMessage.clear();
    Q_EMIT runningChanged();
    setStatus(i18n("Starting…"));
    appendLog(QStringLiteral("\n") + i18n("Writing %1\n", job.value(QLatin1String("device")).toString()));

    rootEngine()->request(
        QJsonObject{{QLatin1String("cmd"), QLatin1String("write")},
                    {QLatin1String("job"), QJsonObject::fromVariantMap(job)}},
        [this](const QJsonObject &msg) {
            const QString ev = eventOf(msg);
            if (ev == QLatin1String("log")) {
                appendLog(msg.value(QLatin1String("text")).toString() + QLatin1Char('\n'));
            } else if (ev == QLatin1String("status")) {
                setStatus(msg.value(QLatin1String("text")).toString());
            } else if (ev == QLatin1String("progress")) {
                const QJsonValue v = msg.value(QLatin1String("value"));
                m_phaseProgress = v.isDouble() ? v.toDouble() : -1;
                m_phaseMessage = msg.value(QLatin1String("message")).toString();
                Q_EMIT progressChanged();
            } else if (ev == QLatin1String("overall")) {
                m_overall = msg.value(QLatin1String("value")).toDouble();
                Q_EMIT progressChanged();
            } else if (ev == QLatin1String("done")) {
                m_running = false;
                m_phaseProgress = -1;
                m_phaseMessage.clear();
                if (msg.value(QLatin1String("ok")).toBool()) {
                    m_overall = 1;
                    m_succeeded = true;
                    setStatus(i18n("READY"));
                    appendLog(i18n("Done.\n"));
                } else if (msg.value(QLatin1String("cancelled")).toBool()) {
                    m_overall = 0;
                    fail(i18n("Cancelled."));
                } else {
                    fail(msg.value(QLatin1String("error")).toString(i18n("The write failed.")));
                    const QString trace = msg.value(QLatin1String("trace")).toString();
                    if (!trace.isEmpty()) {
                        appendLog(trace);
                    }
                }
                Q_EMIT runningChanged();
                if (m_devicesStale) {
                    refreshDevices();
                }
            } else if (msg.contains(QLatin1String("error"))) {
                // Refused outright, or the engine went away before it could
                // answer -- the authentication prompt being dismissed lands
                // here too.
                m_running = false;
                m_overall = 0;
                fail(msg.value(QLatin1String("error")).toString());
                Q_EMIT runningChanged();
                if (m_devicesStale) {
                    refreshDevices();
                }
            }
        });
}

void Backend::cancel()
{
    // Whichever engine holds the job: a write runs as root, a checksum as
    // the user. The engine answers with "cancelling" and then the job's own
    // `done`, so nothing needs to be tracked here.
    const QJsonObject req{{QLatin1String("cmd"), QLatin1String("cancel")}};
    if (m_downloading && m_user) {
        // A download always runs on the unprivileged engine, even if a write
        // from earlier in the session left a root engine standing.
        m_user->request(req, [](const QJsonObject &) {});
    } else if (m_running && m_root) {
        m_root->request(req, [](const QJsonObject &) {});
    } else if ((m_running || m_hashing) && m_user) {
        m_user->request(req, [](const QJsonObject &) {});
    }
    if (m_running) {
        setStatus(i18n("Cancelling…"));
    }
}

void Backend::note(const QString &text)
{
    appendLog(text + QLatin1Char('\n'));
}

void Backend::clearError()
{
    if (m_error.isEmpty()) {
        return;
    }
    m_error.clear();
    Q_EMIT progressChanged();
}

QString Backend::localFile(const QUrl &url) const
{
    return url.isLocalFile() ? url.toLocalFile() : url.toString();
}

QString Backend::humanSize(double bytes) const
{
    // Same rounding as the engine's size_human, so the number next to a
    // drive matches the number in its display string.
    static const char *units[] = {"B", "KB", "MB", "GB", "TB", "PB"};
    int u = 0;
    double v = bytes;
    while (v >= 1024 && u < 5) {
        v /= 1024;
        ++u;
    }
    if (u == 0) {
        return QString::number(static_cast<qint64>(v)) + QLatin1Char(' ') + QLatin1String(units[u]);
    }
    return QString::number(v, 'f', v < 10 ? 1 : 0) + QLatin1Char(' ') + QLatin1String(units[u]);
}

QString Backend::currentUser() const
{
    const QString name = qEnvironmentVariable("USER");
    if (!name.isEmpty()) {
        return name;
    }
    const passwd *pw = getpwuid(getuid());
    return pw ? QString::fromLocal8Bit(pw->pw_name) : QString();
}

QVariantMap Backend::savedWindowsChoices() const
{
    QSettings settings;
    QVariantMap out;
    out.insert(QLatin1String("saved"), settings.contains(QLatin1String("windows/options")));
    out.insert(QLatin1String("options"), settings.value(QLatin1String("windows/options")).toStringList());
    out.insert(QLatin1String("username"), settings.value(QLatin1String("windows/username")).toString());
    return out;
}

void Backend::rememberWindowsChoices(const QStringList &options, const QString &username)
{
    QSettings settings;
    settings.setValue(QLatin1String("windows/options"), options);
    settings.setValue(QLatin1String("windows/username"), username);
}

void Backend::appendLog(const QString &text)
{
    m_log += text;
    qCDebug(lcUsb).noquote() << text.trimmed();
    // Coalesced: a copy phase can log many lines a second, and rebuilding
    // the log view for each of them is work the progress bar would rather
    // have. Four updates a second is faster than anyone reads.
    if (!m_logFlush) {
        m_logFlush = new QTimer(this);
        m_logFlush->setSingleShot(true);
        m_logFlush->setInterval(250);
        connect(m_logFlush, &QTimer::timeout, this, [this] { Q_EMIT logChanged(); });
    }
    if (!m_logFlush->isActive()) {
        m_logFlush->start();
    }
}

void Backend::setStatus(const QString &text)
{
    m_status = text;
    Q_EMIT progressChanged();
}

void Backend::fail(const QString &text)
{
    m_error = text;
    m_status = text;
    appendLog(i18n("Error: %1\n", text));
    Q_EMIT progressChanged();
}
