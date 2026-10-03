import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

// Rufus's "Windows User Experience" dialog: what to patch into the
// installer before it runs. The option names are the engine's; only the
// labels live here.
QQC2.Dialog {
    id: dlg
    modal: true
    anchors.centerIn: parent
    width: Math.min(parent.width - Kirigami.Units.largeSpacing * 2, Kirigami.Units.gridUnit * 30)
    title: i18n("Windows User Experience")
    standardButtons: QQC2.Dialog.Ok | QQC2.Dialog.Cancel

    property var editions: []
    property bool wintogo: false
    property var job: ({})
    signal done(var job)

    readonly property var allOptions: [
        { key: "bypass_requirements", label: i18n("Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0") },
        { key: "no_online_account", label: i18n("Remove requirement for an online Microsoft account") },
        { key: "set_user", label: i18n("Create a local account with username:"), hasText: true },
        { key: "duplicate_locale", label: i18n("Set regional options to the same values as this user's") },
        { key: "no_data_collection", label: i18n("Disable data collection (Skip privacy questions)") },
        { key: "disable_bitlocker", label: i18n("Disable BitLocker automatic device encryption") },
        { key: "offline_internal_drives", label: i18n("Set internal drives offline") },
        { key: "qol_enhancements", label: i18n("Apply Windows quality-of-life defaults (no OneDrive/Outlook/Copilot/ads)") },
        { key: "silent_install", label: i18n("Silent unattended installation (wipes disk 0!)"), hasEditions: true },
        { key: "use_ms2023_bootloaders", label: i18n("Use 'Windows UEFI CA 2023' signed bootloaders") }
    ]
    // Only what this engine says it understands, so an option it would
    // reject is never offered.
    readonly property var options: {
        const known = backend.windowsOptions || []
        if (known.length === 0) return allOptions
        return allOptions.filter(o => known.indexOf(o.key) >= 0)
    }

    function open(newJob) {
        job = newJob
        const saved = backend.savedWindowsChoices()
        let on = saved.saved ? saved.options : backend.windowsDefaults
        // Taking internal drives offline is what keeps a Windows To Go stick
        // from mounting, or worse, using the host's own Windows.
        if (wintogo && on.indexOf("offline_internal_drives") < 0) on = on.concat(["offline_internal_drives"])
        for (let i = 0; i < rows.count; ++i) {
            const item = rows.itemAt(i)
            item.checked = on.indexOf(item.key) >= 0
        }
        usernameField.text = saved.username || backend.currentUser()
        editionCombo.currentIndex = editions.length ? 0 : -1
        visible = true
    }

    onAccepted: {
        let chosen = []
        for (let i = 0; i < rows.count; ++i) {
            const item = rows.itemAt(i)
            if (item.checked) chosen.push(item.key)
        }
        const username = usernameField.text.trim()
        if (!username) chosen = chosen.filter(k => k !== "set_user")
        backend.rememberWindowsChoices(chosen, username)

        let j = job
        j.windows_options = chosen
        j.username = username
        j.edition_index = editions.length && editionCombo.currentIndex >= 0
            ? editions[editionCombo.currentIndex].index : 1
        dlg.done(j)
    }

    contentItem: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: i18n("Customize the Windows installation:")
        }
        Repeater {
            id: rows
            model: dlg.options
            delegate: ColumnLayout {
                id: row
                required property var modelData
                readonly property string key: modelData.key
                property alias checked: box.checked
                Layout.fillWidth: true
                spacing: 0
                QQC2.CheckBox {
                    id: box
                    Layout.fillWidth: true
                    text: row.modelData.label
                }
                QQC2.TextField {
                    id: nameSlot
                    visible: !!row.modelData.hasText
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit * 1.5
                    enabled: box.checked
                    placeholderText: i18n("Username")
                    // Only one such row exists; the id is a hook for the
                    // dialog, which reads it through the alias below.
                    Component.onCompleted: if (visible) dlg.usernameField = nameSlot
                }
                QQC2.ComboBox {
                    id: editionSlot
                    visible: !!row.modelData.hasEditions && dlg.editions.length > 0
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit * 1.5
                    enabled: box.checked
                    model: dlg.editions
                    textRole: "name"
                    Component.onCompleted: if (row.modelData.hasEditions) dlg.editionCombo = editionSlot
                }
            }
        }
    }

    // The two controls inside the repeater that the dialog reads directly.
    // Placeholders keep open() and onAccepted safe if the engine happened to
    // list neither option.
    property var usernameField: QtObject { property string text: "" }
    property var editionCombo: QtObject { property int currentIndex: -1 }
}
