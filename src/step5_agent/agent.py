import json
import os
from typing import Callable, List, Optional
from openai import OpenAI
from rich.console import Console

from src.step5_agent.sandbox import SandboxedWorkspace
from src.step5_agent.tools import OPENAI_SANDBOX_TOOLS, dispatch_tool_call

console = Console()

# Keeps one noisy command (a full pip log, a dumped binary) from filling the context window.
MAX_TOOL_OUTPUT_CHARS = 8000


def truncate_tool_output(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Keeps the head and the tail; errors and exit codes are usually at the end."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n... [TRUNCATED: {omitted} characters omitted] ...\n\n{text[-tail:]}"


class AutonomousCodingAgent:
    """An autonomous agent that inspects, modifies, and validates code inside a sandbox."""

    def __init__(self, model: str = "gpt-4o-mini"):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Please set the environment variable: "
                "set OPENAI_API_KEY=your_key (Windows) or export OPENAI_API_KEY=your_key (Linux/Mac)"
            )
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def solve_task(
        self,
        workspace: SandboxedWorkspace,
        task_instruction: str,
        max_iterations: int = 6,
        on_step_callback: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Runs the autonomous inspect-patch-verify loop."""
        messages: List[dict] = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous software engineer operating inside an isolated Linux sandbox. "
                    "You have tools to read files, write files, and run commands. "
                    "When given a task or a failing test suite, inspect the workspace, execute the test "
                    "command to see what fails, patch the code, and re-run the tests until they pass. "
                    "When everything passes, provide a final concise explanation of the fix."
                ),
            },
            {"role": "user", "content": task_instruction},
        ]

        for step in range(1, max_iterations + 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=OPENAI_SANDBOX_TOOLS,
                tool_choice="auto",
            )

            choice = response.choices[0]
            message = choice.message
            messages.append(message)

            # If the model didn't call any tools, it has reached its final answer
            if not message.tool_calls:
                return message.content or "Task completed."

            # Execute all tool calls emitted by the agent
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name

                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as err:
                    # Provide feedback to the agent so it can self-repair its JSON call
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"Error: Tool arguments were not valid JSON: {str(err)}",
                        }
                    )
                    continue

                if on_step_callback:
                    on_step_callback(fn_name, json.dumps(fn_args, indent=2))

                # Execute inside the isolated sandbox. Any failure (a missing argument,
                # a blocked path, a Docker error) goes back to the model as the tool
                # result so it can correct itself instead of ending the run.
                try:
                    tool_result = dispatch_tool_call(workspace, fn_name, fn_args)
                except Exception as err:
                    tool_result = f"Error: {fn_name} failed ({type(err).__name__}): {err}"
                tool_result = truncate_tool_output(tool_result)

                # Return the execution feedback to the LLM
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                )

        return "Reached maximum iteration limit before completing task."