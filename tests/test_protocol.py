"""Frame encoding/parsing tests against the confirmed wire examples."""

import pytest

from dasungctl.protocol import (
    AcknowledgementError,
    CUSTOM_FRONTLIGHT_MODE,
    Command,
    DisplayMode,
    FrontlightMode,
    Parameter,
    ProtocolError,
    encode_command,
    encode_read,
    display_mode_name,
    frontlight_mode_name,
    parse_ack,
    parse_response,
    speed_name,
)


READ_CASES = (
    (Parameter.VERSION, b"5FF50A10000000000000A0FA", b"5FF5F00A103010000000A0FA"),
    (Parameter.CONTRAST, b"5FF50A01000000000000A0FA", b"5FF5F00A010600000000A0FA"),
    (Parameter.MODE, b"5FF50A02000000000000A0FA", b"5FF5F00A020400000000A0FA"),
    (
        Parameter.TEMPERATURE,
        b"5FF50A08000000000000A0FA",
        b"5FF5F00A080000000000A0FA",
    ),
    (
        Parameter.FRONTLIGHT,
        b"5FF50A09000000000000A0FA",
        b"5FF5F00A092800000000A0FA",
    ),
)


@pytest.mark.parametrize(("parameter", "tx", "rx"), READ_CASES)
def test_exact_confirmed_packets(parameter, tx, rx):
    assert encode_read(parameter) == tx

    response = parse_response(rx, expected_parameter=parameter)
    assert response.parameter_id == parameter


def test_confirmed_response_values():
    version = parse_response(READ_CASES[0][2], expected_parameter=Parameter.VERSION)
    assert version.data == bytes.fromhex("30 10 00 00 00")
    assert parse_response(READ_CASES[1][2]).data[0] == 6
    assert parse_response(READ_CASES[2][2]).data[0] == 4
    assert parse_response(READ_CASES[3][2]).data[0] == 0
    assert parse_response(READ_CASES[4][2]).data[0] == 40


@pytest.mark.parametrize(
    "wire",
    (
        b"",
        b"5FF5F00A010600000000A0F",
        b"5FF5F00A010600000000A0FA00",
        b"5FF5F00A010600000000A0FZ",
        b"00F5F00A010600000000A0FA",
        b"5FF5F00A010600000000A000",
        b"5FF5000A010600000000A0FA",
        b"5FF5F001010600000000A0FA",
    ),
)
def test_rejects_malformed_responses(wire):
    with pytest.raises(ProtocolError):
        parse_response(wire)


def test_rejects_response_for_another_parameter():
    with pytest.raises(ProtocolError, match="does not match"):
        parse_response(
            b"5FF5F00A020400000000A0FA",
            expected_parameter=Parameter.CONTRAST,
        )


@pytest.mark.parametrize(
    ("parameter", "tx"),
    (
        (Parameter.FRONTLIGHT_MODE, b"5FF50A07000000000000A0FA"),
        (Parameter.TEXT_ENHANCEMENT, b"5FF50A12000000000000A0FA"),
    ),
)
def test_official_client_read_selectors_are_encodable(parameter, tx):
    assert encode_read(parameter) == tx


@pytest.mark.parametrize(
    ("parameter", "rx", "value"),
    (
        (Parameter.FRONTLIGHT_MODE, b"5FF5F00A070200000000A0FA", 2),
        (Parameter.TEXT_ENHANCEMENT, b"5FF5F00A120100000000A0FA", 1),
    ),
)
def test_synthetic_response_roundtrip_for_unconfirmed_selectors(
    parameter, rx, value
):
    response = parse_response(rx, expected_parameter=parameter)
    assert response.parameter_id == parameter
    assert response.data[0] == value


NEWLY_CONFIRMED_READS = (
    (0x03, b"5FF5F00A030100000000A0FA", 1),
    (0x04, b"5FF5F00A040400000000A0FA", 4),
    (0x07, b"5FF5F00A070200000000A0FA", 2),
    (0x0B, b"5FF5F00A0B0100000000A0FA", 1),
    (0x11, b"5FF5F00A110200000000A0FA", 2),
)


@pytest.mark.parametrize(("parameter_id", "rx", "value"), NEWLY_CONFIRMED_READS)
def test_reads_captured_during_command_verification_parse(parameter_id, rx, value):
    response = parse_response(rx, expected_parameter=parameter_id)
    assert response.parameter_id == parameter_id
    assert response.data[0] == value


@pytest.mark.parametrize(
    ("command", "value", "expected"),
    (
        (Command.CONTRAST, 6, b"5FF50106000000000000A0FA"),
        (Command.MODE, 4, b"5FF50204000000000000A0FA"),
        (Command.REFRESH, 0, b"5FF50300000000000000A0FA"),
        (Command.REFRESH, 1, b"5FF50301000000000000A0FA"),
        (Command.SPEED, 3, b"5FF50403000000000000A0FA"),
        (Command.FRONTLIGHT_MODE, 2, b"5FF50702000000000000A0FA"),
        (Command.FRONTLIGHT, 40, b"5FF50928000000000000A0FA"),
        (Command.TEMPERATURE, 0, b"5FF50800000000000000A0FA"),
        (Command.MUX, 1, b"5FF50B01000000000000A0FA"),
        (Command.TEXT_ENHANCEMENT, 1, b"5FF51201000000000000A0FA"),
        (Command.DITHERING, 0, b"5FF52000000000000000A0FA"),
    ),
)
def test_official_client_command_encoding(command, value, expected):
    assert encode_command(command, value) == expected


def test_command_encoder_preserves_explicit_six_byte_payload():
    assert encode_command(0x7E, 0x12, bytes.fromhex("010203040506")) == (
        b"5FF57E12010203040506A0FA"
    )


def test_target_display_mode_values_match_physical_control_captures():
    assert {
        mode.cli_name: int(mode)
        for mode in DisplayMode
    } == {"auto": 1, "text": 2, "graphic": 3, "video": 4}


def test_unknown_display_mode_preserves_an_explicit_unknown_label():
    assert display_mode_name(0x7F) == "unknown"


def test_target_frontlight_mode_values_match_physical_lamp_captures():
    assert {
        mode.cli_name: int(mode)
        for mode in FrontlightMode
    } == {"off": 0, "cold": 1, "warm": 2, "mixed": 3}


def test_frontlight_mode_name_labels_the_project_custom_value():
    assert frontlight_mode_name(CUSTOM_FRONTLIGHT_MODE) == "custom"


def test_unknown_frontlight_mode_preserves_an_explicit_unknown_label():
    assert frontlight_mode_name(0x7F) == "unknown"


def test_target_speed_values_use_the_official_client_labels():
    assert [speed_name(value) for value in range(1, 6)] == [
        "Fast",
        "Fast+",
        "Fast++",
        "Fast+++",
        "Fast++++",
    ]


@pytest.mark.parametrize("value", (0, 6, 0x7F, None))
def test_unknown_speed_values_use_an_explicit_unknown_label(value):
    assert speed_name(value) == "unknown"


def test_parse_ack_accepts_the_observed_write_acknowledgement():
    assert (
        parse_ack(b"5FF5F001000000000000A0FA", expected_command=Command.CONTRAST)
        == Command.CONTRAST
    )


def test_parse_ack_rejects_a_different_command():
    with pytest.raises(AcknowledgementError, match="does not match"):
        parse_ack(b"5FF5F001000000000000A0FA", expected_command=Command.MODE)


def test_parse_ack_rejects_a_read_response():
    with pytest.raises(AcknowledgementError, match="read response"):
        parse_ack(b"5FF5F00A010600000000A0FA", expected_command=Command.CONTRAST)


def test_parse_ack_rejects_wrong_length():
    with pytest.raises(AcknowledgementError, match="exactly 24"):
        parse_ack(b"5FF5F001", expected_command=Command.CONTRAST)
