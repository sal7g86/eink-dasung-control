"""Test-wide isolation: never read or write the real user directories."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_dirs(tmp_path, monkeypatch):
    """Point the XDG base directories at a per-test temporary tree."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
