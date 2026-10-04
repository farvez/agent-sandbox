import threading
import time

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.api.limits import LimitTracker, load_tenant_limits
from src.sandbox.workspace_pool import WorkspaceCapacityError
from tests.conftest import TENANT_KEYS, make_offline_workspace

AUTH = {"X-API-Key": TENANT_KEYS["acme"]}
OTHER_TENANT = {"X-API-Key": TENANT_KEYS["globex"]}


class FakeWorkspace:
    """Real host-side file handling, fake container execution."""

    exec_delay = 0.0
    capacity_left = None   # None = unlimited; an int counts down to a 503

    def __init__(self, base_image="sandbox-base:latest", egress=None, session_id=None, tenant_id="local"):
        if FakeWorkspace.capacity_left is not None:
            if FakeWorkspace.capacity_left <= 0:
                raise WorkspaceCapacityError("All sandbox workspaces are in use; try again shortly.")
            FakeWorkspace.capacity_left -= 1
        self._ws = make_offline_workspace()
        self.quota_bytes = self._ws.quota_bytes
        self.workspace_dir = self._ws.workspace_dir
        self.egress = egress or []
        self.cleaned = False

    def write_file(self, path, content):
        return self._ws.write_file(path, content)

    def read_file(self, path):
        return self._ws.read_file(path)

    def execute(self, command, timeout_seconds=15):
        time.sleep(self.exec_delay)
        return {"stdout": f"ran {command}\n", "stderr": "", "exit_code": 0,
                "timed_out": False, "oom_killed": False, "warnings": []}

    def egress_events(self, limit=200):
        return [{"decision": "allow", "host": "pypi.org"}] if self.egress else []

    def cleanup(self):
        self.cleaned = True
        self._ws.cleanup()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "SandboxedWorkspace", FakeWorkspace)
    FakeWorkspace.exec_delay = 0.0
    FakeWorkspace.capacity_left = None
    server.active_sessions.clear()
    # Generous limits so other tests never trip them; limit tests install their own.
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits(
        '{"*": {"max_sessions": 50, "requests_per_minute": 10000, "max_concurrent_exec": 50}}'
    )))
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


# ---------------------------------------------------------------- egress


def test_session_without_egress_has_no_network(client):
    res = client.post("/v1/sessions", json={}, headers=AUTH)
    assert res.json()["egress"] == []
    assert server.active_sessions[res.json()["session_id"]].workspace.egress == []


def test_allowed_egress_preset_is_expanded(client):
    res = client.post("/v1/sessions", json={"egress": ["pypi"]}, headers=AUTH)
    assert res.status_code == 201
    assert res.json()["egress"] == ["pypi.org", "files.pythonhosted.org"]


def test_egress_outside_tenant_policy_is_refused(client):
    res = client.post("/v1/sessions", json={"egress": ["pypi", "evil.com"]}, headers=AUTH)
    assert res.status_code == 403
    assert "evil.com" in res.json()["detail"]


def test_tenant_without_policy_gets_no_egress(client):
    res = client.post("/v1/sessions", json={"egress": ["pypi"]}, headers=OTHER_TENANT)
    assert res.status_code == 403


@pytest.mark.parametrize("rule, status_code", [("api.example.com", 201), ("*.example.com", 201), ("example.com", 403), ("*.com", 403)])
def test_wildcard_policy(client, rule, status_code):
    res = client.post("/v1/sessions", json={"egress": [rule]}, headers={"X-API-Key": TENANT_KEYS["default"]})
    assert res.status_code == status_code


def test_policy_endpoint_shows_own_tenant_only(client):
    assert client.get("/v1/egress/policy", headers=AUTH).json()["allowed"] == [
        "pypi.org", "files.pythonhosted.org", "api.openai.com"
    ]
    assert client.get("/v1/egress/policy", headers=OTHER_TENANT).json()["allowed"] == []


def test_egress_log_endpoint_is_tenant_scoped(client):
    sid = client.post("/v1/sessions", json={"egress": ["pypi"]}, headers=AUTH).json()["session_id"]
    assert client.get(f"/v1/sessions/{sid}/egress", headers=AUTH).json()["events"][0]["host"] == "pypi.org"
    assert client.get(f"/v1/sessions/{sid}/egress", headers=OTHER_TENANT).status_code == 403


@pytest.mark.parametrize("raw", ['["pypi"]', '{"a": "pypi"}', '{"a": [1]}', "not json"])
def test_load_egress_policy_rejects_bad_config(raw):
    with pytest.raises((RuntimeError, ValueError)):
        server.load_egress_policy(raw, None)


def test_load_egress_policy_empty_means_no_internet():
    assert server.load_egress_policy(None, None) == {}
    assert server.load_egress_policy("  ", None) == {}


# ---------------------------------------------------------------- disk quota and capacity


def test_create_reports_disk_quota(client):
    assert client.post("/v1/sessions", json={}, headers=AUTH).json()["disk_quota_mb"] == 512


def test_write_past_quota_returns_413(client):
    sid = create(client)
    ws = server.active_sessions[sid].workspace._ws
    ws.quota_bytes = 2**20
    res = client.post(f"/v1/sessions/{sid}/write", json={"path": "big.txt", "content": "x" * 2_000_000}, headers=AUTH)
    assert res.status_code == 413 and "quota" in res.json()["detail"]


def test_no_free_workspace_returns_503(client):
    FakeWorkspace.capacity_left = 1
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 201
    res = client.post("/v1/sessions", json={}, headers=AUTH)
    assert res.status_code == 503 and "in use" in res.json()["detail"]


# ---------------------------------------------------------------- per-tenant limits


def set_limits(monkeypatch, config):
    tracker = LimitTracker(load_tenant_limits(config))
    monkeypatch.setattr(server, "LIMITS", tracker)
    return tracker


def test_session_limit_returns_429_and_delete_frees_a_slot(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"max_sessions": 2}}')
    first = create(client)
    create(client)
    res = client.post("/v1/sessions", json={}, headers=AUTH)
    assert res.status_code == 429 and "2 of 2" in res.json()["detail"]
    # Another tenant has its own allowance.
    assert client.post("/v1/sessions", json={}, headers=OTHER_TENANT).status_code == 201
    client.delete(f"/v1/sessions/{first}", headers=AUTH)
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 201


def test_reaped_sessions_free_their_slot(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"max_sessions": 1}}')
    sid = create(client)
    server.active_sessions[sid].last_accessed_at -= server.SESSION_TTL_SECONDS + 1
    server.reap_expired_sessions()
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 201


def test_failed_session_start_does_not_use_up_the_limit(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"max_sessions": 1}}')
    FakeWorkspace.capacity_left = 0
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 503
    FakeWorkspace.capacity_left = None
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 201


def test_refused_egress_does_not_use_up_the_limit(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"max_sessions": 1}}')
    assert client.post("/v1/sessions", json={"egress": ["evil.com"]}, headers=AUTH).status_code == 403
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 201


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"requests_per_minute": 3}}')
    for _ in range(3):
        assert client.get("/v1/egress/policy", headers=AUTH).status_code == 200
    res = client.get("/v1/egress/policy", headers=AUTH)
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    assert client.get("/v1/egress/policy", headers=OTHER_TENANT).status_code == 200


def test_bad_key_is_rejected_before_rate_limiting(client, monkeypatch):
    tracker = set_limits(monkeypatch, '{"*": {"requests_per_minute": 1}}')
    for _ in range(3):
        assert client.get("/v1/egress/policy", headers={"X-API-Key": "wrong"}).status_code == 401
    assert tracker.usage("acme")["requests_available"] == 1   # untouched


def test_concurrent_exec_limit_returns_429(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"max_concurrent_exec": 1}}')
    sid = create(client)
    FakeWorkspace.exec_delay = 1.5
    slow = threading.Thread(target=client.post, args=(f"/v1/sessions/{sid}/exec",),
                            kwargs={"json": {"command": "sleep"}, "headers": AUTH})
    slow.start()
    time.sleep(0.3)
    res = client.post(f"/v1/sessions/{sid}/exec", json={"command": "second"}, headers=AUTH)
    slow.join()
    assert res.status_code == 429 and "1 allowed" in res.json()["detail"]
    FakeWorkspace.exec_delay = 0
    assert client.post(f"/v1/sessions/{sid}/exec", json={"command": "after"}, headers=AUTH).status_code == 200


def test_usage_endpoint(client, monkeypatch):
    set_limits(monkeypatch, '{"*": {"max_sessions": 5}, "acme": {"max_sessions": 7}}')
    create(client)
    body = client.get("/v1/usage", headers=AUTH).json()
    assert body["tenant_id"] == "acme"
    assert body["limits"]["max_sessions"] == 7
    assert body["sessions_open"] == 1
    assert client.get("/v1/usage", headers=OTHER_TENANT).json()["limits"]["max_sessions"] == 5


def test_exec_returns_structured_fields_and_text(client):
    sid = create(client)
    body = client.post(f"/v1/sessions/{sid}/exec", json={"command": "echo hi"}, headers=AUTH).json()
    assert body["stdout"] == "ran echo hi\n" and body["exit_code"] == 0
    assert body["timed_out"] is False and body["oom_killed"] is False and body["warnings"] == []
    assert body["output"] == "[STDOUT]:\nran echo hi\n[EXIT CODE]: 0"


# ---------------------------------------------------------------- self-service API keys

ADMIN = "a" * 40


@pytest.fixture
def keys(client, monkeypatch, tmp_path):
    """Enables the key store (SQLite) and the admin key for one test."""
    from src.api.keystore import KeyStore, SqliteBackend

    store = KeyStore(SqliteBackend(str(tmp_path / "keys.db")))
    monkeypatch.setattr(server, "KEYSTORE", store)
    monkeypatch.setattr(server, "ADMIN_KEY", ADMIN)
    return store


def admin_issue(client, tenant="acme", name="laptop"):
    res = client.post("/v1/admin/keys", json={"tenant": tenant, "name": name}, headers={"X-API-Key": ADMIN})
    assert res.status_code == 201, res.text
    return res.json()


def test_admin_issues_a_key_that_works_immediately(client, keys):
    issued = admin_issue(client, tenant="newco")
    assert issued["api_key"].startswith("asb_") and "shown only once" in issued["note"]
    res = client.post("/v1/sessions", json={}, headers={"X-API-Key": issued["api_key"]})
    assert res.status_code == 201 and res.json()["tenant_id"] == "newco"


def test_revoked_key_stops_working_immediately(client, keys):
    issued = admin_issue(client)
    auth = {"X-API-Key": issued["api_key"]}
    assert client.get("/v1/usage", headers=auth).status_code == 200
    assert client.delete(f"/v1/admin/keys/{issued['key_id']}", headers={"X-API-Key": ADMIN}).status_code == 200
    assert client.get("/v1/usage", headers=auth).status_code == 401


def test_listing_never_reveals_secrets(client, keys):
    issued = admin_issue(client)
    for res in (client.get("/v1/admin/keys", headers={"X-API-Key": ADMIN}),
                client.get("/v1/keys", headers={"X-API-Key": issued["api_key"]})):
        body = res.text
        assert issued["api_key"] not in body and "key_hash" not in body and issued["key_id"] in body


def test_tenant_rotates_its_own_key(client, keys):
    old = admin_issue(client)
    new = client.post("/v1/keys", json={"name": "rotated"}, headers={"X-API-Key": old["api_key"]}).json()
    assert new["tenant_id"] == "acme"
    revoked = client.delete(f"/v1/keys/{old['key_id']}", headers={"X-API-Key": new["api_key"]})
    assert revoked.status_code == 200 and revoked.json()["active"] is False
    assert client.get("/v1/usage", headers={"X-API-Key": old["api_key"]}).status_code == 401
    assert client.get("/v1/usage", headers={"X-API-Key": new["api_key"]}).status_code == 200


def test_tenant_cannot_revoke_its_last_key_or_another_tenants(client, keys):
    acme = admin_issue(client, tenant="acme")
    other = admin_issue(client, tenant="globex")
    auth = {"X-API-Key": acme["api_key"]}
    assert client.delete(f"/v1/keys/{acme['key_id']}", headers=auth).status_code == 409
    assert client.delete(f"/v1/keys/{other['key_id']}", headers=auth).status_code == 404
    assert [k["tenant_id"] for k in client.get("/v1/keys", headers=auth).json()["keys"]] == ["acme"]


def test_static_keys_keep_working_next_to_the_store(client, keys):
    assert client.post("/v1/sessions", json={}, headers=AUTH).status_code == 201


def test_admin_key_cannot_run_sandboxes_and_tenant_keys_cannot_administer(client, keys):
    assert client.post("/v1/sessions", json={}, headers={"X-API-Key": ADMIN}).status_code == 401
    assert client.get("/v1/admin/keys", headers=AUTH).status_code == 401
    assert client.post("/v1/admin/keys", json={"tenant": "x"}, headers=AUTH).status_code == 401


def test_admin_validation(client, keys):
    assert client.post("/v1/admin/keys", json={"tenant": "Bad Name"}, headers={"X-API-Key": ADMIN}).status_code == 400
    assert client.delete("/v1/admin/keys/nosuchkey0", headers={"X-API-Key": ADMIN}).status_code == 404


def test_features_report_when_disabled(client, monkeypatch):
    monkeypatch.setattr(server, "KEYSTORE", None)
    monkeypatch.setattr(server, "ADMIN_KEY", None)
    assert client.get("/v1/keys", headers=AUTH).status_code == 501
    assert client.get("/v1/admin/keys", headers={"X-API-Key": ADMIN}).status_code == 404


def test_short_admin_key_is_refused_at_startup(monkeypatch):
    import importlib
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", "too-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        importlib.reload(server)
    monkeypatch.delenv("SANDBOX_ADMIN_KEY")
    importlib.reload(server)


# ---------------------------------------------------------------- console support: metering, default egress, mounting


def test_commands_and_sessions_are_metered(client, monkeypatch, tmp_path):
    from src.api.accounts import AccountStore, _SqliteItems

    store = AccountStore(_SqliteItems(str(tmp_path / "accounts.db")))
    monkeypatch.setattr(server, "ACCOUNTS", store)
    sid = create(client)
    for _ in range(3):
        client.post(f"/v1/sessions/{sid}/exec", json={"command": "x"}, headers=AUTH)
    usage = store.usage("acme")
    assert usage["sessions"] == 1 and usage["commands"] == 3 and usage["command_seconds"] >= 0


def test_metering_failure_never_fails_the_request(client, monkeypatch):
    class Broken:
        def record_usage(self, *a, **k):
            raise RuntimeError("table unavailable")
    monkeypatch.setattr(server, "ACCOUNTS", Broken())
    sid = create(client)
    assert client.post(f"/v1/sessions/{sid}/exec", json={"command": "x"}, headers=AUTH).status_code == 200


def test_star_egress_policy_is_the_default_for_unlisted_tenants(client, monkeypatch):
    monkeypatch.setattr(server, "EGRESS_POLICY", {"*": ["pypi.org", "files.pythonhosted.org"], "acme": ["api.openai.com"]})
    # globex isn't listed -> gets the "*" default
    assert client.post("/v1/sessions", json={"egress": ["pypi"]}, headers=OTHER_TENANT).status_code == 201
    # acme has its own policy, which replaces the default rather than adding to it
    assert client.post("/v1/sessions", json={"egress": ["pypi"]}, headers=AUTH).status_code == 403
    assert client.get("/v1/egress/policy", headers=OTHER_TENANT).json()["allowed"] == ["pypi.org", "files.pythonhosted.org"]


def test_console_is_mounted_when_configured(monkeypatch, tmp_path):
    import importlib

    env = {
        "SANDBOX_CONSOLE_BASE_URL": "https://console.test", "SANDBOX_GITHUB_CLIENT_ID": "id",
        "SANDBOX_GITHUB_CLIENT_SECRET": "secret", "SANDBOX_CONSOLE_SECRET": "c" * 40,
        "SANDBOX_KEYSTORE": f"sqlite:{tmp_path / 'k.db'}", "SANDBOX_ACCOUNTS": f"sqlite:{tmp_path / 'a.db'}",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        importlib.reload(server)
        c = TestClient(server.app, base_url="https://console.test", follow_redirects=False)
        assert c.get("/").headers["location"] == "/console"
        assert "Sign in with GitHub" in c.get("/console").text
        assert c.get("/console/static/console.css").status_code == 200
    finally:
        for k in env:
            monkeypatch.delenv(k)
        importlib.reload(server)


def test_console_needs_both_stores(monkeypatch):
    import importlib

    for k, v in {"SANDBOX_CONSOLE_BASE_URL": "https://x", "SANDBOX_GITHUB_CLIENT_ID": "id",
                 "SANDBOX_GITHUB_CLIENT_SECRET": "s", "SANDBOX_CONSOLE_SECRET": "c" * 40}.items():
        monkeypatch.setenv(k, v)
    try:
        with pytest.raises(RuntimeError, match="SANDBOX_KEYSTORE and SANDBOX_ACCOUNTS"):
            importlib.reload(server)
    finally:
        for k in ("SANDBOX_CONSOLE_BASE_URL", "SANDBOX_GITHUB_CLIENT_ID", "SANDBOX_GITHUB_CLIENT_SECRET", "SANDBOX_CONSOLE_SECRET"):
            monkeypatch.delenv(k)
        importlib.reload(server)


# ------------------------------------------------------------------ session listing and repo import

def test_list_sessions_is_tenant_scoped(client):
    mine = create(client)
    client.post("/v1/sessions", json={}, headers=OTHER_TENANT)
    res = client.get("/v1/sessions", headers=AUTH).json()
    assert [s["session_id"] for s in res["sessions"]] == [mine] and res["tenant_id"] == "acme"
    assert res["sessions"][0]["expires_at"] > res["sessions"][0]["created_at"]


def test_import_repo_endpoint(client, monkeypatch, tmp_path):
    from src.api.repos import RepoImportError

    seen = {}
    monkeypatch.setattr(server, "fetch_archive", lambda repo: seen.setdefault("repo", repo) and b"tgz")

    def fake_import(workspace, repo, archive, dest):
        seen.update(archive=archive, dest=dest)
        return {"repo": repo.full_name, "ref": repo.ref, "path": f"/workspace/{dest}", "dest": dest,
                "files": 7, "archive_bytes": len(archive)}

    monkeypatch.setattr(server, "import_archive", fake_import)
    sid = create(client)
    res = client.post(f"/v1/sessions/{sid}/import", json={"repo": "https://github.com/psf/requests", "ref": "main"},
                      headers=AUTH)
    assert res.status_code == 200 and res.json()["files"] == 7 and res.json()["path"] == "/workspace/requests"
    assert seen["repo"].ref == "main" and seen["archive"] == b"tgz"

    assert client.post(f"/v1/sessions/{sid}/import", json={"repo": "not a repo"}, headers=AUTH).status_code == 400
    assert client.post(f"/v1/sessions/{sid}/import", json={"repo": "a/b"}, headers=OTHER_TENANT).status_code == 403

    def missing(repo):
        raise RepoImportError("not found", 404)

    monkeypatch.setattr(server, "fetch_archive", missing)
    assert client.post(f"/v1/sessions/{sid}/import", json={"repo": "a/b"}, headers=AUTH).status_code == 404


def test_imports_are_metered_as_commands(client, monkeypatch, tmp_path):
    from src.api.accounts import AccountStore, _SqliteItems

    store = AccountStore(_SqliteItems(str(tmp_path / "a.db")))
    monkeypatch.setattr(server, "ACCOUNTS", store)
    monkeypatch.setattr(server, "fetch_archive", lambda repo: b"tgz")
    monkeypatch.setattr(server, "import_archive", lambda ws, repo, archive, dest: {"files": 1})
    sid = create(client)
    assert client.post(f"/v1/sessions/{sid}/import", json={"repo": "a/b"}, headers=AUTH).status_code == 200
    assert store.usage("acme")["commands"] == 1


# ------------------------------------------------------------------ command audit log

@pytest.fixture
def audit(monkeypatch, tmp_path):
    from src.api.audit import AuditLog, _SqliteAudit

    log = AuditLog(_SqliteAudit(str(tmp_path / "audit.db")))
    monkeypatch.setattr(server, "AUDIT", log)
    return log


def test_commands_are_audited_with_the_key_that_ran_them(client, audit):
    sid = create(client)
    client.post(f"/v1/sessions/{sid}/exec", json={"command": "echo hi"}, headers=AUTH)
    entries = client.get("/v1/audit", headers=AUTH).json()["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert e["command"] == "echo hi" and e["session_id"] == sid and e["exit_code"] == 0
    assert e["actor"] == "static key" and e["kind"] == "exec" and "sk" not in e and "expires_at" not in e


def test_self_service_key_id_is_recorded_never_the_secret(client, audit):
    assert server.actor_for_key("asb_k3y1d_s3cretpart") == "key k3y1d"
    assert server.actor_for_key("plain-static-key") == "static key"
    assert server.actor_for_key(None) == "static key"


def test_audit_is_per_tenant(client, audit):
    sid = create(client)
    client.post(f"/v1/sessions/{sid}/exec", json={"command": "acme only"}, headers=AUTH)
    assert client.get("/v1/audit", headers=OTHER_TENANT).json()["entries"] == []


def test_imports_are_audited_including_failures(client, audit, monkeypatch):
    from src.api.repos import RepoImportError

    monkeypatch.setattr(server, "fetch_archive", lambda repo: b"tgz")
    monkeypatch.setattr(server, "import_archive", lambda ws, repo, archive, dest: {"files": 3, "path": f"/workspace/{dest}"})
    sid = create(client)
    client.post(f"/v1/sessions/{sid}/import", json={"repo": "psf/requests", "ref": "main"}, headers=AUTH)

    def missing(repo):
        raise RepoImportError("not found", 404)

    monkeypatch.setattr(server, "fetch_archive", missing)
    client.post(f"/v1/sessions/{sid}/import", json={"repo": "a/private"}, headers=AUTH)
    failed, ok = client.get("/v1/audit", headers=AUTH).json()["entries"]
    assert ok["command"] == "import psf/requests @main" and ok["exit_code"] == 0 and "3 files" in ok["detail"]
    assert failed["command"] == "import a/private" and failed["exit_code"] is None and "404" in failed["detail"]


def test_audit_failure_never_fails_the_command(client, monkeypatch):
    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("dynamodb down")

    monkeypatch.setattr(server, "AUDIT", Broken())
    sid = create(client)
    assert client.post(f"/v1/sessions/{sid}/exec", json={"command": "ls"}, headers=AUTH).status_code == 200


def test_audit_endpoint_reports_when_disabled(client, monkeypatch):
    monkeypatch.setattr(server, "AUDIT", None)
    assert client.get("/v1/audit", headers=AUTH).status_code == 501


def test_closing_all_of_a_tenants_sessions(client):
    a, b = create(client), create(client)
    other = client.post("/v1/sessions", json={}, headers=OTHER_TENANT).json()["session_id"]
    assert server.close_tenant_sessions("acme") == 2
    assert other in server.active_sessions and a not in server.active_sessions and b not in server.active_sessions
