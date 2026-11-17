# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Follow the KDE colour scheme (light/dark) in the PyQt5 window.

Plasma 6 ships its platform theme only for Qt 6, so a Qt 5 app would ignore the system scheme.
We read ~/.config/kdeglobals ourselves, build a QPalette (Fusion style) and re-apply it live when
the user switches the scheme in System Settings.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path

from PyQt5.QtCore import QFileSystemWatcher, QObject
from PyQt5.QtGui import QColor, QPalette

KDEGLOBALS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kdeglobals"

# Breeze (light) defaults, used for keys missing in kdeglobals
DEFAULTS = {
    "Window": {"BackgroundNormal": "239,240,241", "ForegroundNormal": "35,38,41", "ForegroundInactive": "112,125,138"},
    "View": {"BackgroundNormal": "255,255,255", "BackgroundAlternate": "247,247,247", "ForegroundNormal": "35,38,41",
             "ForegroundLink": "41,128,185"},
    "Button": {"BackgroundNormal": "252,252,252", "ForegroundNormal": "35,38,41"},
    "Selection": {"BackgroundNormal": "61,174,233", "ForegroundNormal": "255,255,255"},
    "Tooltip": {"BackgroundNormal": "247,247,247", "ForegroundNormal": "35,38,41"},
}


def _color(cfg, group, key) -> QColor:
    sec = f"Colors:{group}"
    val = cfg.get(sec, key, fallback=None) if cfg.has_section(sec) else None
    val = val or DEFAULTS[group][key]
    r, g, b = (int(x) for x in val.split(",")[:3])
    return QColor(r, g, b)


def kde_palette(path: Path = KDEGLOBALS) -> QPalette:
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cfg.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        pass
    c = lambda g, k: _color(cfg, g, k)  # noqa: E731
    p = QPalette()
    roles = {
        QPalette.Window: c("Window", "BackgroundNormal"),
        QPalette.WindowText: c("Window", "ForegroundNormal"),
        QPalette.Base: c("View", "BackgroundNormal"),
        QPalette.AlternateBase: c("View", "BackgroundAlternate"),
        QPalette.Text: c("View", "ForegroundNormal"),
        QPalette.Button: c("Button", "BackgroundNormal"),
        QPalette.ButtonText: c("Button", "ForegroundNormal"),
        QPalette.Highlight: c("Selection", "BackgroundNormal"),
        QPalette.HighlightedText: c("Selection", "ForegroundNormal"),
        QPalette.ToolTipBase: c("Tooltip", "BackgroundNormal"),
        QPalette.ToolTipText: c("Tooltip", "ForegroundNormal"),
        QPalette.Link: c("View", "ForegroundLink"),
        QPalette.PlaceholderText: c("Window", "ForegroundInactive"),
    }
    for role, col in roles.items():
        p.setColor(QPalette.All, role, col)
    inactive = c("Window", "ForegroundInactive")
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, inactive)
    return p


def is_dark(p: QPalette) -> bool:
    return p.color(QPalette.Window).lightness() < 128


class ThemeFollower(QObject):
    """Applies the KDE palette now and whenever kdeglobals changes."""

    def __init__(self, app, on_change=None):
        super().__init__(app)
        self.app, self.on_change = app, on_change
        app.setStyle("Fusion")
        self.watch = QFileSystemWatcher(self)
        self.watch.addPath(str(KDEGLOBALS.parent))
        if KDEGLOBALS.exists():
            self.watch.addPath(str(KDEGLOBALS))
        self.watch.fileChanged.connect(self.apply)
        self.watch.directoryChanged.connect(self.apply)
        self._last = None
        self.apply()

    def apply(self, *_):
        if KDEGLOBALS.exists() and str(KDEGLOBALS) not in self.watch.files():
            self.watch.addPath(str(KDEGLOBALS))   # KDE replaces the file atomically
        pal = kde_palette()
        key = tuple(pal.color(r).name() for r in (QPalette.Window, QPalette.Text, QPalette.Highlight))
        if key == self._last:
            return
        self._last = key
        self.app.setPalette(pal)
        if self.on_change:
            self.on_change(is_dark(pal))
