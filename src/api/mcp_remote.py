"""Remote MCP endpoint: <server>/mcp over Streamable HTTP, authenticated with an API key.

MCP clients (Claude Code, Claude Desktop, …) connect with just a URL and a key; nothing to
install locally:

    claude mcp add --transport http airlock https://<server>/mcp --header "X-API-Key: <key>"

Add `?egress=pypi` (comma-separated rules) to the URL for internet access.

Every HTTP request is authenticated and rate-limited before it reaches the MCP session
manager; the verified tenant is put on the request (`request.state.airlock_tenant`) and that
is the only identity tools use, never a client-supplied header. An MCP session belongs to the
key's tenant that opened it; requests for it with another tenant's key are refused.

Sandboxes: newer MCP clients are sessionless (each call stands alone), so the sandbox is tied
to the API key rather than to an MCP session: one sandbox per key, internet setting and
optional `?workspace=<name>`, created on first use, reused by every call and reconnect, and
closed by the normal idle TTL (30 minutes). Use different workspace names (or keys) for
separate sandboxes.
"""
from __future__ import annotations

import json
import re
import shlex
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

MAX_OUTPUT_CHARS = 20_000
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SESSION_HEADER = b"mcp-session-id"


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit:,} characters cut ...]\n\n{text[-tail:]}"


class AuthError(Exception):
    def __init__(self, status: int, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RemoteMcp:
    """`ops` is the server's session operations (server.McpOps): authenticate, start, run, …"""

    def __init__(self, ops, version: str = ""):
        self.ops = ops
        self.version = version
        self._lock = threading.Lock()
        self._owner: Dict[str, str] = {}                # MCP session id -> tenant
        self._sandbox: Dict[tuple, str] = {}            # (tenant, key, workspace, egress) -> sandbox id
        self._slot_locks: Dict[tuple, threading.Lock] = {}
        self._manager: Optional[StreamableHTTPSessionManager] = None
        self.server = self._build_server()

    # ------------------------------------------------------------------ HTTP side

    @asynccontextmanager
    async def lifespan(self):
        """Runs the MCP session manager for the life of the API process."""
        self._manager = StreamableHTTPSessionManager(
            app=self.server._lowlevel_server,
            # Clients authenticate with an API key header on every request, so browser-based
            # DNS rebinding can't reach anything; the protection is for unauthenticated local servers.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            session_idle_timeout=1800,
        )
        async with self._manager.run():
            yield
        self._manager = None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        try:
            tenant, actor = await anyio.to_thread.run_sync(self._authenticate, headers)
        except AuthError as e:
            await self._error(send, e)
            return

        mcp_session = headers.get(SESSION_HEADER, b"").decode("latin-1") or None
        if mcp_session:
            with self._lock:
                owner = self._owner.get(mcp_session)
            if owner is not None and owner != tenant:
                await self._error(send, AuthError(404, "Session not found"))
                return

        state = scope.setdefault("state", {})
        state["airlock_tenant"] = tenant
        state["airlock_actor"] = actor
        query = scope.get("query_string", b"")
        state["airlock_egress"] = self._egress_from_query(query)
        workspace = self._query_value(query, "workspace") or "default"
        if not WORKSPACE_RE.match(workspace):
            await self._error(send, AuthError(400, "workspace must be 1-40 letters, digits, '-' or '_'."))
            return
        state["airlock_workspace"] = workspace

        async def send_and_bind(message):
            # The session id is assigned in the response to `initialize`: bind it to this tenant.
            if message["type"] == "http.response.start":
                for name, value in message.get("headers", []):
                    if name.lower() == SESSION_HEADER:
                        with self._lock:
                            self._owner.setdefault(value.decode("latin-1"), tenant)
            await send(message)

        if self._manager is None:
            await self._error(send, AuthError(503, "MCP endpoint is starting; try again."))
            return
        await self._manager.handle_request(scope, receive, send_and_bind)
        if scope.get("method") == "DELETE" and mcp_session:
            with self._lock:
                self._owner.pop(mcp_session, None)

    def _authenticate(self, headers: Dict[bytes, bytes]) -> Tuple[str, str]:
        key = headers.get(b"x-api-key", b"").decode("latin-1").strip()
        if not key:
            auth = headers.get(b"authorization", b"").decode("latin-1")
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
        if not key:
            raise AuthError(401, "Missing API key: send it in the X-API-Key header (or Authorization: Bearer).")
        tenant = self.ops.authenticate(key)
        if tenant is None:
            raise AuthError(401, "Invalid API key.")
        self.ops.check_rate(tenant)   # raises AuthError(429) through ops
        return tenant, f"mcp {self.ops.actor_for_key(key)}"

    @staticmethod
    def _query_value(query: bytes, name: str) -> Optional[str]:
        from urllib.parse import parse_qs

        values = parse_qs(query.decode("latin-1")).get(name, [])
        return values[0].strip() if values else None

    @staticmethod
    def _egress_from_query(query: bytes) -> List[str]:
        from urllib.parse import parse_qs

        values = parse_qs(query.decode("latin-1")).get("egress", [])
        return sorted({r.strip() for v in values for r in v.split(",") if r.strip()})[:20]

    @staticmethod
    async def _error(send, e: AuthError) -> None:
        body = json.dumps({"error": str(e)}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if e.status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="airlock"'))
        if e.retry_after:
            headers.append((b"retry-after", str(e.retry_after).encode()))
        await send({"type": "http.response.start", "status": e.status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    # ------------------------------------------------------------------ MCP side

    def _sandbox_for(self, ctx, fresh: bool = False) -> Tuple[str, str, str]:
        """(tenant, sandbox session id, actor) for the calling key, starting a sandbox if needed.
        Only the identity our own authentication put on the request is used."""
        state = ctx.request_context.request.state
        tenant, actor = state.airlock_tenant, state.airlock_actor
        slot = (tenant, actor, state.airlock_workspace, tuple(state.airlock_egress))
        with self._lock:
            slot_lock = self._slot_locks.setdefault(slot, threading.Lock())
        with slot_lock:   # one sandbox per slot even if calls arrive together; other slots don't wait
            current = self._sandbox.get(slot)
            if current and not fresh and self.ops.exists(tenant, current):
                return tenant, current, actor
            if current:
                self.ops.end(tenant, current)
            sandbox = self.ops.start(tenant, list(state.airlock_egress))
            self._sandbox[slot] = sandbox
        return tenant, sandbox, actor

    async def _call(self, ctx, fn: Callable[[str, str, str], Any], fresh: bool = False):
        def work():
            tenant, sandbox, actor = self._sandbox_for(ctx, fresh)
            return fn(tenant, sandbox, actor)

        try:
            result = await anyio.to_thread.run_sync(work)
        except ToolError:
            raise
        except Exception as e:   # HTTPException, LimitExceeded, PermissionError, …
            raise ToolError(self.ops.describe_error(e)) from None
        return result

    def _build_server(self) -> MCPServer:
        server = MCPServer(
            name="airlock",
            title="Airlock sandbox",
            instructions=(
                "A remote, isolated Linux sandbox (gVisor container, Python 3.11, bash) for running code. "
                "Files in /workspace persist between commands; each command runs in a fresh container, "
                "so background processes don't. Internet access is limited to the hosts listed by sandbox_info."
            ),
            version=self.version,
            website_url="https://github.com/farvez/agent-sandbox",
        )
        ops = self.ops

        @server.tool(annotations=ToolAnnotations(title="Run a shell command", destructive_hint=False))
        async def run_command(command: str, timeout_seconds: int = 30, ctx: Context = None) -> str:
            """Run a shell command in the sandbox and return stdout, stderr and the exit code.

            Each call uses a fresh container; files in /workspace persist between calls.
            timeout_seconds must be 1-60.
            """
            timeout = max(1, min(int(timeout_seconds), 60))
            return truncate(await self._call(ctx, lambda t, s, a: ops.run(t, s, command, timeout, a)))

        @server.tool(annotations=ToolAnnotations(title="Write a file", destructive_hint=False))
        async def write_file(path: str, content: str, ctx: Context = None) -> str:
            """Create or overwrite a text file in the sandbox workspace (path relative to /workspace)."""
            await self._call(ctx, lambda t, s, a: ops.write_file(t, s, path, content))
            return f"Wrote {len(content)} characters to {path}"

        @server.tool(annotations=ToolAnnotations(title="Read a file", read_only_hint=True))
        async def read_file(path: str, ctx: Context = None) -> str:
            """Read a text file from the sandbox workspace (path relative to /workspace)."""
            return truncate(await self._call(ctx, lambda t, s, a: ops.read_file(t, s, path)))

        @server.tool(annotations=ToolAnnotations(title="List files", read_only_hint=True))
        async def list_files(path: str = ".", max_depth: int = 3, ctx: Context = None) -> str:
            """List files and directories in the sandbox workspace, with sizes (installed packages under .local are skipped)."""
            depth = max(1, min(int(max_depth), 10))
            command = (f"find {shlex.quote(path)} -maxdepth {depth} -path '*/.local' -prune -o -printf '%y %8s  %p\\n' "
                       "2>&1 | sort -k3 | head -500")
            return truncate(await self._call(ctx, lambda t, s, a: ops.run(t, s, command, 15, a)))

        @server.tool(annotations=ToolAnnotations(title="Import a GitHub repository", destructive_hint=False))
        async def import_repo(repo: str, ref: str = "", path: str = "", ctx: Context = None) -> str:
            """Copy a public GitHub repository into the sandbox workspace.

            repo is "owner/repo" or https://github.com/owner/repo. ref is a branch, tag or commit
            (default: the default branch); path is the folder under /workspace (default: the repo
            name). Works without internet access in the sandbox.
            """
            r = await self._call(ctx, lambda t, s, a: ops.import_repo(t, s, repo, ref or None, path or None, a))
            return f"Imported {r['repo']}@{r['ref']}: {r['files']} files in {r['path']}"

        @server.tool(annotations=ToolAnnotations(title="Egress log", read_only_hint=True))
        async def egress_log(limit: int = 50, ctx: Context = None) -> str:
            """Show the sandbox's outbound internet connections: which hosts were allowed or denied, and why."""
            events = await self._call(ctx, lambda t, s, a: ops.egress_events(t, s, max(1, min(int(limit), 500))))
            if not events:
                return "No outbound connections yet."
            return "\n".join(
                f"{e.get('decision', '?'):5}  {e.get('host') or e.get('target', '?')}"
                + (f"  ({e['reason']})" if e.get("reason") else "")
                for e in events
            )

        @server.tool(annotations=ToolAnnotations(title="Activity log", read_only_hint=True))
        async def activity(limit: int = 20, ctx: Context = None) -> str:
            """Show recent commands run in this tenant's sandboxes (newest first): command, exit code, duration."""
            entries = await self._call(ctx, lambda t, s, a: ops.audit(t, max(1, min(int(limit), 100))))
            if not entries:
                return "No commands recorded yet."
            lines = []
            for e in entries:
                result = ("timed out" if e.get("timed_out") else "out of memory" if e.get("oom_killed")
                          else "failed" if e.get("exit_code") is None else f"exit {e['exit_code']}")
                lines.append(f"{result:13} {e.get('command', '')}" + (f"   [{e['session_id']}]" if e.get("session_id") else ""))
            return "\n".join(lines)

        @server.tool(annotations=ToolAnnotations(title="Sandbox info", read_only_hint=True))
        async def sandbox_info(ctx: Context = None) -> str:
            """Show the sandbox's session, internet access, disk quota and the tenant's limits."""
            return await self._call(ctx, lambda t, s, a: ops.info(t, s))

        @server.tool(annotations=ToolAnnotations(title="Reset the sandbox", destructive_hint=True))
        async def reset_sandbox(ctx: Context = None) -> str:
            """Delete the sandbox workspace and start a fresh, empty one."""
            sandbox = await self._call(ctx, lambda t, s, a: s, fresh=True)
            return f"Started a fresh sandbox ({sandbox}). All previous files are gone."

        return server
