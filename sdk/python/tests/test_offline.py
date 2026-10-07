"""SDK checks that need no server — CI runs these against the built wheel on several Python versions."""
import json

import pytest

import airlock_sandbox
from airlock_sandbox import (
    CommandError,
    CommandResult,
    RateLimitError,
    Sandbox,
    SandboxError,
)
from airlock_sandbox.errors import STATUS_ERRORS
from airlock_sandbox.tools import anthropic_tools, handle_tool_call, openai_tools, truncate


def test_version_and_public_api():
    assert airlock_sandbox.__version__.count(".") == 2
    for name in airlock_sandbox.__all__:
        assert hasattr(airlock_sandbox, name), name


def test_needs_url_and_key(monkeypatch):
    monkeypatch.delenv("SANDBOX_API_URL", raising=False)
    monkeypatch.delenv("SANDBOX_API_KEY", raising=False)
    with pytest.raises(SandboxError, match="SANDBOX_API_KEY"):
        Sandbox()


def test_status_codes_map_to_exceptions():
    assert STATUS_ERRORS[429] is RateLimitError
    assert all(issubclass(cls, SandboxError) for cls in STATUS_ERRORS.values())


def test_command_result_from_structured_response():
    r = CommandResult.from_response({"command": "x", "stdout": "a\n", "stderr": "", "exit_code": 1,
                                     "timed_out": False, "oom_killed": False, "warnings": [], "output": "..."})
    assert not r.ok and r.stdout == "a\n"
    with pytest.raises(CommandError):
        r.check()


def test_command_result_from_text_only_response():
    r = CommandResult.from_response({"command": "x", "output": "[STDOUT]:\nhi\n[EXIT CODE]: 0"})
    assert r.ok and r.stdout == "hi\n"


def test_tool_schemas_serialise():
    assert len(openai_tools()) == len(anthropic_tools()) == 3
    json.dumps(openai_tools())
    json.dumps(anthropic_tools())


def test_tool_errors_are_text_not_exceptions():
    assert "unknown tool" in handle_tool_call(object(), "nope", {})
    assert "not valid JSON" in handle_tool_call(object(), "run_command", "{")


def test_truncate_keeps_both_ends():
    text = "start" + "x" * 20_000 + "end"
    out = truncate(text, 1000)
    assert out.startswith("start") and out.endswith("end") and "TRUNCATED" in out


def test_mcp_server_lists_tools_without_contacting_the_server():
    """Only when installed with the [mcp] extra (Python 3.10+)."""
    pytest.importorskip("mcp")
    import asyncio

    import mcp
    from airlock_sandbox.mcp_server import SandboxHolder, build_server

    holder = SandboxHolder(egress=[], template="sandbox-base:latest",
                           factory=lambda **kw: (_ for _ in ()).throw(AssertionError("no session expected")))

    async def go():
        async with mcp.Client(build_server(holder)) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert asyncio.run(go()) == ["run_command", "write_file", "read_file", "list_files",
                                 "import_repo", "egress_log", "activity", "sandbox_info", "reset_sandbox"]
