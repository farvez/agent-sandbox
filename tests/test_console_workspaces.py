"""Console workspaces: start/close sessions, repo import, and the browser terminal."""
import html
import shutil
import subprocess
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.accounts import AccountStore, _SqliteItems
from src.api.keystore import KeyStore, SqliteBackend
from src.api.limits import LimitExceeded
from src.console.auth import GitHubOAuth
from src.console.routes import ConsoleConfig, build_console_router
from src.console.terminal import CWD_MARKER, HOME, clean_cwd, split_cwd, wrap_command
from tests.test_console import csrf_of, fake_github, sign_in

EGRESS = ["pypi.org", "files.pythonhosted.org"]


class FakeSessions:
    """Stands in for server.ConsoleSessions; records what the console asked for."""

    ttl_seconds = 1800

    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.fail = None          # exception to raise from the next operation
        self.stdout = "hello\n"

    def _check(self):
        if self.fail:
            error, self.fail = self.fail, None
            raise error

    def _owned(self, tenant, sid):
        rec = self.sessions.get(sid)
        if rec is None:
            raise HTTPException(404, "Session not found or expired")
        if rec["tenant_id"] != tenant:
            raise HTTPException(403, "Forbidden: Access denied to this session")
        return rec

    def list(self, tenant):
        return [s for s in self.sessions.values() if s["tenant_id"] == tenant]

    def get(self, tenant, sid):
        return self._owned(tenant, sid)

    def create(self, tenant, egress):
        self._check()
        sid = f"sbx_{len(self.sessions) + 1:012d}"
        self.sessions[sid] = {"session_id": sid, "tenant_id": tenant, "egress": egress, "created_at": 1.0,
                              "last_accessed_at": 1.0, "expires_at": 1801.0, "disk_quota_mb": 512}
        self.calls.append(("create", tenant, egress))
        return self.sessions[sid]

    def run(self, tenant, sid, command, timeout, actor=None, audit_command=None):
        self._owned(tenant, sid)
        self._check()
        self.calls.append(("run", sid, command, timeout))
        self.last_actor, self.last_audit_command = actor, audit_command
        return {"stdout": f"{self.stdout}\n{CWD_MARKER}/workspace/repo\n", "stderr": "", "exit_code": 0,
                "timed_out": False, "oom_killed": False, "warnings": []}

    def import_repo(self, tenant, sid, repo, ref, path, actor=None):
        self._owned(tenant, sid)
        self._check()
        self.calls.append(("import", sid, repo, ref, path))
        return {"repo": repo, "ref": ref or "HEAD", "path": f"/workspace/{path or repo.split('/')[-1]}",
                "dest": path or repo.split("/")[-1], "files": 3, "archive_bytes": 100}

    def close_all(self, tenant):
        mine = [sid for sid, s in self.sessions.items() if s["tenant_id"] == tenant]
        for sid in mine:
            del self.sessions[sid]
        return len(mine)

    def destroy(self, tenant, sid):
        self._owned(tenant, sid)
        del self.sessions[sid]
        self.calls.append(("destroy", sid))


@pytest.fixture
def console(tmp_path):
    config = ConsoleConfig("https://console.test", "client-id", "client-secret", "s" * 40, {"farvez"}, "open")
    sessions = FakeSessions()
    app = FastAPI()
    app.include_router(build_console_router(
        config,
        keystore=lambda: KeyStore(SqliteBackend(str(tmp_path / "keys.db"))),
        accounts=lambda: AccountStore(_SqliteItems(str(tmp_path / "accounts.db"))),
        limits=lambda t: {"sessions_open": 0, "limits": {"max_sessions": 10, "requests_per_minute": 120, "max_concurrent_exec": 4}},
        egress=lambda t: EGRESS,
        github=GitHubOAuth("client-id", "client-secret", "https://console.test/console/auth/callback", http=fake_github),
        sessions=sessions,
    ))
    client = TestClient(app, base_url="https://console.test", follow_redirects=False)
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/workspaces").text)
    return client, sessions, csrf


def start(client, csrf, **form):
    res = client.post("/console/workspaces", data={"csrf": csrf, **form})
    assert res.status_code == 303, res.text
    return res.headers["location"].split("?")[0].rsplit("/", 1)[-1], res


def test_workspaces_page_and_nav_link(console):
    client, _, _ = console
    page = client.get("/console/workspaces")
    assert page.status_code == 200 and "New workspace" in page.text and "No open workspaces" in page.text
    assert 'href="/console/workspaces"' in client.get("/console").text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_workspaces_need_sign_in(tmp_path, console):
    client, _, _ = console
    client.cookies.clear()
    assert client.get("/console/workspaces").headers["location"] == "/console"
    res = client.post("/console/workspaces/sbx_x/exec", json={"command": "ls"})
    assert res.status_code == 401


def test_start_without_and_with_internet(console):
    client, sessions, csrf = console
    start(client, csrf)
    start(client, csrf, internet="1")
    assert [c[2] for c in sessions.calls if c[0] == "create"] == [[], EGRESS]
    assert all(c[1] == "gh-farvez" for c in sessions.calls if c[0] == "create")


def test_start_requires_csrf(console):
    client, sessions, _ = console
    assert client.post("/console/workspaces", data={"csrf": "wrong"}).status_code == 403
    assert sessions.sessions == {}


def test_start_with_repo_imports_and_flashes_the_result(console):
    client, sessions, csrf = console
    sid, res = start(client, csrf, repo="psf/requests", ref="main")
    assert ("import", sid, "psf/requests", "main", None) in sessions.calls
    assert res.headers["location"].endswith("?cwd=/workspace/requests")   # terminal starts in the repo
    page = client.get(res.headers["location"])
    assert "Imported psf/requests (3 files)" in page.text and 'data-cwd="/workspace/requests"' in page.text
    assert "Imported psf/requests" not in client.get(res.headers["location"]).text   # shown once


def test_failed_import_still_opens_the_workspace(console):
    client, sessions, csrf = console
    original = sessions.import_repo

    def broken(*a, **kw):
        raise HTTPException(404, "o/private@HEAD wasn't found. Only public repositories can be imported for now.")

    sessions.import_repo = broken
    sid, res = start(client, csrf, repo="o/private")
    sessions.import_repo = original
    page = html.unescape(client.get(res.headers["location"]).text)
    assert "the import failed: o/private@HEAD wasn't found" in page and sid in page


def test_session_limit_is_shown_on_the_form(console):
    client, sessions, csrf = console
    sessions.fail = LimitExceeded("Session limit reached: 10 open sessions")
    res = client.post("/console/workspaces", data={"csrf": csrf})
    assert res.status_code == 429 and "Session limit reached" in res.text


def test_terminal_page_renders_session_details(console):
    client, _, csrf = console
    sid, _ = start(client, csrf, internet="1")
    page = client.get(f"/console/workspaces/{sid}")
    assert page.status_code == 200 and f'data-session="{sid}"' in page.text and "pypi.org" in page.text
    assert f'data-csrf="{csrf}"' in page.text


def test_unknown_or_other_tenants_workspace_is_not_shown(console):
    client, sessions, _ = console
    sessions.sessions["sbx_theirs"] = {"session_id": "sbx_theirs", "tenant_id": "gh-someone"}
    assert client.get("/console/workspaces/sbx_theirs").status_code == 403
    assert client.get("/console/workspaces/sbx_nope").status_code == 404


def test_exec_wraps_the_command_and_returns_the_new_directory(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    res = client.post(f"/console/workspaces/{sid}/exec", headers={"X-CSRF-Token": csrf},
                      json={"command": "cd repo && ls", "cwd": "/workspace", "timeout": 999})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["stdout"] == "hello\n" and body["cwd"] == "/workspace/repo" and body["exit_code"] == 0
    _, _, command, timeout = [c for c in sessions.calls if c[0] == "run"][0]
    assert timeout == 60 and "cd repo && ls" in command and CWD_MARKER in command


def test_exec_requires_the_csrf_header(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    for headers in ({}, {"X-CSRF-Token": "forged"}):
        res = client.post(f"/console/workspaces/{sid}/exec", headers=headers, json={"command": "ls"})
        assert res.status_code == 401
    assert not [c for c in sessions.calls if c[0] == "run"]


@pytest.mark.parametrize("body, status", [({"command": " "}, 400), ({"command": "x" * 10_001}, 400), ({"command": "ls", "timeout": "abc"}, 400)])
def test_exec_validates_input(console, body, status):
    client, _, csrf = console
    sid, _ = start(client, csrf)
    assert client.post(f"/console/workspaces/{sid}/exec", headers={"X-CSRF-Token": csrf}, json=body).status_code == status


def test_exec_errors_come_back_as_json(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    sessions.fail = LimitExceeded("Too many commands running")
    res = client.post(f"/console/workspaces/{sid}/exec", headers={"X-CSRF-Token": csrf}, json={"command": "ls"})
    assert res.status_code == 429 and res.json() == {"error": "Too many commands running"}
    res = client.post("/console/workspaces/sbx_gone/exec", headers={"X-CSRF-Token": csrf}, json={"command": "ls"})
    assert res.status_code == 404


def test_import_endpoint(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    res = client.post(f"/console/workspaces/{sid}/import", headers={"X-CSRF-Token": csrf},
                      json={"repo": "psf/requests", "ref": "", "path": "lib"})
    assert res.status_code == 200 and res.json()["path"] == "/workspace/lib"
    assert ("import", sid, "psf/requests", None, "lib") in sessions.calls
    assert client.post(f"/console/workspaces/{sid}/import", json={"repo": "a/b"}).status_code == 401


def test_close_workspace(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    res = client.post(f"/console/workspaces/{sid}/delete", data={"csrf": csrf})
    assert res.status_code == 303 and sid not in sessions.sessions
    assert f"Closed {sid}" in client.get("/console/workspaces").text


def test_workspace_pages_are_hidden_without_a_session_service(tmp_path):
    from tests.test_console import make_app

    app, *_ = make_app(tmp_path)
    client = TestClient(app, base_url="https://console.test", follow_redirects=False)
    sign_in(client, "code-admin")
    assert client.get("/console/workspaces").status_code == 404
    assert 'href="/console/workspaces"' not in client.get("/console").text


# ------------------------------------------------------------------ terminal helpers

def test_split_cwd_restores_output_exactly():
    for out in ("", "a", "a\n", "a\n\n"):
        stdout, cwd = split_cwd(f"{out}\n{CWD_MARKER}/workspace/x\n", "/workspace")
        assert stdout == out and cwd == "/workspace/x"


def test_split_cwd_keeps_directory_when_the_command_exited_early():
    assert split_cwd("bye\n", "/workspace/a") == ("bye\n", "/workspace/a")


@pytest.mark.parametrize("cwd", ["", "relative", "/a\nb", "/" + "a" * 2000])
def test_bad_cwd_falls_back_home(cwd):
    assert clean_cwd(cwd) == HOME


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")
def test_wrapped_commands_behave_like_a_shell(tmp_path):
    def run(command, cwd):
        script = wrap_command(command, cwd).replace("/workspace", str(tmp_path))
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        out, new_cwd = split_cwd(p.stdout, cwd)
        return out, new_cwd, p.returncode

    (tmp_path / "sub").mkdir()
    out, cwd, code = run("cd sub && pwd", str(tmp_path))
    assert out.strip() == str(tmp_path / "sub") and cwd == str(tmp_path / "sub") and code == 0
    assert run("false", cwd)[2] == 1
    assert run("echo 'it''s' \"$((1+2))\"", cwd)[0] == "its 3\n"
    assert run("cd /nonexistent-dir; true", str(tmp_path))[1] == str(tmp_path)   # failed cd keeps the directory
    assert run("exit 3", cwd)[2] == 3


def test_terminal_commands_are_audited_as_typed_by_the_signed_in_user(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    client.post(f"/console/workspaces/{sid}/exec", headers={"X-CSRF-Token": csrf}, json={"command": "ls -la"})
    assert sessions.last_actor == "console @Farvez" and sessions.last_audit_command == "ls -la"
