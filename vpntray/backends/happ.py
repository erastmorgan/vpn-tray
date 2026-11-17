# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Happ (official Xray client, https://happ.su) driven in the background through its official deeplinks.

Happ keeps the (encrypted) subscription itself; it is never decrypted here. We only:
  * keep Happ running in the background (user service vpn-tray-happ.service, Restart=always);
  * send happ://connect / happ://disconnect to it, while a short KWin script closes the window
    Happ would otherwise pop up (its tray icon stays);
  * watch its TUN interface (happ-xray) for state and traffic;
  * read the selected server name/host/port from ~/.config/Happ.conf;
  * register DNS for the tunnel with openresolv (Happ 4.7 on Linux does not), otherwise the box
    has no resolver once the other full-tunnel VPN is stopped.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .. import kwinhide
from ..core import (Backend, Profile, State, Status, helper, iface_counters, iface_exists,
                    iface_up_since, process_started_at, register, resolve, runtime_dir)

HAPP_BIN = os.environ.get("VPN_TRAY_HAPP_BIN", "/opt/happ/bin/Happ")
HAPP_CONF = Path.home() / ".config" / "Happ.conf"
HAPP_SERVICE = "vpn-tray-happ.service"
DEFAULT_IFACE = "happ-xray"
DEFAULT_DNS = ["1.1.1.1"]


def read_happ_conf(path: Path = HAPP_CONF) -> dict:
    """Tiny reader for the Qt ini file (only plain key=value lines we need)."""
    res = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith(("[", "#", ";")):
                k, _, v = line.partition("=")
                res[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return res


SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
                "XDG_CURRENT_DESKTOP", "LANG")


def session_env() -> dict:
    """Our env, completed with the graphical session's variables (CLI over ssh has none)."""
    env = dict(os.environ)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            k, _, v = line.partition("=")
            if k in SESSION_VARS and v:
                env[k] = v
    return env


def happ_running() -> bool:
    return subprocess.run(["pgrep", "-x", "Happ"], capture_output=True).returncode == 0


def _service_installed() -> bool:
    return subprocess.run(["systemctl", "--user", "cat", HAPP_SERVICE],
                          capture_output=True).returncode == 0


def ensure_happ_running(timeout: float = 25):
    """Start Happ in the background (no window) if it is not running yet."""
    if happ_running():
        return
    kwinhide.suppress("Happ", 20, close_existing=True)
    if _service_installed():
        subprocess.run(["systemctl", "--user", "start", HAPP_SERVICE], capture_output=True, timeout=20)
    elif shutil.which("systemd-run"):
        # own transient unit: Happ must not live in our cgroup (agent restarts would kill it)
        subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=happ-{int(time.time())}",
                        HAPP_BIN], capture_output=True, timeout=20, env=session_env())
    else:
        subprocess.Popen([HAPP_BIN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=session_env())
    end = time.monotonic() + timeout
    while not happ_running():
        if time.monotonic() > end:
            raise RuntimeError("Happ не запустился")
        time.sleep(0.5)
    time.sleep(3)  # let it connect to happd and load the subscription before the first deeplink


@register
class HappBackend(Backend):
    key = "happ"
    title = "Happ (Xray)"

    def available(self) -> bool:
        return Path(HAPP_BIN).exists()

    def discover(self):
        conf = read_happ_conf()
        server = conf.get("lastServerName", "")
        return [Profile(
            id="happ:default", backend=self.key,
            name=f"Happ · {server}" if server else "Happ",
            params={"iface": DEFAULT_IFACE, "dns": DEFAULT_DNS},
        )]

    # -- control -----------------------------------------------------------
    def _deeplink(self, action: str):
        ensure_happ_running()
        # Switching VPNs must never leave Happ's window on screen — including one that was
        # already open (it only hides to the tray icon; Happ keeps running).
        kwinhide.suppress("Happ", 15, close_existing=True)
        # a second Happ process forwards the deeplink to the running instance and exits
        r = subprocess.run([HAPP_BIN, f"happ://{action}"], capture_output=True, text=True,
                           timeout=25, env=session_env())
        if r.returncode != 0:
            tail = " ".join((r.stderr or r.stdout).strip().splitlines()[-2:])
            raise RuntimeError(f"Happ не принял команду {action} (код {r.returncode}): {tail}")

    def _dns_marker(self, iface: str) -> Path:
        return runtime_dir() / f"dns.{iface}"

    def _set_dns(self, p: Profile):
        iface = p.params.get("iface", DEFAULT_IFACE)
        helper("dns-set", iface, *p.params.get("dns", DEFAULT_DNS))
        self._dns_marker(iface).touch()

    def _clear_dns(self, p: Profile):
        iface = p.params.get("iface", DEFAULT_IFACE)
        m = self._dns_marker(iface)
        if m.exists():
            try:
                helper("dns-clear", iface)
            finally:
                m.unlink(missing_ok=True)

    def connect(self, p: Profile):
        iface = p.params.get("iface", DEFAULT_IFACE)
        self._deeplink("connect")
        end = time.monotonic() + 40
        while not iface_exists(iface):
            if time.monotonic() > end:
                raise RuntimeError("Happ не поднял туннель (нет интерфейса %s)" % iface)
            time.sleep(0.5)
        if p.params.get("dns"):
            self._set_dns(p)
        kwinhide.suppress("Happ", 5, close_existing=True)   # Happ may re-show it once connected

    def disconnect(self, p: Profile):
        iface = p.params.get("iface", DEFAULT_IFACE)
        if happ_running() and iface_exists(iface):
            self._deeplink("disconnect")
            end = time.monotonic() + 20
            while iface_exists(iface) and time.monotonic() < end:
                time.sleep(0.5)
            kwinhide.suppress("Happ", 5, close_existing=True)
        self._clear_dns(p)

    # -- status ------------------------------------------------------------
    def status(self, p: Profile) -> Status:
        iface = p.params.get("iface", DEFAULT_IFACE)
        conf = read_happ_conf()
        host = conf.get("lastServerHost", "")
        port = conf.get("lastServerPort", "")
        st = Status(
            protocol=p.params.get("user_protocol") or "Xray (Happ)",
            server_host=host, server_port=int(port) if port.isdigit() else None,
            iface=iface, detail=conf.get("lastServerName", ""),
        )
        st.server_ip = resolve(host)
        up = iface_exists(iface)
        if up:
            st.state = State.CONNECTED
            st.rx_bytes, st.tx_bytes = iface_counters(iface)
            st.since = iface_up_since(iface, lambda: process_started_at("/opt/happ/bin/core/xray"))
        # reconcile DNS if Happ was toggled from its own window
        try:
            marker = self._dns_marker(iface).exists()
            if up and not marker and p.params.get("dns"):
                self._set_dns(p)
            elif not up and marker:
                self._clear_dns(p)
        except Exception as e:
            st.detail = f"DNS: {e}"
        return st
