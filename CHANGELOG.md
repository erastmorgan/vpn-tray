# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.3.0] — 2026-11-18

### Added
- Several VPNs through Happ: `vpn-tray-cli happ-import NAME` (link on stdin) adds a subscription as
  its own VPN `happ:NAME`. Happ cannot select a server from outside, so vpn-tray keeps a snapshot of
  Happ's state per VPN (`~/.config/vpn-tray/happ/NAME/`) and swaps it in on connect (Happ restarts in
  the background, ~5 s). The server already selected in Happ is kept as `happ:default`.

### Fixed
- Importing a subscription with a routing profile into a running Happ crashes Happ (4.7.2, 4.8.1)
  and the subscription is lost; `happ-import` imports on a cold start and saves via `happ://close`.

## [0.2.1] — 2026-11-18

### Fixed
- Applet: the expanded details of a VPN (speed, traffic, server, protocol, interface, uptime)
  were empty / never updated — the delegate id `item` was shadowed by `Loader.item` inside the
  expanded view. The uptime now ticks every second.
- Applet: the popup closed by itself on Connect / Disconnect involving Happ. Happ's window took
  focus for ~20 ms before being closed; a KWin window rule (`vpn-tray-happ`, focus stealing
  prevention "Extreme", added by `install.sh`, removed by `uninstall.sh`) stops that.

## [0.2.0] — 2026-11-17

### Added
- KDE Plasma 6 system-tray applet (`io.github.erastmorgan.vpntray`): the popup opens next to the
  icon like the stock *Networks* applet and follows the Plasma light/dark theme.
- Happ runs as a background user service (`vpn-tray-happ.service`); its window no longer pops up
  when switching VPNs (a short-lived KWin script closes it to its tray icon).
- The PyQt5 details window follows the KDE colour scheme (light/dark) live.
- `vpn-tray-cli popup`, `vpn-tray-cli extip`; `make` targets; `uninstall.sh`.
- Agent publishes `$XDG_RUNTIME_DIR/vpn-tray/status.json` every second for the applet.

### Changed
- Switching between full-tunnel VPNs is now *break-before-make* (the kill switch keeps the short gap
  leak-free); if the new VPN fails, the previous one is restored.
- Kill-switch LAN exceptions default to the directly connected subnets (`"lan": "auto"`).
- `awg-quick`/`wg-quick` start is retried with jitter when another hook races it for the same routes.

### Fixed
- Removed VPNs no longer stay in the list forever.
- NetworkManager status parsing (`nmcli -g` prints fields in its own order).
- Connection uptime for already running tunnels.

## [0.1.0] — 2026-11-17

### Added
- Tray icon + window: VPN list, connect/disconnect, status, speed, traffic, protocol, server IP, rename.
- Backends: AmneziaWG/WireGuard (`awg-quick`/`wg-quick`), Happ (Xray) via official deeplinks,
  NetworkManager (OpenVPN, WireGuard, L2TP, …).
- Kill switch (nftables, fail-closed, restored at boot), self-healing agent with watchdog.
- Root helper with an action whitelist, narrow sudoers rule.
