"""XDG base-directory paths used by configuration and runtime state."""

from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "dasungctl"


def _xdg_base(variable: str, fallback: str) -> Path:
    """Return $<variable> when set, else the home-relative fallback."""

    configured = os.environ.get(variable)
    if configured:
        return Path(configured)
    return Path.home() / fallback


def config_dir() -> Path:
    """Directory holding config.json ($XDG_CONFIG_HOME/dasungctl)."""

    return _xdg_base("XDG_CONFIG_HOME", ".config") / APP_NAME


def config_path() -> Path:
    """The optional JSON configuration file."""

    return config_dir() / "config.json"


def state_dir() -> Path:
    """Directory holding runtime state ($XDG_STATE_HOME/dasungctl)."""

    return _xdg_base("XDG_STATE_HOME", ".local/state") / APP_NAME


def last_state_path() -> Path:
    """The last configuration applied by the tray."""

    return state_dir() / "last-state.json"


def ghost_state_path() -> Path:
    """The compact summary of the last ghost estimate."""

    return state_dir() / "ghost.json"


def screencast_path() -> Path:
    """The ScreenCast restore token granted by the user (Wayland capture)."""

    return state_dir() / "screencast.json"


def log_path() -> Path:
    """The per-run log file; every command truncates it at startup."""

    return state_dir() / "dasungctl.log"


def lock_path() -> Path:
    """The file used for exclusive access to the monitor."""

    return state_dir() / "lock"
