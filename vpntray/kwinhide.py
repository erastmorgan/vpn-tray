# SPDX-FileCopyrightText: 2026 erastmorgan
# SPDX-License-Identifier: MIT
"""Keep a GUI client's window from popping up while we drive it in the background (KDE/KWin).

Happ shows its main window whenever it receives a deeplink (happ://connect …). Before sending one
we load a short-lived KWin script: during the next N seconds every *newly shown* window of that
client is closed back to its tray icon. Windows the user already had open are left alone.
Works on Wayland and X11; silently does nothing outside KWin.

    python3 -m vpntray.kwinhide Happ 15
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_NAME = "vpntray-hide-{cls}"

SCRIPT = r"""
const cls = %(cls)s;
const until = Date.now() + %(ms)d;
function isTarget(w) {
    return w && w.resourceClass && w.resourceClass.toString().toLowerCase() === cls;
}
const preexisting = new Set();
if (!%(close_existing)s) {
    for (const w of workspace.windowList())
        if (isTarget(w) && !w.minimized) preexisting.add(w.internalId.toString());
}
function handle(w) {
    if (Date.now() > until || !isTarget(w) || preexisting.has(w.internalId.toString())) return;
    print("vpntray-hide: closing " + w.caption);
    // NB: do not minimise — a minimised Qt Quick window stops rendering and Happ then stalls
    // on the deeplink for ~40 s. Closing makes it hide to its tray icon right away.
    w.closeWindow();
}
for (const w of workspace.windowList()) if (%(close_existing)s) handle(w);
print("vpntray-hide: armed for " + cls + ", preexisting=" + preexisting.size);
workspace.windowAdded.connect(handle);
workspace.windowActivated.connect(handle);
"""


def _busctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["busctl", "--user", "call", "org.kde.KWin", *args],
                          capture_output=True, text=True, timeout=10)


def suppress(window_class: str = "Happ", seconds: float = 15, close_existing: bool = False) -> bool:
    """Arm the KWin script. Returns False when KWin scripting is unavailable."""
    import time
    cls = window_class.lower()
    # A unique name per arming: re-loading under the same name right after unloading makes KWin
    # hand back a stale script id, and the new script silently never runs.
    name = f"{SCRIPT_NAME.format(cls=cls)}-{int(time.time() * 1000)}"
    js = SCRIPT % {"cls": repr(cls), "ms": int(seconds * 1000),
                   "close_existing": "true" if close_existing else "false"}
    d = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "vpn-tray"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"kwin-{name}.js"
    path.write_text(js)
    registry = d / f"kwin-{cls}.loaded"
    try:
        # drop the scripts armed before (each new arming fully supersedes them)
        old = registry.read_text().split() if registry.exists() else []
        for o in old:
            _busctl("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", o)
            (d / f"kwin-{o}.js").unlink(missing_ok=True)
        r = _busctl("/Scripting", "org.kde.kwin.Scripting", "loadScript", "ss", str(path), name)
        if r.returncode != 0:
            return False
        registry.write_text(name)
        # start() runs every loaded-but-idle script. Do not run by the returned id: KWin reuses
        # ids, and with other scripts being (un)loaded concurrently that id may point elsewhere.
        return _busctl("/Scripting", "org.kde.kwin.Scripting", "start").returncode == 0
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return False


if __name__ == "__main__":
    cls = sys.argv[1] if len(sys.argv) > 1 else "Happ"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 15
    ok = suppress(cls, secs, close_existing="--close-existing" in sys.argv)
    sys.exit(0 if ok else 1)
