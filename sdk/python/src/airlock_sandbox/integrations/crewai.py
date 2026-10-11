"""Airlock tools for CrewAI.

    from crewai import Agent, Crew, Task
    from airlock_sandbox import Sandbox
    from airlock_sandbox.integrations.crewai import airlock_tools

    with Sandbox(egress=["pypi"]) as sbx:
        coder = Agent(role="Developer", goal="Write working code", backstory="...",
                      tools=airlock_tools(sbx))
        task = Task(description="Write and run a fizzbuzz script", expected_output="The output", agent=coder)
        print(Crew(agents=[coder], tasks=[task]).kickoff())
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional

from airlock_sandbox.integrations._common import DESCRIPTIONS, pydantic_models, selected
from airlock_sandbox.tools import handle_tool_call


def airlock_tools(sandbox: Any, tools: Optional[Iterable[str]] = None) -> List[Any]:
    """CrewAI tools for `sandbox` (all three, or the names in `tools`)."""
    try:
        from crewai.tools import BaseTool
    except ImportError as err:  # pragma: no cover - depends on the environment
        raise ImportError('Install CrewAI first: pip install "airlock-sandbox[crewai]"') from err
    from pydantic import PrivateAttr

    class AirlockTool(BaseTool):
        _sandbox: Any = PrivateAttr()
        _tool: str = PrivateAttr()

        def _run(self, **kwargs: Any) -> str:
            return handle_tool_call(self._sandbox, self._tool, kwargs)

    models = pydantic_models()
    built = []
    for name in selected(tools):
        tool = AirlockTool(name=name, description=DESCRIPTIONS[name], args_schema=models[name])
        tool._sandbox, tool._tool = sandbox, name
        built.append(tool)
    return built
