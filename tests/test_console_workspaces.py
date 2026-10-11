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

    def create(self, tenant, egress, persistent=False):
        self._check()
        sid = f"sbx_{len(self.sessions) + 1:012d}"
        self.sessions[sid] = {"session_id": sid, "tenant_id": tenant, "egress": egress, "created_at": 1.0,
                              "last_accessed_at": 1.0, "expires_at": 1801.0, "disk_quota_mb": 512,
                              "persistent": persistent, "previews": []}
        self.calls.append(("create", tenant, egress) + (("persistent",) if persistent else ()))
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

    # files and snapshots
    upload_max_bytes = 1000
    snapshots_enabled = True

    def upload(self, tenant, sid, path, data, actor=None):
        self._owned(tenant, sid)
        self._check()
        self.calls.append(("upload", sid, path, data))
        return {"path": path, "size_bytes": len(data)}

    def download(self, tenant, sid, path, actor=None):
        self._owned(tenant, sid)
        self._check()
        self.calls.append(("download", sid, path))
        return b"\x00file-bytes"

    def download_archive(self, tenant, sid, path, actor=None):
        self._owned(tenant, sid)
        self.calls.append(("archive", sid, path))
        return b"\x1f\x8barchive"

    def snapshot(self, tenant, sid, name, actor=None):
        self._owned(tenant, sid)
        self._check()
        snap = {"id": "snap_0123456789ab", "name": name or "snapshot", "size_bytes": 2**20, "session_id": sid,
                "created_at": 1.0, "expires_at": 86401.0, "tenant": tenant}
        self.saved = getattr(self, "saved", []) + [snap]
        return snap

    def snapshots(self, tenant):
        return [s for s in getattr(self, "saved", []) if s["tenant"] == tenant]

    def create_from_snapshot(self, tenant, egress, snapshot_id, actor=None, persistent=False):
        if not any(s["id"] == snapshot_id for s in self.snapshots(tenant)):
            raise HTTPException(404, f"Snapshot {snapshot_id} not found.")
        created = self.create(tenant, egress, persistent)
        self.calls.append(("restore", created["session_id"], snapshot_id))
        return created

    previews_enabled = True

    def set_lesson(self, tenant, sid, slug):
        self._owned(tenant, sid)["lesson"] = slug

    def secrets(self, tenant, sid):
        return sorted(self._owned(tenant, sid).setdefault("secret_values", {}))

    def set_secret(self, tenant, sid, name, value, actor=None):
        rec = self._owned(tenant, sid)
        if not name.isidentifier() or name in ("PATH",):
            raise HTTPException(422, f"{name} is used by the sandbox itself; pick another name.")
        rec.setdefault("secret_values", {})[name] = value
        return sorted(rec["secret_values"])

    def delete_secret(self, tenant, sid, name, actor=None):
        rec = self._owned(tenant, sid)
        if rec.setdefault("secret_values", {}).pop(name, None) is None:
            raise HTTPException(404, f"No secret named {name}.")
        return sorted(rec["secret_values"])

    def expose(self, tenant, sid, port, actor=None):
        rec = self._owned(tenant, sid)
        if not rec["persistent"]:
            raise HTTPException(409, "Previews needs a long-running session.")
        url = f"https://{port}-0123456789abcdef.preview.test"
        rec["previews"].append({"port": port, "url": url})
        return {"port": port, "url": url}

    def unexpose(self, tenant, sid, port, actor=None):
        rec = self._owned(tenant, sid)
        rec["previews"] = [p for p in rec["previews"] if p["port"] != port]

    async def terminal(self, websocket, tenant, sid, actor=None):
        rec = self.sessions.get(sid)
        if rec is None or rec["tenant_id"] != tenant or not rec["persistent"]:
            await websocket.close(1008)
            return
        await websocket.accept()
        self.terminal_actor = actor
        await websocket.send_bytes(b"$ ")
        data = await websocket.receive_bytes()
        await websocket.send_bytes(b"echo:" + data)
        await websocket.close(1000)

    def delete_snapshot(self, tenant, snapshot_id):
        mine = [s for s in self.snapshots(tenant) if s["id"] == snapshot_id]
        if not mine:
            raise HTTPException(404, f"Snapshot {snapshot_id} not found.")
        self.saved.remove(mine[0])
        return mine[0]


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


# ---------------------------------------------------------------- files and snapshots


def test_upload_needs_csrf_header_and_respects_the_limit(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    url = f"/console/workspaces/{sid}/upload?path=data/a.bin"
    assert client.post(url, content=b"\x00\x01").status_code == 401
    res = client.post(url, content=b"\x00\x01", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200 and res.json() == {"path": "data/a.bin", "size_bytes": 2}
    assert sessions.calls[-1] == ("upload", sid, "data/a.bin", b"\x00\x01")
    big = client.post(url, content=b"x" * 1001, headers={"X-CSRF-Token": csrf})
    assert big.status_code == 413
    sessions.fail = HTTPException(403, "Access denied: '../x' is outside the workspace.")
    assert client.post(url, content=b"x", headers={"X-CSRF-Token": csrf}).json()["error"].startswith("Access denied")


def test_download_file_and_folder(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    res = client.get(f"/console/workspaces/{sid}/download", params={"path": "out/report.csv"})
    assert res.status_code == 200 and res.content == b"\x00file-bytes"
    assert 'filename="report.csv"' in res.headers["content-disposition"]
    assert res.headers["cache-control"] == "no-store"
    res = client.get(f"/console/workspaces/{sid}/download", params={"path": ".", "folder": "1"})
    assert res.headers["content-type"] == "application/gzip"
    assert 'filename="workspace.tar.gz"' in res.headers["content-disposition"]
    sessions.fail = HTTPException(404, "'nope' doesn't exist.")
    res = client.get(f"/console/workspaces/{sid}/download", params={"path": "nope"})
    assert res.status_code == 404 and "doesn&#39;t exist" in res.text
    client.cookies.clear()
    assert client.get(f"/console/workspaces/{sid}/download", params={"path": "a"}).status_code == 303


def test_workspace_page_shows_files_and_snapshot_panels(console):
    client, _, csrf = console
    sid, _ = start(client, csrf)
    page = client.get(f"/console/workspaces/{sid}").text
    assert 'id="upload-form"' in page and "Save snapshot" in page


def test_snapshot_then_start_from_it_and_delete(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    assert client.post(f"/console/workspaces/{sid}/snapshot", data={"csrf": "wrong", "name": "x"}).status_code in (303, 403)
    res = client.post(f"/console/workspaces/{sid}/snapshot", data={"csrf": csrf, "name": "before"})
    assert res.status_code == 303
    page = client.get("/console/workspaces").text
    assert "snap_0123456789ab" in page and "before" in page

    res = client.post("/console/snapshots/snap_0123456789ab/start", data={"csrf": csrf, "internet": "1"})
    assert res.status_code == 303
    new_sid = res.headers["location"].rsplit("/", 1)[-1]
    assert ("restore", new_sid, "snap_0123456789ab") in sessions.calls
    assert sessions.sessions[new_sid]["egress"] == EGRESS

    res = client.post("/console/snapshots/snap_404404404404/start", data={"csrf": csrf})
    assert res.status_code == 404 and "not found" in res.text

    assert client.post("/console/snapshots/snap_0123456789ab/delete", data={"csrf": csrf}).status_code == 303
    assert "snap_0123456789ab" not in client.get("/console/workspaces").text



# ---------------------------------------------------------------- long-running workspaces


def test_long_running_checkbox_and_interactive_page(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf, persistent="1")
    assert sessions.calls[-1] == ("create", "u1", [], "persistent") or sessions.calls[-1][-1] == "persistent"
    page = client.get(f"/console/workspaces/{sid}")
    assert 'id="xterm"' in page.text and "vendor/xterm.js" in page.text and 'id="term-form"' not in page.text
    assert "'unsafe-inline'" in page.headers["content-security-policy"]
    assert "script-src 'self';" in page.headers["content-security-policy"]
    assert "Long-running" in client.get("/console/workspaces").text

    regular, _ = start(client, csrf)
    page = client.get(f"/console/workspaces/{regular}")
    assert 'id="term-form"' in page.text and 'id="xterm"' not in page.text
    assert "'unsafe-inline'" not in page.headers["content-security-policy"]


def test_terminal_websocket_checks_origin_and_csrf(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf, persistent="1")
    good = {"Origin": "https://console.test"}
    with client.websocket_connect(f"wss://console.test/console/workspaces/{sid}/terminal?csrf={csrf}", headers=good) as ws:
        assert ws.receive_bytes() == b"$ "
        ws.send_bytes(b"ls\r")
        assert ws.receive_bytes() == b"echo:ls\r"
    assert sessions.terminal_actor.startswith("console @")
    for url, headers in ((f"wss://console.test/console/workspaces/{sid}/terminal?csrf={csrf}", {"Origin": "https://evil.test"}),
                         (f"wss://console.test/console/workspaces/{sid}/terminal?csrf={csrf}", {}),
                         (f"wss://console.test/console/workspaces/{sid}/terminal?csrf=wrong", good)):
        with pytest.raises(Exception):
            with client.websocket_connect(url, headers=headers) as ws:
                ws.receive_bytes()
    client.cookies.clear()
    with pytest.raises(Exception):
        with client.websocket_connect(f"wss://console.test/console/workspaces/{sid}/terminal?csrf={csrf}", headers=good) as ws:
            ws.receive_bytes()


def test_previews_expose_and_close(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf, persistent="1")
    res = client.post(f"/console/workspaces/{sid}/previews", data={"csrf": csrf, "port": "8000"})
    assert res.status_code == 303
    page = client.get(f"/console/workspaces/{sid}").text
    assert "https://8000-0123456789abcdef.preview.test" in page
    assert client.post(f"/console/workspaces/{sid}/previews", data={"csrf": csrf, "port": "abc"}).status_code == 303
    client.post(f"/console/workspaces/{sid}/previews/8000/close", data={"csrf": csrf})
    assert "https://8000-0123456789abcdef.preview.test" not in client.get(f"/console/workspaces/{sid}").text
    regular, _ = start(client, csrf)
    res = client.post(f"/console/workspaces/{regular}/previews", data={"csrf": csrf, "port": "8000"})
    assert res.status_code == 303      # flashed error: needs a long-running session


def test_secrets_panel(console):
    client, sessions, csrf = console
    sid, _ = start(client, csrf)
    res = client.post(f"/console/workspaces/{sid}/secrets", data={"csrf": csrf, "name": "openai_api_key", "value": "sk-hidden-123"})
    assert res.status_code == 303
    page = client.get(f"/console/workspaces/{sid}").text
    assert "$OPENAI_API_KEY" in page and "sk-hidden-123" not in page        # upper-cased; value never shown
    assert sessions.sessions[sid]["secret_values"] == {"OPENAI_API_KEY": "sk-hidden-123"}
    client.post(f"/console/workspaces/{sid}/secrets", data={"csrf": csrf, "name": "PATH", "value": "x"})
    assert "used by the sandbox itself" in html.unescape(client.get(f"/console/workspaces/{sid}").text)
    client.post(f"/console/workspaces/{sid}/secrets/OPENAI_API_KEY/delete", data={"csrf": csrf})
    assert sessions.sessions[sid]["secret_values"] == {}
    assert client.post(f"/console/workspaces/{sid}/secrets", data={"csrf": "forged", "name": "A", "value": "b"}).status_code in (303, 403)
    assert sessions.sessions[sid]["secret_values"] == {}



# ---------------------------------------------------------------- Learn track


def test_learn_pages_list_lessons_and_flag_missing_access(console):
    client, _, _ = console
    page = client.get("/console/learn")
    assert page.status_code == 200 and 'href="/console/learn"' in page.text
    assert "What a sandbox is" in page.text and "Your first tool-calling agent" in page.text
    assert "Needs <code>llm</code> internet access" in page.text   # the test account only has PyPI
    lesson = client.get("/console/learn/02-first-agent").text
    assert "ANTHROPIC_API_KEY" in lesson and "python agent.py" in html.unescape(lesson)
    assert client.get("/console/learn/nope").status_code == 404


def test_start_lesson_copies_files_into_a_long_running_workspace(console):
    client, sessions, csrf = console
    res = client.post("/console/learn/03-coding-agent/start", data={"csrf": csrf})
    assert res.status_code == 303
    sid = res.headers["location"].rsplit("/", 1)[-1]
    assert sessions.sessions[sid]["persistent"] is True
    assert sessions.sessions[sid]["egress"] == EGRESS            # only what the account may use
    uploaded = {c[2] for c in sessions.calls if c[0] == "upload" and c[1] == sid}
    assert {"coder.py", "project/stats.py", "project/test_stats.py"} <= uploaded
    page = client.get(f"/console/workspaces/{sid}").text            # the workspace remembers its lesson
    assert "Lesson: A coding agent that fixes failing tests" in page and "First save your" in page
    assert "project/" in page and "Try this next" in page
    # Saving the secret comes back to the same lesson workspace, with the panel still there.
    res = client.post(f"/console/workspaces/{sid}/secrets", data={"csrf": csrf, "name": "ANTHROPIC_API_KEY", "value": "k"})
    assert res.headers["location"] == f"/console/workspaces/{sid}"
    page = client.get(res.headers["location"]).text
    assert "Lesson: A coding agent" in page and "First save your" not in page
    assert client.post("/console/learn/03-coding-agent/start", data={"csrf": "forged"}).status_code in (303, 403)


def test_lesson_pages_have_concepts_code_and_exercises(console):
    from src.console.lessons import LESSONS

    client, _, _ = console
    for lesson in LESSONS:
        page = html.unescape(client.get(f"/console/learn/{lesson.slug}").text)
        assert page.count('class="card concept"') >= 3, lesson.slug       # concepts
        assert 'id="walkthrough"' in page and 'class="snippet"' in page     # code walkthrough with real code
        assert "Files in this lesson" in page and "Try this next" in page
        main = lesson.file_notes[0][0]
        assert lesson.text(main).splitlines()[0] in page                   # the full file is on the page


def test_lesson_files_are_real():
    from src.console.lessons import LESSONS

    for lesson in LESSONS:
        files = lesson.files()
        assert files and not any("__pycache__" in path for path in files)
        for path, data in files.items():
            if path.endswith(".py"):
                compile(data, path, "exec")                       # every lesson script is valid Python
