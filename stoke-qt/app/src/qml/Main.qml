import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import QtQuick.Dialogs
import org.kde.kirigami as Kirigami

QQC2.ApplicationWindow {
    id: root
    width: 520
    height: 760
    minimumWidth: 480
    minimumHeight: 600
    visible: true
    title: "Stoke"

    Kirigami.Theme.inherit: false
    Kirigami.Theme.colorSet: Kirigami.Theme.Window
    color: Kirigami.Theme.backgroundColor

    readonly property int megabyte: 1024 * 1024
    // The bad-blocks pass-count combo. A plural form per count, built once
    // rather than inline in the model literal so it can be translated.
    readonly property var passChoices: [1, 2, 3, 4].map(n => i18np("%1 pass", "%1 passes", n))

    // ---- what the engine told us -------------------------------------------
    readonly property var image: backend.image
    readonly property bool hasImage: !!(image && image.name)
    readonly property bool windowsInstall: hasImage && !!image.is_windows
                                           && image.wininst && image.wininst.length > 0

    // The drive is remembered by path across refreshes, so a stick appearing
    // or vanishing next to the chosen one does not move the selection.
    property string devicePath: ""
    readonly property var device: {
        const list = backend.devices || []
        for (let i = 0; i < list.length; ++i) {
            if (list[i].device === root.devicePath) return list[i]
        }
        return null
    }

    // ---- the answers ---------------------------------------------------------
    // Boot selection combo, in Rufus's order. Index 0 is the image slot; its
    // text becomes the image name once one is chosen.
    readonly property var bootTypes: [
        { text: hasImage ? image.name : i18n("Disk or ISO image (Please select)"), value: "image" },
        { text: i18n("Non bootable"), value: "none" },
        { text: i18n("FreeDOS"), value: "freedos" },
        { text: i18n("UEFI:NTFS"), value: "uefi_ntfs" },
        { text: i18n("Syslinux (embedded)"), value: "syslinux" }
    ]
    property string bootType: "image"
    property string imageOption: ""     // iso | dd | install | wintogo
    property int wintogoIndex: 1
    property string scheme: "mbr"
    property string target: "dual"
    property string fs: "fat32"
    property int clusterSize: 0
    property string label: ""
    property bool quickFormat: true
    property bool extendedLabel: false
    property int badBlocks: 0
    property bool oldBiosFixes: false
    property bool rufusMbr: false
    property int persistenceMB: 0
    property bool persistenceGB: false
    property bool advancedDrive: false
    property bool advancedFormat: false
    property bool showLog: false

    readonly property bool ddMode: bootType === "image" && imageOption === "dd"
    readonly property bool wintogo: bootType === "image" && imageOption === "wintogo"
    readonly property bool busy: backend.running || backend.probing

    // ---- option lists -------------------------------------------------------
    readonly property var imageOptions: {
        if (bootType !== "image" || !hasImage) return []
        if (windowsInstall) {
            return [{ text: i18n("Standard Windows installation"), value: "install" },
                    { text: i18n("Windows To Go"), value: "wintogo" }]
        }
        const rec = image.recommended || {}
        let out = []
        if (rec.iso_mode_available) out.push({ text: i18n("ISO Image mode (Recommended)"), value: "iso" })
        if (rec.dd_mode_available) out.push({ text: i18n("DD Image mode"), value: "dd" })
        return out
    }
    readonly property bool imageUnsupported: bootType === "image" && hasImage && imageOptions.length === 0

    readonly property var schemes: [
        { text: "MBR", value: "mbr" },
        { text: "GPT", value: "gpt" }
    ]
    // GPT boots UEFI only; MBR can do anything. "BIOS or UEFI" therefore
    // exists only under MBR, which is the whole coupling rule.
    readonly property var targets: scheme === "gpt"
        ? [{ text: i18n("UEFI (non CSM)"), value: "uefi" }]
        : [{ text: i18n("BIOS (or UEFI-CSM)"), value: "bios" },
           { text: i18n("UEFI (non CSM)"), value: "uefi" },
           { text: i18n("BIOS or UEFI"), value: "dual" }]

    readonly property bool needsNtfs: (hasImage && bootType === "image" && !!image.needs_ntfs)
                                      || bootType === "uefi_ntfs"
    readonly property var fileSystems: {
        let out = []
        if (bootType === "freedos") {
            // FreeDOS is a FAT operating system; anything else would format a
            // drive it cannot read.
            return [{ text: "FAT32", value: "fat32" }, { text: "FAT16", value: "fat16" }]
        }
        if (!needsNtfs) out.push({ text: "FAT32", value: "fat32" })
        out.push({ text: "NTFS", value: "ntfs" })
        out.push({ text: "exFAT", value: "exfat" })
        out.push({ text: "ext4", value: "ext4" })
        if (advancedFormat) {
            out.push({ text: "ext3", value: "ext3" })
            out.push({ text: "ext2", value: "ext2" })
        }
        return out
    }

    readonly property var clusterChoices: {
        let out = [{ text: i18n("Default"), value: 0 }]
        const list = backend.clusterChoices || []
        for (let i = 0; i < list.length; ++i) {
            const c = list[i]
            out.push({ text: clusterText(c) + (c === backend.clusterDefault ? i18n(" (Default)") : ""), value: c })
        }
        return out
    }

    // Space left on the drive once the image is on it, with the same margin
    // Rufus keeps for the boot partitions and the file system's own tables.
    readonly property real persistenceMaxMB: {
        if (!device || !hasImage) return 0
        const spare = device.size - (image.projected_size || image.size || 0) - 512 * megabyte
        return Math.max(0, Math.floor(spare / megabyte))
    }
    readonly property bool persistenceAvailable: bootType === "image" && hasImage
                                                 && !!image.supports_persistence && !ddMode

    readonly property bool canStart: !!device && !busy && !backend.hashing
                                     && (bootType !== "image" || (hasImage && !imageUnsupported))

    // ---- helpers ------------------------------------------------------------
    function indexOf(list, value) {
        for (let i = 0; i < list.length; ++i) {
            if (list[i].value === value) return i
        }
        return -1
    }
    function has(list, value) { return indexOf(list, value) >= 0 }

    function clusterText(bytes) {
        if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)) + " MB"
        if (bytes >= 1024) return (bytes / 1024) + " KB"
        return bytes + " bytes"
    }

    // Asked only when the answer can differ. The device list is rebuilt on
    // every udev event, and each rebuild is a new device object even for
    // the same stick; re-asking on each one would also throw away a cluster
    // size the user had just picked.
    property string clusterKey: ""
    function refreshClusters() {
        const key = fs + ":" + (device ? device.size : 0)
        if (key === clusterKey) return
        clusterKey = key
        backend.clusters(fs, device ? device.size : 0)
    }

    // The engine's recommendation for the image, or Rufus's fixed defaults
    // for the other boot selections. Applied whenever the selection changes,
    // since a stick made for the previous image is the wrong stick.
    function applyDefaults() {
        persistenceMB = 0
        if (bootType === "image") {
            if (!hasImage) return
            const rec = image.recommended || {}
            if (windowsInstall) {
                imageOption = "install"
            } else if (rec.mode === "dd" && rec.dd_mode_available) {
                imageOption = "dd"
            } else if (rec.iso_mode_available) {
                imageOption = "iso"
            } else if (rec.dd_mode_available) {
                imageOption = "dd"
            } else {
                imageOption = ""
            }
            wintogoIndex = (image.win_editions && image.win_editions.length) ? image.win_editions[0].index : 1
            scheme = rec.scheme || "mbr"
            target = rec.target || (scheme === "gpt" ? "uefi" : "dual")
            fs = rec.fs || "fat32"
            label = rec.label || image.label || ""
            backend.note("Image: " + image.name + " (" + image.size_human + ", " + image.type
                         + (image.is_windows ? ", Windows " + (image.win_version ? image.win_version.major + " build " + image.win_version.build : "") : "")
                         + (image.is_hybrid ? ", hybrid" : "") + ")")
            backend.note("Defaults: " + (imageOption || "unsupported") + " / " + scheme.toUpperCase() + " / "
                         + target + " / " + fs.toUpperCase() + " / label '" + label + "'"
                         + (image.supports_persistence ? " / persistence available" : "")
                         + (image.needs_ntfs ? " / needs NTFS" : "")
                         + (image.win_editions && image.win_editions.length ? " / " + image.win_editions.length + " Windows editions" : ""))
        } else {
            imageOption = ""
            label = device && device.label ? device.label : ""
            switch (bootType) {
            case "none":      scheme = "mbr"; target = "dual"; fs = "fat32"; break
            case "freedos":   scheme = "mbr"; target = "bios"; fs = "fat32"; label = label || "FREEDOS"; break
            case "uefi_ntfs": scheme = "gpt"; target = "uefi"; fs = "ntfs"; break
            case "syslinux":  scheme = "mbr"; target = "bios"; fs = "fat32"; break
            }
        }
    }

    function selectImage(path) {
        if (!path) return
        bootType = "image"
        backend.probe(path)
    }

    function buildJob() {
        let job = {
            device: device.device,
            boot_type: bootType,
            scheme: scheme,
            target: target,
            fs: fs,
            cluster_size: clusterSize,
            label: label,
            quick_format: quickFormat,
            bad_blocks: badBlocks,
            extended_label: extendedLabel,
            old_bios_fixes: oldBiosFixes,
            rufus_mbr: rufusMbr,
            persistence_size: persistenceAvailable ? persistenceMB * megabyte : 0,
            windows_options: [],
            username: "",
            edition_index: 1,
            verify: false,
            zero_full: false
        }
        if (bootType === "image") {
            job.image = image.path
            job.mode = ddMode ? "dd" : "iso"
            job.wintogo = wintogo
            job.wintogo_index = wintogoIndex
        }
        return job
    }

    function startClicked() {
        backend.clearError()
        const job = buildJob()
        backend.note("Job: " + JSON.stringify(job))
        if (windowsInstall && !ddMode) {
            windowsDialog.open(job)
        } else {
            confirmDialog.open(job)
        }
    }

    // ---- reactions ----------------------------------------------------------
    onSchemeChanged: if (scheme === "gpt") target = "uefi"
    onTargetChanged: if (target === "dual") scheme = "mbr"
    onFileSystemsChanged: if (!has(fileSystems, fs)) fs = fileSystems[0].value
    onFsChanged: refreshClusters()
    onDeviceChanged: {
        refreshClusters()
        if (bootType !== "image" && device && !label) label = device.label || ""
    }
    onBootTypeChanged: applyDefaults()
    onImageOptionsChanged: if (imageOption && !has(imageOptions, imageOption)) imageOption = imageOptions.length ? imageOptions[0].value : ""

    Connections {
        target: backend
        function onImageChanged() {
            if (root.hasImage) root.applyDefaults()
        }
        function onDevicesChanged() {
            // Keep the choice if the drive is still there; otherwise the first
            // drive, which for one stick is the only sensible answer.
            const list = backend.devices || []
            if (root.device) return
            root.devicePath = list.length ? list[0].device : ""
        }
        function onClustersChanged() {
            if (!root.has(root.clusterChoices, root.clusterSize)) root.clusterSize = 0
        }
    }
    // Set when the user chose to close mid-write: the window leaves once the
    // engine has confirmed the cancellation, not before.
    property bool pendingClose: false

    Component.onCompleted: {
        backend.refreshDevices()
        if (typeof openFile !== "undefined" && openFile) selectImage(openFile)
    }

    // The log opens below the form the way Rufus's does: the window grows
    // rather than the form scrolling out of view.
    onShowLogChanged: root.height += showLog ? logHeight : -logHeight
    readonly property int logHeight: Kirigami.Units.gridUnit * 12 + Kirigami.Units.smallSpacing

    onClosing: function (close) {
        if (backend.running) {
            close.accepted = false
            closeDialog.open()
        }
    }

    // ---- dialogs --------------------------------------------------------------
    FileDialog {
        id: fileDialog
        title: i18n("Select an image")
        nameFilters: [
            i18n("Disk images (%1)", "*.iso *.img *.raw *.bin *.vhd *.wim *.esd *.gz *.xz *.bz2 *.zst"),
            i18n("All files (%1)", "*")
        ]
        onAccepted: root.selectImage(backend.localFile(selectedFile))
    }

    WindowsOptionsDialog {
        id: windowsDialog
        editions: root.hasImage && root.image.win_editions ? root.image.win_editions : []
        wintogo: root.wintogo
        onDone: function (job) { confirmDialog.open(job) }
    }

    ConfirmDialog {
        id: confirmDialog
        deviceText: root.device ? root.device.display : ""
        sizeText: root.device ? backend.humanSize(root.device.size) : ""
        onDone: function (job) { backend.start(job) }
    }

    ChecksumDialog {
        id: checksumDialog
    }

    DownloadDialog {
        id: downloadDialog
    }
    Connections {
        target: backend
        // A finished download becomes the boot selection, the same as opening
        // it from the file picker would.
        function onDownloadFinished(path) { root.selectImage(path) }
    }

    QQC2.Dialog {
        id: closeDialog
        parent: root.contentItem
        anchors.centerIn: parent
        modal: true
        title: i18n("Write in progress")
        standardButtons: QQC2.Dialog.Yes | QQC2.Dialog.No
        QQC2.Label {
            text: i18n("A drive is still being written. Cancel it and close?")
        }
        onAccepted: {
            root.pendingClose = true
            backend.cancel()
        }
    }
    Connections {
        target: backend
        function onRunningChanged() {
            if (!backend.running && root.pendingClose) Qt.quit()
        }
    }

    // ---- the window -------------------------------------------------------------
    QQC2.ScrollView {
        id: scroll
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing
        contentWidth: availableWidth
        QQC2.ScrollBar.horizontal.policy: QQC2.ScrollBar.AlwaysOff

        ColumnLayout {
            width: scroll.availableWidth
            spacing: Kirigami.Units.smallSpacing

            // ================= Drive Properties =================
            Kirigami.Heading { text: i18n("Drive Properties"); level: 4 }
            Kirigami.Separator { Layout.fillWidth: true }

            QQC2.Label { text: i18n("Device"); Layout.topMargin: Kirigami.Units.smallSpacing }
            RowLayout {
                Layout.fillWidth: true
                QQC2.ComboBox {
                    id: deviceCombo
                    Layout.fillWidth: true
                    enabled: !root.busy
                    model: backend.devices
                    textRole: "display"
                    displayText: count === 0 ? i18n("No device found") : currentText
                    currentIndex: {
                        const list = backend.devices || []
                        for (let i = 0; i < list.length; ++i) {
                            if (list[i].device === root.devicePath) return i
                        }
                        return -1
                    }
                    onActivated: function (index) { root.devicePath = backend.devices[index].device }
                }
                QQC2.ToolButton {
                    icon.name: "view-refresh"
                    enabled: !root.busy
                    onClicked: backend.refreshDevices()
                    QQC2.ToolTip.text: i18n("Refresh the device list")
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
            }

            QQC2.Label { text: i18n("Boot selection"); Layout.topMargin: Kirigami.Units.smallSpacing }
            RowLayout {
                Layout.fillWidth: true
                QQC2.ComboBox {
                    id: bootCombo
                    Layout.fillWidth: true
                    enabled: !root.busy
                    model: root.bootTypes
                    textRole: "text"
                    currentIndex: root.indexOf(root.bootTypes, root.bootType)
                    onActivated: function (index) { root.bootType = root.bootTypes[index].value }
                }
                QQC2.ToolButton {
                    text: "#"
                    enabled: root.hasImage && !root.busy
                    onClicked: checksumDialog.open(root.image.path, root.image.name)
                    QQC2.ToolTip.text: i18n("Compute image checksums")
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
                QQC2.Button {
                    text: i18nc("@action:button", "SELECT")
                    enabled: !root.busy
                    onClicked: fileDialog.open()
                }
                // Rufus's split "Select" button: the arrow drops a menu whose
                // one entry downloads an ISO from Microsoft instead.
                QQC2.ToolButton {
                    icon.name: "arrow-down"
                    enabled: !root.busy
                    onClicked: selectMenu.popup()
                    QQC2.ToolTip.text: i18n("Download a Windows or UEFI Shell ISO")
                    QQC2.ToolTip.visible: hovered
                    QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
                    QQC2.Menu {
                        id: selectMenu
                        QQC2.MenuItem {
                            text: i18nc("@action:inmenu", "Download")
                            onTriggered: downloadDialog.open()
                        }
                    }
                }
            }

            QQC2.Label {
                visible: root.imageUnsupported
                text: i18n("This image cannot be written: it is neither a bootable ISO nor a disk image.")
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            QQC2.Label {
                visible: imageOptionCombo.visible
                text: i18n("Image option")
                Layout.topMargin: Kirigami.Units.smallSpacing
            }
            QQC2.ComboBox {
                id: imageOptionCombo
                Layout.fillWidth: true
                visible: root.bootType === "image" && root.hasImage && root.imageOptions.length > 0
                enabled: !root.busy
                model: root.imageOptions
                textRole: "text"
                currentIndex: root.indexOf(root.imageOptions, root.imageOption)
                onActivated: function (index) { root.imageOption = root.imageOptions[index].value }
            }

            QQC2.Label {
                visible: editionCombo.visible
                text: i18n("Windows edition")
                Layout.topMargin: Kirigami.Units.smallSpacing
            }
            QQC2.ComboBox {
                id: editionCombo
                Layout.fillWidth: true
                visible: root.wintogo && root.hasImage && root.image.win_editions && root.image.win_editions.length > 0
                enabled: !root.busy
                model: root.hasImage && root.image.win_editions ? root.image.win_editions : []
                textRole: "name"
                currentIndex: {
                    const list = root.hasImage && root.image.win_editions ? root.image.win_editions : []
                    for (let i = 0; i < list.length; ++i) {
                        if (list[i].index === root.wintogoIndex) return i
                    }
                    return list.length ? 0 : -1
                }
                onActivated: function (index) { root.wintogoIndex = root.image.win_editions[index].index }
            }

            QQC2.Label {
                visible: persistenceRow.visible
                text: i18n("Persistent partition size")
                Layout.topMargin: Kirigami.Units.smallSpacing
            }
            RowLayout {
                id: persistenceRow
                Layout.fillWidth: true
                visible: root.persistenceAvailable
                enabled: !root.busy && root.persistenceMaxMB > 0
                QQC2.Slider {
                    Layout.fillWidth: true
                    from: 0
                    to: root.persistenceMaxMB
                    stepSize: 1
                    value: root.persistenceMB
                    onMoved: root.persistenceMB = Math.round(value)
                }
                QQC2.SpinBox {
                    id: persistenceSpin
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                    editable: true
                    from: 0
                    to: root.persistenceGB ? Math.floor(root.persistenceMaxMB / 1024) : root.persistenceMaxMB
                    value: root.persistenceGB ? Math.floor(root.persistenceMB / 1024) : root.persistenceMB
                    onValueModified: root.persistenceMB = root.persistenceGB ? value * 1024 : value
                }
                QQC2.ComboBox {
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                    model: ["MB", "GB"]
                    currentIndex: root.persistenceGB ? 1 : 0
                    onActivated: function (index) {
                        root.persistenceGB = index === 1
                        if (root.persistenceGB) root.persistenceMB = Math.floor(root.persistenceMB / 1024) * 1024
                    }
                }
            }
            QQC2.Label {
                visible: persistenceRow.visible
                text: root.persistenceMB > 0 ? i18n("%1 persistent partition", backend.humanSize(root.persistenceMB * root.megabyte))
                                             : i18n("0 (No persistence)")
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.largeSpacing
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    QQC2.Label { text: i18n("Partition scheme") }
                    QQC2.ComboBox {
                        Layout.fillWidth: true
                        enabled: !root.busy
                        model: root.schemes
                        textRole: "text"
                        currentIndex: root.indexOf(root.schemes, root.scheme)
                        onActivated: function (index) { root.scheme = root.schemes[index].value }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    QQC2.Label { text: i18n("Target system") }
                    QQC2.ComboBox {
                        Layout.fillWidth: true
                        enabled: !root.busy
                        model: root.targets
                        textRole: "text"
                        currentIndex: root.indexOf(root.targets, root.target)
                        onActivated: function (index) { root.target = root.targets[index].value }
                    }
                }
            }

            QQC2.ToolButton {
                Layout.topMargin: Kirigami.Units.smallSpacing
                flat: true
                icon.name: root.advancedDrive ? "arrow-up" : "arrow-down"
                text: root.advancedDrive ? i18n("Hide advanced drive properties") : i18n("Show advanced drive properties")
                onClicked: root.advancedDrive = !root.advancedDrive
            }
            ColumnLayout {
                visible: root.advancedDrive
                spacing: 0
                enabled: !root.busy
                QQC2.CheckBox {
                    text: i18n("List USB Hard Drives")
                    checked: backend.listUsbHdd
                    onToggled: backend.listUsbHdd = checked
                }
                QQC2.CheckBox {
                    text: i18n("Add fixes for old BIOSes (extra partition, align, etc.)")
                    enabled: root.scheme === "mbr" && root.target !== "uefi"
                    checked: root.oldBiosFixes
                    onToggled: root.oldBiosFixes = checked
                }
                QQC2.CheckBox {
                    text: i18n("Use masquerading MBR (BIOS ID 0x81)")
                    enabled: root.scheme === "mbr" && root.target !== "uefi"
                    checked: root.rufusMbr
                    onToggled: root.rufusMbr = checked
                }
            }

            // ================= Format Options =================
            Kirigami.Heading { text: i18n("Format Options"); level: 4; Layout.topMargin: Kirigami.Units.largeSpacing }
            Kirigami.Separator { Layout.fillWidth: true }

            QQC2.Label { text: i18n("Volume label"); Layout.topMargin: Kirigami.Units.smallSpacing }
            QQC2.TextField {
                Layout.fillWidth: true
                // DD writes the image's own partition table and file systems;
                // there is nothing here to label or format.
                enabled: !root.busy && !root.ddMode
                text: root.label
                onTextEdited: root.label = text
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                spacing: Kirigami.Units.largeSpacing
                enabled: !root.busy && !root.ddMode
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    QQC2.Label { text: i18n("File system") }
                    QQC2.ComboBox {
                        Layout.fillWidth: true
                        model: root.fileSystems
                        textRole: "text"
                        currentIndex: root.indexOf(root.fileSystems, root.fs)
                        onActivated: function (index) { root.fs = root.fileSystems[index].value }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing
                    QQC2.Label { text: i18n("Cluster size") }
                    QQC2.ComboBox {
                        Layout.fillWidth: true
                        model: root.clusterChoices
                        textRole: "text"
                        currentIndex: Math.max(0, root.indexOf(root.clusterChoices, root.clusterSize))
                        onActivated: function (index) { root.clusterSize = root.clusterChoices[index].value }
                    }
                }
            }

            QQC2.ToolButton {
                Layout.topMargin: Kirigami.Units.smallSpacing
                flat: true
                icon.name: root.advancedFormat ? "arrow-up" : "arrow-down"
                text: root.advancedFormat ? i18n("Hide advanced format options") : i18n("Show advanced format options")
                onClicked: root.advancedFormat = !root.advancedFormat
            }
            ColumnLayout {
                visible: root.advancedFormat
                spacing: 0
                enabled: !root.busy && !root.ddMode
                QQC2.CheckBox {
                    text: i18n("Quick format")
                    checked: root.quickFormat
                    onToggled: root.quickFormat = checked
                }
                QQC2.CheckBox {
                    text: i18n("Create extended label and icon files")
                    checked: root.extendedLabel
                    onToggled: root.extendedLabel = checked
                }
                RowLayout {
                    QQC2.CheckBox {
                        id: badBlocksCheck
                        text: i18n("Check device for bad blocks")
                        checked: root.badBlocks > 0
                        onToggled: root.badBlocks = checked ? Math.max(1, passesCombo.currentIndex + 1) : 0
                    }
                    QQC2.ComboBox {
                        id: passesCombo
                        enabled: badBlocksCheck.checked
                        model: root.passChoices
                        currentIndex: Math.max(0, root.badBlocks - 1)
                        onActivated: function (index) { root.badBlocks = index + 1 }
                    }
                }
            }

            // ================= Status =================
            Kirigami.Heading { text: i18n("Status"); level: 4; Layout.topMargin: Kirigami.Units.largeSpacing }
            Kirigami.Separator { Layout.fillWidth: true }

            QQC2.Label {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
                font.bold: true
                color: backend.error ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                text: {
                    if (backend.error) return backend.error
                    let s = backend.statusText
                    if (backend.running) {
                        if (backend.phaseProgress >= 0) s += " " + Math.round(backend.phaseProgress * 100) + "%"
                        if (backend.phaseMessage) s += " (" + backend.phaseMessage + ")"
                    }
                    return s
                }
            }
            QQC2.ProgressBar {
                Layout.fillWidth: true
                from: 0
                to: 1
                value: backend.overall
                indeterminate: backend.running && backend.overall <= 0 && backend.phaseProgress < 0
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                QQC2.Button {
                    text: i18nc("@action:button toggles the log pane", "Log")
                    checkable: true
                    checked: root.showLog
                    icon.name: "text-x-log"
                    onToggled: root.showLog = checked
                }
                QQC2.Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignRight
                    text: root.device ? backend.humanSize(root.device.size) : ""
                    opacity: 0.7
                }
                QQC2.Button {
                    text: backend.running ? i18nc("@action:button", "CANCEL") : i18nc("@action:button", "START")
                    enabled: backend.running || root.canStart
                    highlighted: !backend.running
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                    onClicked: backend.running ? backend.cancel() : root.startClicked()
                }
                QQC2.Button {
                    text: i18nc("@action:button", "CLOSE")
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                    onClicked: root.close()
                }
            }

            // ================= Log =================
            QQC2.ScrollView {
                id: logView
                visible: root.showLog
                Layout.fillWidth: true
                Layout.preferredHeight: Kirigami.Units.gridUnit * 12
                Layout.topMargin: Kirigami.Units.smallSpacing
                QQC2.TextArea {
                    id: logArea
                    readOnly: true
                    wrapMode: TextEdit.NoWrap
                    font.family: "monospace"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    text: backend.log
                    placeholderText: i18n("Nothing logged yet.")
                    // Follows the newest line, which is where the news is.
                    onTextChanged: cursorPosition = length
                    background: Rectangle {
                        color: Kirigami.Theme.alternateBackgroundColor
                        border.color: Kirigami.Theme.disabledTextColor
                        border.width: 1
                        radius: 3
                    }
                }
            }
        }
    }
}
