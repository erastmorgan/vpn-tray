# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Tray icon + window (PyQt5; KDE shows it through StatusNotifierItem)."""
from __future__ import annotations

import json
import sys
import time
import traceback
import urllib.request
from typing import Dict

from PyQt5.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtDBus import QDBusConnection, QDBusServiceWatcher
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QAction, QApplication, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QSystemTrayIcon,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import backends  # noqa: F401
from .theme import ThemeFollower
from .core import (STATE_RU, Manager, SingleInstance, State, Status, human_bytes, human_duration,
                   human_rate, runtime_dir)

SOCKET_NAME = "vpn-tray-show"

COLORS = {
    State.CONNECTED: "#2e9e44",
    State.CONNECTING: "#e0a000",
    State.DISCONNECTING: "#e0a000",
    State.DISCONNECTED: "#8a8f98",
    State.ERROR: "#d03b3b",
}
COLS = ["Имя", "Тип / протокол", "Статус", "Сервер", "Скорость ↓ / ↑", "Трафик ↓ / ↑", "Время"]


def shield_icon(color: str, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    path = QPainterPath()
    path.moveTo(s * .5, s * .06)
    path.lineTo(s * .88, s * .2)
    path.cubicTo(s * .88, s * .62, s * .7, s * .82, s * .5, s * .95)
    path.cubicTo(s * .3, s * .82, s * .12, s * .62, s * .12, s * .2)
    path.closeSubpath()
    p.setPen(QPen(QColor("#ffffff"), s * .05))
    p.setBrush(QBrush(QColor(color)))
    p.drawPath(path)
    p.setPen(QPen(QColor("#ffffff"), s * .08, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    if color == COLORS[State.CONNECTED]:
        chk = QPainterPath()
        chk.moveTo(s * .32, s * .5)
        chk.lineTo(s * .45, s * .63)
        chk.lineTo(s * .69, s * .37)
        p.setBrush(Qt.NoBrush)
        p.drawPath(chk)
    p.end()
    return QIcon(pm)


class Signals(QObject):
    done = pyqtSignal(object, object)  # result, error-string


class Job(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args, self.sig = fn, args, Signals()

    def run(self):
        try:
            self.sig.done.emit(self.fn(*self.args), None)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.sig.done.emit(None, str(e))


def external_ip() -> str:
    with urllib.request.urlopen("https://ifconfig.me/ip", timeout=8) as r:
        return r.read().decode().strip()


class Window(QWidget):
    def __init__(self, app: "TrayApp"):
        super().__init__()
        self.app = app
        self.setWindowTitle("VPN")
        self.setWindowIcon(shield_icon(COLORS[State.CONNECTED]))
        self.resize(1180, 300)
        lay = QVBoxLayout(self)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._double)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._ctx)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        lay.addWidget(self.table)

        btns = QHBoxLayout()
        self.b_conn = QPushButton("Подключить")
        self.b_disc = QPushButton("Отключить")
        self.b_ren = QPushButton("Переименовать…")
        self.b_proto = QPushButton("Подпись протокола…")
        self.b_refresh = QPushButton("Обновить список")
        self.b_ip = QPushButton("Внешний IP")
        self.ip_label = QLabel("")
        self.b_conn.clicked.connect(lambda: self.app.connect_vpn(self.selected()))
        self.b_disc.clicked.connect(lambda: self.app.disconnect_vpn(self.selected()))
        self.b_ren.clicked.connect(lambda: self.app.rename_vpn(self.selected(), self))
        self.b_proto.clicked.connect(lambda: self.app.set_protocol(self.selected(), self))
        self.b_refresh.clicked.connect(self.app.rediscover)
        self.b_ip.clicked.connect(self.app.check_ip)
        for b in (self.b_conn, self.b_disc, self.b_ren, self.b_proto, self.b_refresh, self.b_ip):
            btns.addWidget(b)
        btns.addWidget(self.ip_label, 1)
        lay.addLayout(btns)
        ks = QHBoxLayout()
        self.b_ks = QPushButton("Kill switch")
        self.b_ks.setCheckable(True)
        self.b_ks.clicked.connect(lambda checked: self.app.set_killswitch(checked))
        self.ks_label = QLabel("")
        self.agent_label = QLabel("")
        ks.addWidget(self.b_ks)
        ks.addWidget(self.ks_label, 1)
        ks.addWidget(self.agent_label)
        lay.addLayout(ks)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        self._ids = []

    def selected(self):
        r = self.table.currentRow()
        return self._ids[r] if 0 <= r < len(self._ids) else None

    def _double(self, row, col):
        pid = self._ids[row]
        if col == 0:
            self.app.rename_vpn(pid, self)
        else:
            self.app.toggle_vpn(pid)

    def _ctx(self, pos):
        pid = self.selected()
        if not pid:
            return
        m = QMenu(self)
        st = self.app.statuses.get(pid)
        if st and st.state == State.CONNECTED:
            m.addAction("Отключить", lambda: self.app.disconnect_vpn(pid))
        else:
            m.addAction("Подключить", lambda: self.app.connect_vpn(pid))
        m.addAction("Переименовать…", lambda: self.app.rename_vpn(pid, self))
        m.addAction("Подпись протокола…", lambda: self.app.set_protocol(pid, self))
        m.exec_(self.table.viewport().mapToGlobal(pos))

    def _sync_buttons(self):
        pid = self.selected()
        st = self.app.statuses.get(pid) if pid else None
        busy = self.app.busy
        self.b_conn.setEnabled(bool(pid) and not busy and (not st or st.state != State.CONNECTED))
        self.b_disc.setEnabled(bool(pid) and not busy and bool(st) and st.state == State.CONNECTED)
        self.b_ren.setEnabled(bool(pid))
        self.b_proto.setEnabled(bool(pid))

    def refresh_service(self, ks_on, desired_name, agent_text, agent_ok):
        self.b_ks.setChecked(ks_on)
        self.b_ks.setText("Kill switch: ВКЛ" if ks_on else "Kill switch: выкл")
        self.ks_label.setText(
            ("интернет только через VPN" if ks_on else "внимание: без VPN трафик идёт напрямую")
            + (f" · держу подключённым: «{desired_name}»" if desired_name else " · авто-переподключение: нет"))
        self.agent_label.setText(agent_text)
        self.agent_label.setStyleSheet("" if agent_ok else "color: #d03b3b; font-weight: bold;")

    def refresh(self, profiles, statuses: Dict[str, Status]):
        sel = self.selected()
        self._ids = [p.id for p in profiles]
        self.table.setRowCount(len(profiles))
        now = time.time()
        for r, p in enumerate(profiles):
            st = statuses.get(p.id) or Status()
            srv = st.server_ip or st.server_host or "—"
            if st.server_port:
                srv += f":{st.server_port}"
            if st.server_host and st.server_ip and st.server_host != st.server_ip:
                srv += f"  ({st.server_host})"
            on = st.state == State.CONNECTED
            vals = [
                p.name,
                st.protocol,
                STATE_RU.get(st.state, st.state) + (f" — {st.detail}" if st.state == State.ERROR else ""),
                srv,
                f"{human_rate(st.rx_rate)} / {human_rate(st.tx_rate)}" if on else "—",
                f"{human_bytes(st.rx_bytes)} / {human_bytes(st.tx_bytes)}" if on else "—",
                human_duration(now - st.since) if on and st.since else "—",
            ]
            for c, v in enumerate(vals):
                it = self.table.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(r, c, it)
                if it.text() != v:
                    it.setText(v)
                if c == 0:
                    it.setIcon(shield_icon(COLORS.get(st.state, COLORS[State.ERROR]), 32))
                    it.setToolTip(f"id: {p.id}\nинтерфейс: {st.iface or '—'}\n"
                                  f"full-tunnel: {'да' if p.full_tunnel else 'нет'}")
        if sel in self._ids:
            self.table.selectRow(self._ids.index(sel))
        self._sync_buttons()

    def closeEvent(self, e):  # close = hide to tray (or quit in --window mode)
        if self.app.tray is None:
            e.accept()
            self.app.qapp.quit()
            return
        e.ignore()
        self.hide()


class TrayApp(QObject):
    def __init__(self, qapp: QApplication, with_tray: bool = True):
        super().__init__()
        self.qapp = qapp
        self.m = Manager()
        self.pool = QThreadPool.globalInstance()
        self.statuses: Dict[str, Status] = {}
        self.busy = False
        self._polling = False
        self._prev_state: Dict[str, str] = {}

        self.win = Window(self)
        self.tray = None
        self.menu = QMenu()
        self._run(self.m.discover, lambda *_: self.poll())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        if not with_tray:
            return
        self.tray = QSystemTrayIcon(shield_icon(COLORS[State.DISCONNECTED]))
        self.tray.setToolTip("VPN")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self.tray.show()

        # Plasma's tray (StatusNotifierWatcher) restarted => register the icon again
        self.sni_watch = QDBusServiceWatcher("org.kde.StatusNotifierWatcher", QDBusConnection.sessionBus(),
                                             QDBusServiceWatcher.WatchForRegistration, self)
        self.sni_watch.serviceRegistered.connect(self._reregister)
        # "show window" requests from a second launch
        QLocalServer.removeServer(SOCKET_NAME)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._show_request)
        self.server.listen(SOCKET_NAME)

    # -- plumbing ----------------------------------------------------------
    def _run(self, fn, cb=None, *args):
        j = Job(fn, *args)
        if cb:
            j.sig.done.connect(cb)
        self.pool.start(j)

    def _reregister(self, *_):
        if self.tray is None:
            return
        self.tray.hide()
        self.tray.show()

    def _show_request(self):
        c = self.server.nextPendingConnection()
        if c:
            c.disconnectFromServer()
        self.show_window()

    def show_window(self):
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()
        self.poll()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.win.isVisible():
                self.win.hide()
            else:
                self.show_window()

    def say(self, text: str, error=False):
        self.win.msg.setText(text)
        if self.tray:
            self.tray.showMessage("VPN", text,
                                  QSystemTrayIcon.Critical if error else QSystemTrayIcon.Information, 4000)

    # -- polling -----------------------------------------------------------
    def poll(self):
        if self._polling:
            return
        self._polling = True
        self._run(self._collect, self._polled)

    def _collect(self):
        statuses = self.m.all_status()
        st = self.m.state.read()
        try:
            hb = json.loads((runtime_dir() / "agent.json").read_text())
        except (OSError, ValueError):
            hb = {}
        return statuses, st, hb

    def _polled(self, res, err):
        self._polling = False
        if err or res is None:
            return
        statuses, st, hb = res
        self.service = (st, hb)
        self.statuses = statuses
        profiles = self.m.profiles()
        for p in profiles:
            s = statuses[p.id].state
            prev = self._prev_state.get(p.id)
            if self.tray and prev and prev != s and s in (State.CONNECTED, State.DISCONNECTED) and not self.busy:
                self.tray.showMessage("VPN", f"{p.name}: {STATE_RU[s]}", QSystemTrayIcon.Information, 3000)
            self._prev_state[p.id] = s
        self._update_tray(profiles, statuses)
        if self.win.isVisible():
            self.win.refresh(profiles, statuses)
            age = time.time() - hb.get("ts", 0)
            agent_ok = age < 20
            agent_text = (f"агент: работает ({hb.get('last', '')})" if agent_ok
                          else "агент: НЕ ОТВЕЧАЕТ — авто-переподключение не работает")
            desired = st.get("desired")
            dname = self.m.store.profiles[desired].name if desired in self.m.store.profiles else ""
            self.win.refresh_service(bool(st.get("killswitch")), dname, agent_text, agent_ok)

    def _update_tray(self, profiles, statuses):
        if self.tray is None:
            return
        on = [p for p in profiles if statuses[p.id].state == State.CONNECTED]
        trans = any(statuses[p.id].state in (State.CONNECTING, State.DISCONNECTING) for p in profiles)
        color = COLORS[State.CONNECTING] if trans else COLORS[State.CONNECTED] if on else COLORS[State.DISCONNECTED]
        self.tray.setIcon(shield_icon(color))
        lines = []
        for p in profiles:
            st = statuses[p.id]
            line = f"{p.name}: {STATE_RU[st.state]}"
            if st.state == State.CONNECTED:
                line += f"  ↓{human_rate(st.rx_rate)} ↑{human_rate(st.tx_rate)}"
            lines.append(line)
        self.tray.setToolTip("VPN\n" + "\n".join(lines))
        # rebuild menu only when something relevant changed
        key = tuple((p.id, p.name, statuses[p.id].state) for p in profiles) + (
            self.busy, getattr(self, "service", ({}, {}))[0].get("killswitch"))
        if getattr(self, "_menu_key", None) == key:
            return
        self._menu_key = key
        self.menu.clear()
        self.menu.addAction("Открыть окно VPN", lambda: self._activated(QSystemTrayIcon.Trigger))
        self.menu.addSeparator()
        for p in profiles:
            st = statuses[p.id]
            a = QAction(shield_icon(COLORS.get(st.state, COLORS[State.ERROR]), 32),
                        f"{p.name} — {STATE_RU[st.state]}", self.menu)
            a.setCheckable(True)
            a.setChecked(st.state == State.CONNECTED)
            a.setEnabled(not self.busy)
            a.triggered.connect(lambda _c, pid=p.id: self.toggle_vpn(pid))
            self.menu.addAction(a)
        self.menu.addSeparator()
        ks_on = bool(getattr(self, "service", ({}, {}))[0].get("killswitch", True))
        ks = self.menu.addAction("Kill switch (интернет только через VPN)")
        ks.setCheckable(True)
        ks.setChecked(ks_on)
        ks.triggered.connect(self.set_killswitch)
        dis = self.menu.addAction("Отключить все", self.disconnect_all)
        dis.setEnabled(bool(on) and not self.busy)
        self.menu.addAction("Выход", self.qapp.quit)

    # -- actions -----------------------------------------------------------
    def _op(self, label, fn, *args):
        if self.busy:
            return
        self.busy = True
        self.win.msg.setText(label + "…")
        self._menu_key = None

        def done(_res, err):
            self.busy = False
            self._menu_key = None
            if err:
                self.say(f"{label}: ошибка — {err}", error=True)
            else:
                self.say(f"{label}: готово")
            self.poll()
        self._run(fn, done, *args)

    def toggle_vpn(self, pid):
        st = self.statuses.get(pid)
        if st and st.state == State.CONNECTED:
            self.disconnect_vpn(pid)
        else:
            self.connect_vpn(pid)

    def connect_vpn(self, pid):
        if pid:
            name = self.m.get(pid).name
            self._op(f"Подключение «{name}»", lambda: self.m.connect(pid, log=lambda s: None))

    def disconnect_vpn(self, pid):
        if pid:
            self._op(f"Отключение «{self.m.get(pid).name}»", self.m.disconnect, pid)

    def disconnect_all(self):
        ids = [pid for pid, st in self.statuses.items() if st.state == State.CONNECTED]
        self._op("Отключение всех VPN", lambda: [self.m.disconnect(i) for i in ids])

    def set_killswitch(self, on: bool):
        on = bool(on)
        if not on:
            r = QMessageBox.question(self.win, "Kill switch",
                                     "Выключить kill switch? Без VPN трафик пойдёт напрямую в интернет.")
            if r != QMessageBox.Yes:
                self._menu_key = None
                self.poll()
                return
        self.m.state.update(killswitch=on)
        self._op("Kill switch " + ("включение" if on else "выключение"), self.m.apply_killswitch, on)

    def rename_vpn(self, pid, parent):
        if not pid:
            return
        p = self.m.get(pid)
        name, ok = QInputDialog.getText(parent, "Переименовать VPN", "Новое имя:", text=p.name)
        if ok and name.strip():
            self.m.rename(pid, name)
            self._menu_key = None
            self.poll()

    def set_protocol(self, pid, parent):
        if not pid:
            return
        p = self.m.get(pid)
        cur = p.params.get("user_protocol", "")
        val, ok = QInputDialog.getText(parent, "Подпись протокола",
                                       "Как показывать тип/протокол (пусто — автоматически):", text=cur)
        if ok:
            if val.strip():
                p.params["user_protocol"] = val.strip()
            else:
                p.params.pop("user_protocol", None)
            self.m.store.save()
            self.poll()

    def rediscover(self):
        self._run(self.m.discover, lambda *_: self.poll())

    def check_ip(self):
        self.win.ip_label.setText("Внешний IP: …")
        self._run(external_ip, lambda ip, err: self.win.ip_label.setText(
            f"Внешний IP: {ip}" if ip else f"Внешний IP: ошибка ({err})"))


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("vpn-tray")
    qapp.setDesktopFileName("vpn-tray")
    window_only = "--window" in sys.argv          # details window opened from the Plasma applet
    qapp.setQuitOnLastWindowClosed(window_only)
    ThemeFollower(qapp)                            # light/dark exactly as the KDE colour scheme
    lock = SingleInstance("vpn-tray-window" if window_only else "vpn-tray")
    if not lock.acquire():
        if not window_only:
            s = QLocalSocket()
            s.connectToServer(SOCKET_NAME)
            s.waitForConnected(1000)
            s.disconnectFromServer()
        print("vpn-tray уже запущен", file=sys.stderr)
        return 0
    if not window_only:
        # the tray may start before Plasma's panel: wait for it instead of dying
        for _ in range(60):
            if QSystemTrayIcon.isSystemTrayAvailable():
                break
            time.sleep(1)
    app = TrayApp(qapp, with_tray=not window_only)
    if window_only or "--show" in sys.argv:
        app.show_window()
    return qapp.exec_()


if __name__ == "__main__":
    sys.exit(main())
