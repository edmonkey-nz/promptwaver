"""Where things live, which is two different places once the app is packaged.

Running from a checkout, every path is the repo root and this module is a
no-op. Inside a PyInstaller one-file build there are two roots and using the
wrong one is silent data loss:

* **`bundle_dir()`** — `sys._MEIPASS`, the directory PyInstaller unpacks the
  executable into. Read-only in practice and **deleted when the process
  exits**. Everything shipped inside the binary (the web UI, about.md, the
  starter scene library) is here.
* **`data_dir()`** — beside the executable itself, and the only place that
  survives a restart. Anything the app WRITES belongs here: settings.json, the
  scene library, the director's cache, kiosk recordings.

The original build resolved both to `_MEIPASS`, so a packaged build lost every
saved scene and its API key the moment it closed — and, because the assets
were never bundled in the first place, crashed on startup before anyone found
out (aiohttp's `add_static` raises on a missing directory).
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """Root for read-only assets shipped with the app."""
    return getattr(sys, "_MEIPASS", _REPO_ROOT)


def data_dir() -> str:
    """Root for anything written at runtime; persists across restarts.

    Falls back to `~/.promptwaver` when the app sits somewhere unwritable —
    Program Files, /Applications, a read-only mount — which is normal for an
    installed application rather than an error.
    """
    if not frozen():
        return _REPO_ROOT
    beside = os.path.dirname(os.path.abspath(sys.executable))
    if os.access(beside, os.W_OK):
        return beside
    home = os.path.join(os.path.expanduser("~"), ".promptwaver")
    os.makedirs(home, exist_ok=True)
    return home


def seed_scenes(library_dir: str) -> int:
    """Copy the bundled starter scenes next to the executable, once.

    Only ever ADDS files that aren't there — a scene the user edited or deleted
    stays edited or deleted. Returns how many were copied, so startup can say
    so the first time.
    """
    src = os.path.join(bundle_dir(), "scenes")
    if not frozen() or not os.path.isdir(src) or os.path.abspath(src) == os.path.abspath(library_dir):
        return 0
    os.makedirs(library_dir, exist_ok=True)
    copied = 0
    for name in os.listdir(src):
        if not name.endswith(".json"):
            continue
        dst = os.path.join(library_dir, name)
        if os.path.exists(dst):
            continue
        try:
            with open(os.path.join(src, name), "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
            copied += 1
        except OSError:
            pass
    return copied
