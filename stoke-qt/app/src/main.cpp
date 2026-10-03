#include "backend.h"

#include <KLocalizedQmlContext>
#include <KLocalizedString>

#include <QFileInfo>
#include <QIcon>
#include <QApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>

int main(int argc, char *argv[])
{
    // QApplication rather than QGuiApplication: plasma-integration only
    // offers its KDE file dialog (the Dolphin-style one) to widget-capable
    // applications; a QGuiApplication gets Qt Quick's own fallback picker.
    QApplication app(argc, argv);
    // The application name doubles as the QSettings file the Windows dialog
    // remembers its choices in, so it is the package name rather than the
    // engine's.
    app.setApplicationName(QStringLiteral("stoke-qt"));
    app.setOrganizationName(QStringLiteral("Stoke"));
    app.setDesktopFileName(QStringLiteral("io.github.dresden196.stoke"));
    // The title bar and task manager want the icon from the window itself
    // when the desktop file is not where the shell expects it (AppImage).
    app.setWindowIcon(QIcon::fromTheme(QStringLiteral("stoke")));
    QQuickStyle::setStyle(QStringLiteral("org.kde.desktop"));
    KLocalizedString::setApplicationDomain(QByteArrayLiteral("stoke-qt"));

    // An image given on the command line -- an ISO opened with the writer
    // from a file manager. Handed to the window as the boot selection, and
    // nothing more: opening a file must never start writing a drive.
    QString openFile;
    const QStringList args = app.arguments();
    for (int i = 1; i < args.size(); ++i) {
        if (args.at(i).startsWith(QStringLiteral("-"))) {
            continue;
        }
        const QFileInfo info(args.at(i));
        if (info.isFile()) {
            openFile = info.absoluteFilePath();
        }
    }

    Backend backend;
    QQmlApplicationEngine engine;
    // Gives the QML files i18n()/i18nc()/i18np(), bound to the domain set
    // above -- otherwise every "Start" and "Cancel" in Main.qml would be a
    // ReferenceError instead of English.
    KLocalization::setupLocalizedContext(&engine);
    engine.rootContext()->setContextProperty(QStringLiteral("openFile"), openFile);
    engine.rootContext()->setContextProperty(QStringLiteral("backend"), &backend);
    engine.load(QUrl(QStringLiteral("qrc:/qml/Main.qml")));
    return engine.rootObjects().isEmpty() ? 1 : app.exec();
}
