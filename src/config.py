"""JSON configuration: serial, auto-refresh and ghost-estimate defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping

from . import paths
from .ghostwatch import DEFAULT_MODEL_WIDTH
from .panels import DEFAULT_PANEL, get_panel
from .zoneclear import DEFAULT_CLEAR, ClearError, validate_clear


DEFAULT_AUTOREFRESH = {"interval": 300.0, "hard": False}
DEFAULT_GHOST = {
    "enabled": True,
    "interval": 2.0,
    "max_interval": 30.0,
    "threshold": 20.0,
    "output": "auto",
    "width": DEFAULT_MODEL_WIDTH,
    "clear": dict(DEFAULT_CLEAR),
}


class ConfigError(ValueError):
    """The configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Serial settings and auto-refresh/ghost defaults from config.json."""

    device: str = "auto"
    timeout: float = 1.0
    panel: str = DEFAULT_PANEL.key
    autorefresh: Mapping[str, Any] = field(default_factory=dict)
    ghost: Mapping[str, Any] = field(default_factory=dict)


def _validate_autorefresh(data: Any) -> dict[str, Any]:
    """Validate the optional `autorefresh` object and fill its defaults."""

    if not isinstance(data, dict):
        raise ConfigError("'autorefresh' must be a JSON object")
    unknown = set(data) - {"interval", "hard"}
    if unknown:
        raise ConfigError(
            "'autorefresh' has unknown fields: " + ", ".join(sorted(unknown))
        )
    result: dict[str, Any] = {}
    interval = data.get("interval", DEFAULT_AUTOREFRESH["interval"])
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise ConfigError("'autorefresh.interval' must be a number of seconds")
    if interval <= 0:
        raise ConfigError("'autorefresh.interval' must be greater than zero")
    result["interval"] = float(interval)
    hard = data.get("hard", DEFAULT_AUTOREFRESH["hard"])
    if not isinstance(hard, bool):
        raise ConfigError("'autorefresh.hard' must be true or false")
    result["hard"] = hard
    return result


def _validate_clear(data: Any) -> dict[str, Any]:
    """Validate the optional `ghost.clear` object and fill its defaults."""

    try:
        return validate_clear(data)
    except ClearError as exc:
        raise ConfigError(str(exc)) from exc


def _validate_ghost(data: Any) -> dict[str, Any]:
    """Validate the optional `ghost` object and fill its defaults."""

    if not isinstance(data, dict):
        raise ConfigError("'ghost' must be a JSON object")
    unknown = set(data) - {
        "enabled",
        "interval",
        "max_interval",
        "threshold",
        "output",
        "width",
        "clear",
    }
    if unknown:
        raise ConfigError(
            "'ghost' has unknown fields: " + ", ".join(sorted(unknown))
        )
    result: dict[str, Any] = {}
    enabled = data.get("enabled", DEFAULT_GHOST["enabled"])
    if not isinstance(enabled, bool):
        raise ConfigError("'ghost.enabled' must be true or false")
    result["enabled"] = enabled
    interval = data.get("interval", DEFAULT_GHOST["interval"])
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise ConfigError("'ghost.interval' must be a number of seconds")
    if interval <= 0:
        raise ConfigError("'ghost.interval' must be greater than zero")
    result["interval"] = float(interval)
    max_interval = data.get("max_interval", DEFAULT_GHOST["max_interval"])
    if isinstance(max_interval, bool) or not isinstance(
        max_interval, (int, float)
    ):
        raise ConfigError("'ghost.max_interval' must be a number of seconds")
    if max_interval < interval:
        raise ConfigError(
            "'ghost.max_interval' must not be smaller than 'ghost.interval'"
        )
    result["max_interval"] = float(max_interval)
    threshold = data.get("threshold", DEFAULT_GHOST["threshold"])
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ConfigError("'ghost.threshold' must be a number in 0..100")
    if not 0 <= threshold <= 100:
        raise ConfigError("'ghost.threshold' must be in 0..100")
    result["threshold"] = float(threshold)
    output = data.get("output", DEFAULT_GHOST["output"])
    if not isinstance(output, str) or not output:
        raise ConfigError("'ghost.output' must be a non-empty string")
    result["output"] = output
    width = data.get("width", DEFAULT_GHOST["width"])
    if isinstance(width, bool) or not isinstance(width, int):
        raise ConfigError("'ghost.width' must be an integer")
    if not 160 <= width <= 1200:
        raise ConfigError("'ghost.width' must be in 160..1200")
    result["width"] = width
    result["clear"] = _validate_clear(data.get("clear", {}))
    return result


def _parse(data: Any) -> Config:
    """Build a Config from already-decoded JSON, rejecting unknown fields."""

    if not isinstance(data, dict):
        raise ConfigError("configuration must be a JSON object")
    unknown = set(data) - {
        "device",
        "timeout",
        "panel",
        "autorefresh",
        "ghost",
    }
    if unknown:
        raise ConfigError(
            "unknown configuration fields: " + ", ".join(sorted(unknown))
        )

    device = data.get("device", "auto")
    if not isinstance(device, str) or not device:
        raise ConfigError("'device' must be a non-empty string")

    timeout = data.get("timeout", 1.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ConfigError("'timeout' must be a number of seconds")
    if timeout <= 0:
        raise ConfigError("'timeout' must be greater than zero")

    panel = data.get("panel", DEFAULT_PANEL.key)
    if not isinstance(panel, str) or not panel:
        raise ConfigError("'panel' must be a non-empty string")
    try:
        get_panel(panel)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    raw_autorefresh = data.get("autorefresh", {})
    autorefresh = _validate_autorefresh(raw_autorefresh)
    raw_ghost = data.get("ghost", {})
    ghost = _validate_ghost(raw_ghost)

    return Config(
        device=device,
        timeout=float(timeout),
        panel=panel,
        autorefresh=autorefresh,
        ghost=ghost,
    )


def load_config(path: Path | None = None) -> Config:
    """Read the optional config file; a missing file means defaults."""

    target = path or paths.config_path()
    if not target.exists():
        return Config()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target}: invalid JSON: {exc}") from exc
    try:
        return _parse(data)
    except ConfigError as exc:
        raise ConfigError(f"{target}: {exc}") from exc


def autorefresh_settings(config: Config) -> tuple[float, bool]:
    """Return the validated (interval, hard) auto-refresh defaults."""

    interval = config.autorefresh.get(
        "interval", DEFAULT_AUTOREFRESH["interval"]
    )
    hard = config.autorefresh.get("hard", DEFAULT_AUTOREFRESH["hard"])
    return float(interval), bool(hard)


def ghost_settings(config: Config) -> dict[str, Any]:
    """Return the validated ghost-estimate settings, defaults filled in."""

    settings = dict(DEFAULT_GHOST)
    settings.update(config.ghost)
    clear = dict(DEFAULT_CLEAR)
    clear.update(config.ghost.get("clear", {}))
    settings["clear"] = clear
    return settings

