"""SQLite helpers and a cross-platform single-instance lock."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path


class StorageError(RuntimeError):
    """Raised when durable state cannot be read or written. Callers fail closed."""


def connect(path: str | Path, readonly: bool = False, must_exist: bool = True) -> sqlite3.Connection:
    p = Path(path)
    if must_exist and not p.exists():
        raise StorageError(f"Database {p.name} is missing")
    try:
        if readonly:
            con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=10,
                                  isolation_level=None, check_same_thread=False)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(p), timeout=10, isolation_level=None, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error as e:
        raise StorageError(f"Cannot open {p.name}: {e}") from e


@contextlib.contextmanager
def immediate(con: sqlite3.Connection):
    """A write transaction that takes the database write lock up front, so
    read-check-write sequences (like budget reservations) are atomic across
    threads AND processes."""
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as e:
        raise StorageError(f"Could not start a transaction: {e}") from e
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


class InstanceLock:
    """Holds an OS-level exclusive lock on a file for the life of a process.

    Prevents two gateways (or two watchdogs) from running against the same
    data at once. Works on Windows (msvcrt) and POSIX (fcntl). The OS drops
    the lock automatically if the process dies.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise StorageError(f"Another ATLAS process already holds {self.path.name}") from e
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self.fh = fh

    def release(self) -> None:
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
