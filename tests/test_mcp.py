"""The MCP server, driven by a real MCP client, against the real API server (containers faked)."""
import asyncio
import os
import sys

import pytest

mcp = pytest.importorskip("mcp", reason="install the SDK with the [mcp] extra")

import src.api.server as server
from airlock_sandbox import Sandbox
from airlock_sandbox.mcp_server import SandboxHolder, build_server
from tests.test_sdk import KEY, api_url, fake_github_import, fresh_state  # noqa: F401  (pytest fixtures)

TOOLS = ["run_command", "write_file", "read_file", "list_files", "import_repo", "egress_log", "sandbox_info", "reset_sandbox"]


def make_holder(url, egress=None):
    return SandboxHolder(
        egress=egress or [], template="sandbox-base:latest",
        factory=lambda **kw: Sandbox(api_key=KEY, base_url=url, **kw),
    )


def call(holder, steps):
    """Connects an in-memory MCP client, runs `steps(client)`, disconnects; returns its result."""
    async def go():
        async with mcp.Client(build_server(holder)) as client:
            return await steps(client)
    return asyncio.run(go())


def text(result):
    return "\n".join(block.text for block in result.content)


def test_lists_all_tools_with_descriptions_and_hints(api_url):
    async def steps(c):
        return (await c.list_tools()).tools
    tools = {t.name: t for t in call(make_holder(api_url), steps)}
    assert list(tools) == TOOLS
    assert all(t.description for t in tools.values())
    assert tools["read_file"].annotations.read_only_hint is True
    assert tools["reset_sandbox"].annotations.destructive_hint is True
    assert tools["run_command"].input_schema["required"] == ["command"]


def test_session_is_lazy_and_deleted_on_disconnect(api_url):
    holder = make_holder(api_url)

    async def steps(c):
        await c.list_tools()
        assert not server.active_sessions                 # nothing created just for connecting
        await c.call_tool("write_file", {"path": "a.py", "content": "print(1)"})
        assert len(server.active_sessions) == 1
        return await c.call_tool("read_file", {"path": "a.py"})

    assert text(call(holder, steps)) == "print(1)"
    assert not server.active_sessions                     # deleted when the client went away


def test_run_command_and_list_files(api_url):
    async def steps(c):
        return (await c.call_tool("run_command", {"command": "python3 a.py"}),
                await c.call_tool("list_files", {"path": "src", "max_depth": 2}))
    ran, listed = call(make_holder(api_url), steps)
    assert not ran.is_error and "ran python3 a.py" in text(ran) and "[EXIT CODE]: 0" in text(ran)
    assert "find src -maxdepth 2" in text(listed)


def test_errors_come_back_as_tool_errors(api_url):
    async def steps(c):
        return (await c.call_tool("read_file", {"path": "../../etc/passwd"}),
                await c.call_tool("read_file", {"path": "missing.py"}),
                await c.call_tool("run_command", {"command": "ls", "timeout_seconds": 600}))
    traversal, missing, bad_timeout = call(make_holder(api_url), steps)
    assert traversal.is_error and "PermissionDeniedError" in text(traversal)
    assert missing.is_error and "File not found" in text(missing)
    assert bad_timeout.is_error and "ValidationError" in text(bad_timeout)


def test_expired_session_is_replaced_with_a_notice(api_url):
    async def steps(c):
        await c.call_tool("run_command", {"command": "first"})
        server.active_sessions.clear()                    # the server expired it
        return await c.call_tool("run_command", {"command": "second"})
    result = call(make_holder(api_url), steps)
    assert not result.is_error and "previous sandbox session expired" in text(result)


def test_reset_starts_a_new_session(api_url):
    holder = make_holder(api_url)

    async def steps(c):
        await c.call_tool("run_command", {"command": "x"})
        first = holder.get().session_id
        await c.call_tool("reset_sandbox", {})
        return first, holder.get().session_id
    first, second = call(holder, steps)
    assert first != second


def test_info_and_egress_log(api_url):
    async def steps(c):
        return await c.call_tool("sandbox_info", {}), await c.call_tool("egress_log", {})
    info, log = call(make_holder(api_url, egress=["pypi"]), steps)
    assert "Internet: pypi.org, files.pythonhosted.org" in text(info) and "Disk quota: 512 MB" in text(info)
    assert "allow  pypi.org" in text(log)


def test_egress_outside_policy_is_a_clear_error(api_url):
    async def steps(c):
        return await c.call_tool("run_command", {"command": "ls"})
    result = call(make_holder(api_url, egress=["evil.com"]), steps)
    assert result.is_error and "evil.com" in text(result)


def test_real_stdio_process_end_to_end(api_url, tmp_path):
    """Launches `python -m airlock_sandbox.mcp_server` the way Claude Code does."""
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "airlock_sandbox.mcp_server", "--egress", "pypi"],
        env={**os.environ, "SANDBOX_API_URL": api_url, "SANDBOX_API_KEY": KEY, "AIRLOCK_STATE_DIR": str(tmp_path)},
    )

    async def go():
        async with mcp.Client(params) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            ran = await c.call_tool("run_command", {"command": "echo hi"})
            info = await c.call_tool("sandbox_info", {})
            return names, text(ran), text(info)

    names, ran, info = asyncio.run(go())
    assert names == TOOLS
    assert "ran echo hi" in ran
    assert "Internet: pypi.org" in info
    assert not server.active_sessions        # the process closed its session on exit



# ---------------------------------------------------------------- sessions left by a killed process


def test_session_left_by_a_killed_run_is_deleted_on_next_start(api_url, tmp_path):
    from airlock_sandbox.mcp_server import state_file_for

    state = state_file_for(api_url, KEY, "sandbox-base:latest", [], str(tmp_path))
    killed = SandboxHolder(egress=[], template="sandbox-base:latest", state_file=state,
                           factory=lambda **kw: Sandbox(api_key=KEY, base_url=api_url, **kw))
    leaked = killed.get().session_id               # process "killed" here: close() never runs
    assert leaked in server.active_sessions and os.path.exists(state)

    fresh = SandboxHolder(egress=[], template="sandbox-base:latest", state_file=state,
                          factory=lambda **kw: Sandbox(api_key=KEY, base_url=api_url, **kw))
    current = fresh.get().session_id
    assert leaked not in server.active_sessions    # cleaned up by the next start
    assert list(server.active_sessions) == [current]

    fresh.close()
    assert not server.active_sessions and not os.path.exists(state)


def test_state_file_never_contains_the_key(api_url, tmp_path):
    from airlock_sandbox.mcp_server import state_file_for

    state = state_file_for(api_url, KEY, "sandbox-base:latest", ["pypi"], str(tmp_path))
    holder = SandboxHolder(egress=["pypi"], template="sandbox-base:latest", state_file=state,
                           factory=lambda **kw: Sandbox(api_key=KEY, base_url=api_url, **kw))
    holder.get()
    assert KEY not in state and KEY not in open(state).read()
    holder.close()


def test_different_configurations_use_different_state_files(tmp_path):
    from airlock_sandbox.mcp_server import state_file_for

    a = state_file_for("https://x", "k1", "img", [], str(tmp_path))
    assert a == state_file_for("https://x", "k1", "img", [], str(tmp_path))
    assert a != state_file_for("https://x", "k2", "img", [], str(tmp_path))
    assert a != state_file_for("https://x", "k1", "img", ["pypi"], str(tmp_path))


def test_import_repo_tool(api_url, fake_github_import):
    async def steps(c):
        return (await c.call_tool("import_repo", {"repo": "https://github.com/psf/requests"}),
                await c.call_tool("import_repo", {"repo": "someone/private"}))
    ok, missing = call(make_holder(api_url), steps)
    assert not ok.is_error and text(ok) == "Imported psf/requests@HEAD: 5 files in /workspace/requests"
    assert missing.is_error and "wasn't found" in text(missing)
