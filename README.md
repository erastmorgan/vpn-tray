# vpn-tray

**VPN manager for the Linux system tray** — list VPNs, connect/disconnect, status, speed, traffic,
protocol, server IP, rename; a **kill switch** (internet only through a VPN) and **self-healing**.
Works with different VPN types at once and is easy to extend.

[Русская версия](README.ru.md) · License: [MIT](LICENSE)

* KDE Plasma 6: a native system-tray applet — the popup opens next to the icon, just like the stock
  *Networks* applet, and follows the Plasma light/dark theme.
* Other desktops: a PyQt5 tray icon + window (follows the KDE colour scheme if present).
* No protocol is reinvented: vpn-tray drives existing clients.

| Backend | VPN types | Driven through |
|---|---|---|
| `wgquick` | AmneziaWG, WireGuard | `awg-quick@IF` / `wg-quick@IF` systemd units, configs in `/etc/amnezia/amneziawg/`, `/etc/wireguard/` |
| `happ` | VLESS, VMess, Trojan, Shadowsocks, mKCP… (Xray) via the official **[Happ](https://happ.su)** client | Happ's official deeplinks `happ://connect` / `happ://disconnect`; Happ runs in the background, its window is kept hidden |
| `nm` | OpenVPN, WireGuard, L2TP, PPTP, OpenConnect, strongSwan… | NetworkManager (`nmcli connection up/down`) |

## Features

* **Status at a glance** — tray icon: connected / connecting / disconnected / agent down.
* **Popup** — every VPN with state and live ↓/↑ speed; *Connect / Disconnect*; expand a row for
  protocol, server (IP:port and host), traffic, uptime, interface, auto-reconnect; inline rename.
* **Kill switch** (nftables, IPv4 + IPv6) — traffic leaves the box only through a VPN interface,
  to the LAN, or to the VPN servers themselves. Fail-closed: survives crashes, loaded at boot before
  the network.
* **Self-healing** — the agent keeps the chosen VPN up: reconnects with backoff (5 → 120 s),
  probes the tunnel every 20 s and rebuilds it after 3 failed probes, restores missing firewall rules.
  Agent, Happ and the fallback tray run as systemd user services with `Restart=always`
  (agent with a watchdog). The Plasma applet re-attaches automatically.
* **One full-tunnel VPN at a time** — switching is *break-before-make* (the kill switch keeps the
  ~1 s gap leak-free); if the new VPN fails to come up, the previous one is restored.

## Requirements

* Linux with systemd, `nftables`, `openresolv` (`resolvconf`), `sudo`, Python ≥ 3.9.
* KDE Plasma 6 for the applet (`kpackagetool6`), or any tray supporting StatusNotifierItem /
  XEmbed + `PyQt5` for the fallback icon. PyQt5 is also needed for the details window.
* For the backends you use: `amneziawg-tools` (+ `amneziawg-go` or the kernel module),
  `wireguard-tools`, NetworkManager, Happ desktop for Linux.

Arch / Manjaro:

```bash
sudo pacman -S --needed python python-pyqt5 nftables openresolv wireguard-tools networkmanager
# optional: amneziawg-tools amneziawg-go (AUR), Happ: https://github.com/Happ-proxy/happ-desktop/releases
```

Debian / Ubuntu:

```bash
sudo apt install python3 python3-pyqt5 nftables openresolv wireguard-tools network-manager
```

## Install

```bash
git clone https://github.com/erastmorgan/vpn-tray.git ~/Development/vpn-tray
cd ~/Development/vpn-tray
make test        # optional: unit tests
./install.sh     # asks for your sudo password once
```

`install.sh` (safe to re-run for upgrades) installs:

| What | Where |
|---|---|
| root helper (the only thing run as root) | `/usr/local/libexec/vpn-tray-helper` |
| sudoers rule (NOPASSWD for that helper only) | `/etc/sudoers.d/vpn-tray` |
| boot-time kill switch | `/etc/systemd/system/vpn-tray-killswitch.service` |
| launchers | `~/.local/bin/vpn-tray{,-cli,-agent,-hide-window}` |
| agent (auto-reconnect, kill switch, status) | `~/.config/systemd/user/vpn-tray-agent.service` |
| Happ in the background (if Happ is installed) | `~/.config/systemd/user/vpn-tray-happ.service` |
| KWin rule: Happ's window never steals focus (keeps the popup open) | `~/.config/kwinrulesrc` → `[vpn-tray-happ]` |
| Plasma 6 applet, added to the system tray | `~/.local/share/plasma/plasmoids/io.github.erastmorgan.vpntray` |
| fallback tray icon (non-Plasma desktops) | `~/.config/systemd/user/vpn-tray.service` |

Options: `./install.sh --no-killswitch` installs with the kill switch off.
The code is run from the cloned directory — keep it (or re-run `install.sh` after moving it).

**Uninstall:** `./uninstall.sh` (turns the kill switch off first; VPN configs and Happ are untouched).

## Build / packaging

There is nothing to compile (Python + QML). Packaging targets:

```bash
make lint        # syntax checks
make test        # unit tests (no root, no network)
make plasmoid    # dist/vpn-tray-<ver>.plasmoid — install with: kpackagetool6 -t Plasma/Applet -i <file>
make dist        # dist/vpn-tray-<ver>.tar.gz source release (git archive)
```

## Usage

* **Click** the shield in the tray → popup. *Connect* / *Disconnect* per VPN; the arrow expands
  details; *⋯ → Rename…*; *Kill switch* switch in the header; *Details…* opens the table window.
* **Right-click** the icon → kill switch, details.
* A VPN connected from vpn-tray becomes the one the agent keeps alive; disconnecting it means
  "stay without VPN" — with the kill switch on, the internet is then off by design.

CLI (same engine, handy for scripts):

```bash
vpn-tray-cli list                       # all VPNs with state, protocol, server, speed
vpn-tray-cli up happ:default            # connect (becomes the auto-reconnect target)
vpn-tray-cli down awg:awg0
vpn-tray-cli rename happ:default "Office VPN"
vpn-tray-cli set happ:default user_protocol "VLESS / mKCP"   # label shown as type/protocol
vpn-tray-cli killswitch status|on|off
vpn-tray-cli extip                      # external IP
vpn-tray-cli popup                      # open the applet popup (bind it to a global shortcut)
vpn-tray-cli agent                      # agent heartbeat
journalctl --user -u vpn-tray-agent -f  # agent log
```

### Adding VPNs

* **WireGuard / AmneziaWG:** put `wgN.conf` into `/etc/wireguard/` (or `awgN.conf` into
  `/etc/amnezia/amneziawg/`) → it appears within 5 minutes (or *Details… → Refresh list*).
* **OpenVPN etc.:** `nmcli connection import type openvpn file client.ovpn`.
* **Happ (Xray):** import the subscription / link into Happ once (`/opt/happ/bin/Happ 'happ://…'`);
  vpn-tray then controls the server selected in Happ.
* **New VPN type:** add `vpntray/backends/<name>.py` with a `Backend` subclass
  (`discover / connect / disconnect / status`) decorated with `@register`, import it in
  `vpntray/backends/__init__.py`, and allow any new root action in `helper/vpn-tray-helper`.

### Configuration

`~/.config/vpn-tray/state.json` (created on first change):

| Key | Default | Meaning |
|---|---|---|
| `killswitch` | `true` | internet only through a VPN |
| `lan` | `"auto"` | subnets always reachable; `"auto"` = directly connected subnets, or a list like `["192.168.1.0/24"]` |
| `base_dns` | `["1.1.1.1"]` | resolver used while no VPN is up (only root can reach it through the kill switch, e.g. VPN cores resolving their server) |
| `desired` | — | VPN the agent keeps connected (set by connect/disconnect) |

`~/.config/vpn-tray/profiles.json` holds the VPN list with your names. Per VPN `params.dns`
(Happ) sets the tunnel resolvers.

## Architecture

```
 Plasma applet (QML) ──reads── $XDG_RUNTIME_DIR/vpn-tray/status.json ◄── vpn-tray-agent (user service)
        │ actions                                                          │ keeps VPN up, probes,
        ▼                                                                  │ kill switch, status
 vpn-tray-cli ──► Manager ──► backends: wgquick │ happ │ nm ◄──────────────┘
                     │ sudo -n (NOPASSWD, one file)
                     ▼
        /usr/local/libexec/vpn-tray-helper (root, action whitelist)
             ├─ systemctl start/stop awg-quick@ / wg-quick@
             ├─ nftables  table inet vpntray  → /etc/vpn-tray/killswitch.nft (+ boot unit)
             └─ openresolv DNS for tunnels
```

Kill-switch rules (output and forward): accept `lo`, VPN interfaces (`awg*`, `wg*`, `happ*`,
`tun*`, `tap*`, `ppp*`, `tailscale*`), LAN subnets, VPN server IPs, DHCP, link-local/multicast,
DNS from root only; everything else is rejected.

## Tests

```bash
make test   # 20 unit tests: manager, exclusivity, rollback, agent self-healing, kill switch logic …
make e2e    # LIVE: really switches VPNs, breaks things on purpose, then restores the initial state
```

The live test needs the installed agent and two full-tunnel VPNs:
`VPN_TRAY_E2E_A=awg:awg0 VPN_TRAY_E2E_B=happ:default VPN_TRAY_E2E_LAN_GW=<router> make e2e`.
It checks external IP through each VPN, DNS, download speed and the speed meter, exclusivity,
no leaks while switching, kill switch (no internet/DNS without VPN, LAN reachable, flushed rules
restored), self-healing (tunnel dropped, Happ killed, AWG stopped behind its back, agent and tray
killed), Happ's window kept hidden, NetworkManager backend, rename persistence.

## Troubleshooting

* **No internet at all:** that's the kill switch with no VPN connected. Connect a VPN, or
  `vpn-tray-cli killswitch off`. Emergency (as root): `nft delete table inet vpntray; rm /etc/vpn-tray/killswitch.nft`.
* **Applet says "agent not responding":** `systemctl --user restart vpn-tray-agent`; logs:
  `journalctl --user -u vpn-tray-agent`.
* **`awg-quick` sometimes fails with `RTNETLINK answers: File exists`:** another hook (often a
  NetworkManager dispatcher script) adds the same route concurrently. vpn-tray retries; remove the
  duplicate hook to make switching instant.
* **Applet not in the tray:** System Tray Settings → Entries → *VPN* → *Always shown*, or re-run
  `install.sh`.

## Contributing

Issues and pull requests are welcome. Please run `make lint test` before submitting.
Security issues: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 erastmorgan. Third-party tools used at runtime are listed in
[THIRD-PARTY.md](THIRD-PARTY.md); none of them is bundled.
