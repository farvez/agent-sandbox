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


# ---------------------------------------------------------------- egress gateway
# These reach the real internet (PyPI) through the proxy.


@pytest.fixture
def egress_workspace():
    from src.egress.gateway import shutdown_gateway

    import uuid

    # Unique per test: the egress log is shared, and events are matched by session ID.
    with SandboxedWorkspace(egress=["pypi"], session_id=f"sbx_pytest_{uuid.uuid4().hex[:8]}", tenant_id="acme") as ws:
        yield ws
    shutdown_gateway()


def test_pip_install_through_allowlist(egress_workspace):
    out = egress_workspace.run_command(
        "pip install six==1.16.0 >/dev/null 2>&1; python3 -c 'import six; print(six.__version__)'", timeout_seconds=60
    )
    assert "1.16.0" in out
    hosts = {e["host"] for e in egress_workspace.egress_events() if e["decision"] == "allow"}
    assert "pypi.org" in hosts


def test_host_off_allowlist_is_refused_and_logged(egress_workspace):
    out = egress_workspace.run_command(
        "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\" 2>&1 | tail -1"
    )
    assert "403" in out
    assert any(e["host"] == "example.com" and e["decision"] == "deny" for e in egress_workspace.egress_events())


def test_metadata_service_unreachable_with_egress(egress_workspace):
    probe = "python3 -c \"import urllib.request; urllib.request.urlopen('{}://169.254.169.254/latest/meta-data/', timeout=3)\" 2>&1 | tail -1"
    assert "405" in egress_workspace.run_command(probe.format("http"))   # plain HTTP never proxied
    assert "403" in egress_workspace.run_command(probe.format("https"))  # not on the allowlist
    reasons = {e.get("reason") for e in egress_workspace.egress_events()}
    assert {"only HTTPS (CONNECT) is supported", "host not on allowlist"} <= reasons


def test_bypassing_the_proxy_has_no_route(egress_workspace):
    out = egress_workspace.run_command(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)\" 2>&1 | tail -1"
    )
    assert "unreachable" in out.lower() or "timed out" in out.lower()


def test_egress_network_removed_on_cleanup():
    import docker
    from src.egress.gateway import shutdown_gateway

    ws = SandboxedWorkspace(egress=["pypi"], session_id="sbx_pytest_cleanup")
    name = ws.egress_network
    ws.cleanup()
    shutdown_gateway()
    assert not docker.from_env().networks.list(names=[name])


def test_sessions_get_separate_networks():
    from src.egress.gateway import shutdown_gateway

    a = SandboxedWorkspace(egress=["pypi"], session_id="sbx_pytest_a")
    b = SandboxedWorkspace(egress=["pypi"], session_id="sbx_pytest_b")
    try:
        assert a.egress_network != b.egress_network
        assert a.proxy_url.split("@")[1] != b.proxy_url.split("@")[1]  # different proxy IPs
    finally:
        a.cleanup()
        b.cleanup()
        shutdown_gateway()


def test_first_start_prunes_orphaned_networks_but_never_live_ones():
    import docker
    from src.egress.gateway import LABEL, shutdown_gateway

    client = docker.from_env()
    shutdown_gateway()
    orphan = client.networks.create("agent-sandbox-egress-sbx_orphan", internal=True, labels={LABEL: "sbx_orphan"})
    a = SandboxedWorkspace(egress=["pypi"], session_id="sbx_pytest_live_a")
    b = None
    try:
        assert not client.networks.list(names=[orphan.name])        # pruned on first start
        b = SandboxedWorkspace(egress=["pypi"], session_id="sbx_pytest_live_b")
        assert client.networks.list(names=[a.egress_network])        # a's network survives b's arrival
        assert "[EXIT CODE]: 0" in a.run_command("true")
    finally:
        a.cleanup()
        if b:
            b.cleanup()
        shutdown_gateway()
