"""Tests for the command-line entry point, dispatch and tray re-exec."""

from contextlib import nullcontext
import json
from pathlib import Path

import pytest

from dasungctl import cli
from dasungctl.config import Config
from dasungctl.transport import TransportError


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(cli, "serial_lock", lambda: nullcontext())
    monkeypatch.setattr(cli, "load_config", lambda path=None: Config())


@pytest.fixture
def tray_calls(monkeypatch):
    calls = []

    def fake_run_tray(args, log=None):
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "_run_tray", fake_run_tray)
    return calls


@pytest.mark.parametrize("argv", ([], ["tray"]))
def test_without_command_the_tray_starts(argv, tray_calls):
    assert cli.main(argv) == 0
    assert len(tray_calls) == 1


def test_serial_options_reach_the_tray_from_the_default_command(tray_calls):
    assert cli.main(
        ["--device", "/dev/test-monitor", "--timeout", "2.5", "--interval", "60"]
    ) == 0
    args = tray_calls[0]
    assert args.device == "/dev/test-monitor"
    assert args.timeout == 2.5
    assert args.interval == 60


def test_tray_options_are_forwarded(monkeypatch):
    from dasungctl import tray

    calls = []

    def fake_run_tray(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(tray, "run_tray", fake_run_tray)
    assert cli.main(
        [
            "--device",
            "/dev/test-monitor",
            "--timeout",
            "2.5",
            "--interval",
            "60",
            "tray",
            "--install-autostart",
        ]
    ) == 0
    assert calls[0]["device"] == "/dev/test-monitor"
    assert calls[0]["timeout"] == 2.5
    assert calls[0]["interval"] == 60
    assert calls[0]["install_autostart"] is True


def test_tray_without_gtk_reexecutes_the_system_python(monkeypatch, capsys):
    from dasungctl import tray

    def missing(**kwargs):
        raise tray.TrayDependencyError("no gi")

    monkeypatch.setattr(tray, "run_tray", missing)
    reexec = []
    monkeypatch.setattr(
        cli,
        "_reexec_tray",
        lambda args, exc, log=None: reexec.append(str(exc)) or 0,
    )

    assert cli.main(["--interval", "120", "tray"]) == 0
    assert reexec == ["no gi"]


def test_tray_arguments_build_the_module_command_line():
    args = cli.build_parser().parse_args(
        ["--device", "/dev/ttyUSB0", "--timeout", "2.5", "tray"]
    )
    assert cli._tray_arguments(args) == [
        "--module",
        "dasungctl.tray",
        "--device",
        "/dev/ttyUSB0",
        "--timeout",
        "2.5",
    ]


def test_tray_arguments_work_without_the_subcommand():
    args = cli.build_parser().parse_args([])

    assert cli._tray_arguments(args) == ["--module", "dasungctl.tray"]


def test_tray_python_skips_the_venv_even_when_python3_points_to_it(
    monkeypatch, tmp_path
):
    # An active virtualenv puts its own python first on PATH; the probe must
    # still find the system interpreter that provides GTK bindings.
    venv_python = tmp_path / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    monkeypatch.setattr(cli.sys, "executable", str(venv_python))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: str(venv_python))
    probed = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(argv, **kwargs):
        probed.append(argv[0])
        return Result(0 if argv[0] != str(venv_python) else 1)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._tray_python() == str(Path("/usr/bin/python3").resolve())
    assert str(venv_python) not in probed


def test_tray_python_probes_the_system_python_a_venv_is_built_on(
    monkeypatch, tmp_path
):
    # `python3 -m venv` symlinks the interpreter, so the venv python and the
    # system python are the same file: the system one is still a different
    # environment (no venv site-packages) and must be probed.
    system_python = tmp_path / "usr" / "bin" / "python3"
    system_python.parent.mkdir(parents=True)
    system_python.write_text("")
    venv_python = tmp_path / "venv" / "bin" / "python3.14"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(system_python)
    monkeypatch.setattr(cli.sys, "executable", str(venv_python))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: str(venv_python))
    probed = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(argv, **kwargs):
        probed.append(argv[0])
        return Result(0 if argv[0] != str(venv_python) else 1)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._tray_python() == str(Path("/usr/bin/python3").resolve())
    assert str(venv_python) not in probed


def test_tray_python_returns_none_when_no_interpreter_has_the_bindings(
    monkeypatch,
):
    class Result:
        returncode = 1

    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs: Result())

    assert cli._tray_python() is None


def test_reexec_reports_the_dependency_error_when_no_python_helps(
    monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_tray_python", lambda: None)
    args = cli.build_parser().parse_args(["tray"])

    assert cli._reexec_tray(args, RuntimeError("no gi")) == 1
    assert "no gi" in capsys.readouterr().err


def test_transport_errors_have_nonzero_exit(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise TransportError("monitor not found")

    monkeypatch.setattr(cli.doctor_module, "run_checks", fail)
    assert cli.main(["doctor"]) == 1
    assert capsys.readouterr().err == "dasungctl: error: monitor not found\n"


@pytest.mark.parametrize("command", ["info", "tui"])
def test_removed_commands_are_rejected(command, capsys):
    with pytest.raises(SystemExit):
        cli.main([command])
    assert "invalid choice" in capsys.readouterr().err


def test_devices_lists_confirmed_ch340_without_opening_it(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "serial_devices",
        lambda: [("/dev/ttyUSB4", 0x1A86, 0x7523, "USB Serial")],
    )

    assert cli.main(["devices"]) == 0
    assert capsys.readouterr().out == (
        "/dev/ttyUSB4  1a86:7523  USB Serial [Dasung CH340 candidate]\n"
    )


def test_devices_reports_when_no_serial_devices_are_visible(monkeypatch, capsys):
    monkeypatch.setattr(cli, "serial_devices", lambda: [])

    assert cli.main(["devices"]) == 1
    assert capsys.readouterr().out == "No serial devices found.\n"


def test_doctor_returns_failure_when_a_check_fails(monkeypatch, capsys):
    from dasungctl.doctor import Check

    monkeypatch.setattr(
        cli.doctor_module,
        "run_checks",
        lambda *args, **kwargs: [
            Check("device", "ok", "/dev/ttyUSB0"),
            Check("write ack", "fail", "no response"),
        ],
    )

    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "[  ok] device: /dev/ttyUSB0" in out
    assert "[fail] write ack: no response" in out


def test_doctor_json(monkeypatch, capsys):
    monkeypatch.setattr(cli.doctor_module, "run_checks", lambda *args, **kwargs: [])

    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_commands_truncate_and_write_the_log(monkeypatch):
    from dasungctl import paths

    monkeypatch.setattr(cli, "serial_devices", lambda: [])
    target = paths.log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old run\n", encoding="utf-8")

    assert cli.main(["devices"]) == 1

    content = target.read_text(encoding="utf-8")
    assert "old run" not in content
    assert "started: dasungctl devices" in content
    assert "devices: none found" in content
