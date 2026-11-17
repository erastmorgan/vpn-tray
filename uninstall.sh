#!/bin/bash
# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
# Removes vpn-tray. VPN configs, Happ and its subscription are left untouched.
set -uo pipefail
APPLET_ID="io.github.erastmorgan.vpntray"
SUDO=(sudo); [[ -n "${SUDO_ASKPASS:-}" ]] && SUDO=(sudo -A)

systemctl --user disable --now vpn-tray-agent.service vpn-tray.service vpn-tray-happ.service 2>/dev/null
rm -f ~/.config/systemd/user/vpn-tray{,-agent,-happ}.service
systemctl --user daemon-reload
command -v kpackagetool6 >/dev/null && kpackagetool6 -t Plasma/Applet -r "$APPLET_ID" >/dev/null 2>&1
rm -f ~/.local/bin/vpn-tray{,-cli,-agent,-hide-window} ~/.local/share/applications/vpn-tray.desktop

# kill switch off FIRST (otherwise the box stays without internet), then the root parts
sudo -n /usr/local/libexec/vpn-tray-helper killswitch-off >/dev/null 2>&1 \
  || "${SUDO[@]}" sh -c 'nft delete table inet vpntray 2>/dev/null; rm -f /etc/vpn-tray/killswitch.nft'
"${SUDO[@]}" resolvconf -d vpntray.base -f 2>/dev/null
"${SUDO[@]}" systemctl disable vpn-tray-killswitch.service 2>/dev/null
"${SUDO[@]}" rm -f /etc/systemd/system/vpn-tray-killswitch.service /etc/sudoers.d/vpn-tray \
  /usr/local/libexec/vpn-tray-helper
"${SUDO[@]}" rmdir /etc/vpn-tray 2>/dev/null
"${SUDO[@]}" systemctl daemon-reload
echo "vpn-tray removed (settings kept in ~/.config/vpn-tray)"
