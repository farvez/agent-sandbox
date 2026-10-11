"""Lesson 2: a tool-calling agent in about 60 lines.

The model can't run code itself. We give it a tool, `run_python`; when it wants to use it, the
API returns a `tool_use` block, *we* run the code (here, inside this sandbox), send the output
back as a `tool_result`, and ask again. That loop is all an "agent" is.

    pip install anthropic
    python agent.py "What is the 30th Fibonacci number? Compute it, don't guess."

Needs the ANTHROPIC_API_KEY secret (Workspace -> Secrets) and the "llm" internet preset.
Set MODEL to try another model, e.g. MODEL=claude-haiku-5-5 (cheaper).
"""
import os
import subprocess
import sys

import anthropic

MODEL = os.environ.get("MODEL", "claude-opus-5-5")

TOOLS = [{
    "name": "run_python",
    "description": "Run a Python 3 program and return what it printed (stdout and stderr). "
                   "Use print() to see results. Each call starts fresh: define everything you use.",
    "input_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "A complete Python program"}},
        "required": ["code"],
    },
}]


def run_python(code: str) -> str:
    """The tool itself. It runs here, in the sandbox: no access to anything outside it."""
    try:
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "Error: the program ran for more than 20 seconds and was stopped."
    return (done.stdout + done.stderr)[-4000:] or "(no output)"


def main(task: str) -> None:
    client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment
    messages = [{"role": "user", "content": task}]

    for turn in range(10):           # a budget: never let an agent loop forever
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            tools=TOOLS,
            messages=messages,
            # If the model declines a request for safety reasons, retry it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # Keep the whole reply (including any thinking blocks) in the history.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            print("The model declined this request.")
            return
        if response.stop_reason != "tool_use":
            print(next((b.text for b in response.content if b.type == "text"), ""))
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"--- turn {turn + 1}: the model runs ---\n{block.input['code']}\n")
                output = run_python(block.input["code"])
                print(f"--- output ---\n{output}\n")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})   # all results in one message

    print("Stopped after 10 turns.")


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]) or "What is the 30th Fibonacci number? Compute it, don't guess.")
