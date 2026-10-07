"""The remote MCP endpoint (/mcp) through a real MCP client over HTTP (containers faked)."""
import asyncio

import httpx
import pytest

mcp = pytest.importorskip("mcp", reason="install the mcp package")

from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

import src.api.server as server
from tests.conftest import TENANT_KEYS
from tests.test_sdk import KEY, api_url, fresh_state  # noqa: F401  (pytest fixtures)

OTHER_KEY = TENANT_KEYS["globex"]
TOOLS = ["run_command", "write_file", "read_file", "list_files", "import_repo", "egress_log", "activity",
         "sandbox_info", "reset_sandbox"]


def call(url, steps, key=KEY, query=""):
    """Connects an MCP client over HTTP with the key header, runs `steps(client)`, disconnects."""
    async def go():
        http = create_mcp_http_client(headers={"X-API-Key": key})
        async with mcp.Client(streamable_http_client(f"{url}/mcp{query}", http_client=http)) as client:
            return await steps(client)
    return asyncio.run(go())


def text(result):
    return "\n".join(block.text for block in result.content)


def test_lists_tools_without_exposing_internal_parameters(api_url):
    async def steps(c):
        return (await c.list_tools()).tools
    tools = {t.name: t for t in call(api_url, steps)}
    assert list(tools) == TOOLS
    assert all("ctx" not in t.input_schema.get("properties", {}) for t in tools.values())
    assert tools["run_command"].input_schema["required"] == ["command"]
    assert not server.active_sessions                       # listing tools starts no sandbox


def test_calls_and_reconnects_share_the_keys_sandbox(api_url):
    async def first(c):
        await c.call_tool("write_file", {"path": "a.py", "content": "print(1)"})
        ran = await c.call_tool("run_command", {"command": "python3 a.py"})
        return ran, set(server.active_sessions)

    ran, sandboxes = call(api_url, first)
    assert "ran python3 a.py" in text(ran) and len(sandboxes) == 1          # one sandbox, reused by every call

    async def later(c):                                                        # a new connection, same key
        return text(await c.call_tool("read_file", {"path": "a.py"})), set(server.active_sessions)
    content, after = call(api_url, later)
    assert content == "print(1)" and after == sandboxes


def test_workspace_parameter_gives_a_separate_sandbox(api_url):
    async def steps(c):
        await c.call_tool("run_command", {"command": "true"})
        return set(server.active_sessions)
    default = call(api_url, steps)
    named = call(api_url, steps, query="?workspace=experiments")
    assert len(named) == 2 and default < named
    bad = httpx.post(f"{api_url}/mcp?workspace=../x", json={}, headers={"X-API-Key": KEY})
    assert bad.status_code == 400


def test_sessions_run_under_the_keys_tenant(api_url):
    async def steps(c):
        await c.call_tool("run_command", {"command": "true"})
        return [rec.tenant_id for rec in server.active_sessions.values()]
    assert call(api_url, steps) == ["acme"]
    assert sorted(call(api_url, steps, key=OTHER_KEY)) == ["acme", "globex"]   # its own sandbox, not acme's


def test_missing_or_wrong_key_is_refused(api_url):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    accept = {"Accept": "application/json, text/event-stream"}
    assert httpx.post(f"{api_url}/mcp", json=init, headers=accept).status_code == 401
    bad = httpx.post(f"{api_url}/mcp", json=init, headers={**accept, "X-API-Key": "wrong"})
    assert bad.status_code == 401 and "www-authenticate" in bad.headers
    ok = httpx.post(f"{api_url}/mcp", json=init, headers={**accept, "Authorization": f"Bearer {KEY}"})
    assert ok.status_code == 200 and ok.headers.get("mcp-session-id")


def test_another_tenant_cannot_use_an_mcp_session(api_url):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream"}
    session = httpx.post(f"{api_url}/mcp", json=init, headers={**headers, "X-API-Key": KEY}).headers["mcp-session-id"]
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    stolen = httpx.post(f"{api_url}/mcp", json=listing,
                        headers={**headers, "X-API-Key": OTHER_KEY, "mcp-session-id": session})
    assert stolen.status_code == 404


def test_internet_access_comes_from_the_url(api_url):
    async def steps(c):
        return text(await c.call_tool("sandbox_info", {}))
    info = call(api_url, steps, query="?egress=pypi")
    assert "Internet: pypi.org, files.pythonhosted.org" in info
    server.active_sessions.clear()

    refused = call(api_url, lambda c: c.call_tool("run_command", {"command": "true"}), query="?egress=evil.example.com")
    assert refused.is_error and "not permitted" in text(refused)


def test_commands_are_audited_as_mcp(api_url, monkeypatch, tmp_path):
    from src.api.audit import AuditLog, _SqliteAudit

    monkeypatch.setattr(server, "AUDIT", AuditLog(_SqliteAudit(str(tmp_path / "audit.db"))))

    async def steps(c):
        await c.call_tool("run_command", {"command": "echo hi"})
        return text(await c.call_tool("activity", {"limit": 5}))
    assert "exit 0" in call(api_url, steps) and "echo hi" in call(api_url, steps)
    assert server.AUDIT.list("acme")[0][0]["actor"] == "mcp static key"


def test_reset_starts_a_fresh_sandbox(api_url):
    async def steps(c):
        await c.call_tool("run_command", {"command": "true"})
        first = set(server.active_sessions)
        await c.call_tool("reset_sandbox", {})
        return first, set(server.active_sessions)
    first, after = call(api_url, steps)
    assert len(after) == 1 and first.isdisjoint(after)
