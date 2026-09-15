"""Single-instance guard.

Telegram allows one connection per session file, so two `watch` processes on the
same account collide - the second dies deep inside Telethon with
"sqlite3.OperationalError: database is locked", a traceback that tells the user
nothing about the real cause. This takes an OS-level exclusive lock instead: a
second instance learns immediately and cleanly that another is already running,
and because the OS drops the lock when a process exits - even on a crash - there
is no stale lock file to clean up afterwards.
"""

import os


class AlreadyRunning(Exception):
    """Another agent already holds the lock."""


def acquire(lock_path):
    """Hold an exclusive lock for the life of this process.

    Returns the open file handle (keep a reference so it stays locked) or raises
    AlreadyRunning. The handle is released automatically when the process ends.
    """
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning()
    return handle
