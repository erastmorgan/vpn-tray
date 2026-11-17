# Third-party software

vpn-tray (MIT, see [LICENSE](LICENSE)) **does not bundle or redistribute** any third-party
software. It drives tools that you install separately, each under its own license:

| Software | Used for | License |
|---|---|---|
| [Happ](https://happ.su) | Xray-based client (VLESS/VMess/Trojan/…) driven via its deeplinks | proprietary (FlyFrog LLC) |
| [AmneziaWG tools](https://github.com/amnezia-vpn/amneziawg-tools) | `awg-quick` | GPL-2.0 |
| [WireGuard tools](https://www.wireguard.com/) | `wg-quick` | GPL-2.0 |
| [NetworkManager](https://networkmanager.dev/) | other VPN types | GPL-2.0+ |
| [nftables](https://netfilter.org/projects/nftables/) | kill switch | GPL-2.0 |
| [openresolv](https://roy.marples.name/projects/openresolv/) | DNS for tunnels | BSD-2-Clause |
| [PyQt5](https://riverbankcomputing.com/software/pyqt/) | details window / fallback tray icon | GPL-3.0 (or commercial) |
| KDE Plasma / Kirigami | system-tray applet runtime | LGPL |

Icons in `plasmoid/contents/images/` are original work released under MIT. The applet also uses
icons from the user's installed icon theme (e.g. Breeze, LGPL-3.0) at runtime.

Note: PyQt5 is GPL-licensed. vpn-tray imports it at runtime only; if you distribute a combined
work that includes PyQt5, the terms of PyQt5's license apply to that distribution.
