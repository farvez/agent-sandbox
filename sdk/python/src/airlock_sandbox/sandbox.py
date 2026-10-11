"""The Sandbox: one isolated session on an agent-sandbox server."""
from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional, Union

from airlock_sandbox._http import HTTPClient
from airlock_sandbox.errors import CommandError, SandboxError

DEFAULT_TEMPLATE = "sandbox-base:latest"


@dataclass
class CommandResult:
    """What a command did. `output` is the server's text form, handy to hand to an LLM."""

    command: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    oom_killed: bool = False
    warnings: List[str] = field(default_factory=list)
    output: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.oom_killed

    def check(self) -> "CommandResult":
        """Raises CommandError unless the command succeeded; returns self so it can be chained."""
        if not self.ok:
            reason = "timed out" if self.timed_out else "was killed for using too much memory" if self.oom_killed \
                else f"exited with code {self.exit_code}"
            raise CommandError(f"Command {self.command!r} {reason}.\n{self.output}", result=self)
        return self

    def __str__(self) -> str:
        return self.output

    @classmethod
    def from_response(cls, data: dict) -> "CommandResult":
        if "exit_code" in data:
            return cls(
                command=data.get("command", ""),
                stdout=data.get("stdout", ""),
                stderr=data.get("stderr", ""),
                exit_code=data["exit_code"],
                timed_out=data.get("timed_out", False),
                oom_killed=data.get("oom_killed", False),
                warnings=list(data.get("warnings", [])),
                output=data.get("output", ""),
            )
        return cls._from_text(data.get("command", ""), data.get("output", ""))

    @classmethod
    def _from_text(cls, command: str, output: str) -> "CommandResult":
        """Older servers only return the text form; split it back into its sections."""
        sections = {"STDOUT": [], "STDERR": []}
        warnings, exit_code, timed_out, current = [], -1, False, None
        for line in output.split("\n"):
            marker = re.match(r"^\[(STDOUT|STDERR|WARNING|TIMEOUT|EXIT CODE)\]:\s?(.*)$", line)
            if not marker:
                if current:
                    sections[current].append(line)
                continue
            name, rest = marker.groups()
            current = name if name in sections else None
            if name in ("WARNING", "TIMEOUT"):
                warnings.append(rest)
                timed_out = timed_out or name == "TIMEOUT"
            elif name == "EXIT CODE" and re.fullmatch(r"-?\d+", rest.strip()):
                exit_code = int(rest)
        stdout = "\n".join(sections["STDOUT"])
        stderr = "\n".join(sections["STDERR"])
        return cls(
            command=command,
            stdout=stdout + "\n" if stdout else "",
            stderr=stderr + "\n" if stderr else "",
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=any("Memory limit" in w for w in warnings),
            warnings=warnings,
            output=output,
        )


class Files:
    """File operations inside the sandbox workspace (paths are relative to /workspace)."""

    def __init__(self, sandbox: "Sandbox"):
        self._sandbox = sandbox

    def write(self, path: str, content: str) -> None:
        self._sandbox._call("POST", "/write", {"path": path, "content": content})

    def read(self, path: str) -> str:
        """Returns the file's text. Raises FileNotFoundError if it doesn't exist."""
        content = self._sandbox._call("GET", f"/read?{urllib.parse.urlencode({'path': path})}")["content"]
        if content == f"Error: File '{path}' does not exist.":
            raise FileNotFoundError(path)
        return content

    def upload(self, path: str, data: Union[bytes, str]) -> int:
        """Writes any bytes (str is encoded as UTF-8) to `path`, creating folders. Returns the size."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        query = urllib.parse.urlencode({"path": path})
        return self._sandbox._call("PUT", f"/files?{query}", data=data, timeout=max(self._sandbox._http.timeout, 300))["size_bytes"]

    def download(self, path: str) -> bytes:
        """Returns a file's raw bytes. Raises NotFoundError if it doesn't exist."""
        query = urllib.parse.urlencode({"path": path})
        return self._sandbox._call("GET", f"/files?{query}", raw=True, timeout=max(self._sandbox._http.timeout, 300))

    def download_archive(self, path: str = ".") -> bytes:
        """A folder (default: the whole workspace) as .tar.gz bytes."""
        query = urllib.parse.urlencode({"path": path})
        return self._sandbox._call("GET", f"/archive?{query}", raw=True, timeout=max(self._sandbox._http.timeout, 300))


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


class Sandbox:
    """An isolated workspace on an agent-sandbox server.

        with Sandbox(egress=["pypi"]) as sbx:
            sbx.files.write("main.py", "print(2 ** 16)")
            print(sbx.run("python3 main.py").stdout)

    Connection settings default to SANDBOX_API_URL, SANDBOX_API_KEY and
    SANDBOX_API_INSECURE=1 (accept a self-signed certificate).

    Args:
        api_key: tenant API key.
        base_url: server URL, e.g. "https://sandbox.example.com".
        egress: hosts or presets ("pypi", "npm", "github", "huggingface") this
            session may reach over HTTPS. Empty means no network at all.
        template: container image; must be on the server's allowlist.
        verify: True (default), False to accept a self-signed certificate, or a
            path to a CA bundle that signed the server's certificate.
        timeout: seconds to wait for each HTTP response.
        max_retries: retries for rate limits (429), full servers (503) and
            dropped connections on idempotent calls.
        snapshot: id of a saved snapshot to restore into the new session's /workspace.
        persistent: keep one container running for the whole session, so background
            processes (servers, workers) survive between commands and ports can be
            previewed with expose(). Tenants can run only a few at a time.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        egress: Optional[List[str]] = None,
        template: str = DEFAULT_TEMPLATE,
        verify: Optional[Union[bool, str]] = None,
        timeout: float = 90,
        max_retries: int = 3,
        snapshot: Optional[str] = None,
        persistent: bool = False,
    ):
        base_url = base_url or os.getenv("SANDBOX_API_URL")
        api_key = api_key or os.getenv("SANDBOX_API_KEY")
        if not base_url or not api_key:
            raise SandboxError("Set base_url and api_key, or the SANDBOX_API_URL and SANDBOX_API_KEY environment variables.")
        if verify is None:
            verify = not _env_flag("SANDBOX_API_INSECURE")

        from airlock_sandbox import __version__  # late import: avoids a cycle

        self._http = HTTPClient(base_url, api_key, verify=verify, timeout=timeout, max_retries=max_retries,
                                user_agent=f"airlock-sandbox/{__version__}")
        self.template = template
        self.requested_egress = list(egress or [])
        self.snapshot_id = snapshot
        self.persistent = persistent
        self.files = Files(self)
        self.session_id: Optional[str] = None
        self.tenant_id: Optional[str] = None
        self.egress: List[str] = []
        self.disk_quota_mb: Optional[int] = None

    @classmethod
    def attach(cls, session_id: str, *args, **kwargs) -> "Sandbox":
        """A handle to an existing session (e.g. one created by another process), without creating one."""
        sandbox = cls(*args, **kwargs)
        sandbox.session_id = session_id
        return sandbox

    @classmethod
    def create(cls, *args, **kwargs) -> "Sandbox":
        """Creates and starts a sandbox. Remember to close() it, or use `with Sandbox(...)`."""
        return cls(*args, **kwargs).start()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> "Sandbox":
        if self.session_id:
            return self
        body = {"template": self.template, "egress": self.requested_egress}
        if self.snapshot_id:
            body["snapshot_id"] = self.snapshot_id
        if self.persistent:
            body["persistent"] = True
        data = self._http.request("POST", "/v1/sessions", body,
                                  timeout=max(self._http.timeout, 240) if self.snapshot_id else None)
        self.session_id = data["session_id"]
        self.tenant_id = data.get("tenant_id")
        self.egress = data.get("egress", [])
        self.disk_quota_mb = data.get("disk_quota_mb")
        self.persistent = bool(data.get("persistent", self.persistent))
        return self

    def close(self, timeout: Optional[float] = None) -> None:
        """Deletes the session and its files. Safe to call more than once.

        `timeout` (seconds) makes this a single quick attempt with no retries — for
        shutdown paths that only have a short grace period.
        """
        if not self.session_id:
            return
        session_id, self.session_id = self.session_id, None
        try:
            self._http.request("DELETE", f"/v1/sessions/{session_id}",
                               timeout=timeout, retries=0 if timeout else None)
        except SandboxError as err:
            if err.status != 404:  # already gone (expired) is fine
                raise

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        return self._http.base_url

    def __repr__(self) -> str:
        state = self.session_id or "not started"
        return f"<Sandbox {state} tenant={self.tenant_id} egress={self.egress}>"

    # ------------------------------------------------------------------ work

    def run(self, command: str, timeout: int = 15) -> CommandResult:
        """Runs a shell command in a fresh container (1–60 s timeout). Files persist between runs."""
        data = self._call("POST", "/exec", {"command": command, "timeout_seconds": timeout})
        return CommandResult.from_response(data)

    def import_repo(self, repo: str, ref: Optional[str] = None, path: Optional[str] = None) -> dict:
        """Imports a public GitHub repository ("owner/repo" or its URL) into /workspace/<path>.

        `path` defaults to the repository name; `ref` (branch, tag or commit) to the default
        branch. The server downloads it, so the session needs no egress. Returns
        {"repo", "ref", "path", "files", "archive_bytes"}.
        """
        body = {"repo": repo, "ref": ref, "path": path}
        return self._call("POST", "/import", body, timeout=max(self._http.timeout, 240))

    def egress_log(self, limit: int = 200) -> List[dict]:
        """Every outbound connection this session attempted, allowed or denied."""
        return self._call("GET", f"/egress?limit={int(limit)}")["events"]

    def expose(self, port: int) -> str:
        """Returns a public preview URL for a server listening on `port` (0.0.0.0) in this
        long-running sandbox. Anyone with the URL can open it until the sandbox closes or
        unexpose(port). Needs persistent=True and a server with previews enabled."""
        return self._call("POST", "/previews", {"port": int(port)})["url"]

    def unexpose(self, port: int) -> None:
        self._call("DELETE", f"/previews/{int(port)}")

    def snapshot(self, name: Optional[str] = None, path: str = ".") -> dict:
        """Saves /workspace (or one folder) as a snapshot that outlives this session.

        Returns {"id", "name", "size_bytes", "session_id", "created_at", "expires_at"}; start a
        new sandbox from it with Sandbox(snapshot=id) or restore() it into this one.
        """
        return self._call("POST", "/snapshots", {"name": name, "path": path}, timeout=max(self._http.timeout, 240))

    def restore(self, snapshot_id: str, path: str = ".") -> dict:
        """Unpacks a snapshot into /workspace/<path>; files with the same names are replaced."""
        return self._call("POST", "/restore", {"snapshot_id": snapshot_id, "path": path},
                          timeout=max(self._http.timeout, 240))

    def list_snapshots(self) -> List[dict]:
        """This tenant's snapshots, newest first."""
        return self._http.request("GET", "/v1/snapshots")["snapshots"]

    def delete_snapshot(self, snapshot_id: str) -> None:
        self._http.request("DELETE", f"/v1/snapshots/{urllib.parse.quote(snapshot_id, safe='')}")

    # ------------------------------------------------------------------ account

    def usage(self) -> dict:
        """This tenant's limits and current usage."""
        return self._http.request("GET", "/v1/usage")

    def audit(self, limit: int = 100, before: Optional[str] = None) -> dict:
        """This tenant's command history, newest first: {"entries": [...], "next": cursor or None}.

        Each entry has ts, session_id, actor, kind ("exec" or "import"), command, exit_code,
        duration_s, timed_out and oom_killed; command output is never stored. Pass `next`
        back as `before` for older entries. Needs a server with the audit log enabled.
        """
        query = f"/v1/audit?limit={int(limit)}" + (f"&before={before}" if before else "")
        return self._http.request("GET", query)

    def egress_policy(self) -> List[str]:
        """Hosts this tenant's sessions may request."""
        return self._http.request("GET", "/v1/egress/policy")["allowed"]

    def health(self) -> dict:
        return self._http.request("GET", "/healthz")

    # ------------------------------------------------------------------ agent-loop compatibility

    def read_file(self, path: str) -> str:
        return self.files.read(path)

    def write_file(self, path: str, content: str) -> str:
        self.files.write(path, content)
        return f"Successfully wrote {len(content)} characters to {path}"

    def run_command(self, command: str, timeout_seconds: int = 15) -> str:
        return self.run(command, timeout=timeout_seconds).output

    # ------------------------------------------------------------------ internals

    def _call(self, method: str, suffix: str, body: Optional[dict] = None, timeout: Optional[float] = None,
              data: Optional[bytes] = None, raw: bool = False):
        if not self.session_id:
            raise SandboxError("Sandbox not started: use `with Sandbox(...)` or call start().")
        return self._http.request(method, f"/v1/sessions/{self.session_id}{suffix}", body, timeout=timeout,
                                  data=data, raw=raw)
