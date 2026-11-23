"""A small cross-process exclusive lock built on a sidecar ".lock" file.

The review page, the MCP server and CLI commands are separate processes that write
the same audit log and offline ledger. Each write takes this lock, re-reads the file,
then writes, so no process ever acts on a stale in-memory copy.

Windows uses msvcrt.locking; everything else uses fcntl.flock. Both block until the
lock is free (msvcrt retries for up to `timeout` seconds).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_local_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _registry_lock:
        return _local_locks.setdefault(key, threading.Lock())


@contextmanager
def exclusive(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Hold an exclusive lock for `path` across threads and processes."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(str(lock_path.resolve())):
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire(fd, timeout)
            try:
                yield
            finally:
                _release(fd)
        finally:
            os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _acquire(fd: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {fd} within {timeout}s") from None
                time.sleep(0.01)

    def _release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(fd: int, timeout: float) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
