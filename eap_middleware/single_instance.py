"""Single-instance enforcement via PID lockfile.

Two middleware processes pointed at the same machine list would silently
fight over the active HSMS connection (only one ACTIVE peer is allowed) -
the symptom is intermittent dropouts that look like network flakiness but
are actually two clients knocking each other off. SingleInstanceLock writes
a PID into install_dir/middleware.lock at startup and refuses to run if
that PID is still alive.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import TracebackType
from typing import IO, Optional

logger = logging.getLogger(__name__)


class InstanceAlreadyRunning(RuntimeError):
    """Another live middleware instance holds the lock."""


class SingleInstanceLock:
    """PID file protected by a non-blocking operating-system file lock."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._held = False
        self._handle: Optional[IO[str]] = None

    def acquire(self) -> None:
        """Take the lock, or raise InstanceAlreadyRunning.

        Liveness is decided by the operating system's file lock, never by
        probing the PID: a lock held by a dead process is released by the
        kernel when its handle closes, so a lockfile left behind by a crash
        or a hard power-off is reclaimed on the next start with no operator
        intervention. The PID inside the file is a diagnostic - it names the
        holder in the error message - and is deliberately not a gate; a
        recycled PID would otherwise be able to lock out a healthy start.
        """
        if self._held:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            _lock_file(handle)
        except OSError as exc:
            handle.close()
            existing_pid = self._read_existing_pid()
            suffix = f" with PID {existing_pid}" if existing_pid else ""
            raise InstanceAlreadyRunning(
                f"Another middleware instance{suffix} holds {self.lock_path}"
            ) from exc
        existing_pid = self._read_pid_from_handle(handle)
        if existing_pid is not None and existing_pid != os.getpid():
            logger.warning(
                "Reclaiming unlocked PID file %s (previous PID %d)",
                self.lock_path, existing_pid,
            )
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        # Read the owning PID while the handle is still open, then close it
        # BEFORE unlinking: Windows refuses to delete a file the process still
        # holds open, which left a stale lockfile blocking every later start.
        try:
            existing = self._read_existing_pid()
        except Exception:
            existing = None
        if self._handle is not None:
            try:
                _unlock_file(self._handle)
            finally:
                self._handle.close()
                self._handle = None
        try:
            # Only remove the lockfile if it still contains our PID. Defends
            # against a race where another process reclaimed a stale lock.
            if existing == os.getpid():
                self.lock_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("Lockfile release failed", exc_info=True)
        finally:
            self._held = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _read_existing_pid(self) -> Optional[int]:
        try:
            text = self.lock_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except Exception:
            return None
        try:
            return int(text)
        except ValueError:
            # Corrupted lockfile - treat as stale and reclaim.
            return None

    @staticmethod
    def _read_pid_from_handle(handle: IO[str]) -> Optional[int]:
        handle.seek(0)
        try:
            pid = int(handle.read().strip())
        except ValueError:
            return None
        # Not a previous owner: `_lock_file` writes "0" into an empty lockfile
        # because msvcrt.locking cannot lock a zero-length region. Reading that
        # placeholder back as a PID made every clean first start announce
        # "Reclaiming unlocked PID file ... (previous PID 0)" - a warning that
        # reads like crash recovery on a machine that has never run before, and
        # that sat at the top of a NexGen MG log while a genuine fault was
        # being diagnosed.
        return pid if pid > 0 else None


def _lock_file(handle: IO[str]) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on deployment host
        import msvcrt
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write("0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: IO[str]) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on deployment host
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
