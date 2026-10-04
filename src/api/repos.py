"""Importing GitHub repositories into a sandbox workspace.

The host only downloads the archive (from github.com / codeload.github.com, size-capped)
and drops it into the workspace as a single new file. Unpacking happens *inside* the
sandbox container, as the unprivileged sandbox user, so a hostile archive (path
traversal, symlinks, huge files) can only affect the sandbox, and the workspace's
disk quota applies to the extracted files as usual.
"""
from __future__ import annotations

import os
import re
import secrets
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

ALLOWED_HOSTS = {"github.com", "codeload.github.com"}
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
DEST_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
DEFAULT_MAX_MB = 100
IMPORT_TIMEOUT_SECONDS = 120


class RepoImportError(Exception):
    """An import that failed; `status` is the HTTP status the API should answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    ref: str = "HEAD"   # HEAD = the default branch

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def archive_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}/archive/{urllib.parse.quote(self.ref, safe='/')}.tar.gz"


def parse_repo(spec: str, ref: Optional[str] = None) -> RepoRef:
    """Accepts "owner/repo", "https://github.com/owner/repo(.git)" or ".../tree/<ref>"."""
    spec = (spec or "").strip()
    if spec.startswith(("https://", "http://")):
        url = urllib.parse.urlparse(spec)
        if url.hostname not in ("github.com", "www.github.com"):
            raise RepoImportError("Only GitHub repositories can be imported (https://github.com/owner/repo).")
        parts = [p for p in url.path.split("/") if p]
    else:
        parts = [p for p in spec.split("/") if p]
    if len(parts) < 2:
        raise RepoImportError("Give the repository as owner/repo or https://github.com/owner/repo.")
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[:-4]
    if len(parts) > 2:
        if parts[2] != "tree" or len(parts) < 4:
            raise RepoImportError("Use https://github.com/owner/repo or https://github.com/owner/repo/tree/<branch>.")
        ref = ref or "/".join(parts[3:])
    ref = (ref or "").strip() or "HEAD"
    if not OWNER_RE.match(owner) or not REPO_RE.match(name) or name in (".", ".."):
        raise RepoImportError(f"'{owner}/{name}' isn't a valid GitHub repository name.")
    if not REF_RE.match(ref) or ".." in ref or ref.startswith("/") or ref.endswith("/"):
        raise RepoImportError(f"'{ref}' isn't a valid branch, tag or commit.")
    return RepoRef(owner, name, ref)


def clean_destination(dest: Optional[str], repo: RepoRef) -> str:
    """The folder (relative to /workspace) the repository is unpacked into."""
    dest = (dest or "").strip().strip("/") or repo.name
    if not DEST_RE.match(dest) or any(p in (".", "..") for p in dest.split("/")):
        raise RepoImportError(f"'{dest}' isn't a valid folder name inside the workspace.")
    return dest


def max_archive_bytes() -> int:
    return int(os.getenv("SANDBOX_IMPORT_MAX_MB", str(DEFAULT_MAX_MB))) * 2**20


class _GitHubOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname
        if urllib.parse.urlparse(newurl).scheme != "https" or host not in ALLOWED_HOSTS:
            raise RepoImportError(f"GitHub redirected the download to an unexpected host ({host}).", 502)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> Callable:
    opener = urllib.request.build_opener(_GitHubOnlyRedirects())
    return lambda url: opener.open(urllib.request.Request(url, headers={"User-Agent": "airlock-sandbox"}), timeout=30)


def fetch_archive(repo: RepoRef, max_bytes: Optional[int] = None, open_url: Optional[Callable] = None) -> bytes:
    """Downloads the repository's .tar.gz, refusing anything over `max_bytes`."""
    max_bytes = max_archive_bytes() if max_bytes is None else max_bytes
    open_url = open_url or _default_opener()
    try:
        response = open_url(repo.archive_url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RepoImportError(
                f"{repo.full_name}@{repo.ref} wasn't found. Only public repositories can be imported for now.", 404)
        raise RepoImportError(f"GitHub answered {e.code} for {repo.full_name}.", 502)
    except RepoImportError:
        raise
    except Exception as e:
        raise RepoImportError(f"Couldn't download {repo.full_name}: {e}", 502)
    with response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise RepoImportError(_too_large(repo, max_bytes), 413)
        chunks, total = [], 0
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RepoImportError(_too_large(repo, max_bytes), 413)
            chunks.append(chunk)
    return b"".join(chunks)


def _too_large(repo: RepoRef, max_bytes: int) -> str:
    return f"{repo.full_name} is larger than the {max_bytes // 2**20} MB import limit."


def import_archive(workspace, repo: RepoRef, archive: bytes, dest: str) -> dict:
    """Unpacks `archive` into /workspace/<dest> inside the sandbox. Returns {path, files, ...}."""
    used = workspace.disk_usage_bytes()
    if used + len(archive) > workspace.quota_bytes:
        raise RepoImportError(
            f"The archive ({len(archive) / 2**20:.1f} MB) doesn't fit in the workspace's free disk space "
            f"({max(workspace.quota_bytes - used, 0) / 2**20:.1f} MB).", 413)

    # A brand-new file at the workspace root. O_EXCL + O_NOFOLLOW: nothing the sandbox
    # created (e.g. a symlink planted under this name) is ever followed or overwritten.
    tar_name = f".airlock-import-{secrets.token_hex(8)}.tar.gz"
    tar_path = os.path.join(workspace.workspace_dir, tar_name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tar_path, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(archive)
        user = getattr(workspace, "container_user", None)
        if user and hasattr(os, "chown"):
            uid, gid = (int(x) for x in user.split(":"))
            os.chown(tar_path, uid, gid, follow_symlinks=False)
    except Exception:
        _remove_quietly(tar_path)
        raise

    q_dest, q_tar = shlex.quote(dest), shlex.quote(tar_name)
    script = (
        f"trap 'rm -f -- {q_tar}' EXIT\n"
        f"if [ -e {q_dest} ] || [ -L {q_dest} ]; then echo 'already exists' >&2; exit 17; fi\n"
        f"mkdir -p -- {q_dest} || exit 1\n"
        f"tar -xzf {q_tar} -C {q_dest} --strip-components=1 --no-same-owner --no-same-permissions || exit 1\n"
        f"find {q_dest} -type f | wc -l\n"
    )
    try:
        result = workspace.execute(script, timeout_seconds=IMPORT_TIMEOUT_SECONDS)
    finally:
        _remove_quietly(tar_path)   # normally already gone (the trap); not if the container never ran

    if result["exit_code"] == 17:
        raise RepoImportError(f"/workspace/{dest} already exists. Pick another folder name or delete it first.", 409)
    if result["timed_out"] or result["exit_code"] != 0:
        detail = (result["stderr"] or "".join(result["warnings"]) or "unknown error").strip()[-500:]
        raise RepoImportError(f"Unpacking {repo.full_name} failed: {detail}", 422)
    last_line = (result["stdout"].strip().splitlines() or ["0"])[-1].strip()
    files = int(last_line) if last_line.isdigit() else 0
    return {
        "repo": repo.full_name, "ref": repo.ref, "path": f"/workspace/{dest}", "dest": dest,
        "files": files, "archive_bytes": len(archive),
    }


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
