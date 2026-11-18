#!/bin/bash
# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
# vpn-tray installer (per user). Re-run safely to upgrade.
#
#  root part (sudo, asked once):
#    /usr/local/libexec/vpn-tray-helper      root helper — the only thing ever run via sudo
#    /etc/sudoers.d/vpn-tray                 NOPASSWD for that helper only
#    vpn-tray-killswitch.service (system)    restores the kill switch at boot, before the network
#  user part:
#    ~/.local/bin/vpn-tray{,-cli,-agent,-hide-window}
#    vpn-tray-agent.service      auto-reconnect, kill switch, status for the UI (Restart=always, watchdog)
#    vpn-tray-happ.service       Happ in the background (only if Happ is installed)
#    Plasma 6: applet io.github.erastmorgan.vpntray in the system tray
#    other desktops: vpn-tray.service (PyQt5 tray icon)
#
# Options: --no-killswitch   install with the kill switch disabled
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
BIN="$HOME/.local/bin"
APPLET_ID="io.github.erastmorgan.vpntray"
SUDO=(sudo); [[ -n "${SUDO_ASKPASS:-}" ]] && SUDO=(sudo -A)  # non-interactive installs
KILLSWITCH=1; [[ " $* " == *" --no-killswitch "* ]] && KILLSWITCH=0

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)" >&2; exit 1; }; }
need python3 "python"
need nft "nftables"
need resolvconf "openresolv"
python3 -c 'import PyQt5.QtWidgets' 2>/dev/null || { echo "missing: PyQt5 (python-pyqt5)" >&2; exit 1; }

echo "==> root helper, sudoers, boot-time kill switch"
"${SUDO[@]}" install -D -m 0755 -o root -g root "$SRC/helper/vpn-tray-helper" /usr/local/libexec/vpn-tray-helper
tmp="$(mktemp)"
{
  echo "# vpn-tray: allow the tray/agent to run its root helper without a password"
  echo "Defaults!/usr/local/libexec/vpn-tray-helper !log_allowed, !pam_session"
  echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/libexec/vpn-tray-helper"
} > "$tmp"
if ! "${SUDO[@]}" visudo -cf "$tmp" >/dev/null 2>&1; then
  sed -i 's/!log_allowed, //' "$tmp"         # older sudo: no log_allowed
  "${SUDO[@]}" visudo -cf "$tmp" >/dev/null
fi
"${SUDO[@]}" install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/vpn-tray
rm -f "$tmp"
"${SUDO[@]}" install -m 0644 -o root -g root "$SRC/packaging/vpn-tray-killswitch.service" /etc/systemd/system/
"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable vpn-tray-killswitch.service >/dev/null 2>&1

echo "==> launchers"
mkdir -p "$BIN" ~/.local/share/applications ~/.config/systemd/user
for n in vpn-tray:gui vpn-tray-cli:cli vpn-tray-agent:agent vpn-tray-hide-window:kwinhide; do
  bin="${n%%:*}"; mod="${n##*:}"
  cat > "$BIN/$bin" <<EOS
#!/bin/sh
PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}" exec /usr/bin/python3 -m vpntray.$mod "\$@"
EOS
  chmod +x "$BIN/$bin"
done
sed "s|@BIN@|$BIN|g" "$SRC/packaging/vpn-tray.desktop" > ~/.local/share/applications/vpn-tray.desktop
rm -f ~/.config/autostart/vpn-tray.desktop

echo "==> user services"
UNITS=(vpn-tray-agent.service)
for u in vpn-tray-agent.service vpn-tray.service vpn-tray-happ.service; do
  sed "s|@BIN@|$BIN|g" "$SRC/packaging/$u" > ~/.config/systemd/user/$u
done
if [[ -x /opt/happ/bin/Happ ]]; then
  UNITS+=(vpn-tray-happ.service)
  # Happ: start minimised (it still shows the window on deeplinks — vpn-tray hides it via KWin)
  if [[ -f ~/.config/Happ.conf ]] && ! grep -q 'startMinimized=true' ~/.config/Happ.conf; then
    grep -q '^\[Preferences\]' ~/.config/Happ.conf \
      && sed -i 's/^\[Preferences\]$/[Preferences]\nAdvancedSettings\\startMinimized=true/' ~/.config/Happ.conf \
      || printf '\n[Preferences]\nAdvancedSettings\\startMinimized=true\n' >> ~/.config/Happ.conf
  fi
  # Happ's window must never take focus (it would close the open tray popup)
  "$SRC/packaging/kwin-happ-rule.sh" add || true
fi

PLASMOID=0
if command -v kpackagetool6 >/dev/null && [[ "${XDG_CURRENT_DESKTOP:-$(systemctl --user show-environment | sed -n 's/^XDG_CURRENT_DESKTOP=//p')}" == *KDE* ]]; then
  PLASMOID=1
fi

if (( PLASMOID )); then
  echo "==> Plasma applet"
  if kpackagetool6 -t Plasma/Applet -s "$APPLET_ID" >/dev/null 2>&1; then
    kpackagetool6 -t Plasma/Applet -u "$SRC/plasmoid" >/dev/null
  else
    kpackagetool6 -t Plasma/Applet -i "$SRC/plasmoid" >/dev/null
  fi
  systemctl --user disable --now vpn-tray.service >/dev/null 2>&1 || true
  # add the applet to every system tray (the tray picks the change up live)
  js="const id='$APPLET_ID';
function list(v) { return (Array.isArray(v) ? v : String(v || '').split(',')).filter(x => x); }
for (const p of panels()) for (const w of p.widgets('org.kde.plasma.systemtray')) {
  w.currentConfigGroup = ['General'];          // Plasma 6: the tray keeps its items in its own General group
  for (const key of ['extraItems', 'knownItems']) {
    const v = list(w.readConfig(key));
    if (!v.includes(id)) { v.push(id); w.writeConfig(key, v); }
  }
  w.writeConfig('hiddenItems', list(w.readConfig('hiddenItems')).filter(x => x !== id));
  w.reloadConfig();
}"
  busctl --user call org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell evaluateScript s "$js" >/dev/null \
    || echo "   (could not add the applet automatically: System Tray settings → Entries → VPN)"
else
  UNITS+=(vpn-tray.service)
fi

sudo -n /usr/local/libexec/vpn-tray-helper ping >/dev/null
if (( ! KILLSWITCH )); then "$BIN/vpn-tray-cli" killswitch off >/dev/null; fi
"$BIN/vpn-tray-cli" adopt >/dev/null   # keep the currently connected VPN as the one to auto-reconnect
systemctl --user daemon-reload
systemctl --user enable "${UNITS[@]}" >/dev/null 2>&1
systemctl --user restart "${UNITS[@]}"
echo "vpn-tray installed$( (( PLASMOID )) && echo ' (Plasma applet in the system tray)')"
