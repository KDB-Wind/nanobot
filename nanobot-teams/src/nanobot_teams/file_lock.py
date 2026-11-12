"""Non-blocking process ownership for local control files."""

from __future__ import annotations

import os
from pathlib import Path


class FileLock:
    def __init__(self, path: Path) -> None:
        self._fd: int | None = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(self._fd).st_size == 0:
                os.write(self._fd, b"0")
            os.lseek(self._fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
