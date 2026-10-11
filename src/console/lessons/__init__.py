"""Learn track: hands-on lessons that each start a ready-made long-running workspace.

A lesson is metadata (below) plus the files in its folder, which are copied into the workspace
when it starts. Steps are shown on the lesson page and above the workspace's terminal.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class Step:
    text: str
    command: str = ""          # shown as a copyable command


@dataclass(frozen=True)
class Lesson:
    slug: str
    title: str
    summary: str
    minutes: int
    egress: Tuple[str, ...]
    steps: Tuple[Step, ...]
    secret: Optional[str] = None      # a secret the lesson needs, e.g. ANTHROPIC_API_KEY
    learn: Tuple[str, ...] = field(default_factory=tuple)

    def files(self) -> Dict[str, bytes]:
        """{relative path: content} of the lesson's files (no caches)."""
        root = os.path.join(HERE, self.slug)
        out: Dict[str, bytes] = {}
        for folder, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(names):
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    out[os.path.relpath(path, root).replace(os.sep, "/")] = f.read()
        return out


LESSONS: List[Lesson] = [
    Lesson(
        slug="01-sandbox-basics",
        title="What a sandbox is",
        summary="Run code in an isolated workspace, see what it can and can't reach, and keep a server running.",
        minutes=10,
        egress=("pypi",),
        learn=("Files persist; the machine is disposable", "Internet access is an allowlist", "Long-running processes and preview links"),
        steps=(
            Step("Run a script and look at where it runs: a container with its own user and folder.", "python hello.py"),
            Step("Files you create stay for the life of the workspace.", "echo 'saved' > note.txt && ls"),
            Step("Try the network. Only hosts on this workspace's list are reachable; the rest are refused and logged.",
                 "python try_network.py"),
            Step("Install a package (PyPI is on the list).", "pip install requests && python -c 'import requests; print(requests.__version__)'"),
            Step("Start a web server in the background. It keeps running after the command returns.",
                 "nohup python -m http.server 8000 > server.log 2>&1 &"),
            Step("Open it: under Preview a web app, enter port 8000 and open the link."),
        ),
    ),
    Lesson(
        slug="02-first-agent",
        title="Your first tool-calling agent",
        summary="Build the loop behind every AI agent: the model asks to run code, the sandbox runs it, the result goes back.",
        minutes=15,
        egress=("pypi", "llm"),
        secret="ANTHROPIC_API_KEY",
        learn=("Tools and the tool_use / tool_result loop", "Why the code should run in a sandbox", "Budgets: stop runaway loops"),
        steps=(
            Step("Save your Anthropic API key as a secret: under Secrets, name ANTHROPIC_API_KEY, paste the key. "
                 "It's passed to your commands but never shown again."),
            Step("Install the SDK.", "pip install anthropic"),
            Step("Run the agent. Watch it write a program, run it with the tool, and answer from the output.",
                 "python agent.py \"What is the 30th Fibonacci number? Compute it, don't guess.\""),
            Step("Read agent.py: the whole agent is the for-loop in main(). Try another question, e.g. how many "
                 "primes are below one million.", "python agent.py \"How many primes are below 1,000,000?\""),
            Step("Try a cheaper model by setting MODEL for one run.", "MODEL=claude-haiku-5-5 python agent.py \"What is 2**100?\""),
        ),
    ),
    Lesson(
        slug="03-coding-agent",
        title="A coding agent that fixes failing tests",
        summary="Give an agent file and test tools, point it at a buggy project, and let it work until the tests pass.",
        minutes=20,
        egress=("pypi", "llm"),
        secret="ANTHROPIC_API_KEY",
        learn=("Several tools, chosen by the model", "Keeping tools inside a folder", "Tests as the agent's goal"),
        steps=(
            Step("If this is a new workspace, save the ANTHROPIC_API_KEY secret again (secrets belong to one "
                 "workspace) and install the SDK.", "pip install anthropic"),
            Step("See the failing tests first: two functions in project/stats.py are wrong.",
                 "python -m unittest discover -s project"),
            Step("Let the agent fix them. It reads files, edits them and reruns the tests on its own.", "python coder.py"),
            Step("Check its work: rerun the tests, then read what it changed in project/stats.py.",
                 "python -m unittest discover -s project && cat project/stats.py"),
            Step("Save the result: Save a snapshot keeps the fixed project after this workspace closes."),
        ),
    ),
]

BY_SLUG = {lesson.slug: lesson for lesson in LESSONS}
