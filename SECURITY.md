# Security

## Model

* The tray, the applet, the CLI and the agent run as **your user**.
* Exactly one file runs as root: `/usr/local/libexec/vpn-tray-helper`, installed root-owned and
  allowed via `/etc/sudoers.d/vpn-tray` (`NOPASSWD` for that path only). It accepts a fixed list of
  actions (`up`, `down`, `show`, `list-configs`, `dns-set`, `dns-clear`, `dns-base`, `killswitch-on`,
  `killswitch-off`, `killswitch-status`, `ping`) and validates every argument (interface names,
  IPs, CIDRs, host names). It never prints private keys.
* VPN credentials are never read or stored by vpn-tray: WireGuard/AmneziaWG keys stay in
  `/etc/wireguard`, `/etc/amnezia/amneziawg`; Happ keeps (and encrypts) its own subscription;
  NetworkManager keeps its secrets.
* The kill switch is an nftables table `inet vpntray`. It is fail-closed: if vpn-tray crashes,
  the rules stay in the kernel; at boot they are loaded before the network.

## Reporting a vulnerability

Please open a private security advisory on GitHub
(<https://github.com/erastmorgan/vpn-tray/security/advisories/new>) instead of a public issue.
