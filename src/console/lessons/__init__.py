"""Learn track: hands-on lessons that each start a ready-made long-running workspace.

A lesson is metadata (below), the files in its folder (copied into the workspace when it starts),
and its notes: templates/lessons/<slug>.html, with the concepts and a code walkthrough that quotes
the lesson's own files through snippet(), so the explanation can't drift from the code.
"""
from __future__ import annotations

import os
import textwrap
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
    file_notes: Tuple[Tuple[str, str], ...] = ()   # (path, what it's for), in reading order
    exercises: Tuple[str, ...] = ()

    def text(self, path: str) -> str:
        return self.files()[path].decode("utf-8")

    def snippet(self, path: str, start: str, end: Optional[str] = None, after: int = 0) -> str:
        """The lines of a lesson file from the first one containing `start` through the next one
        containing `end` (or just that line), plus `after` more lines. Raises if the markers aren't found, so a test catches
        notes that no longer match the code."""
        lines = self.text(path).splitlines()
        first = next(i for i, line in enumerate(lines) if start in line)
        last = first if end is None else next(i for i in range(first, len(lines)) if end in lines[i])
        return textwrap.dedent("\n".join(lines[first:last + 1 + after]))

    def tree(self) -> List[Tuple[str, str, int]]:
        """(name, note, depth) rows for a folder tree of the lesson's files."""
        notes = dict(self.file_notes)
        rows, seen = [], set()
        for path in sorted(self.files()):
            parts = path.split("/")
            for depth in range(len(parts) - 1):
                folder = "/".join(parts[:depth + 1])
                if folder not in seen:
                    seen.add(folder)
                    rows.append((parts[depth] + "/", notes.get(folder + "/", ""), depth))
            rows.append((parts[-1], notes.get(path, ""), len(parts) - 1))
        return rows

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
        file_notes=(
            ("hello.py", "Prints where it runs: the machine, the Python version, the folder and the user."),
            ("try_network.py", "Tries to reach five places on the internet and reports which ones the sandbox allows."),
        ),
        exercises=(
            "Install something bigger: pip install numpy, then python -c 'import numpy; print(numpy.random.rand(3))'.",
            "Try a host that isn't on the list: curl -sS https://github.com (curl uses the proxy too). Then open the "
            "console's Activity page: the attempt is in the log.",
            "Write a file, close this workspace, start another: the file is gone. Do it again with Save a snapshot first, "
            "then start a workspace from the snapshot on the Workspaces page.",
            "Use more than the 256 MB of memory: python -c \"x = bytearray(400 * 1024 * 1024)\". What happens?",
        ),
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
        file_notes=(
            ("agent.py", "The whole agent: one tool (run_python), the function that runs it, and the loop that talks to the model."),
        ),
        exercises=(
            "Ask something that needs no code (\"What is the capital of France?\"). Does the model still call the tool?",
            "At the end of main(), print the messages list. Find the tool_use block and the tool_result that answers it.",
            "Ask for a program that never ends (\"print numbers forever\"). Which line stops it, and what does the model do next?",
            "Add a second tool, list_files, that returns os.listdir('.'). Add it to TOOLS, handle it in the loop, and ask "
            "\"Which files are in your folder?\"",
        ),
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
        file_notes=(
            ("coder.py", "The coding agent: three tools, a guard that keeps them inside project/, and the loop."),
            ("project/", "The code the agent works on. It only ever reads and writes here."),
            ("project/stats.py", "Four statistics functions. Two have bugs."),
            ("project/test_stats.py", "Unit tests that describe the correct behaviour: the agent's goal is to make them pass."),
        ),
        exercises=(
            "Break mode() in project/stats.py (return min(counts) instead), then run python coder.py again.",
            "Change the task: ask the agent to add a stdev() function and a test for it first, then the code (test-driven).",
            "Give it a list_files tool so it can discover the project instead of being told the file names.",
            "Remove the inside_project() check and ask the agent to read ../coder.py. Then put the check back: why is it "
            "worth having even inside a sandbox?",
        ),
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
