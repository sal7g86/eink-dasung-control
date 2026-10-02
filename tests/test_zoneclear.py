"""Zone-clearing policy, flash sequence and settings; no GTK, no screen."""

import pytest

from dasungctl.ghostwatch import GhostElement
from dasungctl.zoneclear import (
    DEFAULT_CLEAR,
    ClearError,
    ClearSettings,
    due_clear_elements,
    flash_phases,
    validate_clear,
)


def element(x=0, y=0, width=120, height=120, age=60.0):
    return GhostElement(
        x=x,
        y=y,
        width=width,
        height=height,
        severity=20,
        dark=True,
        age=age,
        app="konsole",
    )


def test_default_settings_match_the_documented_values():
    settings = ClearSettings()

    assert settings.delay == 15.0
    assert settings.style == "single"
    assert settings.white_ms == 15
    assert settings.black_ms == 15
    assert settings.white_level == 255
    assert settings.black_level == 0
    assert settings.grey_level == 168
    assert settings.grey_ms == 30
    assert settings.enabled is True


def test_flash_phases_white_black_and_single():
    assert flash_phases(ClearSettings(style="white-black")) == (
        (255, 15),
        (0, 15),
        (None, 0),
    )
    assert flash_phases(ClearSettings()) == ((168, 30), (None, 0))

    custom = ClearSettings(
        style="white-black",
        white_level=200,
        white_ms=150,
        black_level=10,
        black_ms=250,
    )
    assert flash_phases(custom) == ((200, 150), (10, 250), (None, 0))

    # A too-short phase is stretched to the minimum the timers can express.
    quick = ClearSettings(style="white-black", white_ms=0, black_ms=0)
    assert flash_phases(quick) == ((255, 1), (0, 1), (None, 0))


def test_policy_returns_every_due_area_oldest_first():
    settings = ClearSettings(delay=5.0)
    young = element(x=0, y=0, age=3.0)
    first = element(x=500, y=500, age=12.0)
    second = element(x=300, y=300, age=40.0)

    assert due_clear_elements([young], settings) == ()
    due = due_clear_elements([young, first, second], settings)
    assert due == (second, first)
    assert due_clear_elements([], settings) == ()


def test_validate_clear_fills_defaults_and_rejects_bad_values():
    assert validate_clear({}) == DEFAULT_CLEAR
    assert validate_clear({"delay": 7, "style": "single"}) == {
        **DEFAULT_CLEAR,
        "delay": 7.0,
        "style": "single",
    }
    with pytest.raises(ClearError):
        validate_clear({"nope": 1})
    with pytest.raises(ClearError):
        validate_clear({"white_ms": 0})
    with pytest.raises(ClearError):
        validate_clear({"style": "pulse"})
    with pytest.raises(ClearError):
        validate_clear({"grey_level": 256})
    with pytest.raises(ClearError):
        validate_clear({"delay": -1})


def test_validate_clear_rejects_the_removed_parameters():
    # The old parameter-heavy schema is gone: its keys are unknown fields.
    with pytest.raises(ClearError):
        validate_clear({"enabled": True, "min_age": 9.0, "severity": 1.0})


def test_clear_settings_from_mapping_ignores_unknown_keys():
    settings = ClearSettings.from_mapping({"delay": 8, "nope": 1})

    assert settings.delay == 8.0
    assert settings.style == "single"
    assert settings.enabled is True
