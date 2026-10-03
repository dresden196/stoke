import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Rufus's "#" button: the image's checksums, computed by the engine while
// a progress bar shows the file being read.
QQC2.Dialog {
    id: dlg
    modal: true
    anchors.centerIn: parent
    width: Math.min(parent.width - Kirigami.Units.largeSpacing * 2, Kirigami.Units.gridUnit * 30)
    title: i18n("Checksums")
    standardButtons: QQC2.Dialog.Close

    property string imageName: ""
    readonly property var algorithms: [
        { key: "md5", label: "MD5" },
        { key: "sha1", label: "SHA1" },
        { key: "sha256", label: "SHA256" },
        { key: "sha512", label: "SHA512" }
    ]

    function open(path, name) {
        imageName = name
        backend.hash(path)
        visible = true
    }
    // Closing while the file is still being read stops the read; a checksum
    // nobody is waiting for is only disk traffic.
    onClosed: if (backend.hashing) backend.cancel()

    contentItem: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        QQC2.Label {
            Layout.fillWidth: true
            elide: Text.ElideMiddle
            text: dlg.imageName
            font.bold: true
        }
        QQC2.ProgressBar {
            Layout.fillWidth: true
            visible: backend.hashing
            from: 0
            to: 1
            value: backend.hashProgress
        }
        Repeater {
            model: dlg.algorithms
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                QQC2.Label {
                    text: modelData.label
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                }
                QQC2.TextField {
                    Layout.fillWidth: true
                    readOnly: true
                    font.family: "monospace"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    text: backend.hashes[modelData.key] || ""
                    placeholderText: backend.hashing ? i18n("Computing…") : ""
                }
            }
        }
    }
}
