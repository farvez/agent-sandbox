"""Disk-limited workspaces.

Two layers:

* Hard limit (Linux deployment): at boot, root creates a dedicated ext4
  filesystem with project quotas and a fixed set of slot directories
  (slot-001 ...), each with its own kernel-enforced size limit. The API, which
  is not root, only claims a free slot per session and wipes it afterwards.
  The filesystem itself is size-capped, so workspaces can never fill the
  server's main disk. Enabled by SANDBOX_WORKSPACE_POOL=<mount point>.

* Soft limit (everywhere, including laptops and CI): the workspace measures its
  own usage, warns after a command that went over the quota, and refuses
  further file writes through the API until files are removed.
"""
import os
import shutil
import threading
from typing import Optional

DEFAULT_QUOTA_MB = 512


class WorkspaceCapacityError(RuntimeError):
    """Every workspace slot is in use."""


class QuotaExceededError(OSError):
    """A write would take the workspace past its disk quota."""


def quota_bytes_from_env(override_mb: Optional[int] = None) -> int:
    mb = override_mb if override_mb is not None else int(os.getenv("SANDBOX_WORKSPACE_QUOTA_MB", DEFAULT_QUOTA_MB))
    return mb * 1024 * 1024


def disk_usage(path: str) -> int:
    """Bytes the directory tree occupies on disk (allocated blocks, like the kernel quota).

    Symlinks are measured, never followed. Falls back to file size where the
    platform doesn't report blocks (Windows).
    """
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            blocks = getattr(st, "st_blocks", None)
            total += blocks * 512 if blocks is not None else st.st_size
    return total


def wipe_directory(path: str) -> None:
    """Deletes everything inside `path` but keeps the directory itself (and its quota)."""
    for entry in os.scandir(path):
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
        except OSError:
            pass


class WorkspacePool:
    """Hands out pre-made, quota-limited slot directories under `root`.

    A slot is claimed by creating `<root>.locks/<slot>` (mkdir is atomic), so the
    claim marker sits outside the slot and is never visible to sandboxed code.
    """

    def __init__(self, root: str, lock_dir: Optional[str] = None):
        self.root = os.path.realpath(root)
        self.lock_dir = lock_dir or f"{self.root}.locks"
        self._lock = threading.Lock()
        self._reset_done = False

    def _slots(self):
        return sorted(name for name in os.listdir(self.root) if name.startswith("slot-"))

    def _reset_once(self) -> None:
        """First claim in this process: claims held by an earlier API process
        belong to sessions that died with it, so free and wipe them."""
        if self._reset_done:
            return
        os.makedirs(self.lock_dir, exist_ok=True)
        for name in os.listdir(self.lock_dir):
            slot = os.path.join(self.root, name)
            if os.path.isdir(slot):
                wipe_directory(slot)
            try:
                os.rmdir(os.path.join(self.lock_dir, name))
            except OSError:
                pass
        self._reset_done = True

    def claim(self) -> str:
        with self._lock:
            self._reset_once()
            for name in self._slots():
                try:
                    os.mkdir(os.path.join(self.lock_dir, name))
                except FileExistsError:
                    continue
                path = os.path.join(self.root, name)
                wipe_directory(path)  # defensive: a slot is always handed out empty
                return path
        raise WorkspaceCapacityError("All sandbox workspaces are in use; try again shortly.")

    def release(self, path: str) -> None:
        path = os.path.realpath(path)
        if os.path.dirname(path) != self.root:
            return
        wipe_directory(path)
        try:
            os.rmdir(os.path.join(self.lock_dir, os.path.basename(path)))
        except OSError:
            pass


_pool: Optional[WorkspacePool] = None
_pool_lock = threading.Lock()


def get_pool() -> Optional[WorkspacePool]:
    """The process-wide pool when SANDBOX_WORKSPACE_POOL is set, else None (temp dirs)."""
    global _pool
    root = os.getenv("SANDBOX_WORKSPACE_POOL")
    if not root:
        return None
    with _pool_lock:
        if _pool is None or _pool.root != os.path.realpath(root):
            _pool = WorkspacePool(root)
        return _pool
