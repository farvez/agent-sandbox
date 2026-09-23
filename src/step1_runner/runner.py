import subprocess
import shlex
from dataclasses import dataclass
from typing import Optional

@dataclass
class ExecutionResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

def run_command(command: str, timeout_seconds: int = 10, cwd: Optional[str] = None) -> ExecutionResult:
    """Executes a shell command with a hard timeout and standard stream capture."""
    args = shlex.split(command)
    try:
        process = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False
        )
        return ExecutionResult(
            command=command,
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            timed_out=False
        )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(
            command=command,
            exit_code=-1,
            stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
            stderr=f"Command exceeded timeout of {timeout_seconds}s",
            timed_out=True
        )
    except FileNotFoundError:
        return ExecutionResult(
            command=command,
            exit_code=127,
            stdout="",
            stderr=f"Command not found: {args[0]}",
            timed_out=False
        )

if __name__ == "__main__":
    # Smoke test
    res = run_command("python3 -c \"print('Hello from sandboxed tool!')\"")
    print(f"[{res.exit_code}] {res.stdout.strip()}")

    # Timeout test
    timed_out_res = run_command("sleep 5", timeout_seconds=1)
    print(f"Timed out: {timed_out_res.timed_out} | Error: {timed_out_res.stderr}")