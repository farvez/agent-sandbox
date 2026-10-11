"""Pieces shared by the framework integrations: argument schemas and tool selection."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Type

from airlock_sandbox.tools import TOOLS

TOOL_NAMES = [t["name"] for t in TOOLS]
DESCRIPTIONS: Dict[str, str] = {t["name"]: t["description"] for t in TOOLS}


def selected(names: Optional[Iterable[str]]) -> List[str]:
    """The tool names to build, in the standard order; rejects unknown names."""
    if names is None:
        return list(TOOL_NAMES)
    wanted = list(names)
    unknown = [n for n in wanted if n not in TOOL_NAMES]
    if unknown:
        raise ValueError(f"Unknown Airlock tool(s) {unknown}; available: {TOOL_NAMES}")
    return [n for n in TOOL_NAMES if n in wanted]


def json_schema(name: str) -> Dict[str, Any]:
    return next(dict(t["parameters"]) for t in TOOLS if t["name"] == name)


def pydantic_models() -> Dict[str, Type[Any]]:
    """Pydantic argument models (every supported framework depends on pydantic v2)."""
    from pydantic import BaseModel, Field

    class WriteFileArgs(BaseModel):
        path: str = Field(description="Path relative to the workspace, e.g. 'src/main.py'")
        content: str = Field(description="Full file content")

    class ReadFileArgs(BaseModel):
        path: str = Field(description="Path relative to the workspace")

    class RunCommandArgs(BaseModel):
        command: str = Field(description="Shell command, e.g. 'python3 -m pytest -q'")
        timeout_seconds: int = Field(default=15, ge=1, le=60, description="1-60, default 15")

    return {"write_file": WriteFileArgs, "read_file": ReadFileArgs, "run_command": RunCommandArgs}
