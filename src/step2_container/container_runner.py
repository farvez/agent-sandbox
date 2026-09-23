import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional, Dict
import docker
from docker.errors import ContainerError, ImageNotFound, APIError


@dataclass
class ContainerExecutionResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    oom_killed: bool = False


class IsolatedContainerRunner:
    """Manages ephemeral container lifecycles with strict cgroup limits and no network access."""

    def __init__(
        self,
        image: str = "sandbox-base:latest",
        mem_limit: str = "256m",
        cpu_cores: float = 1.0,
        pids_limit: int = 64,
    ):
        self.image = image
        self.mem_limit = mem_limit
        self.nano_cpus = int(cpu_cores * 1_000_000_000)
        self.pids_limit = pids_limit
        self.client = docker.from_env()

    def run_in_sandbox(
        self,
        command: str,
        workspace_dir: Optional[str] = None,
        timeout_seconds: int = 15,
        environment: Optional[Dict[str, str]] = None,
    ) -> ContainerExecutionResult:
        """Executes a command inside an isolated ephemeral container."""
        # Provision an ephemeral host directory if none provided
        auto_cleanup = False
        if workspace_dir is None:
            workspace_dir = tempfile.mkdtemp(prefix="agent_sandbox_")
            auto_cleanup = True

        container = None
        try:
            # Mount host workspace to container /workspace
            mounts = {
                os.path.abspath(workspace_dir): {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            }

            container = self.client.containers.create(
                image=self.image,
                command=["/bin/bash", "-c", command],
                network_mode="none",              # No external egress or ingress
                mem_limit=self.mem_limit,         # Hard memory ceiling
                nano_cpus=self.nano_cpus,         # CFS CPU quota
                pids_limit=self.pids_limit,       # Prevent fork bombs
                volumes=mounts,
                working_dir="/workspace",
                environment=environment or {},
                detach=True,
            )

            container.start()

            # Wait for execution or timeout
            try:
                result = container.wait(timeout=timeout_seconds)
                exit_code = result.get("StatusCode", -1)
            except Exception:
                container.kill()
                return ContainerExecutionResult(
                    command=command,
                    exit_code=-1,
                    stdout="",
                    stderr=f"Execution exceeded timeout limit of {timeout_seconds}s",
                    oom_killed=False,
                )

            # Inspect container state for OOM kills
            inspection = self.client.api.inspect_container(container.id)
            oom_killed = inspection.get("State", {}).get("OOMKilled", False)

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            return ContainerExecutionResult(
                command=command,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                oom_killed=oom_killed,
            )

        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            if auto_cleanup and os.path.exists(workspace_dir):
                shutil.rmtree(workspace_dir, ignore_errors=True)


if __name__ == "__main__":
    runner = IsolatedContainerRunner()

    print("--- Test 1: Workspace File Operations ---")
    res1 = runner.run_in_sandbox("echo 'Agent code output' > output.txt && cat output.txt")
    print(f"Exit Code: {res1.exit_code} | Output: {res1.stdout.strip()}")

    print("\n--- Test 2: Network Isolation Check ---")
    res2 = runner.run_in_sandbox(
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)\""
    )
    print(f"Exit Code: {res2.exit_code}")
    print(f"Stderr (Expected Network Failure):\n{res2.stderr.strip()[:200]}...")

    print("\n--- Test 3: CGroup Memory Kill Check ---")
    res3 = runner.run_in_sandbox("python3 -c \"x = 'a' * (300 * 1024 * 1024)\"")
    print(f"Exit Code: {res3.exit_code} | OOM Killed: {res3.oom_killed}")