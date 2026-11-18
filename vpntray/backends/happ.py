# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Happ (official Xray client, https://happ.su) driven in the background through its official deeplinks.

Happ keeps the (encrypted) subscriptions itself; they are never decrypted here. We only:
  * keep Happ running in the background (user service vpn-tray-happ.service, Restart=always);
  * send happ://connect / happ://disconnect to it, while a short KWin script closes the window
    Happ would otherwise pop up (its tray icon stays);
  * watch its TUN interface (happ-xray) for state and traffic;
  * register DNS for the tunnel with openresolv (Happ on Linux does not), otherwise the box
    has no resolver once the other full-tunnel VPN is stopped.

Several Happ VPNs. Happ has no command to pick a server: the selected server lives inside its
encrypted ~/.config/Happ/subs.db (Happ.conf only mirrors it). So each Happ VPN is a *state
snapshot*: ~/.config/vpn-tray/happ/<name>/{subs.db,selection.ini}, holding one subscription
(whose server Happ selects by itself). Connecting a profile whose snapshot is not loaded stops
Happ, puts the snapshot in place and starts Happ again. New snapshots are made by
`vpn-tray-cli happ-import NAME` (link on stdin): a cold start of Happ with the link on an empty
state, then a graceful happ://close so Happ saves it. Without snapshots there is one profile,
happ:default = whatever server is selected in Happ.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import re
import tempfile

from .. import kwinhide
from ..core import (CONFIG_DIR, Backend, Profile, State, Status, helper, iface_counters, iface_exists,
                    iface_up_since, process_started_at, register, resolve, runtime_dir)

HAPP_BIN = os.environ.get("VPN_TRAY_HAPP_BIN", "/opt/happ/bin/Happ")
HAPP_CONF = Path.home() / ".config" / "Happ.conf"
HAPP_SERVICE = "vpn-tray-happ.service"
HAPP_DB = Path.home() / ".config" / "Happ" / "subs.db"
HAPP_SUB_LOG = Path.home() / ".local" / "share" / "Happ" / "logs" / "subscription_log.txt"
STATES_DIR = CONFIG_DIR / "happ"
DEFAULT_IFACE = "happ-xray"
DEFAULT_DNS = ["1.1.1.1"]
SELECTION_KEYS = ("lastServer", "lastServerGuid", "lastServerHost", "lastServerName", "lastServerPort",
                  "lastSubscription")
_SEL_RE = re.compile(r"^(%s)=" % "|".join(SELECTION_KEYS))
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def read_happ_conf(path: Path = None) -> dict:
    """Tiny reader for the Qt ini file (only plain key=value lines we need)."""
    res = {}
    try:
        for line in (path or HAPP_CONF).read_text(errors="replace").splitlines():
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


def _session_env_for_unit() -> list:
    env = session_env()
    return [f"--setenv={k}={env[k]}" for k in SESSION_VARS if env.get(k)]


def stop_happ(timeout: float = 15):
    """Stop Happ (service or stray process). SIGTERM: Happ does not save anything then — fine,
    the snapshots are the source of truth."""
    if _service_installed():
        subprocess.run(["systemctl", "--user", "stop", HAPP_SERVICE], capture_output=True, timeout=30)
    subprocess.run(["pkill", "-x", "Happ"], capture_output=True)
    end = time.monotonic() + timeout
    while happ_running():
        if time.monotonic() > end:
            subprocess.run(["pkill", "-9", "-x", "Happ"], capture_output=True)
            time.sleep(1)
            break
        time.sleep(0.3)


def close_happ_gracefully(timeout: float = 25) -> bool:
    """happ://close: Happ saves its state (subscriptions, selection) and exits."""
    kwinhide.suppress("Happ", 10, close_existing=True)
    subprocess.run([HAPP_BIN, "happ://close"], capture_output=True, timeout=25, env=session_env())
    end = time.monotonic() + timeout
    while happ_running():
        if time.monotonic() > end:
            return False
        time.sleep(0.3)
    return True


def _conf_selection(text: str) -> dict:
    return {k: v for k, _, v in (l.partition("=") for l in text.splitlines() if _SEL_RE.match(l))}


def _write_conf_selection(lines: list):
    """Replace the server selection keys in Happ.conf (they live in [Preferences])."""
    text = HAPP_CONF.read_text() if HAPP_CONF.exists() else ""
    out, inserted = [], False
    for l in text.splitlines():
        if _SEL_RE.match(l):
            continue
        out.append(l)
        if l.strip() == "[Preferences]" and not inserted:
            out.extend(lines)
            inserted = True
    if not inserted:
        out += ["", "[Preferences]", *lines]
    _atomic_write(HAPP_CONF, "\n".join(out) + "\n")


def _atomic_write(path: Path, data, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def list_states() -> list:
    if not STATES_DIR.is_dir():
        return []
    return sorted(d.name for d in STATES_DIR.iterdir()
                  if d.is_dir() and (d / "subs.db").is_file() and (d / "selection.ini").is_file())


def state_selection(slug: str) -> dict:
    try:
        return _conf_selection((STATES_DIR / slug / "selection.ini").read_text())
    except OSError:
        return {}


def _active_file() -> Path:
    return STATES_DIR / "active"


def active_state():
    """Snapshot currently loaded into Happ: the one whose selected server guid matches Happ.conf."""
    guid = read_happ_conf().get("lastServerGuid", "")
    try:
        marked = _active_file().read_text().strip()
    except OSError:
        marked = ""
    if marked and state_selection(marked).get("lastServerGuid") == guid:
        return marked
    for slug in list_states():
        if guid and state_selection(slug).get("lastServerGuid") == guid:
            return slug
    return marked or None


def _save_live_as(slug: str):
    sel = [l for l in HAPP_CONF.read_text().splitlines() if _SEL_RE.match(l)]
    if not any(l.startswith("lastServerGuid=") and l.partition("=")[2] for l in sel):
        raise RuntimeError("в Happ не выбран сервер — нечего сохранять")
    d = STATES_DIR / slug
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(d / "subs.db", HAPP_DB.read_bytes(), 0o600)
    _atomic_write(d / "selection.ini", "\n".join(sel) + "\n", 0o600)


def load_state(slug: str):
    """Make Happ run with snapshot `slug` (restart it only if another one is loaded)."""
    d = STATES_DIR / slug
    if not (d / "subs.db").is_file():
        raise RuntimeError(f"нет сохранённого состояния Happ «{slug}»")
    if active_state() == slug and happ_running():
        return
    stop_happ()
    _atomic_write(HAPP_DB, (d / "subs.db").read_bytes(), 0o644)
    _write_conf_selection((d / "selection.ini").read_text().splitlines())
    _atomic_write(_active_file(), slug + "\n", 0o600)
    ensure_happ_running()


def import_link(slug: str, link: str, log=print) -> dict:
    """Create snapshot `slug` from a Happ subscription link (happ://crypt…, https://…).

    Happ crashes when such a link arrives at an already running instance (4.7–4.8, links with a
    routing profile), so Happ is cold-started with the link on an empty state, then closed with
    happ://close (it saves on that path only). The previous state is restored afterwards.
    """
    if not SLUG_RE.match(slug):
        raise ValueError("имя: латиница в нижнем регистре, цифры, - и _ (до 32 символов)")
    if not link.strip().startswith(("happ://", "https://", "http://")):
        raise ValueError("ожидается ссылка подписки Happ (happ://… или https://…)")
    if iface_exists(DEFAULT_IFACE):
        raise RuntimeError("сначала отключи VPN через Happ — импорт перезапускает Happ")
    STATES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not list_states() and read_happ_conf().get("lastServerGuid"):
        log("сохраняю текущий сервер Happ как профиль «default»")
        if happ_running():
            close_happ_gracefully()
        _save_live_as("default")
        _atomic_write(_active_file(), "default\n", 0o600)
    prev = active_state()
    conf_backup = HAPP_CONF.read_bytes() if HAPP_CONF.exists() else None
    db_backup = HAPP_DB.read_bytes() if HAPP_DB.exists() else None
    stop_happ()
    try:
        _write_conf_selection([])
        HAPP_DB.unlink(missing_ok=True)
        log_pos = HAPP_SUB_LOG.stat().st_size if HAPP_SUB_LOG.exists() else 0
        kwinhide.suppress("Happ", 60, close_existing=True)
        unit = f"vpntray-happ-import-{int(time.time())}"
        # the link goes through argv of Happ only (as Happ requires), never through our logs
        subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}",
                        *_session_env_for_unit(), HAPP_BIN, link.strip()],
                       capture_output=True, timeout=20, check=True)
        end, added = time.monotonic() + 45, False
        while time.monotonic() < end and not added:
            time.sleep(1)
            try:
                with HAPP_SUB_LOG.open(errors="replace") as f:
                    f.seek(log_pos)
                    tail = f.read()
            except OSError:
                tail = ""
            added = "added successfully" in tail
        if not added:
            raise RuntimeError("Happ не добавил подписку (см. ~/.local/share/Happ/logs/subscription_log.txt)")
        time.sleep(3)   # let it pick the server
        if not close_happ_gracefully():
            raise RuntimeError("Happ не сохранил состояние (happ://close не сработал)")
        _save_live_as(slug)
        sel = state_selection(slug)
        log(f"импортировано: {slug} → сервер «{sel.get('lastServerName', '?')}»")
        return sel
    finally:
        stop_happ()
        if prev and (STATES_DIR / prev / "subs.db").is_file():
            _atomic_write(_active_file(), "\n", 0o600)     # force a real reload
            load_state(prev)
        else:
            if db_backup is not None:
                _atomic_write(HAPP_DB, db_backup, 0o644)
            if conf_backup is not None:
                _atomic_write(HAPP_CONF, conf_backup, 0o644)
            ensure_happ_running()


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
        states = list_states()
        if not states:   # single-server mode: whatever is selected in Happ
            server = read_happ_conf().get("lastServerName", "")
            return [Profile(id="happ:default", backend=self.key,
                            name=f"Happ · {server}" if server else "Happ",
                            params={"iface": DEFAULT_IFACE, "dns": DEFAULT_DNS})]
        out = []
        for slug in states:
            server = state_selection(slug).get("lastServerName", "")
            out.append(Profile(id=f"happ:{slug}", backend=self.key,
                               name=f"Happ · {server or slug}",
                               params={"iface": DEFAULT_IFACE, "dns": DEFAULT_DNS, "state": slug}))
        return out

    def _is_active(self, p: Profile) -> bool:
        slug = p.params.get("state")
        return not slug or active_state() == slug

    def _selection(self, p: Profile) -> dict:
        slug = p.params.get("state")
        return state_selection(slug) if slug else read_happ_conf()

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
        if p.params.get("state"):
            if iface_exists(iface) and not self._is_active(p):
                # another Happ VPN is up (only when connect bypasses the manager's exclusivity)
                self._deeplink("disconnect")
                end = time.monotonic() + 20
                while iface_exists(iface) and time.monotonic() < end:
                    time.sleep(0.5)
            load_state(p.params["state"])
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
        if not self._is_active(p):
            return          # its snapshot is not loaded, so it is not connected
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
        conf = self._selection(p)
        active = self._is_active(p)
        host = conf.get("lastServerHost", "")
        port = conf.get("lastServerPort", "")
        st = Status(
            protocol=p.params.get("user_protocol") or "Xray (Happ)",
            server_host=host, server_port=int(port) if port.isdigit() else None,
            iface=iface, detail=conf.get("lastServerName", ""),
        )
        st.server_ip = resolve(host)
        up = active and iface_exists(iface)
        if up:
            st.state = State.CONNECTED
            st.rx_bytes, st.tx_bytes = iface_counters(iface)
            st.since = iface_up_since(iface, lambda: process_started_at("/opt/happ/bin/core/xray"))
        # reconcile DNS if Happ was toggled from its own window (only the loaded snapshot owns it)
        if not active:
            return st
        try:
            marker = self._dns_marker(iface).exists()
            if up and not marker and p.params.get("dns"):
                self._set_dns(p)
            elif not up and marker:
                self._clear_dns(p)
        except Exception as e:
            st.detail = f"DNS: {e}"
        return st
