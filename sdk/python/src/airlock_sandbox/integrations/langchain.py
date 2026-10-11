"""Airlock tools for LangChain / LangGraph (langchain-core).

    from langchain.agents import create_agent            # or langgraph.prebuilt.create_react_agent
    from airlock_sandbox import Sandbox
    from airlock_sandbox.integrations.langchain import airlock_tools

    with Sandbox(egress=["pypi"]) as sbx:
        agent = create_agent("anthropic:claude-opus-5-5", tools=airlock_tools(sbx))
        agent.invoke({"messages": [{"role": "user", "content": "Write and run a fizzbuzz script"}]})
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable, List, Optional

from airlock_sandbox.integrations._common import DESCRIPTIONS, pydantic_models, selected
from airlock_sandbox.tools import handle_tool_call


def airlock_tools(sandbox: Any, tools: Optional[Iterable[str]] = None) -> List[Any]:
    """LangChain StructuredTools for `sandbox` (all three, or the names in `tools`)."""
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as err:  # pragma: no cover - depends on the environment
        raise ImportError('Install LangChain first: pip install "airlock-sandbox[langchain]"') from err

    models = pydantic_models()
    built = []
    for name in selected(tools):
        def run(_name: str = name, **kwargs: Any) -> str:
            return handle_tool_call(sandbox, _name, kwargs)

        async def arun(_name: str = name, **kwargs: Any) -> str:
            return await asyncio.to_thread(handle_tool_call, sandbox, _name, kwargs)

        built.append(StructuredTool.from_function(
            func=_bind(run, name), coroutine=_bind(arun, name), name=name,
            description=DESCRIPTIONS[name], args_schema=models[name],
        ))
    return built


def _bind(fn, name: str):
    """A function taking only the tool's arguments (LangChain inspects the signature)."""
    import functools

    return functools.partial(fn, _name=name)
