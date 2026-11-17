# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""AmneziaWG / WireGuard tunnels managed by awg-quick@ / wg-quick@ systemd units."""
from __future__ import annotations

import random
import time

from ..core import (Backend, Profile, State, Status, helper, iface_counters, iface_exists,
                    iface_up_since, register, resolve, split_hostport, unit_started_at)

PROTO = {
    "awg": "AmneziaWG (WireGuard + обфускация) / UDP",
    "wg": "WireGuard / UDP",
}
TITLE = {"awg": "AmneziaWG", "wg": "WireGuard"}


@register
class WgQuickBackend(Backend):
    key = "wgquick"
    title = "AmneziaWG / WireGuard (wg-quick)"

    def discover(self):
        out = helper("list-configs")
        res = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name, kind = parts[0], parts[1]
            endpoint = parts[2] if len(parts) > 2 else ""
            res.append(Profile(
                id=f"{kind}:{name}", backend=self.key,
                name=f"{TITLE.get(kind, kind)} · {name}",
                params={"kind": kind, "iface": name, "endpoint": endpoint},
            ))
        return res

    def connect(self, p: Profile, attempts: int = 5):
        # awg-quick/wg-quick can lose a race with other hooks touching the same routes
        # (e.g. a NetworkManager dispatcher script) and fail with "RTNETLINK: File exists";
        # a retry a moment later succeeds.
        for i in range(attempts):
            try:
                helper("up", p.params["kind"], p.params["iface"])
                return
            except RuntimeError:
                if i == attempts - 1:
                    raise
                time.sleep(0.7 + random.random() * 1.5)   # jitter: step out of the other hook's way

    def disconnect(self, p: Profile):
        helper("down", p.params["kind"], p.params["iface"])

    def status(self, p: Profile) -> Status:
        iface = p.params["iface"]
        host, port = split_hostport(p.params.get("endpoint", ""))
        st = Status(protocol=p.params.get("user_protocol") or PROTO.get(p.params["kind"], p.params["kind"]),
                    server_host=host, server_port=port, iface=iface)
        st.server_ip = resolve(host)
        if iface_exists(iface):
            st.state = State.CONNECTED
            st.rx_bytes, st.tx_bytes = iface_counters(iface)
            unit = f"{'awg' if p.params['kind'] == 'awg' else 'wg'}-quick@{iface}.service"
            st.since = iface_up_since(iface, lambda: unit_started_at(unit))
        return st
