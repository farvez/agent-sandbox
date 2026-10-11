"""Airlock tools for agent frameworks: LangChain, the OpenAI Agents SDK and CrewAI.

Each module turns a Sandbox into that framework's tools (write_file, read_file, run_command),
backed by airlock_sandbox.tools.handle_tool_call, so every framework gets the same behaviour:
errors come back as text the model can act on, and long output is truncated in the middle.

    from airlock_sandbox import Sandbox
    from airlock_sandbox.integrations.langchain import airlock_tools        # or .openai_agents, .crewai

    with Sandbox(egress=["pypi"]) as sbx:
        tools = airlock_tools(sbx)

Install the framework yourself (the SDK has no dependencies), or use the extras:
pip install "airlock-sandbox[langchain]" / "[openai-agents]" / "[crewai]".
"""
