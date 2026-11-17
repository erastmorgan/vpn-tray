# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""vpn-tray CLI: same engine as the tray, handy for scripts and e2e tests.

  vpn-tray-cli list [--json]
  vpn-tray-cli up ID | down ID
  vpn-tray-cli rename ID "New name"
  vpn-tray-cli set ID KEY VALUE        (user_protocol, dns, full_tunnel, hidden)
  vpn-tray-cli watch [SECONDS]
  vpn-tray-cli killswitch on|off|status
  vpn-tray-cli adopt                   (remember the currently connected VPN for auto-reconnect)
  vpn-tray-cli agent                   (agent heartbeat)
  vpn-tray-cli extip                   (external IP as seen from the internet)
  vpn-tray-cli popup                   (open the Plasma applet popup — bind it to a shortcut)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from . import backends  # noqa: F401
from .core import STATE_RU, Manager, State, human_bytes, human_rate, runtime_dir


def _row(p, st):
    srv = st.server_ip or st.server_host
    if st.server_port:
        srv += f":{st.server_port}"
    return (f"{p.id:<22} {p.name:<28} {STATE_RU[st.state]:<14} {st.protocol:<44} {srv:<22} "
            f"↓{human_rate(st.rx_rate):>11} ↑{human_rate(st.tx_rate):>11}  "
            f"Σ↓{human_bytes(st.rx_bytes)} Σ↑{human_bytes(st.tx_bytes)}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpn-tray-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    for c in ("up", "down"):
        sub.add_parser(c).add_argument("id")
    rn = sub.add_parser("rename")
    rn.add_argument("id")
    rn.add_argument("name")
    st = sub.add_parser("set")
    st.add_argument("id")
    st.add_argument("key", choices=["user_protocol", "dns", "full_tunnel", "hidden"])
    st.add_argument("value")
    w = sub.add_parser("watch")
    w.add_argument("seconds", nargs="?", type=float, default=5)
    k = sub.add_parser("killswitch")
    k.add_argument("mode", choices=["on", "off", "status"])
    sub.add_parser("adopt")
    sub.add_parser("agent")
    sub.add_parser("extip")
    sub.add_parser("popup")
    a = ap.parse_args(argv)
    if a.cmd == "popup":
        (runtime_dir() / "popup").touch()   # the applet polls for it and opens its popup
        return 0
    if a.cmd == "extip":
        import urllib.request
        for url in ("https://ifconfig.me/ip", "https://api.ipify.org"):
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    print(r.read().decode().strip())
                    return 0
            except Exception:
                continue
        print("недоступен", file=sys.stderr)
        return 1

    m = Manager()
    if a.cmd != "agent":
        m.discover()
    try:
        if a.cmd == "list":
            sts = m.all_status()
            if a.json:
                print(json.dumps([{"profile": asdict(p), "status": asdict(sts[p.id])} for p in m.profiles()],
                                 ensure_ascii=False, indent=1))
            else:
                for p in m.profiles():
                    print(_row(p, sts[p.id]))
        elif a.cmd == "up":
            m.connect(a.id)
            print(f"connected: {m.get(a.id).name}")
        elif a.cmd == "down":
            m.disconnect(a.id)
            print(f"disconnected: {m.get(a.id).name}")
        elif a.cmd == "rename":
            m.rename(a.id, a.name)
            print(f"renamed: {a.id} -> {a.name}")
        elif a.cmd == "set":
            p = m.get(a.id)
            if a.key == "dns":
                p.params["dns"] = [x for x in a.value.replace(",", " ").split() if x]
            elif a.key in ("full_tunnel", "hidden"):
                setattr(p, a.key, a.value.lower() in ("1", "true", "yes", "on"))
            else:
                p.params[a.key] = a.value
            m.store.save()
            print("ok")
        elif a.cmd == "killswitch":
            if a.mode == "status":
                on, servers = m.killswitch_status()
                print(f"kill switch: {'on' if on else 'off'} (в настройках: "
                      f"{'on' if m.state.read()['killswitch'] else 'off'}); серверы VPN: {' '.join(servers)}")
            else:
                m.state.update(killswitch=a.mode == "on")
                print(m.apply_killswitch(a.mode == "on"))
        elif a.cmd == "adopt":
            st = m.state.read()
            if not st.get("desired"):
                on = [p.id for p in m.profiles() if p.full_tunnel and m.status(p.id).state == State.CONNECTED]
                if on:
                    m.state.update(desired=on[0])
            print(f"desired: {m.state.read().get('desired')}")
        elif a.cmd == "agent":
            try:
                hb = json.loads((runtime_dir() / "agent.json").read_text())
                print(f"agent: heartbeat {time.time() - hb['ts']:.1f}s ago, {hb}")
            except (OSError, ValueError, KeyError):
                print("agent: нет heartbeat")
                return 1
        elif a.cmd == "watch":
            end = time.monotonic() + a.seconds
            while time.monotonic() < end:
                sts = m.all_status()
                print(time.strftime("%H:%M:%S"))
                for p in m.profiles():
                    print("  " + _row(p, sts[p.id]))
                time.sleep(1)
    except KeyError as e:
        print(f"нет такого VPN: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
