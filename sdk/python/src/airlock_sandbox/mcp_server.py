"""MCP server: gives MCP clients (Claude Code, Claude Desktop, …) an isolated sandbox as tools.

    pip install "airlock-sandbox[mcp]"
    claude mcp add airlock -e SANDBOX_API_URL=https://… -e SANDBOX_API_KEY=… -- airlock-sandbox-mcp

Configuration (environment variables):
    SANDBOX_API_URL        server URL (required)
    SANDBOX_API_KEY        tenant API key (required)
    SANDBOX_API_INSECURE   1 = accept a self-signed certificate (server without a domain)
    AIRLOCK_EGRESS         comma-separated hosts/presets the sandbox may reach, e.g. "pypi"
                           (must be allowed by the tenant's policy; empty = no network)
    AIRLOCK_TEMPLATE       container image (default sandbox-base:latest)
    AIRLOCK_STATE_DIR      where the current session ID is recorded (default: the user's
                           local state directory)

One sandbox session per MCP server process, created on the first tool call and
deleted when the client disconnects. Files persist between commands within it.
MCP clients kill servers shortly after disconnecting, so the session ID is also
recorded on disk and a session left behind by a killed run is deleted on the
next start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import threading
from contextlib import asynccontextmanager
from typing import List, Optional

try:
    import anyio
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as err:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "The MCP server needs the 'mcp' extra (Python 3.10+):\n    pip install \"airlock-sandbox[mcp]\""
    ) from err

from airlock_sandbox import __version__
from airlock_sandbox.errors import NotFoundError, SandboxError
from airlock_sandbox.sandbox import DEFAULT_TEMPLATE, Sandbox
from airlock_sandbox.tools import truncate

INSTRUCTIONS = """\
Tools for an isolated Linux sandbox (bash, Python 3.11) on a remote server.
Use it to run code you don't want to run on the user's machine: untrusted or
generated code, experiments, package installs, tests.

- run_command runs in a fresh container each time; files in the workspace
  (/workspace) persist between calls, background processes do not.
- Limits: 256 MB RAM, 1 CPU, 64 processes, a per-workspace disk quota, and a
  1-60 s timeout per command.
- No internet unless the sandbox was configured with egress hosts; call
  sandbox_info to see what is reachable. Only HTTPS to those hosts works.
- reset_sandbox wipes the workspace and starts over.
"""


SHUTDOWN_CLOSE_TIMEOUT = 1.5   # MCP clients kill the server ~2 s after disconnecting


def default_state_dir() -> str:
    if os.getenv("AIRLOCK_STATE_DIR"):
        return os.environ["AIRLOCK_STATE_DIR"]
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "airlock-sandbox")


def state_file_for(base_url: str, api_key: str, template: str, egress: List[str], state_dir: str) -> str:
    """One file per configuration; the key itself is hashed, never stored."""
    digest = hashlib.sha256("|".join([base_url, api_key, template, ",".join(egress)]).encode()).hexdigest()[:16]
    return os.path.join(state_dir, f"mcp-session-{digest}.json")


class SandboxHolder:
    """The process's sandbox session: created lazily, re-created if it expired."""

    def __init__(self, egress: List[str], template: str, factory=Sandbox, state_file: Optional[str] = None):
        self.egress = egress
        self.template = template
        self._factory = factory
        self._sandbox: Optional[Sandbox] = None
        self._lock = threading.Lock()
        self.notice = ""   # one-off message for the next tool result
        self.state_file = state_file
        self._previous_cleaned = False

    def get(self) -> Sandbox:
        with self._lock:
            if self._sandbox is None:
                self._delete_previous_session()
                self._sandbox = self._factory(egress=self.egress, template=self.template).start()
                self._write_state(self._sandbox.session_id)
            return self._sandbox

    # -------- session recorded on disk (survives a killed process)

    def _delete_previous_session(self) -> None:
        """Once per process: delete the session a previous, killed run left behind."""
        if self._previous_cleaned or not self.state_file:
            return
        self._previous_cleaned = True
        try:
            with open(self.state_file, encoding="utf-8") as f:
                session_id = json.load(f).get("session_id")
        except (OSError, ValueError):
            return
        if session_id:
            leftover = self._factory(egress=self.egress, template=self.template)
            leftover.session_id = session_id
            try:
                leftover.close(timeout=5)
            except SandboxError:
                pass   # unreachable now; the server's idle reaper removes it eventually
        self._write_state(None)

    def _write_state(self, session_id: Optional[str]) -> None:
        if not self.state_file:
            return
        try:
            if session_id is None:
                if os.path.exists(self.state_file):
                    os.remove(self.state_file)
                return
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"session_id": session_id}, f)
        except OSError:
            pass   # best effort: losing it only means relying on the server's idle reaper

    def call(self, fn):
        """Runs fn(sandbox); if the session expired on the server, starts a new one and retries once."""
        try:
            return fn(self.get())
        except NotFoundError:
            with self._lock:
                self._sandbox = None
            self.notice = "[note] The previous sandbox session expired; this ran in a new, empty workspace.\n"
            return fn(self.get())

    def reset(self) -> Sandbox:
        self.close(timeout=None)
        return self.get()

    def close(self, timeout: Optional[float] = SHUTDOWN_CLOSE_TIMEOUT) -> None:
        """Deletes the session. The default quick timeout fits the client's shutdown grace period;
        if it doesn't finish, the recorded session ID lets the next start clean up."""
        with self._lock:
            sandbox, self._sandbox = self._sandbox, None
        if sandbox is None:
            return
        try:
            sandbox.close(timeout=timeout)
            self._write_state(None)
        except SandboxError:
            pass

    def take_notice(self) -> str:
        notice, self.notice = self.notice, ""
        return notice


def _egress_from_env(value: Optional[str]) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def build_server(holder: SandboxHolder) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            await anyio.to_thread.run_sync(holder.close)   # delete the session when the client goes away

    server = MCPServer(
        "airlock-sandbox",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/farvez/agent-sandbox",
        lifespan=lifespan,
    )

    async def run(fn) -> str:
        """Calls the SDK off the event loop and turns SDK errors into MCP tool errors."""
        try:
            result = await anyio.to_thread.run_sync(holder.call, fn)
        except FileNotFoundError as err:
            raise ToolError(f"File not found: {err}") from None
        except SandboxError as err:
            raise ToolError(f"{type(err).__name__}: {err}") from None
        return holder.take_notice() + truncate(result)

    @server.tool(annotations=ToolAnnotations(title="Run a shell command", destructive_hint=False))
    async def run_command(command: str, timeout_seconds: int = 30) -> str:
        """Run a shell command in the sandbox and return stdout, stderr and the exit code.

        Each call uses a fresh container; files in /workspace persist between calls.
        timeout_seconds must be 1-60.
        """
        return await run(lambda sbx: sbx.run(command, timeout=timeout_seconds).output)

    @server.tool(annotations=ToolAnnotations(title="Write a file", destructive_hint=False))
    async def write_file(path: str, content: str) -> str:
        """Create or overwrite a text file in the sandbox workspace (path relative to /workspace)."""

        def write(sbx: Sandbox) -> str:
            sbx.files.write(path, content)
            return f"Wrote {len(content)} characters to {path}"

        return await run(write)

    @server.tool(annotations=ToolAnnotations(title="Read a file", read_only_hint=True))
    async def read_file(path: str) -> str:
        """Read a text file from the sandbox workspace (path relative to /workspace)."""
        return await run(lambda sbx: sbx.files.read(path))

    @server.tool(annotations=ToolAnnotations(title="List files", read_only_hint=True))
    async def list_files(path: str = ".", max_depth: int = 3) -> str:
        """List files and directories in the sandbox workspace, with sizes (installed packages under .local are skipped)."""
        depth = max(1, min(int(max_depth), 10))
        command = (
            f"find {shlex.quote(path)} -maxdepth {depth} -path '*/.local' -prune -o -printf '%y %8s  %p\\n' "
            "2>&1 | sort -k3 | head -500"
        )
        return await run(lambda sbx: sbx.run(command, timeout=15).output)

    @server.tool(annotations=ToolAnnotations(title="Import a GitHub repository", destructive_hint=False))
    async def import_repo(repo: str, ref: str = "", path: str = "") -> str:
        """Copy a public GitHub repository into the sandbox workspace.

        repo is "owner/repo" or https://github.com/owner/repo. ref is a branch, tag or commit
        (default: the default branch); path is the folder under /workspace (default: the repo
        name). Works without internet access in the sandbox.
        """

        def do_import(sbx: Sandbox) -> str:
            r = sbx.import_repo(repo, ref or None, path or None)
            return f"Imported {r['repo']}@{r['ref']}: {r['files']} files in {r['path']}"

        return await run(do_import)

    @server.tool(annotations=ToolAnnotations(title="Egress log", read_only_hint=True))
    async def egress_log(limit: int = 50) -> str:
        """Show the sandbox's outbound internet connections: which hosts were allowed or denied, and why."""

        def fetch(sbx: Sandbox) -> str:
            events = sbx.egress_log(limit=max(1, min(int(limit), 500)))
            if not events:
                return "No outbound connections yet." if sbx.egress else "This sandbox has no internet access."
            return "\n".join(
                f"{e.get('decision', '?'):5}  {e.get('host') or e.get('target', '?')}"
                + (f"  ({e['reason']})" if e.get("reason") else "")
                + (f"  {e['bytes_down']:,} bytes in" if e.get("bytes_down") else "")
                for e in events
            )

        return await run(fetch)

    @server.tool(annotations=ToolAnnotations(title="Sandbox info", read_only_hint=True))
    async def sandbox_info() -> str:
        """Show the sandbox's session, internet access, disk quota and the tenant's limits."""

        def info(sbx: Sandbox) -> str:
            usage = sbx.usage()
            limits = usage.get("limits", {})
            return "\n".join([
                f"Server: {sbx.base_url}",
                f"Session: {sbx.session_id} (tenant {sbx.tenant_id})",
                f"Internet: {', '.join(sbx.egress) if sbx.egress else 'none'}",
                f"Disk quota: {sbx.disk_quota_mb} MB",
                f"Limits: {limits.get('max_sessions')} sessions, {limits.get('requests_per_minute')} requests/min, "
                f"{limits.get('max_concurrent_exec')} concurrent commands",
            ])

        return await run(info)

    @server.tool(annotations=ToolAnnotations(title="Reset the sandbox", destructive_hint=True))
    async def reset_sandbox() -> str:
        """Delete the sandbox workspace and start a fresh, empty one."""
        try:
            sbx = await anyio.to_thread.run_sync(holder.reset)
        except SandboxError as err:
            raise ToolError(f"{type(err).__name__}: {err}") from None
        return f"Started a fresh sandbox ({sbx.session_id}). All previous files are gone."

    return server


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="airlock-sandbox-mcp",
        description="MCP server exposing an airlock-sandbox session as tools (stdio).",
    )
    parser.add_argument("--egress", help="comma-separated hosts/presets, overrides AIRLOCK_EGRESS (e.g. pypi)")
    parser.add_argument("--template", help=f"container image, overrides AIRLOCK_TEMPLATE (default {DEFAULT_TEMPLATE})")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    missing = [v for v in ("SANDBOX_API_URL", "SANDBOX_API_KEY") if not os.getenv(v)]
    if missing:
        print(f"airlock-sandbox-mcp: set {' and '.join(missing)} (see --help).", file=sys.stderr)
        raise SystemExit(2)

    egress = _egress_from_env(args.egress if args.egress is not None else os.getenv("AIRLOCK_EGRESS"))
    template = args.template or os.getenv("AIRLOCK_TEMPLATE") or DEFAULT_TEMPLATE
    holder = SandboxHolder(
        egress=egress,
        template=template,
        state_file=state_file_for(os.environ["SANDBOX_API_URL"], os.environ["SANDBOX_API_KEY"],
                                  template, egress, default_state_dir()),
    )
    build_server(holder).run("stdio")


if __name__ == "__main__":
    main()
