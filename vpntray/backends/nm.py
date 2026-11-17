# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""NetworkManager connections: OpenVPN / L2TP / PPTP / OpenConnect / strongSwan (via NM plugins) and WireGuard.

Anything you import into NM (`nmcli connection import type openvpn file x.ovpn`,
`nmcli connection import type wireguard file wg1.conf`, KDE network settings...) shows up here.
"""
from __future__ import annotations

import shutil

from ..core import (Backend, Profile, State, Status, iface_counters, iface_exists, iface_up_since,
                    register, resolve, run, split_hostport)

VPN_TYPES = {"vpn", "wireguard"}


def nm_get(uuid: str, *fields: str) -> dict:
    """Named field values. (`nmcli -g` prints values in nmcli's own order and drops empty ones,
    so positional parsing is wrong — use `-t -f` and match by name.)"""
    p = run(["nmcli", "-t", "-f", ",".join(fields), "connection", "show", uuid], check=False)
    res = {f: "" for f in fields}
    for line in p.stdout.splitlines():
        k, sep, v = line.partition(":")
        if sep and k in res:
            res[k] = v.replace("\\:", ":")
    return res


def parse_vpn_data(data: str) -> dict:
    # "remote = vpn.example.com:1194, dev = tun, ..."  (nmcli escapes ',' inside values as '\,')
    res = {}
    for item in data.replace("\\,", "\0").split(","):
        k, _, v = item.replace("\0", ",").partition("=")
        if k.strip():
            res[k.strip()] = v.strip()
    return res


@register
class NetworkManagerBackend(Backend):
    key = "nm"
    title = "NetworkManager"

    def available(self) -> bool:
        return shutil.which("nmcli") is not None

    def discover(self):
        out = run(["nmcli", "-t", "-f", "NAME,UUID,TYPE", "connection", "show"]).stdout
        res = []
        for line in out.splitlines():
            name, uuid, typ = (line.replace("\\:", "\0").split(":") + ["", "", ""])[:3]
            name = name.replace("\0", ":")
            if typ not in VPN_TYPES:
                continue
            never_default = nm_get(uuid, "ipv4.never-default")["ipv4.never-default"]
            res.append(Profile(id=f"nm:{uuid}", backend=self.key, name=name,
                               params={"uuid": uuid, "type": typ},
                               full_tunnel=never_default != "yes"))
        return res

    def connect(self, p: Profile):
        run(["nmcli", "connection", "up", "uuid", p.params["uuid"]], timeout=90)

    def disconnect(self, p: Profile):
        run(["nmcli", "connection", "down", "uuid", p.params["uuid"]], timeout=60)

    def status(self, p: Profile) -> Status:
        uuid, typ = p.params["uuid"], p.params["type"]
        fields = ["GENERAL.STATE", "GENERAL.IP-IFACE", "connection.interface-name"]
        if typ == "vpn":
            fields += ["vpn.service-type", "vpn.data"]
        f = nm_get(uuid, *fields)
        state, iface, ifname = f["GENERAL.STATE"], f["GENERAL.IP-IFACE"], f["connection.interface-name"]
        svc, data = f.get("vpn.service-type", ""), f.get("vpn.data", "")
        st = Status(iface=iface or ifname)
        if typ == "wireguard":
            st.protocol = "WireGuard / UDP (NetworkManager)"
        else:
            st.protocol = (svc.rsplit(".", 1)[-1] or "vpn").upper() + " (NetworkManager)"
            d = parse_vpn_data(data)
            host, port = split_hostport((d.get("remote") or d.get("gateway") or "").split(",")[0].split(" ")[0])
            st.server_host, st.server_port = host, port
            st.server_ip = resolve(host) if host else ""
        if p.params.get("user_protocol"):
            st.protocol = p.params["user_protocol"]
        state = state.lower()
        if state == "activated":
            st.state = State.CONNECTED
        elif state == "activating":
            st.state = State.CONNECTING
        elif state == "deactivating":
            st.state = State.DISCONNECTING
        if st.state == State.CONNECTED and iface_exists(st.iface):
            st.rx_bytes, st.tx_bytes = iface_counters(st.iface)
            st.since = iface_up_since(st.iface)
        return st
