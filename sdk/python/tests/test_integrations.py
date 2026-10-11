"""Framework integrations against the real frameworks (each test skips when its framework isn't installed)."""
import asyncio
import json
from types import SimpleNamespace

import pytest


class FakeSandbox:
    def __init__(self):
        self.store = {}
        self.ran = []
        self.files = SimpleNamespace(write=self._write, read=self._read)

    def _write(self, path, content):
        self.store[path] = content

    def _read(self, path):
        if path not in self.store:
            raise FileNotFoundError(path)
        return self.store[path]

    def run(self, command, timeout=15):
        self.ran.append((command, timeout))
        return SimpleNamespace(output=f"[STDOUT]:\nran {command}\n[EXIT CODE]: 0")


def test_tool_selection():
    from airlock_sandbox.integrations._common import selected

    assert selected(None) == ["write_file", "read_file", "run_command"]
    assert selected(["run_command", "read_file"]) == ["read_file", "run_command"]
    with pytest.raises(ValueError):
        selected(["rm_rf"])


def test_langchain():
    pytest.importorskip("langchain_core")
    from airlock_sandbox.integrations.langchain import airlock_tools

    sbx = FakeSandbox()
    tools = {t.name: t for t in airlock_tools(sbx)}
    assert list(tools) == ["write_file", "read_file", "run_command"]
    assert tools["run_command"].args["command"]["type"] == "string"
    assert "Wrote 8 characters" in tools["write_file"].invoke({"path": "a.py", "content": "print(1)"})
    assert tools["read_file"].invoke({"path": "a.py"}) == "print(1)"
    assert "file not found" in tools["read_file"].invoke({"path": "missing.py"})
    assert "ran ls" in asyncio.run(tools["run_command"].ainvoke({"command": "ls", "timeout_seconds": 30}))
    assert sbx.ran == [("ls", 30)]
    assert [t.name for t in airlock_tools(sbx, tools=["run_command"])] == ["run_command"]


def test_openai_agents():
    pytest.importorskip("agents")
    from airlock_sandbox.integrations.openai_agents import airlock_tools

    sbx = FakeSandbox()
    tools = {t.name: t for t in airlock_tools(sbx)}
    assert tools["run_command"].params_json_schema["required"] == ["command"]
    out = asyncio.run(tools["run_command"].on_invoke_tool(None, json.dumps({"command": "pytest -q"})))
    assert "ran pytest -q" in out and sbx.ran == [("pytest -q", 15)]
    bad = asyncio.run(tools["write_file"].on_invoke_tool(None, "{not json"))
    assert bad.startswith("Error: tool arguments were not valid JSON")


def test_openai_agents_tools_attach_to_an_agent():
    agents = pytest.importorskip("agents")
    from airlock_sandbox.integrations.openai_agents import airlock_tools

    agent = agents.Agent(name="coder", instructions="Use the sandbox.", tools=airlock_tools(FakeSandbox()))
    assert [t.name for t in agent.tools] == ["write_file", "read_file", "run_command"]


def test_crewai():
    pytest.importorskip("crewai")
    from airlock_sandbox.integrations.crewai import airlock_tools

    sbx = FakeSandbox()
    tools = {t.name: t for t in airlock_tools(sbx)}
    assert "Wrote" in tools["write_file"].run(path="x.txt", content="hi")
    assert tools["read_file"].run(path="x.txt") == "hi"
    assert "ran echo 1" in tools["run_command"].run(command="echo 1")
    assert set(tools["run_command"].args_schema.model_fields) == {"command", "timeout_seconds"}
