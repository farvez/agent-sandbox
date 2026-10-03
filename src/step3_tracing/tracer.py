import os
import shlex
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


def parse_strace_summary(text: str) -> Dict[str, int]:
    """Parses `strace -c` output into {syscall: calls}.

    Rows sit between the two dashed separator lines, in one of two shapes:
        % time  seconds  usecs/call  calls  errors  syscall
        % time  seconds  usecs/call  calls  syscall          (no errors)
    The aggregate `total` row comes after the second separator and is skipped.
    """
    counts: Dict[str, int] = {}
    recording = False
    for line in text.splitlines():
        if line.startswith("------"):
            recording = not recording
            continue
        if not recording:
            continue

        parts = line.split()
        if len(parts) < 5 or parts[-1].lower() == "total":
            continue
        calls_str = parts[-3] if len(parts) == 6 else parts[-2]
        try:
            counts[parts[-1]] = int(calls_str)
        except ValueError:
            pass
    return counts


class SyscallTracer:
    """Executes containerized commands under strace with robust parsing and quoting."""

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
        auto_cleanup = False
        if workspace_dir is None:
            workspace_dir = tempfile.mkdtemp(prefix="agent_trace_")
            auto_cleanup = True

        container = None
        filter_expr = f"-e trace={','.join(filter_calls)}" if filter_calls else "-e trace=file,process,network"
        
        # Safely quote user command to prevent single-quote escaping syntax breakage
        safe_cmd = shlex.quote(command)
        traced_cmd = f"strace -f -c {filter_expr} -o /workspace/.strace_summary /bin/bash -c {safe_cmd}"

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
                cap_add=["SYS_PTRACE"],
                volumes=mounts,
                working_dir="/workspace",
                detach=True,
            )

            container.start()

            # Guard against commands hanging indefinitely
            try:
                wait_res = container.wait(timeout=timeout_seconds)
                exit_code = wait_res.get("StatusCode", -1)
            except Exception:
                try:
                    container.kill()
                except Exception:
                    pass
                exit_code = -1

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            if exit_code == -1:
                stderr += f"\n[TIMEOUT]: Process exceeded {timeout_seconds}s limit."

            summary_file = os.path.join(workspace_dir, ".strace_summary")
            syscall_counts = {}
            if os.path.exists(summary_file):
                with open(summary_file, "r") as f:
                    syscall_counts = parse_strace_summary(f.read())

            return SyscallTraceResult(
                command=command,
                exit_code=exit_code,
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
    print("--- Testing Quote Escaping & Complex Commands ---")
    res = tracer.trace_command(
        """python3 -c "msg = 'escaped single quotes'; print(f'Result: {msg}')" """,
        filter_calls=["write", "openat"],
    )
    print(f"Exit Code: {res.exit_code}")
    print(f"Stdout   : {res.stdout.strip()}")
    print(f"Syscalls : {res.syscall_counts}")