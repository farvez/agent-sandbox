import os
import shutil
import tempfile
from typing import Optional, Dict
import docker

from src.step4_gvisor.gvisor_runner import GVisorSandboxRunner


class SandboxedWorkspace:
    """Manages an ephemeral directory and routes commands into an isolated gVisor/Docker container."""

    def __init__(self, base_image: str = "sandbox-base:latest"):
        self.base_image = base_image
        self.client = docker.from_env()
        self.gvisor_runner = GVisorSandboxRunner(image=base_image)
        self.has_gvisor = self.gvisor_runner.is_gvisor_available()

        # Production sets SANDBOX_REQUIRE_GVISOR=1 so a missing runsc fails loudly
        # instead of silently degrading to the shared-kernel runc runtime.
        if os.getenv("SANDBOX_REQUIRE_GVISOR") == "1" and not self.has_gvisor:
            raise RuntimeError("SANDBOX_REQUIRE_GVISOR=1 but the Docker daemon has no 'runsc' runtime.")

        self.workspace_dir = os.path.realpath(tempfile.mkdtemp(prefix="agent_workspace_"))
        self.container_user = self._container_user()

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

    def write_file(self, path: str, content: str) -> str:
        """Writes content safely into the workspace."""
        safe_path = self._resolve_safe_path(path)
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
        """Executes a command inside the hardened container with dropped privileges."""
        container = None
        try:
            mounts = {
                self.workspace_dir: {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            }

            container_kwargs = {
                "image": self.base_image,
                "command": ["/bin/bash", "-c", command],
                "network_mode": "none",
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
                "environment": {"HOME": "/tmp"},         # tools that write to ~ still work
                "volumes": mounts,
                "working_dir": "/workspace",
                "detach": True,
            }

            if self.has_gvisor:
                container_kwargs["runtime"] = "runsc"
            if self.container_user:
                container_kwargs["user"] = self.container_user

            container = self.client.containers.create(**container_kwargs)
            container.start()

            oom_killed = False
            try:
                wait_res = container.wait(timeout=timeout_seconds)
                exit_code = wait_res.get("StatusCode", -1)
            except Exception:
                container.kill()
                return f"[TIMEOUT]: Execution exceeded {timeout_seconds}s limit.\n[EXIT CODE]: -1"

            # attrs is a snapshot from create(); refresh it to see the final state
            container.reload()
            if container.attrs.get("State", {}).get("OOMKilled", False):
                oom_killed = True

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            output = []
            if stdout.strip():
                output.append(f"[STDOUT]:\n{stdout.strip()}")
            if stderr.strip():
                output.append(f"[STDERR]:\n{stderr.strip()}")
            if oom_killed:
                output.append("[WARNING]: Process killed by cgroups (Memory limit exceeded).")
            output.append(f"[EXIT CODE]: {exit_code}")

            return "\n".join(output)

        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def cleanup(self):
        """Wipes the ephemeral workspace directory."""
        if os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()