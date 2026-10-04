"""Agent loop tests with a scripted fake model; no OpenAI calls are made."""
import json
from types import SimpleNamespace

from src.agent.agent import MAX_TOOL_OUTPUT_CHARS, AutonomousCodingAgent, truncate_tool_output


def tool_call(call_id, name, arguments):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=args))


def reply(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


class ScriptedModel:
    """Returns the given replies in order and records the messages it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(list(kwargs["messages"]))
        return self.replies.pop(0)


def make_agent(model):
    agent = AutonomousCodingAgent.__new__(AutonomousCodingAgent)
    agent.client = model
    agent.model = "fake"
    return agent


def tool_messages(model):
    """Tool results the model saw on its final call."""
    return [m for m in model.calls[-1] if isinstance(m, dict) and m["role"] == "tool"]


class FakeWorkspace:
    def __init__(self, output=""):
        self.output = output

    def read_file(self, path):
        raise PermissionError(f"Access denied: path traversal outside workspace for '{path}'")

    def write_file(self, path, content):
        return "ok"

    def run_command(self, command):
        return self.output


def test_tool_exception_is_returned_to_model_not_raised():
    model = ScriptedModel(
        reply(tool_calls=[tool_call("c1", "read_file", {"path": "../../etc/passwd"})]),
        reply(content="done"),
    )
    assert make_agent(model).solve_task(FakeWorkspace(), "task") == "done"
    [result] = tool_messages(model)
    assert result["tool_call_id"] == "c1"
    assert "PermissionError" in result["content"]


def test_missing_argument_is_returned_to_model():
    model = ScriptedModel(reply(tool_calls=[tool_call("c1", "write_file", {"path": "a.py"})]), reply(content="done"))
    assert make_agent(model).solve_task(FakeWorkspace(), "task") == "done"
    assert "KeyError" in tool_messages(model)[0]["content"]


def test_invalid_json_arguments_are_returned_to_model():
    model = ScriptedModel(reply(tool_calls=[tool_call("c1", "run_command", "{not json")]), reply(content="done"))
    assert make_agent(model).solve_task(FakeWorkspace(), "task") == "done"
    assert "not valid JSON" in tool_messages(model)[0]["content"]


def test_large_output_is_truncated_before_reaching_model():
    huge = "x" * 50_000 + "\n[EXIT CODE]: 1"
    model = ScriptedModel(reply(tool_calls=[tool_call("c1", "run_command", {"command": "spam"})]), reply(content="done"))
    make_agent(model).solve_task(FakeWorkspace(output=huge), "task")
    content = tool_messages(model)[0]["content"]
    assert len(content) < MAX_TOOL_OUTPUT_CHARS + 100
    assert "TRUNCATED" in content
    assert content.endswith("[EXIT CODE]: 1")  # the tail survives


def test_truncate_leaves_short_output_alone():
    assert truncate_tool_output("short") == "short"


def test_stops_at_iteration_limit():
    looping = [reply(tool_calls=[tool_call(f"c{i}", "write_file", {"path": "a", "content": "b"})]) for i in range(3)]
    model = ScriptedModel(*looping)
    assert "maximum iteration" in make_agent(model).solve_task(FakeWorkspace(), "task", max_iterations=3)
