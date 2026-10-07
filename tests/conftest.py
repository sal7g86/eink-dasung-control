"""Test-wide isolation: never read or write the real user directories."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_dirs(tmp_path, monkeypatch):
    """Point the XDG base directories at a per-test temporary tree."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture(autouse=True)
def _no_live_session(monkeypatch):
    """Hide the host's display session: the tests must not depend on it.

    The suite is run both on X11 and on Wayland desktops; the backend
    selection reads `WAYLAND_DISPLAY` and `XDG_CURRENT_DESKTOP`, so a test
    would otherwise behave differently on the two hosts. Tests that need a
    specific session set the variable themselves with monkeypatch.
    """

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
