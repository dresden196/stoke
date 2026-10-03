import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// The last thing between the user and an erased drive.
QQC2.Dialog {
    id: dlg
    modal: true
    anchors.centerIn: parent
    width: Math.min(parent.width - Kirigami.Units.largeSpacing * 2, Kirigami.Units.gridUnit * 26)
    title: "Stoke"
    standardButtons: QQC2.Dialog.Ok | QQC2.Dialog.Cancel

    property string deviceText: ""
    property string sizeText: ""
    property var job: ({})
    signal done(var job)

    function open(newJob) {
        job = newJob
        visible = true
    }
    onAccepted: dlg.done(job)

    contentItem: ColumnLayout {
        spacing: Kirigami.Units.largeSpacing
        RowLayout {
            spacing: Kirigami.Units.largeSpacing
            Kirigami.Icon {
                source: "dialog-warning"
                Layout.preferredWidth: Kirigami.Units.iconSizes.large
                Layout.preferredHeight: Kirigami.Units.iconSizes.large
                Layout.alignment: Qt.AlignTop
            }
            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                font.bold: true
                text: i18n("WARNING: ALL DATA ON DEVICE '%1' WILL BE DESTROYED.", dlg.deviceText)
            }
        }
        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: dlg.sizeText ? i18n("Device: %1\nSize: %2", dlg.deviceText, dlg.sizeText)
                                : i18n("Device: %1", dlg.deviceText)
        }
        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: i18n("To continue with this operation, click OK. To quit click CANCEL.")
        }
    }
}
