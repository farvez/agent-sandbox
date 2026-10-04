import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional, Dict
import docker


@dataclass
class GVisorExecutionResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    runtime_used: str
    is_sandboxed: bool


class GVisorSandboxRunner:
    """Manages container execution using Google's gVisor (runsc) with graceful fallback."""

    def __init__(self, image: str = "sandbox-base:latest"):
        self.image = image
        self.client = docker.from_env()

    def is_gvisor_available(self) -> bool:
        """Verifies if the runsc runtime is registered in Docker daemon."""
        try:
            info = self.client.info()
            runtimes = info.get("Runtimes", {})
            return "runsc" in runtimes
        except Exception:
            return False

    def run(
        self,
        command: str,
        workspace_dir: Optional[str] = None,
        timeout_seconds: int = 15,
        environment: Optional[Dict[str, str]] = None,
    ) -> GVisorExecutionResult:
        """Executes a command inside the container using runsc or standard runtime fallback."""
        auto_cleanup = False
        if workspace_dir is None:
            workspace_dir = tempfile.mkdtemp(prefix="sandbox_gvisor_")
            auto_cleanup = True

        # Check for gVisor runtime availability
        has_gvisor = self.is_gvisor_available()
        runtime_to_use = "runsc" if has_gvisor else None
        runtime_label = "gVisor (runsc)" if has_gvisor else "Standard Docker (runc fallback)"

        container = None
        try:
            mounts = {
                os.path.abspath(workspace_dir): {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            }

            container_kwargs = {
                "image": self.image,
                "command": ["/bin/bash", "-c", command],
                "network_mode": "none",
                "mem_limit": "256m",
                "memswap_limit": "256m",
                "nano_cpus": 1_000_000_000,
                "pids_limit": 64,
                "volumes": mounts,
                "working_dir": "/workspace",
                "environment": environment or {},
                "detach": True,
            }

            # Only pass runtime parameter if runsc is registered
            if runtime_to_use:
                container_kwargs["runtime"] = runtime_to_use

            container = self.client.containers.create(**container_kwargs)
            container.start()

            try:
                result = container.wait(timeout=timeout_seconds)
                exit_code = result.get("StatusCode", -1)
            except Exception:
                container.kill()
                return GVisorExecutionResult(
                    command=command,
                    exit_code=-1,
                    stdout="",
                    stderr=f"Execution exceeded timeout limit of {timeout_seconds}s",
                    runtime_used=runtime_label,
                    is_sandboxed=True,
                )

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            return GVisorExecutionResult(
                command=command,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                runtime_used=runtime_label,
                is_sandboxed=True,
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
    runner = GVisorSandboxRunner()
    has_runsc = runner.is_gvisor_available()

    print("==================================================")
    print("      STEP 4: KERNEL ISOLATION RUNTIME CHECK      ")
    print("==================================================")
    print(f"Docker Daemon gVisor Support Detected: {has_runsc}")

    if has_runsc:
        print("[Status] Using gVisor 'runsc' userspace kernel sandbox.")
    else:
        print("[Status] 'runsc' not detected on local host.")
        print("[Fallback] Gracefully defaulting to standard 'runc' with cgroups & network isolation.")
        print("[Note] Cloud deployment (AWS EC2 / Terraform) installs runsc automatically via bootstrap.")

    print("\n--- Executing Test Command inside Sandbox ---")
    test_result = runner.run("python3 -c \"import os, platform; print(f'OS: {platform.system()} | PIDs: {os.getpid()}')\"")
    
    print(f"Runtime Engine : {test_result.runtime_used}")
    print(f"Exit Code      : {test_result.exit_code}")
    print(f"Sandboxed Stdout: {test_result.stdout.strip()}")
    print("==================================================")