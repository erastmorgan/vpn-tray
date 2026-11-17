# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""LIVE end-to-end test on the real machine (the installed agent keeps running alongside).

Configure which VPNs to exercise (defaults match a box with an AmneziaWG `awg0` + Happ):
    VPN_TRAY_E2E_A=awg:awg0  VPN_TRAY_E2E_B=happ:default  VPN_TRAY_E2E_LAN_GW=<router ip>
A must be a VPN whose exit IP equals its server IP (WireGuard-like); B any other full-tunnel VPN.

Really switches VPNs, breaks things on purpose, checks that they heal, then restores the initial state.
    python3 tests/e2e_live.py            # everything
    python3 tests/e2e_live.py --no-nm    # skip the NetworkManager scenario

Covered:
  * each VPN: connect, state, external IP through the VPN, DNS, download + speed meter, server/protocol
  * full-tunnel exclusivity, rename persistence
  * kill switch: no internet without VPN (IPv4 + DNS), LAN still reachable, no leaks while switching VPNs,
    flushed nftables rules are restored by the agent
  * self-healing: Happ + xray killed -9, AWG stopped behind our back, agent killed -9, tray killed -9
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vpntray import backends, kwinhide  # noqa: E402,F401
from vpntray.core import Manager, ProfileStore, State, helper, human_rate, runtime_dir  # noqa: E402

AWG = os.environ.get("VPN_TRAY_E2E_A", "awg:awg0")
HAPP = os.environ.get("VPN_TRAY_E2E_B", "happ:default")


def _default_gw():
    out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if "via" in parts:
            return parts[parts.index("via") + 1]
    return ""


LAN_GW = os.environ.get("VPN_TRAY_E2E_LAN_GW") or _default_gw()
results = []


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def check(name, cond, info=""):
    results.append((name, bool(cond), info))
    log(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    return cond


def sh(*cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ext_ip(timeout=8):
    for url in ("https://ifconfig.me/ip", "https://api.ipify.org"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return ""


def direct_ip_reachable(timeout=6):
    """HTTPS to 1.1.1.1 by IP — needs no DNS."""
    try:
        with urllib.request.urlopen("https://1.1.1.1/cdn-cgi/trace", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def dns_ok(host="github.com"):
    try:
        return bool(socket.getaddrinfo(host, 443, socket.AF_INET))
    except OSError:
        return False


def wait_for(fn, timeout, step=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def visible_windows(cls="happ"):
    """Captions of non-minimised windows of a class, asked from KWin (works on Wayland)."""
    tag = f"VPNTRAYE2E{int(time.time() * 1000)}"
    js = runtime_dir() / "e2e-kwin.js"
    js.write_text("for (const w of workspace.windowList()) if (w.resourceClass.toString().toLowerCase() === %r"
                  " && !w.minimized) print(%r + ' ' + w.caption); print(%r + ' END');" % (cls, tag, tag))
    name = "vpntray-e2e-list"
    sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", name)
    r = sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
           "ss", str(js), name)
    if r.returncode != 0:
        return None
    sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "start")
    time.sleep(1)
    sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", name)
    out = sh("journalctl", "--user", "--since", "-15s", "-o", "cat").stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith(tag) and not ln.endswith(" END")]


class WindowWatch:
    """Traces a GUI client's windows through KWin while a switch is in progress.

    Records how long every window of the class stayed mapped (added -> removed). Our KWin script
    must close each one within a few frames and never leave one open."""

    def __init__(self, cls="happ"):
        self.cls, self.durations, self.left_open = cls, [], 0
        self.tag = f"VPNTRAYWW{int(time.time() * 1000)}"
        self.name = f"vpntray-e2e-trace-{self.tag}"

    def __enter__(self):
        js = runtime_dir() / f"{self.name}.js"
        js.write_text(
            "function isT(w){ return w && String(w.resourceClass).toLowerCase() === %r; }\n"
            "workspace.windowAdded.connect(function(w){ if (isT(w)) print(%r + ' added ' + Date.now()); });\n"
            "workspace.windowRemoved.connect(function(w){ if (isT(w)) print(%r + ' removed ' + Date.now()); });\n"
            % (self.cls, self.tag, self.tag))
        sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
           "ss", str(js), self.name)
        sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "start")
        return self

    def __exit__(self, *a):
        time.sleep(6)   # include the post-switch sweep
        sh("busctl", "--user", "call", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
           "s", self.name)
        added = None
        for ln in sh("journalctl", "--user", "--since", "-10min", "-o", "cat").stdout.splitlines():
            if not ln.startswith(self.tag):
                continue
            _, ev, ts = ln.split()
            if ev == "added":
                added = int(ts)
            elif added is not None:
                self.durations.append((int(ts) - added) / 1000)
                added = None
        self.left_open = 1 if added is not None else 0


def svc_pid(unit):
    return sh("systemctl", "--user", "show", unit, "-p", "MainPID", "--value").stdout.strip()


def measure(m: Manager, pid: str, nbytes=8_000_000):
    rates, done, got = [], threading.Event(), {"n": 0, "t": 0.0}

    def dl():
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(f"https://speed.cloudflare.com/__down?bytes={nbytes}",
                                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) vpn-tray-e2e"})
            with urllib.request.urlopen(req, timeout=60) as r:
                while chunk := r.read(65536):
                    got["n"] += len(chunk)
        except Exception as e:
            log(f"download error: {e}")
        got["t"] = time.monotonic() - t0
        done.set()

    m.status(pid)
    threading.Thread(target=dl, daemon=True).start()
    while not done.is_set():
        time.sleep(0.5)
        rates.append(m.status(pid).rx_rate)
    return got["n"], got["t"], max(rates or [0]), m.status(pid)


class LeakWatch:
    """Polls the external IP in the background; every IP seen must be a VPN exit."""

    def __init__(self):
        self.seen, self._stop = [], threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            ip = ext_ip(timeout=3)
            if ip:
                self.seen.append(ip)
            time.sleep(0.3)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self.t.join(10)


def scenario_vpn(m: Manager, pid: str, other, ips: dict):
    p = m.get(pid)
    log(f"=== {p.name} ({pid}) ===")
    t0 = time.monotonic()
    m.connect(pid, log=log)
    check(f"{pid}: connect", m.status(pid).state == State.CONNECTED, f"{time.monotonic() - t0:.1f}s")
    check(f"{pid}: becomes the auto-reconnect target", m.state.read()["desired"] == pid)
    if other:
        check(f"{pid}: exclusivity — {other} disconnected", m.status(other).state == State.DISCONNECTED)
    check(f"{pid}: DNS works", wait_for(dns_ok, 15))
    ip = ext_ip()
    ips[pid] = ip
    st = m.status(pid)
    check(f"{pid}: external IP present", bool(ip), ip)
    if pid == AWG:
        check(f"{pid}: external IP == VPN server IP", ip == st.server_ip, f"{ip} vs {st.server_ip}")
    if pid == HAPP and ips.get(AWG):
        check(f"{pid}: external IP differs from AWG", ip and ip != ips[AWG], f"{ip} vs {ips[AWG]}")
    check(f"{pid}: protocol shown", bool(st.protocol), st.protocol)
    check(f"{pid}: server shown", bool(st.server_ip), f"{st.server_host} -> {st.server_ip}:{st.server_port}")
    check(f"{pid}: uptime plausible", st.since and 0 <= time.time() - st.since < 24 * 3600 * 30,
          f"{time.time() - (st.since or 0):.0f}s")
    n, t, peak, st = measure(m, pid)
    check(f"{pid}: download through VPN", n > 1_000_000, f"{n} B in {t:.1f}s = {human_rate(n / t if t else 0)}")
    check(f"{pid}: speed meter saw traffic", peak > 50_000, f"peak {human_rate(peak)}")


def scenario_killswitch(m: Manager):
    log("=== kill switch ===")
    on, servers = m.killswitch_status()
    check("killswitch: active", on, " ".join(servers))
    cur = next((p.id for p in m.profiles() if m.status(p.id).state == State.CONNECTED), None)
    if cur:
        m.disconnect(cur)
    check("killswitch: no VPN connected", all(m.status(p.id).state != State.CONNECTED for p in m.profiles()))
    time.sleep(2)
    check("killswitch: internet by IP blocked", not direct_ip_reachable())
    check("killswitch: external IP unreachable", ext_ip(timeout=5) == "")
    check("killswitch: user DNS blocked", not dns_ok("example.org"))
    check("killswitch: LAN still reachable", sh("ping", "-c", "2", "-W", "2", LAN_GW).returncode == 0)
    time.sleep(8)
    check("agent: does not reconnect after an explicit disconnect",
          all(m.status(p.id).state != State.CONNECTED for p in m.profiles()))
    # somebody flushes the firewall -> agent must put it back
    helper("killswitch-off")
    check("killswitch: removed (simulated flush)", not m.killswitch_status()[0])
    check("agent: restored kill switch", wait_for(lambda: m.killswitch_status()[0], 30),
          "within 30s")
    check("killswitch: still blocked after restore", not direct_ip_reachable())
    return cur


def scenario_selfheal_happ(m: Manager):
    log("=== self-heal: Happ disconnected behind our back (happ://disconnect) ===")
    kwinhide.suppress("Happ", 10)    # the test itself must not leave Happ's window open
    subprocess.run(["/opt/happ/bin/Happ", "happ://disconnect"], capture_output=True, timeout=20)
    check("happ: tunnel dropped externally", wait_for(lambda: m.status(HAPP).state != State.CONNECTED, 20, 0.5))
    t0 = time.monotonic()
    ok = wait_for(lambda: m.status(HAPP).state == State.CONNECTED and direct_ip_reachable(), 120, 2)
    check("agent: Happ reconnected", ok, f"{time.monotonic() - t0:.0f}s")

    log("=== self-heal: Happ GUI killed -9 ===")
    old = sh("pgrep", "-x", "Happ").stdout.split()
    sh("pkill", "-9", "-x", "Happ")
    ok = wait_for(lambda: (p := sh("pgrep", "-x", "Happ").stdout.split()) and p != old, 30)
    check("systemd restarted Happ in the background", ok)
    time.sleep(6)
    check("restarted Happ shows no window", not visible_windows("happ"))
    t0 = time.monotonic()
    ok = wait_for(lambda: m.status(HAPP).state == State.CONNECTED and direct_ip_reachable(), 120, 2)
    check("happ: tunnel works after Happ kill -9", ok, f"{time.monotonic() - t0:.0f}s")


def scenario_selfheal_awg(m: Manager):
    log("=== self-heal: AWG stopped behind our back ===")
    wait_for(lambda: m.status(AWG).state == State.CONNECTED and direct_ip_reachable(), 60, 1)
    time.sleep(3)                       # let the agent finish whatever it was doing
    kind, iface = m.get(AWG).params["kind"], m.get(AWG).params["iface"]
    for _ in range(3):                  # systemd may cancel the stop if a start job races it
        try:
            helper("down", kind, iface)
            break
        except RuntimeError as e:
            log(f"stop raced: {e}")
            time.sleep(3)
    check("awg: stopped externally", m.status(AWG).state == State.DISCONNECTED)
    check("agent: AWG reconnected", wait_for(
        lambda: m.status(AWG).state == State.CONNECTED and direct_ip_reachable(), 60, 1))


def scenario_services():
    log("=== self-heal: services killed -9 ===")
    units = [u for u in ("vpn-tray-agent.service", "vpn-tray.service")
             if sh("systemctl", "--user", "is-active", u).stdout.strip() == "active"]
    for unit in units:
        old = svc_pid(unit)
        sh("kill", "-9", old)
        ok = wait_for(lambda: svc_pid(unit) not in ("", "0", old), 20)
        check(f"systemd restarted {unit}", ok, f"{old} -> {svc_pid(unit)}")
    ok = wait_for(lambda: time.time() - json.loads((runtime_dir() / "agent.json").read_text())["ts"] < 5, 20)
    check("agent heartbeat fresh after restart", ok)
    ok = wait_for(lambda: time.time() - json.loads((runtime_dir() / "status.json").read_text())["ts"] < 3, 20)
    check("status.json for the Plasma applet is fresh", ok)


def scenario_nm(m: Manager):
    name, pid = "vpntray-e2e", None
    sh("nmcli", "connection", "delete", name)
    r = sh("nmcli", "connection", "add", "type", "wireguard", "con-name", name, "ifname", "wg-e2e",
           "autoconnect", "no", "ipv4.method", "manual", "ipv4.addresses", "10.254.254.1/32",
           "ipv4.never-default", "yes", "ipv6.method", "disabled",
           "wireguard.private-key", sh("wg", "genkey").stdout.strip())
    if not check("nm: create test connection", r.returncode == 0, r.stderr.strip()):
        return
    try:
        m.discover()
        pid = next((p.id for p in m.profiles() if p.name == name), None)
        if not check("nm: discovered", pid is not None):
            return
        check("nm: split-tunnel detected (never-default)", not m.get(pid).full_tunnel)
        desired = m.state.read()["desired"]
        m.connect(pid, timeout=30, remember=False)
        st = m.status(pid)
        check("nm: connect", st.state == State.CONNECTED, f"iface={st.iface} proto={st.protocol}")
        check("nm: split-tunnel did not disturb the full-tunnel VPN",
              desired is None or m.status(desired).state == State.CONNECTED)
        m.disconnect(pid, remember=False)
        check("nm: disconnect", m.status(pid).state == State.DISCONNECTED)
    finally:
        sh("nmcli", "connection", "delete", name)
        if pid:
            m.store.reload_if_changed()
            m.store.profiles.pop(pid, None)
            m.store.save()


def main():
    m = Manager()
    check("helper via sudo -n", helper("ping").strip() == "ok")
    m.discover()
    ids = {p.id for p in m.profiles()}
    check("discovered current VPN (AmneziaWG awg0)", AWG in ids, ", ".join(sorted(ids)))
    check("discovered Happ VPN", HAPP in ids)
    initial_desired = m.state.read()["desired"]
    log(f"initial desired: {initial_desired}; states: { {i: m.status(i).state for i in ids} }")

    old = m.get(HAPP).name
    m.rename(HAPP, "e2e rename test")
    check("rename persisted", ProfileStore().profiles[HAPP].name == "e2e rename test")
    m.rename(HAPP, old)

    ips = {}
    try:
        scenario_vpn(m, AWG, None, ips)
        with LeakWatch() as lw, WindowWatch("happ") as ww:
            scenario_vpn(m, HAPP, AWG, ips)
        if m.get(HAPP).backend == "happ":
            longest = max(ww.durations or [0])
            check("happ: no window left open after switching", ww.left_open == 0)
            check("happ: any window it popped was closed within 300 ms", longest < 0.3,
                  f"{len(ww.durations)} popup(s), longest {longest * 1000:.0f} ms")
        vpn_ips = {ips.get(AWG), ips.get(HAPP)}
        leaks = sorted(set(lw.seen) - vpn_ips)
        check("no leaks while switching AWG -> Happ", not leaks, f"seen={sorted(set(lw.seen))} leaks={leaks}")
        scenario_selfheal_happ(m)
        with LeakWatch() as lw:
            scenario_vpn(m, AWG, HAPP, ips)
        leaks = sorted(set(lw.seen) - {ips.get(AWG), ips.get(HAPP)})
        check("no leaks while switching Happ -> AWG", not leaks, f"seen={sorted(set(lw.seen))} leaks={leaks}")
        scenario_selfheal_awg(m)
        scenario_killswitch(m)
        m.connect(AWG)
        check("internet back after reconnect", wait_for(direct_ip_reachable, 20))
        scenario_services()
        if "--no-nm" not in sys.argv:
            scenario_nm(m)
    except Exception as e:
        check("no exceptions", False, repr(e))
    finally:
        log("restoring initial state…")
        try:
            if initial_desired:
                m.connect(initial_desired)
            m.state.update(desired=initial_desired)
        except Exception as e:
            log(f"restore: {e}")
        log(f"final: { {p.id: m.status(p.id).state for p in m.profiles()} } desired={m.state.read()['desired']} "
            f"ext ip: {ext_ip()}")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for n, _, info in failed:
        print(f"  FAILED: {n} {info}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
