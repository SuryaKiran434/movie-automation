"""
Process lock — shared by monitor.py and test_email.py
======================================================
Uses a file-based exclusive lock (fcntl.flock) so that only ONE of the two
scripts can run at a time, regardless of whether it was launched by cron or
triggered manually.

How it works
------------
- Both scripts try to acquire an exclusive lock on the same file: .monitor.lock
- The lock is non-blocking: if it's already held, the caller gets None immediately
  (no waiting, no hanging).
- The lock is released automatically when the process exits — even on crash or
  Ctrl-C — because the OS closes all open file handles on process termination.
- The current PID is written into the lock file so you can see who holds it.

Usage (context manager)
-----------------------
    with ProcessLock(logger) as locked:
        if not locked:
            sys.exit(0)   # another instance is running — exit gracefully
        # ... do work ...
"""

import fcntl
import logging
import os
from pathlib import Path
from types import TracebackType

LOCK_FILE = Path(__file__).parent.resolve() / ".monitor.lock"


class ProcessLock:
    """
    Exclusive non-blocking process lock backed by a file.

    Use as a context manager:

        with ProcessLock(logger) as locked:
            if not locked:
                sys.exit(0)
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._fh = None          # file handle — kept open for the lock lifetime
        self.acquired = False

    def __enter__(self) -> "ProcessLock":
        try:
            # Open (or create) the lock file
            self._fh = open(LOCK_FILE, "w")
            # LOCK_EX = exclusive lock  |  LOCK_NB = non-blocking (raise immediately if busy)
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # We got the lock — record our PID so it's visible in the file
            self._fh.write(f"{os.getpid()}\n")
            self._fh.flush()
            self.acquired = True
            self._logger.debug(
                "Process lock acquired — PID %d → %s", os.getpid(), LOCK_FILE
            )
        except BlockingIOError:
            # Lock is held by another process
            if self._fh:
                self._fh.close()
                self._fh = None
            holder = _read_lock_pid()
            self._logger.warning(
                "Another instance is already running (PID %s). "
                "This run will be skipped.",
                holder if holder else "unknown",
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fh is not None:
            # Releasing the lock: unlock then close the file handle.
            # The OS also releases it automatically on process exit.
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                self._fh.close()
                self._fh = None
            self._logger.debug("Process lock released — PID %d", os.getpid())


def _read_lock_pid() -> str | None:
    """Return the PID written in the lock file, or None if unreadable."""
    try:
        return LOCK_FILE.read_text().strip() or None
    except OSError:
        return None
