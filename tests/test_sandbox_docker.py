"""Integration tests against real containers. Skipped without Docker + sandbox-base:latest."""
import os

import pytest

from src.step5_agent.sandbox import SandboxedWorkspace
from tests.conftest import requires_docker

pytestmark = [pytest.mark.docker, requires_docker]


@pytest.fixture
def workspace():
    with SandboxedWorkspace() as ws:
        yield ws


def test_runs_command_and_reports_exit_code(workspace):
    out = workspace.run_command("echo hello && exit 3")
    assert "hello" in out
    assert "[EXIT CODE]: 3" in out


def test_files_persist_between_commands(workspace):
    workspace.run_command("echo persisted > note.txt")
    assert workspace.read_file("note.txt").strip() == "persisted"


def test_network_is_blocked(workspace):
    out = workspace.run_command(
        "python3 -c \"import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=3)\""
    )
    assert "[EXIT CODE]: 0" not in out


def test_oom_kill_is_detected(workspace):
    """Regression: stale container.attrs meant OOM kills were never reported."""
    out = workspace.run_command("python3 -c \"b = []\nwhile True: b.append(' ' * 10**7)\"", timeout_seconds=30)
    assert "Memory limit exceeded" in out


def test_timeout_kills_container(workspace):
    out = workspace.run_command("sleep 30", timeout_seconds=2)
    assert "[TIMEOUT]" in out


def test_root_filesystem_is_read_only(workspace):
    out = workspace.run_command("touch /etc/pwned || touch /usr/local/bin/pwned")
    assert "Read-only file system" in out
    assert "[EXIT CODE]: 0" not in out


def test_tmp_is_writable_but_size_limited(workspace):
    assert "[EXIT CODE]: 0" in workspace.run_command("echo ok > /tmp/x && cat /tmp/x")
    out = workspace.run_command("head -c 100M /dev/zero > /tmp/big")
    assert "No space left on device" in out


def test_home_is_writable(workspace):
    assert "[EXIT CODE]: 0" in workspace.run_command("touch ~/.cache_probe")


def test_runs_without_root(workspace):
    out = workspace.run_command("id -u")
    assert "[STDOUT]:\n0\n" not in out


def test_container_symlink_cannot_expose_host_files(workspace, outside_dir):
    # The container can create a link pointing anywhere; reading it through the
    # host-side API must not follow it out of the workspace.
    target = os.path.join(outside_dir, "secret.txt").replace("\\", "/")
    workspace.run_command(f"ln -s '{target}' leak")
    if not os.path.islink(os.path.join(workspace.workspace_dir, "leak")):
        # Docker Desktop on Windows stores container symlinks in a Linux-only
        # format the host cannot follow, so there is nothing to escape through.
        pytest.skip("Container symlinks are not followable host symlinks on this platform")
    with pytest.raises(PermissionError):
        workspace.read_file("leak")
