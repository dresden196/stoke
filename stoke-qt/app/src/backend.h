#pragma once

#include <QByteArray>
#include <QHash>
#include <QJsonObject>
#include <QList>
#include <QObject>
#include <QProcess>
#include <QStringList>
#include <QVariantList>
#include <QVariantMap>

#include <functional>

class QTimer;

/**
 * One `stoke serve` process and the JSON-lines conversation with it.
 *
 * Requests carry an id; every reply and every streamed event echoes it, so a
 * handler is kept per id and fed each message until the terminal one (a
 * result, an error, or a `done` event). Requests sent before the engine's
 * `hello` line are queued, which is what lets the window ask for a write the
 * moment START is pressed while pkexec is still showing a password prompt.
 */
class Engine : public QObject
{
    Q_OBJECT
public:
    using Handler = std::function<void(const QJsonObject &message)>;

    explicit Engine(bool privileged, QObject *parent = nullptr);
    ~Engine() override;

    // False once the process has gone away for any reason. A dead engine is
    // replaced, not restarted, so the caller can keep its own pointer simple.
    bool alive() const { return m_proc && !m_dead; }
    bool ready() const { return m_ready; }
    QJsonObject hello() const { return m_hello; }

    int request(QJsonObject req, Handler handler);
    // Sends `quit`, closes stdin and waits a little. Closing stdin is the one
    // way to stop the privileged engine: it runs as root under pkexec, so a
    // signal from the user's window would simply be refused.
    void shutdown();

Q_SIGNALS:
    void helloReceived(const QJsonObject &hello);
    // Empty reason means it left because we asked.
    void stopped(const QString &reason);
    // Anything the engine printed outside the protocol: tracebacks, pkexec
    // complaints. Shown in the log so a failure is never silent.
    void stderrText(const QString &text);

private:
    void start();
    void readStdout();
    void deliver(const QJsonObject &msg);
    void finished(int code, QProcess::ExitStatus status);
    void failAll(const QString &reason);

    bool m_privileged;
    QProcess *m_proc = nullptr;
    QByteArray m_buf;
    QByteArray m_errTail;
    QJsonObject m_hello;
    QHash<int, Handler> m_handlers;
    QList<QByteArray> m_queued;
    int m_nextId = 1;
    bool m_ready = false;
    bool m_dead = false;
    bool m_quitting = false;
};

/**
 * Everything the window shows comes from stoke.
 *
 * Two engine processes: one as the user for looking at drives and images,
 * one under pkexec, started the first time a drive is actually written and
 * kept for the next one so the password is asked once per session.
 */
class Backend : public QObject
{
    Q_OBJECT
    Q_PROPERTY(QVariantList devices READ devices NOTIFY devicesChanged)
    Q_PROPERTY(bool listUsbHdd READ listUsbHdd WRITE setListUsbHdd NOTIFY listUsbHddChanged)
    Q_PROPERTY(QVariantMap image READ image NOTIFY imageChanged)
    Q_PROPERTY(bool probing READ probing NOTIFY imageChanged)
    Q_PROPERTY(bool running READ running NOTIFY runningChanged)
    Q_PROPERTY(bool hashing READ hashing NOTIFY hashChanged)
    Q_PROPERTY(QVariantMap hashes READ hashes NOTIFY hashChanged)
    Q_PROPERTY(double hashProgress READ hashProgress NOTIFY hashChanged)
    Q_PROPERTY(QString statusText READ statusText NOTIFY progressChanged)
    Q_PROPERTY(double overall READ overall NOTIFY progressChanged)
    // -1 when the current phase has no measurable progress.
    Q_PROPERTY(double phaseProgress READ phaseProgress NOTIFY progressChanged)
    Q_PROPERTY(QString phaseMessage READ phaseMessage NOTIFY progressChanged)
    Q_PROPERTY(QString log READ log NOTIFY logChanged)
    // The last failure, in the engine's own words. Cleared by the next action.
    Q_PROPERTY(QString error READ error NOTIFY progressChanged)
    Q_PROPERTY(bool succeeded READ succeeded NOTIFY progressChanged)
    Q_PROPERTY(QStringList windowsOptions READ windowsOptions NOTIFY helloChanged)
    Q_PROPERTY(QStringList windowsDefaults READ windowsDefaults NOTIFY helloChanged)
    Q_PROPERTY(QString engineVersion READ engineVersion NOTIFY helloChanged)
    Q_PROPERTY(QVariantList clusterChoices READ clusterChoices NOTIFY clustersChanged)
    Q_PROPERTY(int clusterDefault READ clusterDefault NOTIFY clustersChanged)
    // The Windows/UEFI-Shell download picker. Versions, releases and editions
    // are local; languages and links are the two network steps, so a busy flag
    // and a last-error string sit alongside them.
    Q_PROPERTY(QVariantList winVersionsList READ winVersionsList NOTIFY winVersionsChanged)
    Q_PROPERTY(QVariantList winReleasesList READ winReleasesList NOTIFY winReleasesChanged)
    Q_PROPERTY(QVariantList winEditionsList READ winEditionsList NOTIFY winEditionsChanged)
    Q_PROPERTY(QVariantList winLanguagesList READ winLanguagesList NOTIFY winLanguagesChanged)
    Q_PROPERTY(QVariantList winLinksList READ winLinksList NOTIFY winLinksChanged)
    Q_PROPERTY(bool winBusy READ winBusy NOTIFY winBusyChanged)
    Q_PROPERTY(QString winError READ winError NOTIFY winErrorChanged)

public:
    explicit Backend(QObject *parent = nullptr);
    ~Backend() override;

    QVariantList devices() const { return m_devices; }
    bool listUsbHdd() const { return m_listUsbHdd; }
    void setListUsbHdd(bool on);
    QVariantMap image() const { return m_image; }
    bool probing() const { return m_probing; }
    bool running() const { return m_running; }
    bool hashing() const { return m_hashing; }
    QVariantMap hashes() const { return m_hashes; }
    double hashProgress() const { return m_hashProgress; }
    QString statusText() const { return m_status; }
    double overall() const { return m_overall; }
    double phaseProgress() const { return m_phaseProgress; }
    QString phaseMessage() const { return m_phaseMessage; }
    QString log() const { return m_log; }
    QString error() const { return m_error; }
    bool succeeded() const { return m_succeeded; }
    QStringList windowsOptions() const { return m_windowsOptions; }
    QStringList windowsDefaults() const { return m_windowsDefaults; }
    QString engineVersion() const { return m_engineVersion; }
    QVariantList clusterChoices() const { return m_clusterChoices; }
    int clusterDefault() const { return m_clusterDefault; }
    QVariantList winVersionsList() const { return m_winVersions; }
    QVariantList winReleasesList() const { return m_winReleases; }
    QVariantList winEditionsList() const { return m_winEditions; }
    QVariantList winLanguagesList() const { return m_winLanguages; }
    QVariantList winLinksList() const { return m_winLinks; }
    bool winBusy() const { return m_winBusy; }
    QString winError() const { return m_winError; }

    Q_INVOKABLE void refreshDevices();
    Q_INVOKABLE void probe(const QString &path);
    Q_INVOKABLE void clearImage();
    Q_INVOKABLE void hash(const QString &path);
    Q_INVOKABLE void clusters(const QString &fs, double size);
    Q_INVOKABLE void start(const QVariantMap &job);
    Q_INVOKABLE void cancel();
    Q_INVOKABLE void clearError();
    // A line from the window for the log pane: the defaults it applied, the
    // job it is about to send. Rufus logs the same, and it is what makes a
    // report from a user readable.
    Q_INVOKABLE void note(const QString &text);

    // The download picker's engine-facing steps. versions/releases/editions
    // are local and instant; languages/links go to Microsoft and raise winBusy.
    Q_INVOKABLE void winVersions();
    Q_INVOKABLE void winReleases(int version);
    Q_INVOKABLE void winEditions(int version, int release);
    Q_INVOKABLE void winLanguages(int version, const QVariantList &editionIds);
    Q_INVOKABLE void winLinks(int version, int release, const QVariantList &editionIds,
                              const QVariantList &languageData);
    // Streams into the main window's progress area exactly as a write does, on
    // the unprivileged engine -- fetching an ISO needs no root.
    Q_INVOKABLE void download(const QString &url, const QString &dest);
    // The default save name Microsoft's link carries, under the Downloads dir.
    Q_INVOKABLE QUrl suggestedSaveUrl(const QString &downloadUrl) const;

    Q_INVOKABLE QString localFile(const QUrl &url) const;
    Q_INVOKABLE QString humanSize(double bytes) const;
    // The login name, which is what Rufus offers as the Windows account name.
    Q_INVOKABLE QString currentUser() const;

    // What the Windows dialog was last answered with. `saved` is false the
    // first time, when the engine's defaults apply instead.
    Q_INVOKABLE QVariantMap savedWindowsChoices() const;
    Q_INVOKABLE void rememberWindowsChoices(const QStringList &options,
                                            const QString &username);

Q_SIGNALS:
    void devicesChanged();
    void listUsbHddChanged();
    void imageChanged();
    void runningChanged();
    void hashChanged();
    void progressChanged();
    void logChanged();
    void helloChanged();
    void clustersChanged();
    void winVersionsChanged();
    void winReleasesChanged();
    void winEditionsChanged();
    void winLanguagesChanged();
    void winLinksChanged();
    void winBusyChanged();
    void winErrorChanged();
    // A finished download, so the window can adopt the ISO as its boot
    // selection the same way the file picker does.
    void downloadFinished(const QString &path);

private:
    Engine *userEngine();
    Engine *rootEngine();
    Engine *makeEngine(bool privileged);
    void appendLog(const QString &text);
    void setStatus(const QString &text);
    void fail(const QString &text);
    void watchDevices();

    Engine *m_user = nullptr;
    Engine *m_root = nullptr;
    QProcess *m_udev = nullptr;
    QTimer *m_udevDebounce = nullptr;
    QTimer *m_poll = nullptr;
    QTimer *m_logFlush = nullptr;

    QVariantList m_devices;
    bool m_listUsbHdd = false;
    bool m_devicesPending = false;
    bool m_devicesStale = false;
    QVariantMap m_image;
    bool m_probing = false;
    bool m_running = false;
    bool m_hashing = false;
    QVariantMap m_hashes;
    double m_hashProgress = 0;
    QString m_status;
    double m_overall = 0;
    double m_phaseProgress = -1;
    QString m_phaseMessage;
    QString m_log;
    QString m_error;
    bool m_succeeded = false;
    QStringList m_windowsOptions;
    QStringList m_windowsDefaults;
    QString m_engineVersion;
    QVariantList m_clusterChoices;
    int m_clusterDefault = 0;
    int m_clusterRequest = 0;

    QVariantList m_winVersions;
    QVariantList m_winReleases;
    QVariantList m_winEditions;
    QVariantList m_winLanguages;
    QVariantList m_winLinks;
    QString m_winToken;
    bool m_winBusy = false;
    QString m_winError;
    // Set while a win_download runs so cancel() knows to reach the user engine
    // even when a privileged engine from an earlier write is still around.
    bool m_downloading = false;
};
