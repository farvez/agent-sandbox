import os
import shutil
import tempfile

import pytest

# The API module refuses to import without keys, so set them before any test imports it:
# two tenants with their own keys, plus the single-key "default" tenant.
TENANT_KEYS = {"acme": "acme-test-key", "globex": "globex-test-key", "default": "default-test-key"}
os.environ["SANDBOX_API_KEYS"] = "acme:acme-test-key,globex:globex-test-key"
os.environ["SANDBOX_API_KEY"] = TENANT_KEYS["default"]
# acme may use PyPI and one API; globex has no internet; default may use anything under example.com.
os.environ["SANDBOX_EGRESS_POLICY"] = '{"acme": ["pypi", "api.openai.com"], "default": ["*.example.com"]}'

from src.step5_agent.sandbox import SandboxedWorkspace


def make_offline_workspace() -> SandboxedWorkspace:
    """A SandboxedWorkspace with a real temp dir but no Docker connection.

    Enough for the host-side file operations (path checks, read/write).
    """
    ws = SandboxedWorkspace.__new__(SandboxedWorkspace)
    ws.base_image = "sandbox-base:latest"
    ws.client = None
    ws.has_gvisor = False
    ws.container_user = None
    ws.session_id = "local_offline"
    ws.tenant_id = "local"
    ws.egress_rules = []
    ws.gateway = None
    ws.egress_network = None
    ws.proxy_url = None
    ws.workspace_dir = os.path.realpath(tempfile.mkdtemp(prefix="agent_workspace_test_"))
    return ws


@pytest.fixture
def offline_workspace():
    ws = make_offline_workspace()
    yield ws
    ws.cleanup()


@pytest.fixture
def outside_dir():
    """A directory outside any workspace, standing in for host files like .env."""
    path = tempfile.mkdtemp(prefix="outside_")
    with open(os.path.join(path, "secret.txt"), "w") as f:
        f.write("HOST SECRET")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _docker_ready() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get("sandbox-base:latest")
        return True
    except Exception:
        return False


DOCKER_READY = _docker_ready()
requires_docker = pytest.mark.skipif(
    not DOCKER_READY, reason="Docker daemon or sandbox-base:latest image not available"
)
