"""Airlock tools for the OpenAI Agents SDK (`openai-agents`).

    from agents import Agent, Runner
    from airlock_sandbox import Sandbox
    from airlock_sandbox.integrations.openai_agents import airlock_tools

    with Sandbox(egress=["pypi"]) as sbx:
        agent = Agent(name="coder", instructions="Write code and run it in the sandbox.",
                      tools=airlock_tools(sbx))
        print(Runner.run_sync(agent, "Write and run a fizzbuzz script").final_output)
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable, List, Optional

from airlock_sandbox.integrations._common import DESCRIPTIONS, json_schema, selected
from airlock_sandbox.tools import handle_tool_call


def airlock_tools(sandbox: Any, tools: Optional[Iterable[str]] = None) -> List[Any]:
    """FunctionTools for `sandbox` (all three, or the names in `tools`)."""
    try:
        from agents import FunctionTool
    except ImportError as err:  # pragma: no cover - depends on the environment
        raise ImportError('Install the OpenAI Agents SDK first: pip install "airlock-sandbox[openai-agents]"') from err

    built = []
    for name in selected(tools):
        async def invoke(ctx: Any, arguments: str, _name: str = name) -> str:
            # handle_tool_call parses the JSON arguments and never raises.
            return await asyncio.to_thread(handle_tool_call, sandbox, _name, arguments)

        built.append(FunctionTool(
            name=name,
            description=DESCRIPTIONS[name],
            params_json_schema=json_schema(name),
            on_invoke_tool=invoke,
            strict_json_schema=False,   # run_command's timeout is optional, which strict mode doesn't allow
        ))
    return built
