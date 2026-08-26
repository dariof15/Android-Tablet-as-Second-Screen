"""Configuration loading shared by mjpeg-screen.py and auto-layout.py.

Precedence is command-line flag > environment variable > .env file > default.

The .env file is read by the scripts themselves rather than relying on systemd's
EnvironmentFile=, so that running a script by hand in a terminal behaves exactly
like the service does. It is looked up next to the scripts, so it works no
matter which directory you launch from.
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"
_loaded = False


def load(path=ENV_PATH):
    """Read KEY=VALUE lines from .env without overriding the real environment.

    Deliberately minimal - no dependency, no interpolation, no export syntax.
    A real environment variable always wins, so you can override a single
    setting for one run without editing the file.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _raw(name, default):
    load()
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def get_str(name, default):
    return str(_raw(name, default))


def get_int(name, default):
    try:
        return int(str(_raw(name, default)).strip())
    except ValueError:
        return default


def get_float(name, default):
    try:
        return float(str(_raw(name, default)).strip())
    except ValueError:
        return default


def get_bool(name, default):
    value = str(_raw(name, default)).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default
