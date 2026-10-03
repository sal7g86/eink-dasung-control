"""The last configuration applied by the user.

The tray writes one JSON file after every successful change and applies it
again on startup, so the monitor settings and the auto-refresh timer survive
tray restarts and monitor power cycles without named profiles or manual
snapshots.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from . import paths
from .client import DasungClient, MonitorInfo
from .panels import DEFAULT_PANEL, get_panel
from .protocol import display_mode_name
from .zoneclear import ClearError, validate_clear


# The fields the tray displays and persists, in the order the project applies
# them on the wire (mode first, then the image fields, then the frontlight
# fields). They come from the default panel profile so existing importers keep
# working; the tray uses its own resolved profile instead.
LAST_FIELDS = DEFAULT_PANEL.read_fields
APPLY_ORDER = LAST_FIELDS
# Accepted ranges of the default profile; see panels.PAPERLIKE_HD_13.limits.
LAST_LIMITS = DEFAULT_PANEL.limits

# Extra keys accepted in the JSON file: the save timestamp and the tray
# preferences (auto-refresh on/off and interval, ghost auto-clear on/off,
# ghost estimate running/stopped), which are not monitor fields and never
# reach the wire through apply_fields.
PREF_FIELDS = (
    "autorefresh",
    "autorefresh_interval",
    "ghost_clear",
    "ghost_estimate",
)
_UNKNOWN_KEYS = frozenset({"saved_at"})


class StateError(ValueError):
    """The saved last configuration is unreadable or invalid."""


def resolve_mode(value: Any, panel=None) -> int:
    """Accept a mode value or its profile name (a bool is rejected).

    The accepted numbers and names come from the panel profile, so another
    model can use a different mode table without touching this loader.
    """

    modes = get_panel(panel).modes
    if isinstance(value, bool):
        raise ValueError(f"invalid mode: {value!r}")
    if isinstance(value, int):
        if int(value) in modes:
            return int(value)
        raise ValueError(f"invalid mode value: {value!r}")
    name = str(value).lower()
    for mode_value, mode_name in modes.items():
        if mode_name == name:
            return mode_value
    raise ValueError(f"invalid mode name: {value!r}")


def apply_fields(
    client: DasungClient,
    values: Mapping[str, Any],
    *,
    wait: bool = True,
    panel=None,
) -> dict[str, bytes]:
    """Apply the saved fields in order and return each sent frame."""

    profile = get_panel(panel)
    frames: dict[str, bytes] = {}
    for field in profile.read_fields:
        if field not in values:
            continue
        value = values[field]
        if field == "mode":
            request = client.set_mode(resolve_mode(value, profile), wait=wait)
        else:
            setter = getattr(client, f"set_{field}")
            request = setter(int(value), wait=wait)
        frames[field] = request
    return frames


def save_last(
    info: MonitorInfo,
    path: Path | None = None,
    *,
    prefs: Mapping[str, Any] | None = None,
    panel=None,
) -> Path:
    """Write the known fields of `info` as JSON and return the file path.

    `prefs` adds the optional tray preferences (auto-refresh timer, ghost
    clearing and the estimate's running state), so they survive tray restarts
    together with the monitor fields; when omitted, the file keeps only the
    monitor fields.
    """

    profile = get_panel(panel)
    data: dict[str, Any] = {
        "saved_at": datetime.now().isoformat(timespec="seconds")
    }
    for field in profile.read_fields:
        value = getattr(info, field)
        if value is None:
            continue
        data[field] = (
            display_mode_name(value, profile) if field == "mode" else int(value)
        )
    if prefs is not None:
        for field in PREF_FIELDS:
            if field not in prefs:
                continue
            if field == "autorefresh_interval":
                data[field] = float(prefs[field])
            elif field == "ghost_clear":
                data[field] = dict(prefs[field])
            else:
                data[field] = bool(prefs[field])
    target = path or paths.last_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target


def load_last(path: Path | None = None, panel=None) -> dict[str, Any] | None:
    """Return the validated saved fields, or None when nothing is saved.

    A corrupt or invalid file raises StateError: the tray reports it and
    continues with a plain read instead of failing to start.
    """

    profile = get_panel(panel)
    target = path or paths.last_state_path()
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StateError(f"cannot read {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StateError(f"{target}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"{target}: the saved configuration must be an object")
    unknown = (
        set(data) - set(profile.read_fields) - set(PREF_FIELDS) - _UNKNOWN_KEYS
    )
    if unknown:
        raise StateError(f"{target}: unknown fields: {', '.join(sorted(unknown))}")
    result = {
        field: _validated(field, data[field], target, profile)
        for field in profile.read_fields
        if field in data
    }
    for field in PREF_FIELDS:
        if field in data:
            result[field] = _validated_pref(field, data[field], target)
    return result


def _validated(field: str, value: Any, target: Path, panel=None) -> Any:
    """Return one saved value after range/type validation.

    Invalid entries raise StateError with the file path so the tray can tell
    the user which file to fix instead of failing to start.
    """

    profile = get_panel(panel)
    if field == "mode":
        try:
            return resolve_mode(value, profile)
        except ValueError as exc:
            raise StateError(f"{target}: {exc}") from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateError(f"{target}: field {field!r} must be an integer")
    low, high = profile.limits[field]
    if not low <= value <= high:
        raise StateError(
            f"{target}: field {field!r} must be in {low}..{high}"
        )
    return value


def _validated_pref(field: str, value: Any, target: Path) -> Any:
    """Return one saved tray preference after type validation."""

    if field == "ghost_clear":
        try:
            return validate_clear(value, prefix="ghost_clear")
        except ClearError as exc:
            raise StateError(f"{target}: {exc}") from exc
    if field in ("autorefresh", "ghost_estimate"):
        if not isinstance(value, bool):
            raise StateError(f"{target}: field {field!r} must be true or false")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateError(
            f"{target}: field {field!r} must be a number of seconds"
        )
    if value <= 0:
        raise StateError(f"{target}: field {field!r} must be greater than zero")
    return float(value)
