"""Exclusive access to the monitor's serial interface."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import fcntl
from typing import Iterator

from . import paths
from .transport import TransportError


@contextmanager
def serial_lock(path: Path | None = None) -> Iterator[None]:
    """Hold an exclusive lock so two dasungctl processes cannot interleave.

    The lock is non-blocking: a second process gets a clear error instead of
    waiting behind an operation that may be sitting on a monitor timeout.
    The file content is only a diagnostic; the lock itself is the flock().
    """

    lock_file = path or paths.lock_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TransportError(
                "another dasungctl process is already using the monitor; "
                "stop it before starting a new session"
            ) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(handle.fileno()))
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
