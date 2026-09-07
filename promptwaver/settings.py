"""Tiny local settings store.

Holds the Anthropic API key (and future prefs) in a plaintext JSON file next to
the project. This is deliberately simple for local use — the file is gitignored.
Before a public release, move the key to the OS keyring / an env-only flow.
"""

from __future__ import annotations

import json
import os

# data_dir(), not this file's directory: in a packaged build that directory is
# PyInstaller's temp bundle, which is deleted on exit — the API key and every
# rig setting would be gone every time the app closed.
from .paths import data_dir

_PATH = os.path.join(data_dir(), "settings.json")


def load() -> dict:
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save(data: dict) -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _PATH)
    try:
        os.chmod(_PATH, 0o600)   # key is secret-ish; restrict perms
    except Exception:
        pass


def get(key: str, default=None):
    return load().get(key, default)


def set(key: str, value) -> None:
    data = load()
    data[key] = value
    save(data)


def apply_env() -> bool:
    """If a stored API key exists and the env var isn't already set, export it
    so the director picks it up. Returns True if a key is now in the env."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    k = get("anthropic_api_key")
    if k:
        os.environ["ANTHROPIC_API_KEY"] = k
        return True
    return False
