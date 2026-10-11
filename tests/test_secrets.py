"""Workspace secrets: set, list (names only), mask in output, delete, validation, restarts."""
import pytest

import src.api.server as server
from src.api.limits import LimitTracker, load_tenant_limits
from tests.test_api_server import AUTH, OTHER_TENANT, client, create  # noqa: F401

KEY = "sk-test-0123456789abcdef"


def run(client, sid, command):
    return client.post(f"/v1/sessions/{sid}/exec", json={"command": command}, headers=AUTH).json()


def test_secrets_reach_commands_and_are_masked(client):
    sid = create(client)
    res = client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {"OPENAI_API_KEY": KEY}}, headers=AUTH)
    assert res.status_code == 200 and res.json()["names"] == ["OPENAI_API_KEY"]
    out = run(client, sid, "echo $OPENAI_API_KEY")
    assert KEY not in out["stdout"] and "[secret OPENAI_API_KEY]" in out["stdout"]
    assert KEY not in out["output"]


def test_values_are_never_returned(client):
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {"A_KEY": KEY, "B_KEY": "other-value-123"}}, headers=AUTH)
    listing = client.get(f"/v1/sessions/{sid}/secrets", headers=AUTH)
    assert listing.json()["names"] == ["A_KEY", "B_KEY"] and KEY not in listing.text
    assert KEY not in client.get("/v1/sessions", headers=AUTH).text
    assert client.get(f"/v1/sessions/{sid}/secrets", headers=OTHER_TENANT).status_code == 403


def test_create_session_with_secrets(client):
    res = client.post("/v1/sessions", json={"secrets": {"ANTHROPIC_API_KEY": KEY}}, headers=AUTH)
    assert res.status_code == 201 and KEY not in res.text
    sid = res.json()["session_id"]
    assert client.get(f"/v1/sessions/{sid}/secrets", headers=AUTH).json()["names"] == ["ANTHROPIC_API_KEY"]
    bad = client.post("/v1/sessions", json={"secrets": {"PATH": "/evil"}}, headers=AUTH)
    assert bad.status_code == 422 and not server.active_sessions.get(bad.json().get("session_id", ""))


def test_delete(client):
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {"TOKEN": KEY}}, headers=AUTH)
    assert client.delete(f"/v1/sessions/{sid}/secrets/TOKEN", headers=AUTH).json()["names"] == []
    assert client.delete(f"/v1/sessions/{sid}/secrets/TOKEN", headers=AUTH).status_code == 404
    assert "$TOKEN" in run(client, sid, "echo $TOKEN")["stdout"]          # no longer set


@pytest.mark.parametrize("name", ["lower", "1ABC", "A-B", "PATH", "HTTPS_PROXY", "AIRLOCK_CMD", "PIP_INDEX_URL",
                                  "LD_PRELOAD", "X" * 65])
def test_name_validation(client, name):
    sid = create(client)
    res = client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {name: "v"}}, headers=AUTH)
    assert res.status_code == 422


def test_value_and_count_limits(client):
    sid = create(client)
    assert client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {"BIG": "x" * 9000}},
                      headers=AUTH).status_code == 422
    many = {f"K{i}": "v" for i in range(21)}
    assert client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": many}, headers=AUTH).status_code == 422


def test_audit_has_names_not_values(client, monkeypatch):
    entries = []
    monkeypatch.setattr(server, "record_audit", lambda tenant, **e: entries.append(e))
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {"GROQ_API_KEY": KEY}}, headers=AUTH)
    assert entries[-1]["kind"] == "secret" and "GROQ_API_KEY" in entries[-1]["command"]
    assert all(KEY not in str(e) for e in entries)


def test_secrets_survive_a_restart(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SESSION_STATE_PATH", str(tmp_path / "sessions.json"))
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/secrets", json={"secrets": {"TOKEN": KEY}}, headers=AUTH)
    server.persist_sessions()
    server.active_sessions.clear()
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits('{"*": {"requests_per_minute": 1000}}')))
    assert server.restore_sessions() == 1
    assert server.active_sessions[sid].workspace.secrets == {"TOKEN": KEY}


def test_lesson_link_is_kept_and_survives_a_restart(client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SESSION_STATE_PATH", str(tmp_path / "sessions.json"))
    sid = create(client)
    server.ConsoleSessions().set_lesson("acme", sid, "02-first-agent")
    assert server.ConsoleSessions().get("acme", sid)["lesson"] == "02-first-agent"
    server.persist_sessions()
    server.active_sessions.clear()
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits('{"*": {"requests_per_minute": 1000}}')))
    server.restore_sessions()
    assert server.active_sessions[sid].lesson == "02-first-agent"
