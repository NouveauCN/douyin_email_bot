"""Small cooperative cross-process locks for the shared media tree.

Each operation takes one coarse tree lock plus its target lock.  The coarse
lock is intentionally simple for this private, single-instance deployment: it
makes directory deletion conflict with child-file work without introducing a
hierarchical lock manager.  Sidecars live in a hidden directory below the
shared download root.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path


LOCK_DIR_NAME = ".media_locks"
DEFAULT_LOCK_TIMEOUT = 5.0


class MediaFileLockBusy(TimeoutError):
    """Raised when another process keeps a media lock past the timeout."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = Path(path)
        self.timeout = timeout
        super().__init__(f"media file is busy: {self.path}")


# flock is process-oriented on Linux, so guard against two file descriptors in
# this process both acquiring the same lock.  Reentrant acquisition by the
# owning thread avoids accidental double locking in wrappers such as
# process_media -> process_image.
_LOCAL_STATE_LOCK = threading.RLock()
_LOCAL_LOCKS: dict[Path, tuple[int, int, int, int]] = {}


def _after_fork_in_child() -> None:
    """Drop descriptors inherited from a parent that owns process-local locks."""
    global _LOCAL_STATE_LOCK
    # Only the forking thread survives in the child.  Avoid acquiring a
    # user-created lock here because another vanished parent thread may have
    # owned it at the instant of fork.
    descriptors = {entry[2] for entry in _LOCAL_LOCKS.values()}
    _LOCAL_LOCKS.clear()
    _LOCAL_STATE_LOCK = threading.RLock()
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass


os.register_at_fork(after_in_child=_after_fork_in_child)


def _root_and_relative(path: Path, root: Path | None) -> tuple[Path, str]:
    target = Path(path)
    download_root = Path(root) if root is not None else target.parent
    download_root = download_root.resolve()
    target = target.resolve()
    try:
        relative = target.relative_to(download_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"media path is outside download root: {target}") from exc
    return download_root, relative


def lock_path(path: Path, *, root: Path | None = None) -> Path:
    """Return the hashed lock sidecar for ``path`` under ``root``."""
    download_root, relative = _root_and_relative(Path(path), root)
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return download_root / LOCK_DIR_NAME / f"{digest}.lock"


class MediaFileLock(AbstractContextManager["MediaFileLock"]):
    def __init__(
        self,
        path: Path,
        *,
        root: Path | None = None,
        timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.path = Path(path)
        self.root = Path(root) if root is not None else None
        self.timeout = max(0.0, float(timeout))
        self.lockfile = lock_path(self.path, root=self.root)
        self._reentrant = False
        self._acquired = False

    def acquire(self) -> "MediaFileLock":
        pid = os.getpid()
        thread_id = threading.get_ident()
        with _LOCAL_STATE_LOCK:
            current = _LOCAL_LOCKS.get(self.lockfile)
            if current is not None and current[0] == pid and current[1] == thread_id:
                _LOCAL_LOCKS[self.lockfile] = (pid, thread_id, current[2], current[3] + 1)
                self._reentrant = True
                self._acquired = True
                return self

        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                acquired = False
                with _LOCAL_STATE_LOCK:
                    # flock may be acquired twice by two descriptors in one
                    # process; the registry makes other local threads wait.
                    current = _LOCAL_LOCKS.get(self.lockfile)
                    if current is not None and current[0] != pid:
                        # A forked child inherits the registry snapshot, but
                        # must still contend with the parent's kernel lock.
                        _LOCAL_LOCKS.pop(self.lockfile, None)
                        current = None
                    if current is None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                            _LOCAL_LOCKS[self.lockfile] = (pid, thread_id, fd, 1)
                        except BlockingIOError:
                            pass
                        except OSError as exc:
                            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                                raise
                if acquired:
                    self._acquired = True
                    return self
                if time.monotonic() >= deadline:
                    raise MediaFileLockBusy(self.path, self.timeout)
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
        except Exception:
            os.close(fd)
            raise

    def release(self) -> None:
        if not self._acquired:
            return
        pid = os.getpid()
        thread_id = threading.get_ident()
        if self._reentrant:
            with _LOCAL_STATE_LOCK:
                current = _LOCAL_LOCKS.get(self.lockfile)
                if current is not None and current[0] == pid and current[1] == thread_id:
                    if current[3] > 1:
                        _LOCAL_LOCKS[self.lockfile] = (current[0], current[1], current[2], current[3] - 1)
                    else:
                        # The outer lock owns the descriptor and will close it.
                        _LOCAL_LOCKS.pop(self.lockfile, None)
            self._acquired = False
            return

        with _LOCAL_STATE_LOCK:
            current = _LOCAL_LOCKS.get(self.lockfile)
            if current is not None and current[0] == pid and current[1] == thread_id:
                _LOCAL_LOCKS.pop(self.lockfile, None)
                fd = current[2]
            else:
                fd = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._acquired = False

    def __enter__(self) -> "MediaFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class MediaOperationLock(AbstractContextManager["MediaOperationLock"]):
    """Acquire the shared-tree lock and then the per-target lock."""

    def __init__(self, path: Path, *, root: Path | None, timeout: float) -> None:
        resolved_root, _relative = _root_and_relative(Path(path), root)
        self._tree = MediaFileLock(
            resolved_root / ".media-tree-transaction",
            root=resolved_root,
            timeout=timeout,
        )
        self._target = MediaFileLock(path, root=resolved_root, timeout=timeout)
        self._acquired = False

    def acquire(self) -> "MediaOperationLock":
        self._tree.acquire()
        try:
            self._target.acquire()
        except BaseException:
            self._tree.release()
            raise
        self._acquired = True
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self._target.release()
        finally:
            self._tree.release()
            self._acquired = False

    def __enter__(self) -> "MediaOperationLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

def media_file_lock(
    path: Path,
    *,
    root: Path | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> MediaOperationLock:
    """Create a reentrant tree-and-target lock for one media operation."""
    return MediaOperationLock(path, root=root, timeout=timeout)
