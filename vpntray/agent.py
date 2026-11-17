# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""vpn-tray-agent: headless supervisor (systemd --user service, Restart=always, watchdog).

* keeps the "desired" VPN connected: reconnects with exponential backoff after any failure
  (tunnel dropped, Happ/xray crashed, awg-quick stopped, ...);
* health-probes the tunnel and re-establishes it when traffic stops flowing;
* keeps the kill switch (nftables) in place and its VPN-server allow-list up to date;
* publishes a heartbeat the tray shows ("агент работает").
The GUI may die at any time — the agent and the kernel kill switch keep working without it.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.request
from dataclasses import asdict

from . import backends  # noqa: F401
from .core import Manager, State, runtime_dir

TICK = 1.0
KS_CHECK_EVERY = 15.0
PROBE_EVERY = 20.0
PROBE_FAILS_TO_RECONNECT = 3
BACKOFF_MIN, BACKOFF_MAX = 5.0, 120.0
PROBE_URL = "https://1.1.1.1/cdn-cgi/trace"   # by IP: works even while DNS is broken


def log(msg: str):
    print(msg, flush=True)


def sd_notify(msg: str):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), addr)
    except OSError:
        pass


def probe(timeout: float = 6) -> bool:
    try:
        with urllib.request.urlopen(PROBE_URL, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status == 200
    except Exception:
        return False


class Agent:
    def __init__(self, m: Manager = None):
        self.m = m or Manager()
        self.backoff = BACKOFF_MIN
        self.next_try = 0.0
        self.next_ks = 0.0
        self.next_probe = 0.0
        self.probe_fails = 0
        self.ks_endpoints = None
        self.last = "старт"
        self.next_discover = 0.0
        self.ks_active = None

    def heartbeat(self, st: dict):
        data = {"ts": time.time(), "pid": os.getpid(), "desired": st.get("desired"),
                "killswitch": st.get("killswitch"), "last": self.last, "probe_fails": self.probe_fails,
                "next_try_in": max(0, round(self.next_try - time.monotonic()))}
        self._write("agent.json", data)

    @staticmethod
    def _write(name: str, data):
        p = runtime_dir() / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, p)

    def publish(self, pm: Manager):
        """Full snapshot for the Plasma applet (read every second, no Python start-up per poll).

        Runs in its own thread with its own Manager, so it keeps ticking while the main loop
        is busy reconnecting a VPN."""
        st = pm.state.read()
        vpns = []
        for p in pm.profiles():
            s = asdict(pm.status(p.id))
            s.update(id=p.id, name=p.name, backend=p.backend, full_tunnel=p.full_tunnel)
            vpns.append(s)
        self._write("status.json", {
            "ts": time.time(), "desired": st.get("desired"), "killswitch": bool(st.get("killswitch")),
            "killswitch_active": self.ks_active, "agent_last": self.last, "vpns": vpns})

    def ensure_killswitch(self, st: dict):
        now = time.monotonic()
        if st["killswitch"]:
            eps = self.m.endpoints()
            if now < self.next_ks and eps == self.ks_endpoints:
                return
            self.next_ks = now + KS_CHECK_EVERY
            on, servers = self.m.killswitch_status()
            self.ks_active = on
            if not on or eps != self.ks_endpoints:
                res = self.m.apply_killswitch(True)
                self.ks_endpoints = eps
                self.ks_active = True
                if not on:
                    self.last = "kill switch восстановлен"
                    log(f"kill switch applied: {res}")
        else:
            if now < self.next_ks:
                return
            self.next_ks = now + KS_CHECK_EVERY
            on, _ = self.m.killswitch_status()
            if on:
                log(f"kill switch off: {self.m.apply_killswitch(False)}")
            self.ks_active = False
            self.ks_endpoints = None

    def ensure_desired(self, st: dict):
        pid = st.get("desired")
        if not pid or pid not in self.m.store.profiles:
            self.probe_fails = 0
            return
        now = time.monotonic()
        state = self.m.raw_status(pid).state
        pend = self.m._read_pending().get(pid)
        if pend and state != State.CONNECTED:
            state = pend
        if state == State.CONNECTED:
            self.backoff = BACKOFF_MIN
            if now >= self.next_probe:
                self.next_probe = now + PROBE_EVERY
                if probe():
                    self.probe_fails = 0
                else:
                    self.probe_fails += 1
                    log(f"probe failed ({self.probe_fails}/{PROBE_FAILS_TO_RECONNECT}) for {pid}")
                    self.next_probe = now + 5
            if self.probe_fails >= PROBE_FAILS_TO_RECONNECT:
                self._locked(lambda: (self.m.disconnect(pid, remember=False),
                                      self.m.connect(pid, remember=False, log=log)),
                             f"туннель «{pid}» не пропускал трафик — переподключён")
                self.probe_fails = 0
            return
        if state in (State.CONNECTING, State.DISCONNECTING) or now < self.next_try:
            return
        log(f"desired {pid} is {state} — reconnecting")
        ok = self._locked(lambda: self.m.connect(pid, remember=False, log=log),
                          f"«{pid}» переподключён автоматически")
        if ok:
            self.backoff = BACKOFF_MIN
            self.next_probe = time.monotonic() + PROBE_EVERY
        elif ok is False:
            self.next_try = time.monotonic() + self.backoff
            log(f"retry in {self.backoff:.0f}s")
            self.backoff = min(self.backoff * 2, BACKOFF_MAX)

    def _locked(self, fn, success_msg):
        """Run fn under the op lock without waiting (user action in progress => skip). None = skipped."""
        if not self.m.lock.acquire(timeout=0):
            return None
        try:
            # re-check under the lock: the user may have changed their mind meanwhile
            fn()
            self.last = success_msg
            log(success_msg)
            return True
        except Exception as e:
            self.last = f"ошибка: {e}"
            log(f"error: {e}")
            return False
        finally:
            self.m.lock.release()

    def tick(self):
        st = self.m.state.read()
        if time.monotonic() >= self.next_discover:
            self.next_discover = time.monotonic() + 300
            self.m.store.load()
            self.m.discover()
        for step in (self.ensure_killswitch, self.ensure_desired):
            try:
                step(st)
            except Exception as e:  # one failing step must not stop the others
                self.last = f"ошибка: {e}"
                log(f"{step.__name__}: {e}")
        self.heartbeat(st)

    def _publisher(self):
        pm = Manager()
        while True:
            t0 = time.monotonic()
            try:
                self.publish(pm)
            except Exception as e:
                log(f"publish: {e}")
            time.sleep(max(0.2, 1.0 - (time.monotonic() - t0)))

    def run(self):
        log("vpn-tray-agent started")
        self.m.store.load()
        threading.Thread(target=self._publisher, name="publisher", daemon=True).start()
        sd_notify("READY=1")
        while True:
            t0 = time.monotonic()
            self.tick()
            sd_notify("WATCHDOG=1")
            time.sleep(max(0.2, TICK - (time.monotonic() - t0)))


def main():
    try:
        Agent().run()
    except KeyboardInterrupt:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
