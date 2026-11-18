# vpn-tray

**Менеджер VPN в системном трее Linux**: список VPN, подключение и отключение, статус, скорость,
трафик, протокол, IP сервера, переименование. Есть **kill switch** (интернет только через VPN)
и **самовосстановление**. Поддерживает VPN разных типов одновременно и легко расширяется.

[English version](README.md) · Лицензия: [MIT](LICENSE)

* KDE Plasma 6: нативный апплет системного трея. Панель открывается рядом с иконкой, как у
  штатного апплета «Сеть», и следует светлой/тёмной теме Plasma.
* Другие рабочие столы: иконка в трее и окно на PyQt5 (следуют цветовой схеме KDE, если она есть).
* Протоколы не изобретаются заново, vpn-tray управляет готовыми клиентами.

| Бэкенд | Типы VPN | Через что |
|---|---|---|
| `wgquick` | AmneziaWG, WireGuard | systemd-юниты `awg-quick@IF` / `wg-quick@IF`, конфиги в `/etc/amnezia/amneziawg/`, `/etc/wireguard/` |
| `happ` | VLESS, VMess, Trojan, Shadowsocks, mKCP… (Xray) через официальный клиент **[Happ](https://happ.su)** | официальные deeplink-команды Happ `happ://connect` / `happ://disconnect`; Happ работает в фоне, его окно скрыто |
| `nm` | OpenVPN, WireGuard, L2TP, PPTP, OpenConnect, strongSwan… | NetworkManager (`nmcli connection up/down`) |

## Возможности

* **Статус с одного взгляда.** Иконка в трее показывает: подключено / подключение / отключено / агент не отвечает.
* **Панель.** Все VPN с состоянием и текущей скоростью ↓/↑, кнопки *Подключить / Отключить*.
  Раскрытая строка показывает протокол, сервер (IP:порт и хост), трафик, время подключения,
  интерфейс и признак авто-переподключения; там же переименование.
* **Kill switch** (nftables, IPv4 + IPv6). Трафик выходит с машины только через VPN-интерфейс,
  в LAN или к самим VPN-серверам. Fail-closed: переживает падения, при загрузке ставится до сети.
* **Самовосстановление.** Агент держит выбранный VPN поднятым:
  * переподключает с паузами от 5 до 120 с;
  * каждые 20 с проверяет туннель и пересобирает его после 3 неудачных проверок подряд;
  * возвращает пропавшие правила firewall.

  Агент, Happ и запасная иконка трея — systemd user-сервисы с `Restart=always`, у агента
  ещё watchdog. Апплет Plasma переподключается к агенту сам.
* **Один full-tunnel VPN одновременно.** Переключение идёт по схеме «сначала погасить старый,
  потом поднять новый». Kill switch не даёт утечек в паузе около 1 с. Если новый VPN не поднялся,
  возвращается прежний.

## Требования

* Linux с systemd, `nftables`, `openresolv` (`resolvconf`), `sudo`, Python ≥ 3.9.
* Для апплета — KDE Plasma 6 (`kpackagetool6`). Для запасной иконки — любой трей с
  StatusNotifierItem / XEmbed и `PyQt5`. PyQt5 нужен и для окна «Подробнее».
* Под используемые бэкенды: `amneziawg-tools` (+ `amneziawg-go` или модуль ядра),
  `wireguard-tools`, NetworkManager, Happ desktop для Linux.

Arch / Manjaro:

```bash
sudo pacman -S --needed python python-pyqt5 nftables openresolv wireguard-tools networkmanager
# опционально: amneziawg-tools amneziawg-go (AUR), Happ: https://github.com/Happ-proxy/happ-desktop/releases
```

Debian / Ubuntu:

```bash
sudo apt install python3 python3-pyqt5 nftables openresolv wireguard-tools network-manager
```

## Установка

```bash
git clone https://github.com/erastmorgan/vpn-tray.git ~/Development/vpn-tray
cd ~/Development/vpn-tray
make test        # необязательно: юнит-тесты
./install.sh     # один раз спросит пароль sudo
```

`install.sh` можно запускать повторно, так же делается обновление. Он устанавливает:

| Что | Куда |
|---|---|
| root-helper (единственное, что работает от root) | `/usr/local/libexec/vpn-tray-helper` |
| правило sudoers (NOPASSWD только для helper) | `/etc/sudoers.d/vpn-tray` |
| kill switch при загрузке | `/etc/systemd/system/vpn-tray-killswitch.service` |
| запускаемые файлы | `~/.local/bin/vpn-tray{,-cli,-agent,-hide-window}` |
| агент (авто-переподключение, kill switch, статус) | `~/.config/systemd/user/vpn-tray-agent.service` |
| фоновый Happ (если Happ установлен) | `~/.config/systemd/user/vpn-tray-happ.service` |
| правило KWin: окно Happ не забирает фокус (панель не закрывается) | `~/.config/kwinrulesrc` → `[vpn-tray-happ]` |
| апплет Plasma 6, добавляется в трей | `~/.local/share/plasma/plasmoids/io.github.erastmorgan.vpntray` |
| запасная иконка трея (не Plasma) | `~/.config/systemd/user/vpn-tray.service` |

Опция: `./install.sh --no-killswitch` ставит утилиту с выключенным kill switch.
Код запускается прямо из склонированной папки. Не удаляй её, а после переноса снова запусти `install.sh`.

**Удаление:** `./uninstall.sh`. Сначала выключается kill switch; конфиги VPN и Happ не трогаются.

## Сборка / пакеты

Компилировать нечего (Python + QML). Цели для упаковки:

```bash
make lint        # проверка синтаксиса
make test        # юнит-тесты (без root и сети)
make plasmoid    # dist/vpn-tray-<версия>.plasmoid — ставится: kpackagetool6 -t Plasma/Applet -i <файл>
make dist        # dist/vpn-tray-<версия>.tar.gz — архив исходников (git archive)
```

## Использование

* **Клик** по щиту в трее открывает панель:
  * *Подключить* / *Отключить* у каждого VPN;
  * стрелка раскрывает подробности;
  * *⋯ → Переименовать…*;
  * переключатель *Kill switch* в заголовке;
  * *Подробнее…* открывает окно с таблицей.
* **Правый клик** по иконке: kill switch и окно «Подробнее».
* VPN, подключённый из vpn-tray, становится тем, который агент держит поднятым. Отключить его
  значит «остаться без VPN»; при включённом kill switch интернета тогда нет, так задумано.

CLI работает на том же движке и удобен для скриптов:

```bash
vpn-tray-cli list                       # все VPN: состояние, протокол, сервер, скорость
vpn-tray-cli up happ:default            # подключить (станет целью авто-переподключения)
vpn-tray-cli down awg:awg0
vpn-tray-cli rename happ:default "Рабочий VPN"
vpn-tray-cli set happ:default user_protocol "VLESS / mKCP"   # подпись типа/протокола
vpn-tray-cli killswitch status|on|off
vpn-tray-cli extip                      # внешний IP
vpn-tray-cli popup                      # открыть панель апплета (повесь на глобальное сочетание клавиш)
vpn-tray-cli happ-import work < link.txt # добавить подписку Happ как VPN «happ:work»
vpn-tray-cli agent                      # heartbeat агента
journalctl --user -u vpn-tray-agent -f  # лог агента
```

### Как добавить VPN

* **WireGuard / AmneziaWG:** положи `wgN.conf` в `/etc/wireguard/` (или `awgN.conf` в
  `/etc/amnezia/amneziawg/`). VPN появится в течение 5 минут или сразу после
  *Подробнее… → Обновить список*.
* **OpenVPN и другие:** `nmcli connection import type openvpn file client.ovpn`.
* **Happ (Xray):** `vpn-tray-cli happ-import ИМЯ < ссылка.txt`. Каждая подписка становится отдельным
  VPN `happ:ИМЯ`. Ссылка читается из stdin, поэтому не видна в `ps` и истории shell.
  У Happ нет команды выбора сервера, поэтому vpn-tray хранит снимок состояния Happ для каждого VPN
  (`~/.config/vpn-tray/happ/ИМЯ/`) и подкладывает нужный при переключении. Happ при этом
  перезапускается в фоне, это ~5 с. При первом импорте сервер, уже выбранный в Happ, сохраняется как
  `happ:default`. Без импорта `happ:default` — это просто сервер, выбранный в Happ.
* **Новый тип VPN:**
  1. Создай `vpntray/backends/<имя>.py` с наследником `Backend`
     (`discover / connect / disconnect / status`) и декоратором `@register`.
  2. Импортируй его в `vpntray/backends/__init__.py`.
  3. Если нужны новые root-действия, разреши их в `helper/vpn-tray-helper`.

### Настройки

`~/.config/vpn-tray/state.json` (создаётся при первом изменении):

| Ключ | По умолчанию | Значение |
|---|---|---|
| `killswitch` | `true` | интернет только через VPN |
| `lan` | `"auto"` | всегда доступные подсети: `"auto"` — напрямую подключённые, или список вида `["192.168.1.0/24"]` |
| `base_dns` | `["1.1.1.1"]` | DNS, пока ни один VPN не поднят. Через kill switch к нему ходит только root, например ядро VPN, которому нужно найти свой сервер |
| `desired` | — | VPN, который агент держит поднятым (ставится при подключении и отключении) |

В `~/.config/vpn-tray/profiles.json` хранится список VPN с твоими именами. Параметр `params.dns`
у конкретного VPN (для Happ) задаёт DNS туннеля.

## Архитектура

```
 Апплет Plasma (QML) ──читает── $XDG_RUNTIME_DIR/vpn-tray/status.json ◄── vpn-tray-agent (user-сервис)
        │ действия                                                          │ держит VPN, проверяет,
        ▼                                                                   │ kill switch, статус
 vpn-tray-cli ──► Manager ──► бэкенды: wgquick │ happ │ nm ◄───────────────┘
                     │ sudo -n (NOPASSWD, один файл)
                     ▼
        /usr/local/libexec/vpn-tray-helper (root, whitelist действий)
             ├─ systemctl start/stop awg-quick@ / wg-quick@
             ├─ nftables  table inet vpntray  → /etc/vpn-tray/killswitch.nft (+ юнит загрузки)
             └─ openresolv: DNS для туннелей
```

Правила kill switch (цепочки output и forward):
* разрешено: `lo`, VPN-интерфейсы (`awg*`, `wg*`, `happ*`, `tun*`, `tap*`, `ppp*`, `tailscale*`),
  подсети LAN, IP VPN-серверов, DHCP, link-local и multicast, DNS только от root;
* всё остальное — reject.

## Тесты

```bash
make test   # 25 юнит-тестов: менеджер, взаимоисключаемость, откат, самовосстановление агента, логика kill switch, снимки Happ…
make e2e    # ЖИВОЙ: реально переключает VPN, специально ломает, потом возвращает исходное состояние
```

Живому тесту нужны установленный агент и два full-tunnel VPN:
`VPN_TRAY_E2E_A=awg:awg0 VPN_TRAY_E2E_B=happ:default VPN_TRAY_E2E_LAN_GW=<роутер> make e2e`.

Что проверяется:
* для каждого VPN — внешний IP, DNS, скорость скачивания и показания счётчика скорости;
* взаимоисключаемость VPN и отсутствие утечек при переключении;
* kill switch: без VPN нет ни интернета, ни DNS; LAN доступен; сброшенные правила возвращаются;
* самовосстановление: обрыв туннеля, kill Happ, остановка AWG в обход утилиты, kill агента и трея;
* окно Happ остаётся скрытым;
* бэкенд NetworkManager;
* переименование сохраняется.

## Если что-то не так

* **Интернета нет совсем.** Это kill switch: ни один VPN не подключён. Подключи VPN или выполни
  `vpn-tray-cli killswitch off`. Аварийно, от root: `nft delete table inet vpntray; rm /etc/vpn-tray/killswitch.nft`.
* **Апплет пишет «агент не отвечает».** Выполни `systemctl --user restart vpn-tray-agent`;
  логи: `journalctl --user -u vpn-tray-agent`.
* **`awg-quick` иногда падает с `RTNETLINK answers: File exists`.** Другой хук (часто
  dispatcher-скрипт NetworkManager) одновременно добавляет тот же маршрут. vpn-tray повторит попытку;
  если убрать дублирующий хук, переключение будет мгновенным.
* **Happ падает, если открыть ссылку подписки при запущенном Happ.** Так бывает в версиях 4.7–4.8 со
  ссылками, в которых есть профиль маршрутизации. Используй `vpn-tray-cli happ-import`: он
  импортирует при холодном старте Happ.
* **Апплета нет в трее.** Настройки системного трея → Элементы → *VPN* → *Всегда показывать*,
  или запусти `install.sh` ещё раз.

## Участие

Issues и pull request'ы приветствуются. Перед отправкой прогони `make lint test`.
Уязвимости: см. [SECURITY.md](SECURITY.md).

## Лицензия

[MIT](LICENSE) © 2026 erastmorgan. Сторонние программы, которые используются во время работы,
перечислены в [THIRD-PARTY.md](THIRD-PARTY.md); ни одна из них не входит в репозиторий.
