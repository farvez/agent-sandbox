import os
import re
import tempfile
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional
import docker


@dataclass
class SyscallTraceResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    syscall_counts: Dict[str, int]
    monitored_calls: List[str]


class SyscallTracer:
    """Executes containerized commands under strace to monitor kernel boundaries."""

    def __init__(self, image: str = "sandbox-base:latest"):
        self.image = image
        self.client = docker.from_env()

    def trace_command(
        self,
        command: str,
        workspace_dir: Optional[str] = None,
        filter_calls: Optional[List[str]] = None,
        timeout_seconds: int = 15,
    ) -> SyscallTraceResult:
        """Runs a command wrapped with strace and parses captured syscall events."""
        auto_cleanup = False
        if workspace_dir is None:
            workspace_dir = tempfile.mkdtemp(prefix="agent_trace_")
            auto_cleanup = True

        container = None
        filter_expr = f"-e trace={','.join(filter_calls)}" if filter_calls else "-e trace=file,process,network"
        
        # Output strace logs to a dedicated trace file inside /workspace
        traced_cmd = f"strace -f -c {filter_expr} -o /workspace/.strace_summary /bin/bash -c '{command}'"

        try:
            mounts = {
                os.path.abspath(workspace_dir): {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            }

            container = self.client.containers.create(
                image=self.image,
                command=["/bin/bash", "-c", traced_cmd],
                network_mode="none",
                mem_limit="256m",
                memswap_limit="256m",
                cap_add=["SYS_PTRACE"],  # Required for strace inside container
                volumes=mounts,
                working_dir="/workspace",
                detach=True,
            )

            container.start()
            container.wait(timeout=timeout_seconds)

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            # Parse strace summary table
            summary_file = os.path.join(workspace_dir, ".strace_summary")
            syscall_counts = {}
            if os.path.exists(summary_file):
                with open(summary_file, "r") as f:
                    for line in f:
                        match = re.search(r"(\d+)\s+([a-zA-Z0-9_]+)$", line.strip())
                        if match:
                            calls, name = match.groups()
                            syscall_counts[name] = int(calls)

            return SyscallTraceResult(
                command=command,
                exit_code=0,
                stdout=stdout,
                stderr=stderr,
                syscall_counts=syscall_counts,
                monitored_calls=list(syscall_counts.keys()),
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
    tracer = SyscallTracer()

    print("--- Test 1: File Creation & Writing Syscalls ---")
    res1 = tracer.trace_command(
        "python3 -c \"with open('test.txt', 'w') as f: f.write('audit log')\"",
        filter_calls=["openat", "write", "close"],
    )
    print("Detected Syscalls:", res1.syscall_counts)

    print("\n--- Test 2: Process Spawning (clone / execve) ---")
    res2 = tracer.trace_command(
        "python3 -c \"import subprocess; subprocess.run(['echo', 'subprocess called'])\"",
        filter_calls=["clone", "execve", "wait4"],
    )
    print("Detected Syscalls:", res2.syscall_counts)

    print("\n--- Test 3: Network Socket Syscall Interception ---")
    res3 = tracer.trace_command(
        "python3 -c \"import socket; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.connect(('1.1.1.1', 80))\"",
        filter_calls=["socket", "connect"],
    )
    print("Detected Syscalls:", res3.syscall_counts)