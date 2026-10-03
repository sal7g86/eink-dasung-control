"""Last-configuration persistence tests: JSON round trip and validation."""

import json

import pytest

from dasungctl.client import DasungClient, MonitorInfo
from dasungctl.protocol import DisplayMode
from dasungctl.state import (
    StateError,
    apply_fields,
    load_last,
    resolve_mode,
    save_last,
)

from fakes import FakeTransport


def _info(**overrides) -> MonitorInfo:
    base = dict(
        protocol_version=0x30,
        additional_version_field=0x10,
        contrast=6,
        mode=4,
        selector_03=1,
        speed=4,
        frontlight_mode=2,
        temperature=0,
        frontlight=40,
        mux=1,
        selector_11=2,
    )
    base.update(overrides)
    return MonitorInfo(**base)


def test_save_last_writes_only_the_restorable_fields(tmp_path):
    path = save_last(_info(), tmp_path / "last-state.json")

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["mode"] == "video"
    assert data["contrast"] == 6
    assert data["speed"] == 4
    assert data["frontlight"] == 40
    assert data["frontlight_mode"] == 2
    assert "autorefresh" not in data
    assert "mux" not in data
    assert "protocol_version" not in data
    assert "saved_at" in data


def test_save_last_stores_the_tray_preferences(tmp_path):
    path = save_last(
        _info(),
        tmp_path / "last-state.json",
        prefs={"autorefresh": True, "autorefresh_interval": 5},
    )

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["autorefresh"] is True
    assert data["autorefresh_interval"] == 5.0
    assert data["contrast"] == 6


def test_save_last_skips_unknown_values(tmp_path):
    path = save_last(
        _info(
            mode=None,
            contrast=None,
            speed=None,
            frontlight=None,
            temperature=None,
            frontlight_mode=None,
        ),
        tmp_path / "last-state.json",
    )

    data = json.loads(path.read_text(encoding="utf-8"))

    assert set(data) == {"saved_at"}


def test_load_last_returns_none_without_a_file(tmp_path):
    assert load_last(tmp_path / "absent.json") is None


def test_last_configuration_round_trip(tmp_path):
    path = tmp_path / "last-state.json"
    save_last(_info(contrast=3, temperature=140, frontlight_mode=4), path)

    saved = load_last(path)

    assert saved["mode"] == DisplayMode.VIDEO
    assert saved["contrast"] == 3
    assert saved["temperature"] == 140
    assert saved["frontlight_mode"] == 4


def test_tray_preferences_round_trip(tmp_path):
    path = tmp_path / "last-state.json"
    save_last(
        _info(),
        path,
        prefs={"autorefresh": True, "autorefresh_interval": 30},
    )

    saved = load_last(path)

    assert saved["autorefresh"] is True
    assert saved["autorefresh_interval"] == 30.0
    assert saved["contrast"] == 6


def test_ghost_clear_preference_round_trip(tmp_path):
    path = tmp_path / "last-state.json"
    save_last(
        _info(),
        path,
        prefs={"ghost_clear": {"enabled": True, "white_ms": 300}},
    )

    saved = load_last(path)

    assert saved["ghost_clear"]["enabled"] is True
    assert saved["ghost_clear"]["white_ms"] == 300
    # The loader fills in the defaults of the missing fields.
    assert saved["ghost_clear"]["black_ms"] == 15
    assert saved["ghost_clear"]["delay"] == 15.0
    # Auto-refresh keys the caller did not provide must not appear.
    assert "autorefresh" not in saved


def test_ghost_estimate_preference_round_trip(tmp_path):
    path = tmp_path / "last-state.json"
    save_last(_info(), path, prefs={"ghost_estimate": False})

    saved = load_last(path)

    assert saved["ghost_estimate"] is False


def test_removed_ghost_clear_values_are_rejected(tmp_path):
    path = tmp_path / "last-state.json"
    path.write_text(json.dumps({"ghost_clear": True}))

    with pytest.raises(StateError, match="ghost_clear"):
        load_last(path)


@pytest.mark.parametrize(
    "payload",
    (
        "{not json",
        json.dumps([1, 2]),
        json.dumps({"mode": "sepia"}),
        json.dumps({"contrast": 99}),
        json.dumps({"speed": True}),
        json.dumps({"frontlight": "bright"}),
        json.dumps({"temperature": 300}),
        json.dumps({"frontlight_mode": 9}),
        json.dumps({"autorefresh": "yes"}),
        json.dumps({"autorefresh": 1}),
        json.dumps({"autorefresh_interval": -5}),
        json.dumps({"autorefresh_interval": True}),
        json.dumps({"autorefresh_interval": "5s"}),
        json.dumps({"ghost_clear": "yes"}),
        json.dumps({"ghost_clear": 1}),
        json.dumps({"ghost_clear": {"white_ms": 0}}),
        json.dumps({"ghost_clear": {"delay": -1}}),
        json.dumps({"ghost_clear": {"nope": 1}}),
        json.dumps({"ghost_estimate": "off"}),
        json.dumps({"ghost_estimate": 1}),
        json.dumps({"nope": 1}),
    ),
)
def test_invalid_last_configuration_is_rejected(tmp_path, payload):
    path = tmp_path / "last-state.json"
    path.write_text(payload)

    with pytest.raises(StateError):
        load_last(path)


def test_resolve_mode_accepts_names_numbers_and_enums():
    assert resolve_mode("text") == DisplayMode.TEXT
    assert resolve_mode("VIDEO") == DisplayMode.VIDEO
    assert resolve_mode(3) == DisplayMode.GRAPHIC
    assert resolve_mode(DisplayMode.AUTO) == DisplayMode.AUTO


@pytest.mark.parametrize("value", ("sepia", 9, True))
def test_resolve_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        resolve_mode(value)


def test_apply_fields_sends_fields_in_safe_order():
    transport = FakeTransport()
    client = DasungClient(transport)

    frames = apply_fields(
        client,
        {
            "frontlight": 40,
            "contrast": 6,
            "mode": "text",
            "speed": 3,
        },
    )

    assert list(frames) == ["mode", "contrast", "speed", "frontlight"]
    assert transport.requests == [
        b"5FF50202000000000000A0FA",
        b"5FF50106000000000000A0FA",
        b"5FF50403000000000000A0FA",
        b"5FF50928000000000000A0FA",
    ]


def test_apply_fields_without_wait_uses_send():
    transport = FakeTransport()
    client = DasungClient(transport)

    apply_fields(client, {"contrast": 4}, wait=False)

    assert transport.sent == [b"5FF50104000000000000A0FA"]
    assert transport.requests == []


def test_apply_fields_ignores_unset_fields():
    transport = FakeTransport()

    frames = apply_fields(DasungClient(transport), {})

    assert frames == {}
    assert transport.requests == []
