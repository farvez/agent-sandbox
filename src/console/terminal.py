"""The console terminal: each line runs as its own sandbox command (a fresh container
on the session's persistent workspace). The working directory is carried between
commands by the browser, so `cd` behaves like a shell; environment variables and
background processes do not persist.
"""
from __future__ import annotations

import shlex
from typing import Tuple

HOME = "/workspace"
CWD_MARKER = "__AIRLOCK_CWD__"
MAX_COMMAND_CHARS = 10_000


def clean_cwd(cwd: str) -> str:
    cwd = (cwd or "").strip()
    if not cwd.startswith("/") or len(cwd) > 1024 or any(c in cwd for c in "\n\r\0"):
        return HOME
    return cwd


def wrap_command(command: str, cwd: str) -> str:
    """Runs `command` in `cwd` and reports the final directory after a marker line."""
    return (
        f"cd -- {shlex.quote(clean_cwd(cwd))} 2>/dev/null || cd {HOME}\n"
        f"{command}\n"
        f"__airlock_status=$?; printf '\\n{CWD_MARKER}%s\\n' \"$PWD\"; exit $__airlock_status\n"
    )


def split_cwd(stdout: str, previous_cwd: str) -> Tuple[str, str]:
    """Removes the marker line; returns (stdout as the command printed it, new cwd).

    If the command exited the shell itself (so no marker), the directory stays put.
    """
    index = stdout.rfind("\n" + CWD_MARKER)
    if index == -1:
        return stdout, clean_cwd(previous_cwd)
    tail = stdout[index + 1 + len(CWD_MARKER):]
    new_cwd = tail.split("\n", 1)[0]
    return stdout[:index], clean_cwd(new_cwd)
