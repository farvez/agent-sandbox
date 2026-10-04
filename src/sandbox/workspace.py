import os
import shutil
import tempfile
import uuid
from typing import Dict, List, Optional
import docker

from src.egress.gateway import EgressGateway, EgressSession, get_gateway
from src.egress.proxy import expand_rules
from src.sandbox.gvisor import GVisorSandboxRunner
from src.sandbox.workspace_pool import QuotaExceededError, disk_usage, get_pool, quota_bytes_from_env

# Every container gets these. User installs land in the persistent workspace
# (the root filesystem is read-only and /tmp is noexec, which breaks native wheels).
BASE_ENV = {
    "HOME": "/tmp",
    "PYTHONUSERBASE": "/workspace/.local",
    "PIP_USER": "1",
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PATH": "/workspace/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


class SandboxedWorkspace:
    """Manages an ephemeral directory and routes commands into an isolated gVisor/Docker container.

    By default containers have no network at all. Passing `egress` (host rules or
    presets such as "pypi") routes them through the egress gateway instead, which
    allows HTTPS to exactly those hosts and logs every connection.

    The workspace has a disk quota (SANDBOX_WORKSPACE_QUOTA_MB, default 512): enforced
    by the kernel when SANDBOX_WORKSPACE_POOL points at the quota-limited slot pool,
    and checked by the workspace itself everywhere (see workspace_pool.py).
    """

    def __init__(
        self,
        base_image: str = "sandbox-base:latest",
        egress: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        tenant_id: str = "local",
        gateway: Optional[EgressGateway] = None,
        quota_mb: Optional[int] = None,
    ):
        self.base_image = base_image
        self.session_id = session_id or f"local_{uuid.uuid4().hex[:12]}"
        self.tenant_id = tenant_id
        self.client = docker.from_env()
        self.gvisor_runner = GVisorSandboxRunner(image=base_image)
        self.has_gvisor = self.gvisor_runner.is_gvisor_available()

        # Production sets SANDBOX_REQUIRE_GVISOR=1 so a missing runsc fails loudly
        # instead of silently degrading to the shared-kernel runc runtime.
        if os.getenv("SANDBOX_REQUIRE_GVISOR") == "1" and not self.has_gvisor:
            raise RuntimeError("SANDBOX_REQUIRE_GVISOR=1 but the Docker daemon has no 'runsc' runtime.")

        self.quota_bytes = quota_bytes_from_env(quota_mb)
        self.pool = get_pool()
        claimed = self.pool.claim() if self.pool else tempfile.mkdtemp(prefix="agent_workspace_")
        self.workspace_dir = os.path.realpath(claimed)
        self.container_user = self._container_user()

        self.egress_rules = expand_rules(egress or [])
        self.gateway: Optional[EgressGateway] = None
        self.egress: Optional[EgressSession] = None
        if self.egress_rules:
            try:
                self._connect_egress(gateway)
            except Exception:
                self.cleanup()
                raise

    def _connect_egress(self, gateway: Optional[EgressGateway]) -> None:
        self.gateway = gateway or get_gateway(
            self.client,
            runtime="runsc" if self.has_gvisor else None,
            user=self.container_user,
        )
        self.egress = self.gateway.attach(self.session_id, self.tenant_id, self.egress_rules)

    @property
    def egress_network(self) -> Optional[str]:
        return self.egress.network_name if self.egress else None

    @property
    def proxy_url(self) -> Optional[str]:
        return self.egress.proxy_url if self.egress else None

    def egress_events(self, limit: int = 200) -> List[dict]:
        return self.gateway.events(self.session_id, limit) if self.gateway else []

    def _container_user(self) -> Optional[str]:
        """Runs containers as the host owner of the workspace so the bind mount is writable.

        On Windows/macOS Docker Desktop handles mount permissions, so the image's
        own user is kept. A root host process maps to uid 1000 rather than root.
        """
        if not hasattr(os, "getuid"):
            return None
        uid, gid = os.getuid(), os.getgid()
        if uid == 0:
            uid = gid = 1000
            os.chown(self.workspace_dir, uid, gid)
        return f"{uid}:{gid}"

    def _resolve_safe_path(self, relative_path: str) -> str:
        """Guards against directory and symlink traversal attacks using realpath and commonpath."""
        # Join and resolve symbolic links on the host
        candidate_path = os.path.realpath(os.path.join(self.workspace_dir, relative_path))
        
        # Verify candidate path is strictly within the workspace root
        if os.path.commonpath([self.workspace_dir, candidate_path]) != self.workspace_dir:
            raise PermissionError(f"Access denied: symlink/path traversal outside workspace for '{relative_path}'")
        return candidate_path

    def disk_usage_bytes(self) -> int:
        return disk_usage(self.workspace_dir)

    def write_file(self, path: str, content: str) -> str:
        """Writes content safely into the workspace, within its disk quota."""
        safe_path = self._resolve_safe_path(path)
        data_len = len(content.encode("utf-8"))
        replaced = os.path.getsize(safe_path) if os.path.isfile(safe_path) else 0
        projected = self.disk_usage_bytes() - replaced + data_len
        if projected > self.quota_bytes:
            raise QuotaExceededError(
                f"Writing {path} would use {projected / 2**20:.1f} MB of the workspace's "
                f"{self.quota_bytes / 2**20:.0f} MB disk quota. Delete files first."
            )
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to {path}"

    def read_file(self, path: str) -> str:
        """Reads content safely from the workspace."""
        safe_path = self._resolve_safe_path(path)
        if not os.path.exists(safe_path):
            return f"Error: File '{path}' does not exist."
        with open(safe_path, "r", encoding="utf-8") as f:
            return f.read()

    def run_command(self, command: str, timeout_seconds: int = 15) -> str:
        """Executes a command and returns the result as text (the agent tools' format)."""
        return format_result(self.execute(command, timeout_seconds))

    def execute(self, command: str, timeout_seconds: int = 15) -> dict:
        """Executes a command inside the hardened container with dropped privileges.

        Returns {stdout, stderr, exit_code, timed_out, oom_killed, warnings}.
        """
        container = None
        try:
            mounts = {
                self.workspace_dir: {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            }

            environment: Dict[str, str] = dict(BASE_ENV)
            if self.proxy_url:
                # Upper- and lower-case: different tools read different spellings.
                for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                    environment[var] = self.proxy_url

            container_kwargs = {
                "image": self.base_image,
                "command": ["/bin/bash", "-c", command],
                "mem_limit": "256m",
                "memswap_limit": "256m",
                "nano_cpus": 1_000_000_000,
                "pids_limit": 64,
                "cap_drop": ["ALL"],                     # Drop all Linux capabilities
                "security_opt": ["no-new-privileges:true"], # Prevent setuid escalations
                # Image filesystem is immutable; the only writable places are the
                # workspace and a small RAM-backed /tmp (Docker's default noexec,nosuid).
                "read_only": True,
                "tmpfs": {"/tmp": "size=64m"},
                "environment": environment,
                "volumes": mounts,
                "working_dir": "/workspace",
                "detach": True,
            }
            if self.egress_network:
                # The session's private network: its only reachable host is the proxy.
                container_kwargs["network"] = self.egress_network
            else:
                container_kwargs["network_mode"] = "none"

            if self.has_gvisor:
                container_kwargs["runtime"] = "runsc"
            if self.container_user:
                container_kwargs["user"] = self.container_user

            container = self.client.containers.create(**container_kwargs)
            container.start()

            try:
                wait_res = container.wait(timeout=timeout_seconds)
                exit_code = wait_res.get("StatusCode", -1)
            except Exception:
                container.kill()
                return {
                    "stdout": "", "stderr": "", "exit_code": -1, "timed_out": True, "oom_killed": False,
                    "warnings": [f"Execution exceeded {timeout_seconds}s limit."],
                }

            # attrs is a snapshot from create(); refresh it to see the final state
            container.reload()
            oom_killed = bool(container.attrs.get("State", {}).get("OOMKilled", False))

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            warnings = []
            if oom_killed:
                warnings.append("Process killed by cgroups (Memory limit exceeded).")
            elif exit_code == 137:
                # SIGKILL. We only kill on timeout (handled above), so this is almost always
                # the memory limit; Docker doesn't always set OOMKilled (seen on cgroup v2 hosts).
                warnings.append("Process was killed (exit 137, SIGKILL), most likely for exceeding the memory limit.")
            used = self.disk_usage_bytes()
            if used > self.quota_bytes:
                warnings.append(
                    f"Workspace uses {used / 2**20:.1f} MB, over its "
                    f"{self.quota_bytes / 2**20:.0f} MB disk quota. Delete files before writing more."
                )
            return {
                "stdout": stdout, "stderr": stderr, "exit_code": exit_code,
                "timed_out": False, "oom_killed": oom_killed, "warnings": warnings,
            }

        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def cleanup(self):
        """Wipes the ephemeral workspace directory and removes the session's egress network."""
        if self.gateway and self.egress:
            self.gateway.detach(self.egress)
            self.egress = None
        if self.pool:
            # Exactly once: after release the slot may already belong to another session.
            if not getattr(self, "_released", False):
                self._released = True
                self.pool.release(self.workspace_dir)
        elif os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


def format_result(result: dict) -> str:
    """The text form of an execute() result: [STDOUT] / [STDERR] / [WARNING] / [TIMEOUT] / [EXIT CODE]."""
    if result["timed_out"]:
        return f"[TIMEOUT]: {result['warnings'][0]}\n[EXIT CODE]: {result['exit_code']}"
    output = []
    if result["stdout"].strip():
        output.append(f"[STDOUT]:\n{result['stdout'].strip()}")
    if result["stderr"].strip():
        output.append(f"[STDERR]:\n{result['stderr'].strip()}")
    output.extend(f"[WARNING]: {warning}" for warning in result["warnings"])
    output.append(f"[EXIT CODE]: {result['exit_code']}")
    return "\n".join(output)