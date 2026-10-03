import threading
import time

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from tests.conftest import TENANT_KEYS, make_offline_workspace

AUTH = {"X-API-Key": TENANT_KEYS["acme"]}
OTHER_TENANT = {"X-API-Key": TENANT_KEYS["globex"]}


class FakeWorkspace:
    """Real host-side file handling, fake container execution."""

    exec_delay = 0.0

    def __init__(self, base_image: str = "sandbox-base:latest"):
        self._ws = make_offline_workspace()
        self.workspace_dir = self._ws.workspace_dir
        self.cleaned = False

    def write_file(self, path, content):
        return self._ws.write_file(path, content)

    def read_file(self, path):
        return self._ws.read_file(path)

    def run_command(self, command, timeout_seconds=15):
        time.sleep(self.exec_delay)
        return f"[STDOUT]:\nran {command}\n[EXIT CODE]: 0"

    def cleanup(self):
        self.cleaned = True
        self._ws.cleanup()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "SandboxedWorkspace", FakeWorkspace)
    FakeWorkspace.exec_delay = 0.0
    server.active_sessions.clear()
    with TestClient(server.app) as c:
        yield c
    server.active_sessions.clear()


def create(client):
    res = client.post("/v1/sessions", json={}, headers=AUTH)
    assert res.status_code == 201
    return res.json()["session_id"]


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": "ключ".encode()}])
def test_rejects_missing_wrong_or_non_ascii_key(client, headers):
    res = client.post("/v1/sessions", json={}, headers=headers)
    assert res.status_code == 401


def test_healthz_needs_no_key(client):
    assert client.get("/healthz").json()["status"] == "healthy"


def test_disallowed_template_is_rejected(client):
    res = client.post("/v1/sessions", json={"template": "attacker/miner:latest"}, headers=AUTH)
    assert res.status_code == 400


def test_full_session_lifecycle(client):
    sid = create(client)
    sbx = f"/v1/sessions/{sid}"

    assert client.post(f"{sbx}/write", json={"path": "a.py", "content": "x=1"}, headers=AUTH).status_code == 200
    assert client.get(f"{sbx}/read", params={"path": "a.py"}, headers=AUTH).json()["content"] == "x=1"
    assert "ran python3 a.py" in client.post(f"{sbx}/exec", json={"command": "python3 a.py"}, headers=AUTH).json()["output"]

    record = server.active_sessions[sid]
    assert client.delete(sbx, headers=AUTH).json()["status"] == "terminated"
    assert record.workspace.cleaned
    assert client.get(f"{sbx}/read", params={"path": "a.py"}, headers=AUTH).status_code == 404


def test_path_traversal_returns_403(client):
    sid = create(client)
    res = client.post(f"/v1/sessions/{sid}/write", json={"path": "../evil.py", "content": "x"}, headers=AUTH)
    assert res.status_code == 403


def test_exec_timeout_bounds_are_validated(client):
    sid = create(client)
    res = client.post(f"/v1/sessions/{sid}/exec", json={"command": "true", "timeout_seconds": 600}, headers=AUTH)
    assert res.status_code == 422


@pytest.mark.parametrize("tenant", ["acme", "globex", "default"])
def test_each_key_maps_to_its_tenant(client, tenant):
    res = client.post("/v1/sessions", json={}, headers={"X-API-Key": TENANT_KEYS[tenant]})
    assert res.json()["tenant_id"] == tenant


def test_other_tenant_cannot_touch_session(client):
    sid = create(client)
    sbx = f"/v1/sessions/{sid}"
    assert client.get(f"{sbx}/read", params={"path": "a"}, headers=OTHER_TENANT).status_code == 403
    assert client.post(f"{sbx}/write", json={"path": "a", "content": "x"}, headers=OTHER_TENANT).status_code == 403
    assert client.post(f"{sbx}/exec", json={"command": "id"}, headers=OTHER_TENANT).status_code == 403
    assert client.delete(sbx, headers=OTHER_TENANT).status_code == 403
    assert sid in server.active_sessions


@pytest.mark.parametrize(
    "multi, single, tenants",
    [
        ("a:k1, b:k2", None, ["a", "b"]),
        (None, "solo", ["default"]),
        ("a:k1", "solo", ["a", "default"]),
        ("a:key:with:colons", None, ["a"]),
    ],
)
def test_load_api_keys_parses_tenants(multi, single, tenants):
    assert [t for _, t in server.load_api_keys(multi, single)] == tenants


@pytest.mark.parametrize(
    "multi, single",
    [(None, None), ("", ""), ("no-colon", None), (":key", None), ("a:", None), ("a:same,b:same", None)],
)
def test_load_api_keys_rejects_bad_config(multi, single):
    with pytest.raises(RuntimeError):
        server.load_api_keys(multi, single)


def test_reaper_removes_only_idle_sessions(client):
    idle, fresh = create(client), create(client)
    idle_ws = server.active_sessions[idle].workspace
    server.active_sessions[idle].last_accessed_at -= server.SESSION_TTL_SECONDS + 1

    server.reap_expired_sessions()

    assert idle not in server.active_sessions
    assert fresh in server.active_sessions
    assert idle_ws.cleaned


def test_slow_exec_does_not_block_other_requests(client):
    """Regression: async endpoints calling blocking Docker code froze the whole server."""
    sid = create(client)
    FakeWorkspace.exec_delay = 2.0

    worker = threading.Thread(
        target=client.post, args=(f"/v1/sessions/{sid}/exec",), kwargs={"json": {"command": "sleep"}, "headers": AUTH}
    )
    worker.start()
    time.sleep(0.3)  # let the exec request start

    started = time.monotonic()
    assert client.get("/healthz").status_code == 200
    elapsed = time.monotonic() - started
    worker.join()

    assert elapsed < 1.0, f"/healthz waited {elapsed:.2f}s behind a running exec"
