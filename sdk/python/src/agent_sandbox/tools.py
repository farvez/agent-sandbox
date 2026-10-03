"""Ready-made tools for LLM agents: read files, write files, run commands — in a sandbox.

    from agent_sandbox import Sandbox
    from agent_sandbox.tools import openai_tools, handle_tool_call

    with Sandbox() as sbx:
        response = client.chat.completions.create(model=..., messages=..., tools=openai_tools())
        for call in response.choices[0].message.tool_calls or []:
            result = handle_tool_call(sbx, call.function.name, call.function.arguments)

`anthropic_tools()` gives the same tools in the Anthropic Messages API format.
handle_tool_call never raises: errors come back as text the model can act on.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Union

from agent_sandbox.errors import SandboxError

MAX_TOOL_OUTPUT_CHARS = 8000

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "write_file",
        "description": "Create or overwrite a text file in the sandbox workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace, e.g. 'src/main.py'"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the sandbox workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the workspace"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in an isolated Linux sandbox (bash, Python 3.11). Files in the workspace "
            "persist between commands; processes do not. Returns stdout, stderr and the exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command, e.g. 'python3 -m pytest -q'"},
                "timeout_seconds": {"type": "integer", "description": "1-60, default 15", "minimum": 1, "maximum": 60},
            },
            "required": ["command"],
        },
    },
]


def openai_tools() -> List[Dict[str, Any]]:
    """Tool definitions for OpenAI Chat Completions (`tools=`)."""
    return [{"type": "function", "function": dict(t)} for t in TOOLS]


def anthropic_tools() -> List[Dict[str, Any]]:
    """Tool definitions for the Anthropic Messages API (`tools=`)."""
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOLS]


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Keeps the head and the tail; errors and exit codes are usually at the end."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return f"{text[:head]}\n\n... [TRUNCATED: {len(text) - head - tail} characters omitted] ...\n\n{text[-tail:]}"


def handle_tool_call(sandbox: Any, name: str, arguments: Union[str, Dict[str, Any], None]) -> str:
    """Runs one tool call against `sandbox` and returns text for the model. Never raises."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except (json.JSONDecodeError, TypeError, ValueError) as err:
        return f"Error: tool arguments were not valid JSON: {err}"
    if not isinstance(args, dict):
        return "Error: tool arguments must be a JSON object."

    try:
        if name == "write_file":
            sandbox.files.write(args["path"], args["content"])
            result = f"Wrote {len(args['content'])} characters to {args['path']}"
        elif name == "read_file":
            result = sandbox.files.read(args["path"])
        elif name == "run_command":
            result = sandbox.run(args["command"], timeout=int(args.get("timeout_seconds", 15))).output
        else:
            return f"Error: unknown tool '{name}'. Available: {[t['name'] for t in TOOLS]}"
    except KeyError as err:
        return f"Error: missing required argument {err} for {name}."
    except FileNotFoundError as err:
        return f"Error: file not found: {err}"
    except SandboxError as err:
        return f"Error from sandbox ({type(err).__name__}): {err}"
    except Exception as err:  # the model should see the problem, not crash the agent loop
        return f"Error: {name} failed ({type(err).__name__}): {err}"
    return truncate(result)
