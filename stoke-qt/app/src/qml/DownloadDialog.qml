import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import QtQuick.Dialogs
import org.kde.kirigami as Kirigami

// Rufus's "Download" button (a port of Fido): pick a Windows version, release,
// edition, language and architecture, then stream the ISO straight from
// Microsoft. Versions/releases/editions are instant and local; languages and
// links are the two network hops, shown with a busy spinner. The finished ISO
// becomes the window's boot selection.
QQC2.Dialog {
    id: dlg
    modal: true
    anchors.centerIn: parent
    width: Math.min(parent.width - Kirigami.Units.largeSpacing * 2, Kirigami.Units.gridUnit * 30)
    title: i18n("Download ISO")
    closePolicy: QQC2.Popup.CloseOnEscape

    // The current selection, by index into the backend's lists.
    property int versionIndex: 0
    property int releaseIndex: 0
    property int editionIndex: 0
    property int languageIndex: 0
    property int archIndex: 0

    // How far the staged flow has got. Languages and links are fetched on
    // demand, so a "Continue" button advances one stage at a time until the
    // links are in hand and it becomes "Download".
    property bool languagesLoaded: false
    property bool linksLoaded: false
    readonly property int stage: !languagesLoaded ? 0 : (!linksLoaded ? 1 : 2)

    readonly property var versions: backend.winVersionsList || []
    readonly property var releases: backend.winReleasesList || []
    readonly property var editions: backend.winEditionsList || []
    readonly property var languages: backend.winLanguagesList || []
    readonly property var links: backend.winLinksList || []
    readonly property bool haveEdition: editionIndex >= 0 && editionIndex < editions.length

    function open() {
        versionIndex = 0
        releaseIndex = 0
        editionIndex = 0
        languageIndex = 0
        archIndex = 0
        languagesLoaded = false
        linksLoaded = false
        // The version list drives releases, releases drive editions; asking for
        // versions kicks the whole cascade off (see the Connections below).
        backend.winVersions()
        visible = true
    }

    onClosed: if (backend.winBusy) backend.cancel()

    // Fetching versions fills the version combo; each subsequent list arriving
    // resets the one below it, the way changing a parent combo would.
    Connections {
        target: backend
        function onWinVersionsChanged() {
            dlg.versionIndex = 0
            if (dlg.versions.length > 0) backend.winReleases(0)
        }
        function onWinReleasesChanged() {
            dlg.releaseIndex = 0
            if (dlg.releases.length > 0) backend.winEditions(dlg.versionIndex, 0)
        }
        function onWinEditionsChanged() {
            dlg.editionIndex = 0
            dlg.languagesLoaded = false
            dlg.linksLoaded = false
        }
        function onWinLanguagesChanged() {
            dlg.languageIndex = 0
            dlg.languagesLoaded = true
            dlg.linksLoaded = false
        }
        function onWinLinksChanged() {
            dlg.archIndex = 0
            dlg.linksLoaded = true
        }
    }

    function primaryClicked() {
        if (dlg.stage === 0) {
            backend.winLanguages(dlg.versionIndex, dlg.editions[dlg.editionIndex].ids)
        } else if (dlg.stage === 1) {
            backend.winLinks(dlg.versionIndex, dlg.releaseIndex,
                             dlg.editions[dlg.editionIndex].ids,
                             dlg.languages[dlg.languageIndex].data)
        } else {
            const url = dlg.links[dlg.archIndex].url
            saveDialog.selectedFile = backend.suggestedSaveUrl(url)
            saveDialog.open()
        }
    }

    FileDialog {
        id: saveDialog
        title: i18n("Save the ISO")
        fileMode: FileDialog.SaveFile
        nameFilters: [i18n("Disk images (%1)", "*.iso"), i18n("All files (%1)", "*")]
        onAccepted: {
            const dest = backend.localFile(selectedFile)
            const url = dlg.links[dlg.archIndex].url
            dlg.close()
            // From here the main window's status area shows the progress and
            // START turns into CANCEL, exactly as for a write.
            backend.download(url, dest)
        }
    }

    contentItem: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing
        enabled: !backend.winBusy

        QQC2.Label { text: i18n("Version") }
        QQC2.ComboBox {
            id: versionCombo
            Layout.fillWidth: true
            model: dlg.versions
            textRole: "name"
            currentIndex: dlg.versionIndex
            onActivated: function (index) {
                dlg.versionIndex = index
                dlg.languagesLoaded = false
                dlg.linksLoaded = false
                backend.winReleases(index)
            }
        }

        QQC2.Label { text: i18n("Release"); Layout.topMargin: Kirigami.Units.smallSpacing }
        QQC2.ComboBox {
            id: releaseCombo
            Layout.fillWidth: true
            model: dlg.releases
            textRole: "label"
            currentIndex: dlg.releaseIndex
            onActivated: function (index) {
                dlg.releaseIndex = index
                dlg.languagesLoaded = false
                dlg.linksLoaded = false
                backend.winEditions(dlg.versionIndex, index)
            }
        }

        QQC2.Label { text: i18n("Edition"); Layout.topMargin: Kirigami.Units.smallSpacing }
        QQC2.ComboBox {
            id: editionCombo
            Layout.fillWidth: true
            model: dlg.editions
            textRole: "name"
            currentIndex: dlg.editionIndex
            onActivated: function (index) {
                dlg.editionIndex = index
                dlg.languagesLoaded = false
                dlg.linksLoaded = false
            }
        }

        QQC2.Label {
            text: i18n("Language")
            visible: dlg.languagesLoaded
            Layout.topMargin: Kirigami.Units.smallSpacing
        }
        QQC2.ComboBox {
            id: languageCombo
            Layout.fillWidth: true
            visible: dlg.languagesLoaded
            model: dlg.languages
            textRole: "display"
            currentIndex: dlg.languageIndex
            onActivated: function (index) {
                dlg.languageIndex = index
                // A new language means new links.
                dlg.linksLoaded = false
            }
        }

        QQC2.Label {
            text: i18n("Architecture")
            visible: dlg.linksLoaded
            Layout.topMargin: Kirigami.Units.smallSpacing
        }
        QQC2.ComboBox {
            id: archCombo
            Layout.fillWidth: true
            visible: dlg.linksLoaded
            model: dlg.links
            textRole: "arch"
            currentIndex: dlg.archIndex
            onActivated: function (index) { dlg.archIndex = index }
        }

        QQC2.Label {
            Layout.fillWidth: true
            visible: !!backend.winError
            wrapMode: Text.WordWrap
            color: Kirigami.Theme.negativeTextColor
            text: backend.winError
        }
    }

    // The busy spinner sits over the disabled form during the two network
    // steps. Placed in the footer row next to the buttons so it never covers
    // the combos.
    footer: RowLayout {
        spacing: Kirigami.Units.smallSpacing
        QQC2.BusyIndicator {
            running: backend.winBusy
            visible: backend.winBusy
            Layout.preferredHeight: Kirigami.Units.gridUnit * 1.5
            Layout.preferredWidth: Kirigami.Units.gridUnit * 1.5
            Layout.leftMargin: Kirigami.Units.smallSpacing
        }
        QQC2.Label {
            visible: backend.winBusy
            text: dlg.stage === 0 ? i18n("Fetching languages…") : i18n("Fetching download links…")
            opacity: 0.7
        }
        Item { Layout.fillWidth: true }
        QQC2.Button {
            text: i18nc("@action:button", "Cancel")
            onClicked: dlg.close()
            Layout.bottomMargin: Kirigami.Units.smallSpacing
        }
        QQC2.Button {
            text: dlg.stage < 2 ? i18nc("@action:button", "Continue")
                                : i18nc("@action:button", "Download")
            highlighted: true
            enabled: !backend.winBusy && dlg.haveEdition
            onClicked: dlg.primaryClicked()
            Layout.rightMargin: Kirigami.Units.smallSpacing
            Layout.bottomMargin: Kirigami.Units.smallSpacing
        }
    }
}
