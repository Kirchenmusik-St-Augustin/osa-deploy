"""A private scratch directory in memory-backed storage.

The vault password file and the decrypted operator key live only here, in a
directory that is readable by the current user alone and removed on exit, so
neither ever reaches a persistent disk.
"""

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from osa_tools.errors import OsaError

SHARED_MEMORY_DIR = Path("/dev/shm")
_FILE_MODE = 0o600


@dataclass(frozen=True)
class Session:
    """Memory-backed files that exist while a command runs."""

    password_file: Path
    key_file: Path


class Workspace:
    """Context manager that owns one 0700 directory below `base`."""

    def __init__(self, base: Path = SHARED_MEMORY_DIR) -> None:
        self._base = base
        self._directory: Path | None = None

    def __enter__(self) -> Self:
        if not self._base.is_dir():
            msg = (
                f"{self._base} is not available. The vault password and the "
                "operator key are only ever written to memory-backed storage."
            )
            raise OsaError(msg)
        self._directory = Path(tempfile.mkdtemp(prefix="osa-", dir=self._base))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    def path(self, name: str) -> Path:
        """Return the path of a file inside the workspace (not created)."""
        if self._directory is None:
            msg = "The workspace is not open."
            raise OsaError(msg)
        return self._directory / name

    def write(self, name: str, content: str) -> Path:
        """Write a text file readable by the current user only."""
        target = self.path(name)
        target.touch(mode=_FILE_MODE)
        target.write_text(content, encoding="utf-8")
        return target
