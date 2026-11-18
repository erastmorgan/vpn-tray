#!/bin/bash
# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
# KWin window rule "vpn-tray-happ": Happ's window never takes focus by itself.
# Happ pops its window up on every deeplink; vpn-tray closes it at once (vpntray/kwinhide.py),
# but if the window got focus, the open vpn-tray popup (and any other Plasma popup) would close.
# Focus stealing prevention "Extreme" stops that; clicking Happ's own tray icon still works.
# Usage: kwin-happ-rule.sh add|remove
set -euo pipefail
command -v kwriteconfig6 >/dev/null && command -v kreadconfig6 >/dev/null || exit 0
F=kwinrulesrc; G=vpn-tray-happ
rules="$(kreadconfig6 --file $F --group General --key rules)"
list=(); IFS=, read -ra cur <<<"$rules"
for r in "${cur[@]}"; do [[ -n "$r" && "$r" != "$G" ]] && list+=("$r"); done
case "${1:-}" in
  add)
    list+=("$G")
    w() { kwriteconfig6 --file $F --group $G --key "$1" "$2"; }
    w Description "vpn-tray: Happ never steals focus"
    w wmclass Happ; w wmclassmatch 1; w wmclasscomplete false
    w fsplevel 4; w fsplevelrule 2
    ;;
  remove)
    kwriteconfig6 --file $F --group $G --key Description --delete
    for k in wmclass wmclassmatch wmclasscomplete fsplevel fsplevelrule; do
      kwriteconfig6 --file $F --group $G --key $k --delete
    done
    ;;
  *) echo "usage: $0 add|remove" >&2; exit 2 ;;
esac
kwriteconfig6 --file $F --group General --key rules "$(IFS=,; echo "${list[*]}")"
kwriteconfig6 --file $F --group General --key count "${#list[@]}"
busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure >/dev/null 2>&1 || true
