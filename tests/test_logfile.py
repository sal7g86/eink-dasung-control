"""Per-run log file tests: truncation, header, echo and fallback."""

from dasungctl.logfile import AppLog, NullLog, start


def test_start_truncates_the_file_and_writes_the_header(tmp_path):
    path = tmp_path / "dasungctl.log"
    path.write_text("previous run\n", encoding="utf-8")

    log = start(["devices"], command="devices", path=path)
    log.info("3 devices found")
    log.close()

    content = path.read_text(encoding="utf-8")
    assert "previous run" not in content
    assert "started: dasungctl devices (pid " in content
    assert "[info] 3 devices found" in content


def test_start_without_arguments_does_not_leave_a_double_space(tmp_path):
    log = start([], command="doctor", path=tmp_path / "dasungctl.log")
    log.close()

    header = (tmp_path / "dasungctl.log").read_text(encoding="utf-8")
    assert "started: dasungctl doctor (pid " in header
    assert "  (pid" not in header


def test_echo_mirrors_lines_to_stderr(tmp_path, capsys):
    log = AppLog(tmp_path / "dasungctl.log", echo=True)

    log.warn("watch out")

    err = capsys.readouterr().err
    assert "[warn] watch out" in err
    assert "[warn] watch out" in (tmp_path / "dasungctl.log").read_text(
        encoding="utf-8"
    )
    log.close()


def test_echo_off_keeps_the_terminal_clean(tmp_path, capsys):
    log = AppLog(tmp_path / "dasungctl.log")

    log.error("hidden")

    assert capsys.readouterr().err == ""
    log.close()


def test_unwritable_path_falls_back_to_the_terminal(tmp_path, capsys):
    # A directory cannot be opened as a file: the command must keep running.
    log = AppLog(tmp_path)

    log.info("still alive")

    err = capsys.readouterr().err
    assert "cannot write the log file" in err
    assert "[info] still alive" in err
    log.close()


def test_default_path_follows_the_isolated_state_dir(tmp_path):
    from dasungctl import paths

    log = start(["tray"], command="tray")
    try:
        assert log.path == paths.log_path()
        assert log.path.parent == tmp_path / "state" / "dasungctl"
        assert log.path.exists()
    finally:
        log.close()


def test_close_stops_file_writes(tmp_path):
    path = tmp_path / "dasungctl.log"
    log = AppLog(path)
    log.info("before")
    log.close()
    log.info("after")

    content = path.read_text(encoding="utf-8")
    assert "before" in content
    assert "after" not in content


def test_null_log_discards_everything(capsys):
    log = NullLog()

    log.info("a")
    log.warn("b")
    log.error("c")
    log.close()

    assert capsys.readouterr().err == ""
