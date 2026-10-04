"""The Python SDK against the real API server over real HTTP (containers faked)."""
import json
import socket
import threading
import time

import pytest
import uvicorn

import airlock_sandbox._http as sdk_http
import src.api.server as server
from airlock_sandbox import (
    AuthenticationError,
    CapacityError,
    CommandError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    Sandbox,
    SandboxError,
    ValidationError,
)
from airlock_sandbox.tools import anthropic_tools, handle_tool_call, openai_tools
from src.api.limits import LimitTracker, load_tenant_limits
from tests.conftest import TENANT_KEYS
from tests.test_api_server import FakeWorkspace

KEY = TENANT_KEYS["acme"]


class ScriptedWorkspace(FakeWorkspace):
    """Fake container execution with a few recognisable outcomes."""

    def execute(self, command, timeout_seconds=15):
        base = {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False, "oom_killed": False, "warnings": []}
        if command.startswith("fail"):
            return {**base, "stderr": "boom\n", "exit_code": 3}
        if command == "hang":
            return {**base, "exit_code": -1, "timed_out": True, "warnings": [f"Execution exceeded {timeout_seconds}s limit."]}
        if command == "eat-memory":
            return {**base, "exit_code": 137, "oom_killed": True, "warnings": ["Process killed by cgroups (Memory limit exceeded)."]}
        if command == "big-output":
            return {**base, "stdout": "x" * 50_000 + "\nEND"}
        return {**base, "stdout": f"ran {command}\n"}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def api_url():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "SandboxedWorkspace", ScriptedWorkspace)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not srv.started and time.time() < deadline:
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
        srv.should_exit = True
        thread.join(5)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    server.active_sessions.clear()
    FakeWorkspace.capacity_left = None
    FakeWorkspace.exec_delay = 0
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits(
        '{"*": {"max_sessions": 50, "requests_per_minute": 10000, "max_concurrent_exec": 50}}')))
    for var in ("SANDBOX_API_URL", "SANDBOX_API_KEY", "SANDBOX_API_INSECURE"):
        monkeypatch.delenv(var, raising=False)
    yield
    server.active_sessions.clear()


@pytest.fixture
def sbx(api_url):
    with Sandbox(api_key=KEY, base_url=api_url) as s:
        yield s


# ---------------------------------------------------------------- lifecycle


def test_context_manager_creates_and_deletes_session(api_url):
    with Sandbox(api_key=KEY, base_url=api_url) as s:
        assert s.session_id in server.active_sessions
        assert s.tenant_id == "acme" and s.disk_quota_mb == 512
        sid = s.session_id
    assert sid not in server.active_sessions
    s.close()   # idempotent


def test_create_and_close(api_url):
    s = Sandbox.create(api_key=KEY, base_url=api_url)
    assert s.session_id
    s.close()
    assert not server.active_sessions


def test_close_after_expiry_is_quiet(api_url):
    s = Sandbox.create(api_key=KEY, base_url=api_url)
    server.active_sessions.clear()   # expired on the server
    s.close()


def test_settings_from_environment(api_url, monkeypatch):
    monkeypatch.setenv("SANDBOX_API_URL", api_url)
    monkeypatch.setenv("SANDBOX_API_KEY", KEY)
    with Sandbox() as s:
        assert s.tenant_id == "acme"


def test_missing_settings_explain_what_to_set():
    with pytest.raises(SandboxError, match="SANDBOX_API_URL"):
        Sandbox()


def test_using_an_unstarted_sandbox_is_a_clear_error(api_url):
    with pytest.raises(SandboxError, match="not started"):
        Sandbox(api_key=KEY, base_url=api_url).run("ls")


# ---------------------------------------------------------------- commands and files


def test_run_returns_structured_result(sbx):
    r = sbx.run("echo hi")
    assert r.stdout == "ran echo hi\n" and r.exit_code == 0 and r.ok
    assert str(r) == "[STDOUT]:\nran echo hi\n[EXIT CODE]: 0"
    assert r.check() is r


@pytest.mark.parametrize("command, attr", [("fail now", None), ("hang", "timed_out"), ("eat-memory", "oom_killed")])
def test_failed_commands(sbx, command, attr):
    r = sbx.run(command)
    assert not r.ok
    if attr:
        assert getattr(r, attr)
    with pytest.raises(CommandError) as exc:
        r.check()
    assert exc.value.result is r


def test_files_roundtrip_and_missing_file(sbx):
    sbx.files.write("pkg/main.py", "print(1)")
    assert sbx.files.read("pkg/main.py") == "print(1)"
    with pytest.raises(FileNotFoundError):
        sbx.files.read("nope.py")


def test_path_traversal_is_permission_denied(sbx):
    with pytest.raises(PermissionDeniedError):
        sbx.files.write("../escape.txt", "x")
    with pytest.raises(PermissionError):   # also a builtin PermissionError
        sbx.files.read("../../etc/passwd")


def test_quota_exceeded(sbx):
    server.active_sessions[sbx.session_id].workspace._ws.quota_bytes = 2**20
    with pytest.raises(QuotaExceededError, match="quota"):
        sbx.files.write("big.txt", "x" * 2_000_000)


def test_invalid_timeout_is_validation_error(sbx):
    with pytest.raises(ValidationError):
        sbx.run("ls", timeout=600)


# ---------------------------------------------------------------- errors


def test_bad_key(api_url):
    with pytest.raises(AuthenticationError) as exc:
        Sandbox(api_key="wrong", base_url=api_url).start()
    assert exc.value.status == 401


def test_session_gone_is_not_found(sbx):
    server.active_sessions.clear()
    with pytest.raises(NotFoundError):
        sbx.run("ls")
    sbx.session_id = None


def test_egress_outside_policy(api_url):
    with pytest.raises(PermissionDeniedError, match="evil.com"):
        Sandbox(api_key=KEY, base_url=api_url, egress=["evil.com"]).start()


def test_egress_presets_expand(api_url):
    with Sandbox(api_key=KEY, base_url=api_url, egress=["pypi"]) as s:
        assert s.egress == ["pypi.org", "files.pythonhosted.org"]
        assert s.egress_log()[0]["host"] == "pypi.org"
        assert "api.openai.com" in s.egress_policy()


def test_full_server_raises_capacity_error_after_retries(api_url, monkeypatch):
    waits = []
    monkeypatch.setattr(sdk_http.time, "sleep", waits.append)
    FakeWorkspace.capacity_left = 0
    with pytest.raises(CapacityError):
        Sandbox(api_key=KEY, base_url=api_url, max_retries=2).start()
    assert waits == [1, 2]   # exponential backoff, then give up


def test_server_unreachable(monkeypatch):
    from airlock_sandbox import APIConnectionError
    monkeypatch.setattr(sdk_http.time, "sleep", lambda s: None)
    with pytest.raises(APIConnectionError):
        Sandbox(api_key=KEY, base_url="http://127.0.0.1:9", timeout=2).start()


# ---------------------------------------------------------------- rate limits and retries


def test_rate_limit_is_retried_using_retry_after(api_url, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits('{"*": {"requests_per_minute": 2}}'), clock=clock))
    waits = []

    def fake_sleep(seconds):          # instead of sleeping, move the server's clock forward
        waits.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(sdk_http.time, "sleep", fake_sleep)
    with Sandbox(api_key=KEY, base_url=api_url) as s:   # request 1
        s.run("a")                                       # request 2 — bucket now empty
        assert s.run("b").ok                             # 429, waits Retry-After, succeeds
    assert waits and waits[0] == 30                      # 2/min → one token per 30 s


def test_rate_limit_error_when_retries_exhausted(api_url, monkeypatch):
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits('{"*": {"requests_per_minute": 1}}'), clock=FakeClock()))
    monkeypatch.setattr(sdk_http.time, "sleep", lambda s: None)
    s = Sandbox(api_key=KEY, base_url=api_url, max_retries=0).start()
    with pytest.raises(RateLimitError) as exc:
        s.run("ls")
    assert exc.value.retry_after == 60
    s.session_id = None


def test_usage(sbx):
    usage = sbx.usage()
    assert usage["tenant_id"] == "acme" and usage["sessions_open"] == 1


# ---------------------------------------------------------------- agent tools


def test_tool_definitions_in_both_formats():
    names = ["write_file", "read_file", "run_command"]
    assert [t["function"]["name"] for t in openai_tools()] == names
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in openai_tools())
    assert [t["name"] for t in anthropic_tools()] == names
    assert all("input_schema" in t for t in anthropic_tools())
    json.dumps(openai_tools()), json.dumps(anthropic_tools())   # serialisable as-is


def test_handle_tool_call_round_trip(sbx):
    assert "Wrote" in handle_tool_call(sbx, "write_file", '{"path": "a.py", "content": "print(1)"}')
    assert handle_tool_call(sbx, "read_file", {"path": "a.py"}) == "print(1)"
    assert "ran python3 a.py" in handle_tool_call(sbx, "run_command", {"command": "python3 a.py"})


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("run_command", "{not json", "not valid JSON"),
        ("run_command", "[1, 2]", "must be a JSON object"),
        ("write_file", {"path": "a.py"}, "missing required argument"),
        ("read_file", {"path": "missing.py"}, "file not found"),
        ("read_file", {"path": "../../etc/passwd"}, "PermissionDeniedError"),
        ("delete_everything", {}, "unknown tool"),
        ("run_command", {"command": "ls", "timeout_seconds": 999}, "ValidationError"),
    ],
)
def test_handle_tool_call_turns_errors_into_text(sbx, name, args, expected):
    assert expected in handle_tool_call(sbx, name, args)


def test_handle_tool_call_truncates_keeping_the_end(sbx):
    out = handle_tool_call(sbx, "run_command", {"command": "big-output"})
    assert len(out) < 8200 and "TRUNCATED" in out and "[EXIT CODE]: 0" in out


def test_sdk_sandbox_plugs_into_the_repo_agent(sbx):
    """AutonomousCodingAgent only needs read_file / write_file / run_command."""
    from types import SimpleNamespace
    from src.agent.agent import AutonomousCodingAgent

    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="run_command", arguments='{"command": "pytest"}'))
    replies = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))]),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))]),
    ]
    agent = AutonomousCodingAgent.__new__(AutonomousCodingAgent)
    agent.model = "fake"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: replies.pop(0))))
    assert agent.solve_task(sbx, "task") == "done"


# ---------------------------------------------------------------- older servers (text-only exec output)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("[STDOUT]:\nline 1\nline 2\n[STDERR]:\nwarn\n[EXIT CODE]: 2",
         dict(stdout="line 1\nline 2\n", stderr="warn\n", exit_code=2, timed_out=False, oom_killed=False, warnings=[])),
        ("[EXIT CODE]: 0", dict(stdout="", stderr="", exit_code=0, warnings=[])),
        ("[TIMEOUT]: Execution exceeded 3s limit.\n[EXIT CODE]: -1",
         dict(exit_code=-1, timed_out=True, warnings=["Execution exceeded 3s limit."])),
        ("[STDERR]:\nKilled\n[WARNING]: Process killed by cgroups (Memory limit exceeded).\n[EXIT CODE]: 137",
         dict(stderr="Killed\n", exit_code=137, oom_killed=True)),
    ],
)
def test_text_only_output_is_split_into_fields(text, expected):
    from airlock_sandbox import CommandResult
    result = CommandResult.from_response({"command": "x", "output": text})
    for key, value in expected.items():
        assert getattr(result, key) == value, key
    assert result.output == text


# ---------------------------------------------------------------- key management (Keys + CLI)

ADMIN_KEY = "b" * 40


@pytest.fixture
def keystore(api_url, monkeypatch, tmp_path):
    from src.api.keystore import KeyStore, SqliteBackend

    monkeypatch.setattr(server, "KEYSTORE", KeyStore(SqliteBackend(str(tmp_path / "keys.db"))))
    monkeypatch.setattr(server, "ADMIN_KEY", ADMIN_KEY)
    return api_url


def test_admin_issues_tenant_rotates_old_key_dies(keystore):
    from airlock_sandbox import AuthenticationError
    from airlock_sandbox.keys import Keys

    admin = Keys(api_key=ADMIN_KEY, base_url=keystore, admin=True)
    first = admin.create(tenant="newco", name="onboarding")
    assert first["api_key"].startswith("asb_")

    with Sandbox(api_key=first["api_key"], base_url=keystore) as s:     # the new key runs sandboxes
        assert s.tenant_id == "newco" and s.run("ls").ok

    mine = Keys(api_key=first["api_key"], base_url=keystore)
    second = mine.create(name="rotated")
    Keys(api_key=second["api_key"], base_url=keystore).revoke(first["key_id"])
    assert {k["key_id"]: k["active"] for k in Keys(api_key=second["api_key"], base_url=keystore).list()} == {
        first["key_id"]: False, second["key_id"]: True}
    with pytest.raises(AuthenticationError):
        Sandbox(api_key=first["api_key"], base_url=keystore).start()
    assert [k["tenant_id"] for k in admin.list(tenant="newco")] == ["newco", "newco"]


def test_keys_errors_are_typed(keystore):
    from airlock_sandbox import AuthenticationError, PermissionDeniedError, SandboxError
    from airlock_sandbox.keys import Keys

    issued = Keys(api_key=ADMIN_KEY, base_url=keystore, admin=True).create(tenant="acme")
    with pytest.raises(SandboxError) as last:                       # 409: can't revoke the last key
        Keys(api_key=issued["api_key"], base_url=keystore).revoke(issued["key_id"])
    assert last.value.status == 409
    with pytest.raises(AuthenticationError):                        # a tenant key isn't an admin key
        Keys(api_key=issued["api_key"], base_url=keystore, admin=True).list()
    with pytest.raises(SandboxError, match="tenant"):
        Keys(api_key=ADMIN_KEY, base_url=keystore, admin=True).create(name="x")


def test_keys_cli(keystore, monkeypatch, capsys):
    from airlock_sandbox.keys import main

    monkeypatch.setenv("SANDBOX_API_URL", keystore)
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", ADMIN_KEY)
    main(["--admin", "create", "--tenant", "cli-co", "--name", "laptop"])
    out = capsys.readouterr().out
    assert "shown only once" in out
    api_key = next(line.strip() for line in out.splitlines() if line.strip().startswith("asb_"))

    monkeypatch.setenv("SANDBOX_API_KEY", api_key)
    main(["list"])
    listing = capsys.readouterr().out
    assert "cli-co" in listing and "laptop" in listing and "active" in listing and api_key not in listing

    with pytest.raises(SystemExit):                                  # last key: refused, clean error
        main(["revoke", listing.split("\n")[1].split()[0]])
    assert "last active key" in capsys.readouterr().err


# ---------------------------------------------------------------- repo import


@pytest.fixture
def fake_github_import(monkeypatch):
    from src.api.repos import RepoImportError

    def fetch(repo):
        if repo.name == "private":
            raise RepoImportError(f"{repo.full_name}@{repo.ref} wasn't found.", 404)
        return b"tgz"

    monkeypatch.setattr(server, "fetch_archive", fetch)
    monkeypatch.setattr(server, "import_archive", lambda ws, repo, archive, dest: {
        "repo": repo.full_name, "ref": repo.ref, "path": f"/workspace/{dest}", "dest": dest, "files": 5,
        "archive_bytes": len(archive)})


def test_import_repo(sbx, fake_github_import):
    result = sbx.import_repo("psf/requests", ref="v2.32.3", path="lib")
    assert result["path"] == "/workspace/lib" and result["ref"] == "v2.32.3" and result["files"] == 5
    with pytest.raises(NotFoundError, match="wasn't found"):
        sbx.import_repo("someone/private")
    with pytest.raises(ValidationError):
        sbx.import_repo("not a repo")
