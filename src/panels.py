"""Panel profiles: the model-specific values behind the common protocol.

The Dasung serial family shares the transport and the frame layout, but every
model has its own confirmed values: display-mode numbers, speed labels,
frontlight presets and temperature mapping, the selector fields worth reading
with their limits, and the EDID model names used to find the monitor for the
ghost capture. All of it lives in a `PanelProfile`; the rest of the package
reads `get_panel()` instead of hardcoding one unit's calibration.

Only `paperlike-hd-13.3` is confirmed today (protocol `0x30`, 40 Hz, the
HD-FT variant with frontlight and touchscreen). `docs/panels.md` explains
what each value means and how to add another model, which requires capturing
the same evidence first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _frontlight_levels(step: int, count: int) -> tuple[tuple[int, str], ...]:
    """Build the `(byte, label)` presets of a linear brightness scale."""

    return tuple(
        (level * step, "Off" if level == 0 else str(level))
        for level in range(count + 1)
    )


@dataclass(frozen=True)
class PanelProfile:
    """One monitor model's confirmed tables.

    Values are plain ints and strings so a profile can describe any
    Dasung-family panel; the methods turn raw wire values into the labels the
    CLI and the tray display. The field layout is documented in
    `docs/panels.md`; keep a profile honest: only confirmed values belong
    here, everything else stays an open question in `docs/protocol.md`.
    """

    key: str
    name: str
    protocol: int
    refresh_hz: int
    edid_names: tuple[str, ...]
    modes: Mapping[int, str]
    speed_labels: tuple[str, ...]
    frontlight_levels: tuple[tuple[int, str], ...]
    frontlight_modes: Mapping[int, str]
    preset_temperatures: Mapping[int, int]
    custom_frontlight_mode: int
    temperature_levels: tuple[int, ...]
    read_fields: tuple[str, ...]
    limits: Mapping[str, tuple[int, int]]

    # -- labels ------------------------------------------------------------

    def display_mode_name(self, value: int | None) -> str:
        """Stable name of a mode value, or `unknown` for raw strangers."""

        if value is None:
            return "unknown"
        return self.modes.get(int(value), "unknown")

    def frontlight_mode_name(self, value: int | None) -> str:
        """Name a frontlight preset plus the project's custom value."""

        if value is None:
            return "unknown"
        if int(value) == self.custom_frontlight_mode:
            return "custom"
        return self.frontlight_modes.get(int(value), "unknown")

    def speed_name(self, value: int | None) -> str:
        """Label a speed value as the official client's combo does."""

        try:
            index = int(value) - 1
        except (TypeError, ValueError):
            return "unknown"
        if 0 <= index < len(self.speed_labels):
            return self.speed_labels[index]
        return "unknown"

    def frontlight_level(self, value: int | None) -> tuple[int, str] | None:
        """Preset whose byte is closest to `value`; None when unknown."""

        if value is None or not self.frontlight_levels:
            return None
        return min(
            self.frontlight_levels, key=lambda preset: abs(preset[0] - int(value))
        )

    def frontlight_label(self, value: int | None) -> str | None:
        """Label a raw frontlight byte with its calibrated level."""

        preset = self.frontlight_level(value)
        return preset[1] if preset else None

    def temperature_level(self, value: int | None) -> int | None:
        """Byte of the project temperature level closest to `value`."""

        if value is None:
            return None
        return min(self.temperature_levels, key=lambda byte: abs(byte - int(value)))

    def temperature_level_number(self, value: int | None) -> int | None:
        """Level 1..10 closest to a raw temperature byte; None when unknown."""

        byte = self.temperature_level(value)
        if byte is None:
            return None
        return self.temperature_levels.index(byte) + 1

    def temperature_byte(self, level: int) -> int:
        """Byte for a 1..10 temperature level (1 coldest, 10 warmest)."""

        clamped = max(1, min(len(self.temperature_levels), int(level)))
        return self.temperature_levels[clamped - 1]

    @property
    def frontlight_step(self) -> int:
        """Distance between two adjacent frontlight presets."""

        levels = self.frontlight_levels
        if len(levels) < 2:
            return 1
        return levels[1][0] - levels[0][0]

    @property
    def mixed_frontlight_mode(self) -> int | None:
        """Value of the `mixed` preset, used to spot custom-mode read-backs."""

        for value, name in self.frontlight_modes.items():
            if name == "mixed":
                return value
        return None

    @property
    def off_frontlight_mode(self) -> int | None:
        """Value of the `off` preset, used to gate the brightness slider."""

        for value, name in self.frontlight_modes.items():
            if name == "off":
                return value
        return None

    @property
    def frontlight_max(self) -> int:
        """Highest brightness preset of the profile."""

        if not self.frontlight_levels:
            return 0
        return max(value for value, _label in self.frontlight_levels)


# The confirmed values of the target unit. Protocol `0x30`, 40 Hz, EDID
# model `Paperlike H D`; this is the HD-FT variant of the monitor, with
# frontlight and touchscreen (dasungctl uses only the serial side). The
# evidence is in docs/protocol.md. The mode numbers, speed labels and
# frontlight tables were calibrated with the physical controls on this one
# panel, so they must not be copied to another model without the same tests.
PAPERLIKE_HD_13 = PanelProfile(
    key="paperlike-hd-13.3",
    name='Dasung Paperlike HD Revolutionary 13.3" (FT)',
    protocol=0x30,
    refresh_hz=40,
    edid_names=("Paperlike",),
    modes={1: "auto", 2: "text", 3: "graphic", 4: "video"},
    speed_labels=("Fast", "Fast+", "Fast++", "Fast+++", "Fast++++"),
    frontlight_levels=_frontlight_levels(step=10, count=10),
    frontlight_modes={0: "off", 1: "cold", 2: "warm", 3: "mixed"},
    preset_temperatures={1: 100, 2: 0, 3: 70},
    custom_frontlight_mode=4,
    temperature_levels=(100, 89, 78, 67, 56, 44, 33, 22, 11, 0),
    read_fields=(
        "mode",
        "contrast",
        "speed",
        "frontlight",
        "temperature",
        "frontlight_mode",
    ),
    limits={
        "contrast": (1, 9),
        "speed": (1, 5),
        "frontlight": (0, 0xFF),
        "temperature": (0, 0xFF),
        "frontlight_mode": (0, 4),
    },
)

# Registry of the shipped profiles. A new model is a new `PanelProfile` here
# plus the tests and documentation described in docs/panels.md.
PANELS: tuple[PanelProfile, ...] = (PAPERLIKE_HD_13,)
DEFAULT_PANEL = PAPERLIKE_HD_13


def panel_names() -> tuple[str, ...]:
    """Keys of the shipped profiles, for error messages and docs."""

    return tuple(profile.key for profile in PANELS)


def get_panel(panel: "PanelProfile | str | None" = None) -> PanelProfile:
    """Resolve a profile by key; None means the default profile."""

    if panel is None:
        return DEFAULT_PANEL
    if isinstance(panel, PanelProfile):
        return panel
    for profile in PANELS:
        if profile.key == panel:
            return profile
    known = ", ".join(panel_names())
    raise ValueError(f"unknown panel {panel!r}: known panels are {known}")
