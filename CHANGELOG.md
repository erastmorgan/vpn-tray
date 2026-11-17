# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

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
