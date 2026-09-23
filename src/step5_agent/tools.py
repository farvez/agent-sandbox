import json
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from src.step5_agent.sandbox import SandboxedWorkspace

OPENAI_SANDBOX_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the isolated workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path (e.g., 'main.py')"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command inside the isolated container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
]


def dispatch_tool_call(workspace: Any, tool_name: str, arguments: Dict[str, Any]) -> str:
    """Dispatches tool call arguments to the corresponding workspace method."""
    if tool_name == "write_file":
        return workspace.write_file(arguments["path"], arguments["content"])
    elif tool_name == "read_file":
        return workspace.read_file(arguments["path"])
    elif tool_name == "run_command":
        return workspace.run_command(arguments["command"])
    else:
        return f"Unknown tool: {tool_name}"