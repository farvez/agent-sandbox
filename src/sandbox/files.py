"""Reading and writing workspace files from the host, safely.

Sandboxed code controls the workspace and can swap a path for a symlink at any moment, so a
"resolve the path, check it, then open it" sequence has a race (TOCTOU). Here every open uses
O_NOFOLLOW (a symlink as the last component fails), and on Linux the opened descriptor's real
path is checked to be inside the workspace (catching a swapped parent directory too). Only
regular files are ever read or written.
"""
from __future__ import annotations

import os
import stat
from typing import Optional

O_BINARY = getattr(os, "O_BINARY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class WorkspaceFileError(Exception):
    """`status` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _inside(root: str, path: str) -> bool:
    return os.path.commonpath([root, path]) == root


def resolve(workspace_dir: str, relative: str) -> str:
    relative = (relative or "").strip()
    if not relative or "\0" in relative:
        raise WorkspaceFileError("Give a file path inside the workspace.")
    root = os.path.realpath(workspace_dir)
    candidate = os.path.realpath(os.path.join(root, relative))
    if not _inside(root, candidate) or candidate == root:
        raise WorkspaceFileError(f"Access denied: '{relative}' is outside the workspace.", 403)
    return candidate


def _check_fd(fd: int, root: str, relative: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceFileError(f"'{relative}' is not a regular file.")
    proc = f"/proc/self/fd/{fd}"
    if os.path.exists(proc) and not _inside(root, os.path.realpath(proc)):
        raise WorkspaceFileError(f"Access denied: '{relative}' is outside the workspace.", 403)


def read_bytes(workspace_dir: str, relative: str, max_bytes: int) -> bytes:
    root = os.path.realpath(workspace_dir)
    path = resolve(root, relative)
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_BINARY)
    except FileNotFoundError:
        raise WorkspaceFileError(f"'{relative}' doesn't exist.", 404)
    except OSError:
        raise WorkspaceFileError(f"'{relative}' can't be read (is it a symlink or a directory?).")
    with os.fdopen(fd, "rb") as f:
        _check_fd(f.fileno(), root, relative)
        if os.fstat(f.fileno()).st_size > max_bytes:
            raise WorkspaceFileError(f"'{relative}' is larger than the {max_bytes // 2**20} MB download limit.", 413)
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise WorkspaceFileError(f"'{relative}' is larger than the {max_bytes // 2**20} MB download limit.", 413)
    return data


def write_bytes(workspace_dir: str, relative: str, data: bytes, owner: Optional[str] = None) -> int:
    """Creates or replaces a regular file; parent folders are created inside the workspace."""
    root = os.path.realpath(workspace_dir)
    path = resolve(root, relative)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if not _inside(root, os.path.realpath(parent)):
        raise WorkspaceFileError(f"Access denied: '{relative}' is outside the workspace.", 403)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | O_NOFOLLOW | O_BINARY, 0o644)
    except OSError:
        raise WorkspaceFileError(f"'{relative}' can't be written (is it a symlink or a directory?).")
    with os.fdopen(fd, "wb") as f:
        _check_fd(f.fileno(), root, relative)
        f.write(data)
    _chown(path, owner)
    return len(data)


def create_new(workspace_dir: str, name: str, data: bytes, owner: Optional[str] = None) -> str:
    """A brand-new file at the workspace root (O_EXCL: never follows or replaces anything)."""
    path = os.path.join(os.path.realpath(workspace_dir), name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_BINARY, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _chown(path, owner)
    except Exception:
        remove_quietly(path)
        raise
    return path


def read_root_file(workspace_dir: str, name: str, max_bytes: int) -> bytes:
    """Reads a file the sandbox wrote at the workspace root (e.g. an archive), refusing symlinks."""
    return read_bytes(workspace_dir, name, max_bytes)


def _chown(path: str, owner: Optional[str]) -> None:
    if owner and hasattr(os, "chown"):
        uid, gid = (int(x) for x in owner.split(":"))
        os.chown(path, uid, gid, follow_symlinks=False)


def remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
