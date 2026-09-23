import os
import shutil
import tempfile
from typing import Optional
from src.step2_container.container_runner import IsolatedContainerRunner, ContainerExecutionResult


class SandboxedWorkspace:
    """Manages an ephemeral directory and routes commands into an isolated container."""

    def __init__(self, base_image: str = "sandbox-base:latest"):
        self.workspace_dir = tempfile.mkdtemp(prefix="agent_workspace_")
        self.runner = IsolatedContainerRunner(image=base_image)

    def _resolve_safe_path(self, relative_path: str) -> str:
        """Guards against directory traversal attacks (e.g., ../../etc/passwd)."""
        clean_path = os.path.normpath(os.path.join(self.workspace_dir, relative_path))
        if not clean_path.startswith(self.workspace_dir):
            raise PermissionError(f"Access denied: path traversal attempt for '{relative_path}'")
        return clean_path

    def write_file(self, path: str, content: str) -> str:
        """Writes content to a file inside the isolated workspace."""
        safe_path = self._resolve_safe_path(path)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to {path}"

    def read_file(self, path: str) -> str:
        """Reads content from a file inside the isolated workspace."""
        safe_path = self._resolve_safe_path(path)
        if not os.path.exists(safe_path):
            return f"Error: File '{path}' does not exist."
        with open(safe_path, "r", encoding="utf-8") as f:
            return f.read()

    def run_command(self, command: str, timeout_seconds: int = 15) -> str:
        """Executes a command inside the locked-down container workspace."""
        res: ContainerExecutionResult = self.runner.run_in_sandbox(
            command=command,
            workspace_dir=self.workspace_dir,
            timeout_seconds=timeout_seconds,
        )

        output = []
        if res.stdout.strip():
            output.append(f"[STDOUT]:\n{res.stdout.strip()}")
        if res.stderr.strip():
            output.append(f"[STDERR]:\n{res.stderr.strip()}")
        if res.oom_killed:
            output.append("[WARNING]: Process was killed by cgroups for exceeding memory limits.")
        output.append(f"[EXIT CODE]: {res.exit_code}")

        return "\n".join(output)

    def cleanup(self):
        """Wipes the ephemeral workspace from the host machine."""
        if os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()