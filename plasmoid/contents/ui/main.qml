/*
    SPDX-FileCopyrightText: 2026 erastmorgan
    SPDX-License-Identifier: MIT

    VPN applet for the Plasma system tray. Pure UI: the state comes from the vpn-tray agent
    ($XDG_RUNTIME_DIR/vpn-tray/status.json, refreshed every second) and every action is a
    vpn-tray-cli call. Colours/fonts come from the Plasma theme, so light/dark follows the system.
*/
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    readonly property string cli: "\"$HOME/.local/bin/vpn-tray-cli\""
    readonly property string gui: "\"$HOME/.local/bin/vpn-tray\""

    property var snapshot: ({ vpns: [] })
    property bool agentAlive: false
    property bool loaded: false
    property var busy: ({})            // id -> "connecting" | "disconnecting" | "renaming"
    property string message: ""
    property bool messageIsError: false
    property string extIp: ""
    property bool confirmKillswitchOff: false
    property string expandedId: ""
    property int seq: 0

    readonly property var vpns: snapshot.vpns || []
    readonly property var activeVpn: {
        for (const v of vpns) if (v.state === "connected" && v.full_tunnel) return v
        for (const v of vpns) if (v.state === "connected") return v
        return null
    }
    readonly property bool anyBusy: Object.keys(busy).length > 0
        || vpns.some(v => v.state === "connecting" || v.state === "disconnecting")
    readonly property string iconState: !agentAlive && loaded ? "vpn-error"
        : anyBusy ? "vpn-busy" : activeVpn ? "vpn-on" : "vpn-off"
    readonly property color iconColor: iconState === "vpn-on" ? Kirigami.Theme.positiveTextColor
        : iconState === "vpn-busy" ? Kirigami.Theme.neutralTextColor
        : iconState === "vpn-error" ? Kirigami.Theme.negativeTextColor
        : Kirigami.Theme.textColor

    Plasmoid.icon: "network-vpn"
    Plasmoid.title: i18n("VPN")
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    toolTipMainText: i18n("VPN")
    toolTipSubText: {
        if (!agentAlive) return i18n("Агент vpn-tray не отвечает")
        let s = activeVpn ? activeVpn.name + " — " + i18n("подключён") + "\n↓ " + fmtRate(activeVpn.rx_rate)
                + "  ↑ " + fmtRate(activeVpn.tx_rate) : i18n("Не подключено")
        if (snapshot.killswitch) s += "\n" + i18n("Kill switch: интернет только через VPN")
        return s
    }

    // ---------------------------------------------------------------- helpers
    function q(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" }
    function fmtBytes(n) {
        const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
        let i = 0
        n = Number(n) || 0
        while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++ }
        return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i]
    }
    function fmtRate(n) { return fmtBytes(n) + "/с" }
    function fmtDuration(since, now) {
        if (!since) return "—"
        let s = Math.max(0, Math.floor((now || Date.now() / 1000) - since))
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60)
        s = s % 60
        const p = x => (x < 10 ? "0" : "") + x
        return h ? h + ":" + p(m) + ":" + p(s) : m + ":" + p(s)
    }
    function stateText(v) {
        const b = busy[v.id]
        if (b === "connecting" || v.state === "connecting") return i18n("Подключение…")
        if (b === "disconnecting" || v.state === "disconnecting") return i18n("Отключение…")
        if (v.state === "connected") return i18n("Подключён")
        if (v.state === "error") return i18n("Ошибка: %1", v.detail)
        return i18n("Отключён")
    }
    function server(v) {
        let s = v.server_ip || v.server_host || "—"
        if (v.server_port) s += ":" + v.server_port
        if (v.server_host && v.server_ip && v.server_host !== v.server_ip) s += "  (" + v.server_host + ")"
        return s
    }
    function setBusy(id, what) {
        const b = Object.assign({}, busy)
        if (what) b[id] = what; else delete b[id]
        busy = b
    }
    function say(text, isError) { message = text; messageIsError = !!isError }

    // ---------------------------------------------------------------- commands
    function run(cmd, cb) {
        const src = cmd + " #" + (++seq)
        executable.callbacks[src] = cb
        executable.connectSource(src)
    }
    function refresh() {
        const dir = "\"$XDG_RUNTIME_DIR/vpn-tray\""
        run("cat " + dir + "/status.json; [ -e " + dir + "/popup ] && rm -f " + dir + "/popup && printf '\\n@@POPUP'",
            (code, out) => {
            loaded = true
            if (out.indexOf("@@POPUP") >= 0) {      // `vpn-tray-cli popup`
                out = out.replace("@@POPUP", "")
                root.expanded = true
            }
            if (!out.trim()) { agentAlive = false; return }
            try {
                const data = JSON.parse(out)
                snapshot = data
                agentAlive = (Date.now() / 1000 - (data.ts || 0)) < 15
                vpnModel.sync(data.vpns || [])
            } catch (e) { agentAlive = false }
        })
    }
    function toggle(v) {
        if (busy[v.id]) return
        const up = v.state !== "connected"
        setBusy(v.id, up ? "connecting" : "disconnecting")
        say("")
        run(cli + " " + (up ? "up " : "down ") + q(v.id), (code, out, err) => {
            setBusy(v.id, "")
            if (code !== 0) say((err || out).trim() || i18n("Ошибка"), true)
            refresh()
        })
    }
    function rename(id, name) {
        if (!name.trim()) return
        run(cli + " rename " + q(id) + " " + q(name.trim()), (code, out, err) => {
            if (code !== 0) say((err || out).trim(), true)
            refresh()
        })
    }
    function setKillswitch(on) {
        confirmKillswitchOff = false
        run(cli + " killswitch " + (on ? "on" : "off"), (code, out, err) => {
            if (code !== 0) say((err || out).trim(), true)
            else say(on ? i18n("Kill switch включён") : i18n("Kill switch выключен: без VPN трафик идёт напрямую"))
            refresh()
        })
    }
    function checkIp() {
        extIp = i18n("проверяю…")
        run(cli + " extip", (code, out) => { extIp = code === 0 ? out.trim() : i18n("недоступен") })
    }
    function openDetails() { run(gui + " --window", () => {}) }

    P5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        property var callbacks: ({})
        onNewData: (sourceName, data) => {
            const cb = callbacks[sourceName]
            delete callbacks[sourceName]
            disconnectSource(sourceName)
            if (cb) cb(data["exit code"], data["stdout"] || "", data["stderr"] || "")
        }
    }

    Timer {
        interval: root.expanded ? 1000 : 2500
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    // ListModel updated in place, so delegates (and an open rename field) survive refreshes
    ListModel {
        id: vpnModel
        function norm(v) {
            return {
                vid: String(v.id), name: String(v.name || ""), vstate: String(v.state || ""),
                protocol: String(v.protocol || ""), server_host: String(v.server_host || ""),
                server_ip: String(v.server_ip || ""), server_port: Number(v.server_port || 0),
                iface: String(v.iface || ""), rx_rate: Number(v.rx_rate || 0), tx_rate: Number(v.tx_rate || 0),
                rx_bytes: Number(v.rx_bytes || 0), tx_bytes: Number(v.tx_bytes || 0),
                since: Number(v.since || 0), detail: String(v.detail || ""), full_tunnel: !!v.full_tunnel
            }
        }
        function sync(list) {
            const same = list.length === count && list.every((v, i) => get(i).vid === String(v.id))
            if (!same) {
                clear()
                for (const v of list) append(norm(v))
                return
            }
            for (let i = 0; i < list.length; i++) {
                const n = norm(list[i])
                for (const k in n) if (get(i)[k] !== n[k]) setProperty(i, k, n[k])
            }
        }
    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Kill switch (интернет только через VPN)")
            checkable: true
            checked: root.snapshot.killswitch === true
            onTriggered: checked => root.setKillswitch(checked)
        },
        PlasmaCore.Action {
            text: i18n("Подробнее…")
            icon.name: "view-list-details"
            onTriggered: root.openDetails()
        }
    ]

    // ---------------------------------------------------------------- panel icon
    compactRepresentation: MouseArea {
        id: compact
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton
        onClicked: root.expanded = !root.expanded
        Kirigami.Icon {
            anchors.fill: parent
            source: Qt.resolvedUrl("../images/" + root.iconState + ".svg")
            isMask: true
            color: root.iconColor
            active: compact.containsMouse
        }
    }

    // ---------------------------------------------------------------- popup
    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredWidth: Kirigami.Units.gridUnit * 26
        Layout.preferredHeight: Kirigami.Units.gridUnit * 24
        collapseMarginsHint: true

        header: PlasmaExtras.PlasmoidHeading {
            RowLayout {
                anchors.fill: parent
                PlasmaComponents3.Switch {
                    text: i18n("Kill switch")
                    checked: root.snapshot.killswitch === true
                    onToggled: {
                        if (checked) root.setKillswitch(true)
                        else { root.confirmKillswitchOff = true; checked = Qt.binding(() => root.snapshot.killswitch === true) }
                    }
                    PlasmaComponents3.ToolTip.text: i18n("Интернет только через VPN; без VPN связь обрывается")
                    PlasmaComponents3.ToolTip.visible: hovered
                    PlasmaComponents3.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
                Item { Layout.fillWidth: true }
                PlasmaComponents3.ToolButton {
                    icon.name: "view-list-details"
                    display: PlasmaComponents3.AbstractButton.IconOnly
                    text: i18n("Подробнее…")
                    onClicked: root.openDetails()
                    PlasmaComponents3.ToolTip.text: text
                    PlasmaComponents3.ToolTip.visible: hovered
                }
            }
        }

        contentItem: ColumnLayout {
            spacing: Kirigami.Units.smallSpacing

            PlasmaExtras.PlaceholderMessage {
                Layout.alignment: Qt.AlignCenter
                Layout.margins: Kirigami.Units.largeSpacing
                Layout.fillWidth: true
                visible: root.loaded && !root.agentAlive
                iconName: "dialog-warning"
                text: i18n("Агент vpn-tray не отвечает")
                explanation: i18n("systemctl --user restart vpn-tray-agent")
            }

            RowLayout {   // confirmation for turning the kill switch off
                visible: root.confirmKillswitchOff
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                Kirigami.Icon { source: "dialog-warning"; Layout.preferredWidth: Kirigami.Units.iconSizes.small; Layout.preferredHeight: Layout.preferredWidth }
                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: i18n("Выключить kill switch? Без VPN трафик пойдёт напрямую.")
                }
                PlasmaComponents3.Button { text: i18n("Выключить"); onClicked: root.setKillswitch(false) }
                PlasmaComponents3.Button { text: i18n("Отмена"); onClicked: root.confirmKillswitchOff = false }
            }

            PlasmaComponents3.ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: root.agentAlive || !root.loaded
                contentWidth: availableWidth - contentItem.leftMargin - contentItem.rightMargin

                contentItem: ListView {
                    id: list
                    model: vpnModel
                    spacing: Kirigami.Units.smallSpacing
                    topMargin: Kirigami.Units.smallSpacing
                    bottomMargin: Kirigami.Units.smallSpacing
                    leftMargin: Kirigami.Units.smallSpacing
                    rightMargin: Kirigami.Units.smallSpacing
                    boundsBehavior: Flickable.StopAtBounds
                    delegate: VpnItem {
                        width: ListView.view.width - ListView.view.leftMargin - ListView.view.rightMargin
                    }
                }
            }

            PlasmaComponents3.Label {
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing
                visible: root.message !== ""
                text: root.message
                wrapMode: Text.WordWrap
                color: root.messageIsError ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.smallSpacing
                Layout.bottomMargin: Kirigami.Units.smallSpacing
                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    opacity: 0.7
                    font: Kirigami.Theme.smallFont
                    text: (root.snapshot.killswitch ? i18n("Интернет только через VPN") : i18n("Kill switch выключен"))
                          + (root.extIp ? " · " + i18n("внешний IP: %1", root.extIp) : "")
                }
                PlasmaComponents3.ToolButton {
                    text: i18n("Внешний IP")
                    icon.name: "network-wired"
                    onClicked: root.checkIp()
                }
            }
        }
    }
}
