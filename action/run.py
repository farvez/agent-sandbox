"""GitHub Action: run a command (e.g. a repository's tests) in an Airlock sandbox.

Uploads the checked-out repository, starts the command in the background of a long-running
sandbox (so it can take longer than one command's 60 s limit), streams its output into the job
log, optionally downloads a folder of results, and exits with the command's exit code.

Configured through INPUT_* environment variables (see action.yml); runnable locally too:
    INPUT_API_KEY=... INPUT_RUN="python -m pytest -q" python action/run.py
"""
from __future__ import annotations

import base64
import io
import os
import sys
import tarfile
import time
from typing import Callable, List, Optional

LOG = ".airlock-run.log"
EXIT = ".airlock-run.exit"
SCRIPT = ".airlock-run.sh"
ARCHIVE = ".airlock-upload.tar.gz"
REPO_DIR = "repo"
CHUNK = 256 * 1024
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox"}

READ_PROGRESS = (
    "python3 -c \"import base64,os;f=open('{log}','rb');f.seek({offset});d=f.read({chunk});"
    "print(base64.b64encode(d).decode());"
    "print(open('{exit}').read().strip() if os.path.exists('{exit}') else '')\""
)


def env(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name.upper().replace('-', '_')}", default).strip()


def pack(directory: str, max_bytes: int = 50 * 2**20) -> bytes:
    """A .tar.gz of `directory`, without .git and other caches; symlinks are kept as links."""
    buf = io.BytesIO()

    def keep(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        parts = info.name.split("/")
        return None if any(p in SKIP_DIRS for p in parts) else info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(os.listdir(directory)):
            tar.add(os.path.join(directory, name), arcname=name, filter=keep)
    data = buf.getvalue()
    if len(data) > max_bytes:
        raise SystemExit(f"The repository is {len(data) / 2**20:.1f} MB compressed; uploads are limited to "
                         f"{max_bytes // 2**20} MB. Set `upload: false` or `path:` to a smaller folder.")
    return data


def upload_repo(sandbox, directory: str) -> None:
    sandbox.files.upload(ARCHIVE, pack(directory))
    sandbox.run(f"mkdir -p {REPO_DIR} && tar -xzf {ARCHIVE} -C {REPO_DIR} && rm -f {ARCHIVE}", timeout=60).check()


def start(sandbox, command: str, workdir: str) -> None:
    """Runs `command` in the background; its output goes to LOG and its exit code to EXIT."""
    sandbox.files.write(SCRIPT, command + "\n")
    sandbox.run(
        f"rm -f {LOG} {EXIT}; nohup bash -c 'cd {workdir} && bash /workspace/{SCRIPT} > /workspace/{LOG} 2>&1; "
        f"echo $? > /workspace/{EXIT}' > /dev/null 2>&1 & echo started",
        timeout=30,
    ).check()


def follow(sandbox, write: Callable[[bytes], None], deadline: float, interval: float = 3.0,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> Optional[int]:
    """Streams new output until the command exits; returns its exit code, or None on timeout."""
    offset = 0
    while True:
        result = sandbox.run(READ_PROGRESS.format(log=LOG, exit=EXIT, offset=offset, chunk=CHUNK), timeout=30)
        lines = result.stdout.split("\n")
        data = base64.b64decode(lines[0]) if lines and lines[0] else b""
        status = lines[1].strip() if len(lines) > 1 else ""
        if data:
            write(data)
            offset += len(data)
        if len(data) == CHUNK:
            continue                      # more output waiting: read it before checking for the end
        if status:
            return int(status) if status.lstrip("-").isdigit() else 1
        if clock() > deadline:
            return None
        sleep(interval)


def download(sandbox, path: str, local_dir: str, base: str = REPO_DIR) -> List[str]:
    """Unpacks /workspace/<base>/<path> (a folder, as .tar.gz) into local_dir; returns the files written."""
    remote = "/".join(p for p in (base, path.strip("/")) if p) or "."
    data = sandbox.files.download_archive(remote)
    os.makedirs(local_dir, exist_ok=True)
    prefix = "./" if remote == "." else remote + "/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            if not (m.isfile() or m.isdir()) or not m.name.startswith(prefix):   # no links or devices
                continue
            m.name = m.name[len(prefix):]          # results/x.xml → x.xml under local_dir
            if m.name:
                members.append(m)
        tar.extractall(local_dir, members=members, filter="data")
    return [m.name for m in members if m.isfile()]


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def main() -> int:
    from airlock_sandbox import Sandbox, SandboxError

    # Status lines and the command's raw output share the job log: keep them in order.
    sys.stdout.reconfigure(line_buffering=True)

    command = env("run")
    if not command:
        print("::error::Set the `run` input: the command to run in the sandbox.")
        return 2
    api_key = env("api-key")
    if not api_key:
        print("::error::Set the `api-key` input, e.g. api-key: ${{ secrets.AIRLOCK_API_KEY }}")
        return 2
    print(f"::add-mask::{api_key}")
    egress = [e.strip() for e in env("egress").split(",") if e.strip()]
    timeout_minutes = float(env("timeout-minutes", "20") or 20)
    source = env("path", ".") or "."
    do_upload = env("upload", "true").lower() not in ("false", "0", "no")
    workdir = f"/workspace/{REPO_DIR}" if do_upload else "/workspace"

    try:
        with Sandbox(api_key=api_key, base_url=env("api-url", "https://airlock.complyoo.com"),
                     egress=egress, persistent=True) as sbx:
            print(f"Sandbox {sbx.session_id} (internet: {', '.join(sbx.egress) or 'none'})")
            set_output("session-id", sbx.session_id)
            if do_upload:
                print(f"::group::Uploading {os.path.abspath(source)}")
                upload_repo(sbx, source)
                print("::endgroup::")
            start(sbx, command, workdir)
            out = sys.stdout.buffer

            def write(data: bytes) -> None:
                sys.stdout.flush()
                out.write(data)
                out.flush()

            code = follow(sbx, write, time.monotonic() + timeout_minutes * 60)
            if code is None:
                print(f"\n::error::The command didn't finish within {timeout_minutes:g} minutes.")
                code = 124
            results = env("download")
            if results:
                local = env("download-to", "airlock-results") or "airlock-results"
                try:
                    files = download(sbx, results, local, REPO_DIR if do_upload else "")
                    print(f"Downloaded {len(files)} file(s) from {results} into {local}/")
                except SandboxError as e:
                    print(f"::warning::Couldn't download {results}: {e}")
    except SandboxError as e:
        print(f"::error::Airlock: {e}")
        return 1
    set_output("exit-code", str(code))
    print(f"\nExit code {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
