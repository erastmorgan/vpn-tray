/*
    SPDX-FileCopyrightText: 2026 erastmorgan
    SPDX-License-Identifier: MIT

    One VPN row, styled like the stock Networks / Bluetooth applets.
*/
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

PlasmaExtras.ExpandableListItem {
    id: vpnRow

    required property int index
    required property string vid
    required property string name
    required property string vstate
    required property string protocol
    required property string server_host
    required property string server_ip
    required property int server_port
    required property string iface
    required property real rx_rate
    required property real tx_rate
    required property real rx_bytes
    required property real tx_bytes
    required property real since
    required property string detail
    required property bool full_tunnel

    readonly property var vpn: ({ id: vid, name: name, state: vstate, server_host: server_host,
                                  server_ip: server_ip, server_port: server_port, detail: detail })
    readonly property bool connected: vstate === "connected"
    readonly property bool busyNow: !!root.busy[vid] || vstate === "connecting" || vstate === "disconnecting"

    icon: connected ? "network-vpn" : "network-vpn-symbolic"
    iconEmblem: vstate === "error" ? "emblem-error" : ""
    title: name
    subtitle: root.stateText(vpn) + (connected ? "  ·  ↓ " + root.fmtRate(rx_rate) + "  ↑ " + root.fmtRate(tx_rate) : "")
    isBusy: busyNow
    isDefault: connected

    defaultActionButtonAction: QQC2.Action {
        enabled: !vpnRow.busyNow
        text: vpnRow.connected ? i18n("Отключить") : i18n("Подключить")
        icon.name: vpnRow.connected ? "network-disconnect-symbolic" : "network-connect-symbolic"
        onTriggered: root.toggle(vpnRow.vpn)
    }

    contextualActions: [
        QQC2.Action {
            text: i18n("Переименовать…")
            icon.name: "edit-rename"
            onTriggered: {
                vpnRow.renaming = true
                vpnRow.expand()
            }
        }
    ]

    property bool renaming: false

    customExpandedViewContent: Component {
        ColumnLayout {
            spacing: Kirigami.Units.smallSpacing

            RowLayout {
                visible: vpnRow.renaming
                Layout.fillWidth: true
                PlasmaComponents3.TextField {
                    id: nameField
                    Layout.fillWidth: true
                    text: vpnRow.name
                    placeholderText: i18n("Новое имя")
                    Component.onCompleted: if (vpnRow.renaming) forceActiveFocus()
                    onVisibleChanged: if (visible) { text = vpnRow.name; forceActiveFocus(); selectAll() }
                    Keys.onReturnPressed: apply.clicked()
                    Keys.onEscapePressed: vpnRow.renaming = false
                }
                PlasmaComponents3.Button {
                    id: apply
                    icon.name: "dialog-ok-apply"
                    text: i18n("Сохранить")
                    onClicked: { root.rename(vpnRow.vid, nameField.text); vpnRow.renaming = false }
                }
                PlasmaComponents3.ToolButton {
                    icon.name: "dialog-cancel"
                    display: PlasmaComponents3.AbstractButton.IconOnly
                    text: i18n("Отмена")
                    onClicked: vpnRow.renaming = false
                }
            }

            Kirigami.FormLayout {
                Layout.fillWidth: true
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Тип / протокол:")
                    text: vpnRow.protocol || "—"
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Сервер:")
                    text: root.server(vpnRow.vpn)
                    wrapMode: Text.WrapAnywhere
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                }
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Скорость ↓ / ↑:")
                    visible: vpnRow.connected
                    text: root.fmtRate(vpnRow.rx_rate) + " / " + root.fmtRate(vpnRow.tx_rate)
                }
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Трафик ↓ / ↑:")
                    visible: vpnRow.connected
                    text: root.fmtBytes(vpnRow.rx_bytes) + " / " + root.fmtBytes(vpnRow.tx_bytes)
                }
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Подключён:")
                    visible: vpnRow.connected
                    text: root.fmtDuration(vpnRow.since, root.snapshot.ts)   // snapshot.ts ticks every second
                }
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Интерфейс:")
                    text: (vpnRow.iface || "—") + (vpnRow.full_tunnel ? "" : i18n(" (split-tunnel)"))
                }
                PlasmaComponents3.Label {
                    Kirigami.FormData.label: i18n("Авто-переподключение:")
                    text: root.snapshot.desired === vpnRow.vid ? i18n("да") : i18n("нет")
                }
            }
        }
    }
}
