"""Panel profiles: confirmed tables, lookup and wiring into state/config."""

import json

import pytest

from dasungctl.config import Config
from dasungctl.panels import (
    DEFAULT_PANEL,
    PANELS,
    PanelProfile,
    get_panel,
    panel_names,
)
from dasungctl.state import StateError, load_last, resolve_mode


def test_default_profile_keeps_the_confirmed_values():
    profile = DEFAULT_PANEL

    assert profile.key == "paperlike-hd-13.3"
    assert profile.protocol == 0x30
    assert profile.refresh_hz == 40
    assert profile.name.startswith("Dasung Paperlike HD Revolutionary 13.3")

    assert profile.modes == {
        1: "auto",
        2: "text",
        3: "graphic",
        4: "video",
    }
    assert profile.display_mode_name(2) == "text"
    assert profile.display_mode_name(0x7F) == "unknown"
    assert profile.display_mode_name(None) == "unknown"

    assert profile.speed_labels == (
        "Fast",
        "Fast+",
        "Fast++",
        "Fast+++",
        "Fast++++",
    )
    assert profile.speed_name(1) == "Fast"
    assert profile.speed_name(5) == "Fast++++"
    assert profile.speed_name(9) == "unknown"

    assert profile.frontlight_levels[0] == (0, "Off")
    assert profile.frontlight_levels[-1] == (100, "10")
    assert profile.frontlight_step == 10
    assert profile.frontlight_max == 100
    assert profile.frontlight_label(58) == "6"
    assert profile.frontlight_label(None) is None

    assert profile.frontlight_modes == {
        0: "off",
        1: "cold",
        2: "warm",
        3: "mixed",
    }
    assert profile.mixed_frontlight_mode == 3
    assert profile.off_frontlight_mode == 0
    assert profile.custom_frontlight_mode == 4
    assert profile.frontlight_mode_name(4) == "custom"
    assert profile.frontlight_mode_name(1) == "cold"
    assert profile.frontlight_mode_name(9) == "unknown"

    assert profile.preset_temperatures == {1: 100, 2: 0, 3: 70}
    assert profile.temperature_levels == (
        100,
        89,
        78,
        67,
        56,
        44,
        33,
        22,
        11,
        0,
    )
    assert profile.temperature_level_number(56) == 5
    assert profile.temperature_byte(1) == 100
    assert profile.temperature_byte(10) == 0

    assert profile.read_fields == (
        "mode",
        "contrast",
        "speed",
        "frontlight",
        "temperature",
        "frontlight_mode",
    )
    assert profile.limits["contrast"] == (1, 9)
    assert profile.limits["frontlight_mode"] == (0, 4)


def test_get_panel_resolves_keys_and_profiles():
    assert get_panel() is DEFAULT_PANEL
    assert get_panel(None) is DEFAULT_PANEL
    assert get_panel(DEFAULT_PANEL) is DEFAULT_PANEL
    assert get_panel(DEFAULT_PANEL.key) is DEFAULT_PANEL
    assert DEFAULT_PANEL.key in panel_names()


def test_get_panel_rejects_unknown_keys_and_lists_the_known_ones():
    with pytest.raises(ValueError, match="paperlike-hd-13.3"):
        get_panel("nope")


def test_shipped_profiles_are_complete_and_unique():
    keys = [profile.key for profile in PANELS]
    assert len(keys) == len(set(keys))
    for profile in PANELS:
        assert isinstance(profile, PanelProfile)
        assert profile.name and profile.edid_names
        assert profile.modes and profile.speed_labels
        assert profile.frontlight_levels and profile.frontlight_modes
        assert profile.temperature_levels
        # Every read field except `mode` has a validation range.
        assert set(profile.limits) == set(profile.read_fields) - {"mode"}
        # The custom value is project-invented, never a preset.
        assert profile.custom_frontlight_mode not in profile.frontlight_modes


def test_state_validation_follows_the_profile(tmp_path):
    from dataclasses import replace

    profile = replace(
        DEFAULT_PANEL,
        limits={**DEFAULT_PANEL.limits, "contrast": (1, 5)},
    )
    path = tmp_path / "last-state.json"
    path.write_text(json.dumps({"contrast": 6}))

    with pytest.raises(StateError, match="1..5"):
        load_last(path, panel=profile)


def test_resolve_mode_follows_the_profile(tmp_path):
    from dataclasses import replace

    profile = replace(DEFAULT_PANEL, modes={1: "only"})
    assert resolve_mode("only", profile) == 1
    assert resolve_mode(1, profile) == 1
    with pytest.raises(ValueError):
        resolve_mode("text", profile)


def test_config_defaults_to_the_shipped_panel():
    assert Config().panel == DEFAULT_PANEL.key
