# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Core model: profiles, backend registry, status, shared state and the Manager that ties them together."""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

CONFIG_DIR = Path(os.environ.get("VPN_TRAY_CONFIG_DIR", Path.home() / ".config" / "vpn-tray"))
HELPER = os.environ.get("VPN_TRAY_HELPER", "/usr/local/libexec/vpn-tray-helper")


class State:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


STATE_RU = {
    State.DISCONNECTED: "Отключён",
    State.CONNECTING: "Подключение…",
    State.CONNECTED: "Подключён",
    State.DISCONNECTING: "Отключение…",
    State.ERROR: "Ошибка",
}


@dataclass
class Profile:
    id: str                 # stable id, e.g. "awg:awg0", "happ:default", "nm:<uuid>"
    backend: str            # backend key in BACKENDS
    name: str               # user-visible, renameable
    params: dict = field(default_factory=dict)
    full_tunnel: bool = True  # full-tunnel VPNs are mutually exclusive
    hidden: bool = False


@dataclass
class Status:
    state: str = State.DISCONNECTED
    protocol: str = ""
    server_host: str = ""
    server_ip: str = ""
    server_port: Optional[int] = None
    iface: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_rate: float = 0.0     # bytes/s, filled by Manager
    tx_rate: float = 0.0
    since: Optional[float] = None   # epoch when connection came up
    detail: str = ""


class PrivilegeError(RuntimeError):
    pass


def run(cmd: List[str], timeout: float = 30, check: bool = True, env=None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.returncode}: {(p.stderr or p.stdout).strip()}")
    return p


def helper(*args: str, timeout: float = 60) -> str:
    """Run the root helper through sudo -n (never prompts)."""
    p = subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip()
        if "password is required" in err or "a terminal is required" in err:
            raise PrivilegeError("нет прав sudo на vpn-tray-helper — запустите install.sh")
        raise RuntimeError(f"helper {' '.join(args)}: {err}")
    return p.stdout


def iface_exists(iface: str) -> bool:
    return bool(iface) and Path(f"/sys/class/net/{iface}").exists()


def iface_counters(iface: str):
    base = Path(f"/sys/class/net/{iface}/statistics")
    try:
        return int((base / "rx_bytes").read_text()), int((base / "tx_bytes").read_text())
    except OSError:
        return 0, 0


_SINCE_CACHE: Dict[tuple, float] = {}


def _ifindex(iface: str) -> str:
    try:
        return Path(f"/sys/class/net/{iface}/ifindex").read_text().strip()
    except OSError:
        return ""


def iface_up_since(iface: str, source: Callable[[], Optional[float]] = None) -> Optional[float]:
    """Epoch when this incarnation of the interface came up (cached per ifindex).

    `source` gives the real start time (systemd unit / process start); otherwise the
    first time we saw the interface is used.
    """
    key = (iface, _ifindex(iface))
    if key not in _SINCE_CACHE:
        t = None
        if source:
            try:
                t = source()
            except Exception:
                t = None
        _SINCE_CACHE[key] = t or time.time()
    return _SINCE_CACHE[key]


def unit_started_at(unit: str, user: bool = False) -> Optional[float]:
    cmd = ["systemctl"] + (["--user"] if user else []) + ["show", unit, "-p", "ActiveEnterTimestampMonotonic",
                                                          "--value"]
    v = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    if not v.isdigit() or v == "0":
        return None
    return time.time() - (time.monotonic() - int(v) / 1e6)


def process_started_at(pattern: str) -> Optional[float]:
    p = subprocess.run(["pgrep", "-o", "-f", pattern], capture_output=True, text=True)
    pid = p.stdout.strip()
    if not pid:
        return None
    start_ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    boot = float(Path("/proc/uptime").read_text().split()[0])
    return time.time() - (boot - start_ticks / os.sysconf("SC_CLK_TCK"))


_RESOLVE_CACHE: Dict[str, tuple] = {}


def resolve(host: str, ttl: float = 300) -> str:
    """IPv4 of host, cached (status is polled every second; DNS may be slow or down)."""
    if not host:
        return ""
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    ip, ts = _RESOLVE_CACHE.get(host, ("", 0.0))
    if time.time() - ts < (ttl if ip else 30):
        return ip
    try:
        new = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except OSError:
        new = ""
    ip = new or ip
    _RESOLVE_CACHE[host] = (ip, time.time())
    return ip


def split_hostport(ep: str):
    ep = ep.strip()
    if not ep:
        return "", None
    if ep.startswith("["):
        host, _, port = ep[1:].partition("]:")
    else:
        host, _, port = ep.rpartition(":")
        if not host:
            host, port = port, ""
    return host, int(port) if port.isdigit() else None


# ---------------------------------------------------------------- backends

class Backend:
    """Base class. A backend knows how to discover, connect, disconnect and inspect one VPN technology."""
    key = "base"
    title = "base"

    def discover(self) -> List[Profile]:
        return []

    def connect(self, profile: Profile) -> None:
        raise NotImplementedError

    def disconnect(self, profile: Profile) -> None:
        raise NotImplementedError

    def status(self, profile: Profile) -> Status:
        raise NotImplementedError

    def available(self) -> bool:
        return True


BACKENDS: Dict[str, Backend] = {}


def register(backend_cls):
    BACKENDS[backend_cls.key] = backend_cls()
    return backend_cls


# ---------------------------------------------------------------- persistence

def _atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class ProfileStore:
    def __init__(self, path: Path = None):
        self.path = path or CONFIG_DIR / "profiles.json"
        self.profiles: Dict[str, Profile] = {}
        self.load()

    def _mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def reload_if_changed(self):
        if self._mtime() != self._loaded_mtime:
            self.load()

    def load(self):
        self.profiles = {}
        self._loaded_mtime = self._mtime()
        try:
            data = json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            data = {}
        for d in data.get("profiles", []):
            p = Profile(**d)
            self.profiles[p.id] = p

    def save(self):
        _atomic_json(self.path, {"profiles": [asdict(p) for p in self.profiles.values()]})
        self._loaded_mtime = self._mtime()

    def merge_discovered(self, found: List[Profile], complete_backends=()) -> bool:
        """Add newly discovered profiles; keep user edits (name, user_* params) of existing ones.

        Profiles of a backend in `complete_backends` (its discovery succeeded) that it no longer
        reports are removed — the VPN was deleted.
        """
        self.reload_if_changed()
        changed = False
        found_ids = {p.id for p in found}
        for pid in [pid for pid, p in self.profiles.items()
                    if p.backend in complete_backends and pid not in found_ids]:
            del self.profiles[pid]
            changed = True
        for p in found:
            cur = self.profiles.get(p.id)
            if cur is None:
                self.profiles[p.id] = p
                changed = True
                continue
            merged = {**p.params, **{k: v for k, v in cur.params.items() if k.startswith("user_") or k == "dns"}}
            if cur.params != merged:
                cur.params = merged
                changed = True
        if changed:
            self.save()
        return changed

    def rename(self, pid: str, name: str):
        name = name.strip()
        if not name:
            raise ValueError("пустое имя")
        self.reload_if_changed()
        self.profiles[pid].name = name
        self.save()

    def visible(self) -> List[Profile]:
        return [p for p in self.profiles.values() if not p.hidden and p.backend in BACKENDS]


DEFAULT_STATE = {
    "desired": None,                 # profile id the agent keeps connected (None = stay disconnected)
    "killswitch": True,              # internet only through a VPN
    "lan": "auto",   # always reachable: "auto" = directly connected subnets, or an explicit list of CIDRs
    "base_dns": ["1.1.1.1"],
}


class StateFile:
    """Small JSON shared by GUI, CLI and agent (~/.config/vpn-tray/state.json)."""

    def __init__(self, path: Path = None):
        self.path = path or CONFIG_DIR / "state.json"

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return {**DEFAULT_STATE, **data}

    def update(self, **kw) -> dict:
        data = self.read()
        data.update(kw)
        _atomic_json(self.path, data)
        return data


def runtime_dir() -> Path:
    d = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "vpn-tray"
    d.mkdir(parents=True, exist_ok=True)
    return d


class OpLock:
    """Inter-process (flock) + re-entrant in-process lock: one connect/disconnect at a time."""

    def __init__(self):
        self._rlock = threading.RLock()
        self._depth = 0
        self._fh = None

    def acquire(self, timeout: float = 120) -> bool:
        end = time.monotonic() + timeout
        if not self._rlock.acquire(timeout=max(0.0, timeout)):
            return False
        if self._depth == 0:
            fh = open(runtime_dir() / "op.lock", "w")
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= end:
                        fh.close()
                        self._rlock.release()
                        return False
                    time.sleep(0.2)
            self._fh = fh
        self._depth += 1
        return True

    def release(self):
        self._depth -= 1
        if self._depth == 0 and self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        self._rlock.release()

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("другая операция с VPN ещё выполняется")
        return self

    def __exit__(self, *a):
        self.release()


# ---------------------------------------------------------------- manager

class Manager:
    def __init__(self, store: ProfileStore = None, state: StateFile = None):
        self.store = store or ProfileStore()
        self.state = state or StateFile(self.store.path.parent / "state.json")
        self.lock = OpLock()
        self._last: Dict[str, tuple] = {}     # pid -> (t, rx, tx)
        self._pending: Dict[str, str] = {}    # pid -> transitional state

    def discover(self):
        found, complete = [], []
        for b in BACKENDS.values():
            try:
                if b.available():
                    found.extend(b.discover())
                    complete.append(b.key)
            except Exception as e:  # discovery must never kill the app
                print(f"discover {b.key}: {e}")
        self.store.merge_discovered(found, complete)

    def profiles(self) -> List[Profile]:
        self.store.reload_if_changed()
        return self.store.visible()

    def get(self, pid: str) -> Profile:
        self.store.reload_if_changed()
        return self.store.profiles[pid]

    # transitional states are shared between processes (tray, CLI, agent, plasmoid commands)
    def _pending_file(self) -> Path:
        return runtime_dir() / "pending.json"

    def _read_pending(self) -> Dict[str, str]:
        try:
            data = json.loads(self._pending_file().read_text())
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {k: v["state"] for k, v in data.items() if now - v.get("ts", 0) < 180}

    def _set_pending(self, pid: str, state: Optional[str]):
        if state:
            self._pending[pid] = state
        else:
            self._pending.pop(pid, None)
        try:
            data = json.loads(self._pending_file().read_text())
        except (OSError, ValueError):
            data = {}
        if state:
            data[pid] = {"state": state, "ts": time.time(), "pid": os.getpid()}
        else:
            data.pop(pid, None)
        _atomic_json(self._pending_file(), data)

    def raw_status(self, pid: str) -> Status:
        """Backend status without rate bookkeeping or transitional overlay."""
        p = self.get(pid)
        try:
            return BACKENDS[p.backend].status(p)
        except Exception as e:
            return Status(state=State.ERROR, detail=str(e))

    def status(self, pid: str) -> Status:
        st = self.raw_status(pid)
        pending = self._pending.get(pid) or self._read_pending().get(pid)
        if pending == State.CONNECTING and st.state != State.CONNECTED:
            st.state = State.CONNECTING
        elif pending == State.DISCONNECTING and st.state != State.DISCONNECTED:
            st.state = State.DISCONNECTING
        now = time.monotonic()
        prev = self._last.get(pid)
        if st.state == State.CONNECTED and prev and prev[0] < now:
            dt = now - prev[0]
            st.rx_rate = max(0.0, (st.rx_bytes - prev[1]) / dt)
            st.tx_rate = max(0.0, (st.tx_bytes - prev[2]) / dt)
        self._last[pid] = (now, st.rx_bytes, st.tx_bytes)
        return st

    def all_status(self) -> Dict[str, Status]:
        return {p.id: self.status(p.id) for p in self.profiles()}

    def _wait(self, pid: str, want: str, timeout: float) -> bool:
        p = self.get(pid)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if BACKENDS[p.backend].status(p).state == want:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def connect(self, pid: str, timeout: float = 45, log: Callable[[str], None] = print,
                remember: bool = True):
        """Connect pid; with remember=True it becomes the VPN the agent keeps alive."""
        p = self.get(pid)
        b = BACKENDS[p.backend]
        if remember:
            self.state.update(desired=pid)
        with self.lock:
            self._set_pending(pid, State.CONNECTING)
            try:
                if b.status(p).state == State.CONNECTED:
                    return
                # Full-tunnel VPNs fight over routing: while two are up, the new one's handshake can
                # leave through the old tunnel and stall for up to ~30 s. So: break before make.
                # The kill switch keeps the short gap leak-free; on failure the previous VPN returns.
                previous = []
                if p.full_tunnel:
                    for o in self.profiles():
                        if o.id != pid and o.full_tunnel and \
                                BACKENDS[o.backend].status(o).state == State.CONNECTED:
                            log(f"отключаю «{o.name}» (одновременно может работать только один full-tunnel VPN)")
                            self.disconnect(o.id, timeout=timeout, remember=False)
                            previous.append(o.id)
                try:
                    b.connect(p)
                    if not self._wait(pid, State.CONNECTED, timeout):
                        raise RuntimeError(f"«{p.name}» не подключился за {int(timeout)} с")
                except Exception:
                    for oid in previous:
                        log(f"возвращаю «{self.get(oid).name}»")
                        try:
                            BACKENDS[self.get(oid).backend].connect(self.get(oid))
                            self._wait(oid, State.CONNECTED, timeout)
                        except Exception as e2:
                            log(f"не удалось вернуть «{self.get(oid).name}»: {e2}")
                    if remember and previous:
                        self.state.update(desired=previous[0])
                    raise
            finally:
                self._set_pending(pid, None)

    def disconnect(self, pid: str, timeout: float = 30, remember: bool = True):
        p = self.get(pid)
        if remember and self.state.read().get("desired") == pid:
            self.state.update(desired=None)
        with self.lock:
            self._set_pending(pid, State.DISCONNECTING)
            try:
                BACKENDS[p.backend].disconnect(p)
                if not self._wait(pid, State.DISCONNECTED, timeout):
                    raise RuntimeError(f"«{p.name}» не отключился за {int(timeout)} с")
            finally:
                self._set_pending(pid, None)

    def rename(self, pid: str, name: str):
        self.store.rename(pid, name)

    # -- kill switch -------------------------------------------------------
    def endpoints(self) -> List[str]:
        """Every VPN server (host and last known IP) the kill switch must let through."""
        eps = set()
        for p in self.profiles():
            try:
                st = BACKENDS[p.backend].status(p)
            except Exception:
                continue
            for v in (st.server_host, st.server_ip):
                if v:
                    eps.add(v)
        return sorted(eps)

    def killswitch_status(self) -> tuple:
        out = helper("killswitch-status").split("\n")
        return out[0].strip() == "on", (out[1].split() if len(out) > 1 else [])

    def lan_cidrs(self) -> List[str]:
        lan = self.state.read().get("lan", "auto")
        if lan != "auto":
            return list(lan)
        return connected_subnets()

    def apply_killswitch(self, on: bool = None) -> str:
        st = self.state.read()
        on = st["killswitch"] if on is None else on
        if on:
            helper("dns-base", *st["base_dns"])
            return helper("killswitch-on", ",".join(self.lan_cidrs()), *self.endpoints()).strip()
        return helper("killswitch-off").strip()


VPN_IFACE_PREFIXES = ("awg", "wg", "happ", "tun", "tap", "ppp", "tailscale", "docker", "br-", "veth", "virbr")


def connected_subnets() -> List[str]:
    """IPv4 subnets of physical/LAN interfaces (VPN and container bridges excluded)."""
    import ipaddress
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    nets = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1].startswith(VPN_IFACE_PREFIXES):
            continue
        try:
            net = str(ipaddress.ip_interface(parts[3]).network)
        except ValueError:
            continue
        if net not in nets:
            nets.append(net)
    return nets or ["192.168.0.0/16"]


class SingleInstance:
    def __init__(self, name="vpn-tray"):
        self.fh = open(runtime_dir() / f"{name}.lock", "w")

    def acquire(self) -> bool:
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def human_rate(bps: float) -> str:
    return human_bytes(bps) + "/с"


def human_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_duration(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
