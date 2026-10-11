"""Lesson 3: a coding agent that reads code, edits it and runs the tests until they pass.

Same loop as lesson 2, with three tools that only touch the project/ folder:
read_file, write_file and run_tests. The agent decides what to read, what to change and when
it's done; the sandbox makes it safe to let it.

    pip install anthropic
    python -m unittest discover -s project      # see the failures first
    python coder.py
    python -m unittest discover -s project      # and after

Needs the ANTHROPIC_API_KEY secret and the "llm" internet preset. MODEL picks another model.
"""
import os
import subprocess
import sys

import anthropic

MODEL = os.environ.get("MODEL", "claude-opus-5-5")
PROJECT = os.path.realpath("project")

TOOLS = [
    {"name": "read_file", "description": "Read a file in the project.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Replace a file in the project with new content.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "run_tests", "description": "Run the project's unit tests and return the report.",
     "input_schema": {"type": "object", "properties": {}}},
]


def inside_project(path: str) -> str:
    """Tools get a path from the model: keep it inside project/ (defence in depth, even in a sandbox)."""
    full = os.path.realpath(os.path.join(PROJECT, path))
    if os.path.commonpath([PROJECT, full]) != PROJECT:
        raise ValueError(f"{path} is outside the project")
    return full


def run_tool(name: str, args: dict) -> str:
    if name == "read_file":
        with open(inside_project(args["path"]), encoding="utf-8") as f:
            return f.read()
    if name == "write_file":
        with open(inside_project(args["path"]), "w", encoding="utf-8") as f:
            f.write(args["content"])
        return f"Wrote {args['path']}"
    if name == "run_tests":
        done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", PROJECT],
                              capture_output=True, text=True, timeout=60)
        return (done.stdout + done.stderr)[-6000:]
    raise ValueError(f"unknown tool {name}")


def main() -> None:
    client = anthropic.Anthropic()
    files = ", ".join(sorted(os.listdir(PROJECT)))
    messages = [{"role": "user", "content": (
        f"The project folder contains: {files}. Some unit tests fail. Run the tests, find the bugs in "
        "the code (not the tests), fix them, and run the tests again until they all pass. "
        "Then summarise what was wrong in two or three sentences.")}]

    for turn in range(15):
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, tools=TOOLS, messages=messages,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            print("The model declined this request.")
            return
        if response.stop_reason != "tool_use":
            print("\n" + next((b.text for b in response.content if b.type == "text"), ""))
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"[turn {turn + 1}] {block.name} {block.input.get('path', '')}")
            try:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": run_tool(block.name, block.input)})
            except Exception as e:   # tell the model what went wrong; it can try something else
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"Error: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})

    print("Stopped after 15 turns.")


if __name__ == "__main__":
    main()
