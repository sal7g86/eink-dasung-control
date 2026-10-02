"""Config file parsing and validation tests."""

import json

import pytest

from dasungctl.config import (
    DEFAULT_GHOST,
    Config,
    ConfigError,
    autorefresh_settings,
    ghost_settings,
    load_config,
)


def test_missing_file_returns_defaults(tmp_path):
    config = load_config(tmp_path / "absent.json")
    assert config == Config()
    assert config.device == "auto"
    assert config.timeout == 1.0
    assert config.panel == "paperlike-hd-13.3"
    assert config.autorefresh == {}
    assert config.ghost == {}
    assert ghost_settings(config) == DEFAULT_GHOST


def test_panel_key_selects_a_shipped_profile(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"panel": "paperlike-hd-13.3"}))

    assert load_config(path).panel == "paperlike-hd-13.3"


def test_loads_device_timeout_and_autorefresh(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "device": "/dev/ttyUSB9",
                "timeout": 2.5,
                "autorefresh": {"interval": 120, "hard": True},
                "ghost": {
                    "interval": 5,
                    "max_interval": 60,
                    "threshold": 33,
                    "output": "DP-2",
                },
            }
        )
    )

    config = load_config(path)

    assert config.device == "/dev/ttyUSB9"
    assert config.timeout == 2.5
    assert autorefresh_settings(config) == (120.0, True)
    settings = ghost_settings(config)
    assert settings["interval"] == 5.0
    assert settings["max_interval"] == 60.0
    assert settings["threshold"] == 33.0
    assert settings["output"] == "DP-2"
    # Untouched keys keep their defaults.
    assert settings["enabled"] is True


def test_ghost_clear_section_is_validated_and_merged(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "ghost": {
                    "clear": {
                        "enabled": True,
                        "style": "single",
                        "white_ms": 300,
                        "grey_level": 200,
                        "delay": 7,
                    }
                }
            }
        )
    )

    settings = ghost_settings(load_config(path))

    assert settings["clear"]["enabled"] is True
    assert settings["clear"]["style"] == "single"
    assert settings["clear"]["white_ms"] == 300
    assert settings["clear"]["grey_level"] == 200
    assert settings["clear"]["delay"] == 7.0
    # Untouched clear keys keep their defaults.
    assert settings["clear"]["black_level"] == DEFAULT_GHOST["clear"]["black_level"]
    assert settings["clear"]["grey_ms"] == DEFAULT_GHOST["clear"]["grey_ms"]


def test_removed_clear_parameters_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "ghost": {
                    "clear": {
                        "severity": 1,
                        "min_age": 9,
                        "max_area": 0.6,
                    }
                }
            }
        )
    )

    with pytest.raises(ConfigError, match="unknown fields"):
        load_config(path)


@pytest.mark.parametrize(
    "payload",
    (
        "{not json",
        json.dumps({"unknown": 1}),
        json.dumps({"timeout": 0}),
        json.dumps({"device": ""}),
        json.dumps({"profiles": {"reading": {"contrast": 6}}}),
        json.dumps({"autorefresh": {"interval": -1}}),
        json.dumps({"autorefresh": {"hard": "yes"}}),
        json.dumps({"ghost": []}),
        json.dumps({"ghost": {"interval": 0}}),
        json.dumps({"ghost": {"max_interval": 1}}),
        json.dumps({"ghost": {"max_interval": "soon"}}),
        json.dumps({"ghost": {"threshold": 101}}),
        json.dumps({"ghost": {"notify": True}}),
        json.dumps({"ghost": {"enabled": 1}}),
        json.dumps({"ghost": {"output": ""}}),
        json.dumps({"ghost": {"width": 80}}),
        json.dumps({"ghost": {"unknown": 1}}),
        json.dumps({"ghost": {"clear": []}}),
        json.dumps({"ghost": {"clear": {"enabled": "yes"}}}),
        json.dumps({"ghost": {"clear": {"style": "pulse"}}}),
        json.dumps({"ghost": {"clear": {"white_ms": 0}}}),
        json.dumps({"ghost": {"clear": {"black_ms": "fast"}}}),
        json.dumps({"ghost": {"clear": {"grey_level": 256}}}),
        json.dumps({"ghost": {"clear": {"delay": -1}}}),
        json.dumps({"ghost": {"clear": {"delay": "soon"}}}),
        json.dumps({"ghost": {"clear": {"unknown": 1}}}),
        json.dumps({"panel": ""}),
        json.dumps({"panel": 1}),
        json.dumps({"panel": "unknown-model"}),
    ),
)
def test_invalid_config_is_rejected(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(payload)

    with pytest.raises(ConfigError):
        load_config(path)
