import os
import sys
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from src.step5_agent.sandbox import SandboxedWorkspace
from src.step5_agent.agent import AutonomousCodingAgent

console = Console()


def log_tool_event(tool_name: str, payload: str):
    """Prints a styled terminal card whenever the agent invokes a sandbox tool."""
    color = "cyan" if "read" in tool_name else ("green" if "write" in tool_name else "yellow")
    console.print(
        Panel(
            payload,
            title=f"[bold {color}]Agent Invoked: {tool_name}[/bold {color}]",
            border_style=color,
        )
    )


def run_demo():
    console.print(
        Panel(
            "[bold white]AUTONOMOUS CODING AGENT SELF-HEALING DEMO[/bold white]\n"
            "[dim]Running inside isolated Docker sandbox (cgroups + network: none)[/dim]",
            border_style="magenta",
        )
    )

    if not os.getenv("OPENAI_API_KEY"):
        console.print(
            "[bold red]Error: OPENAI_API_KEY environment variable is not set.[/bold red]\n"
            "Run: [yellow]set OPENAI_API_KEY=sk-...[/yellow] (in PowerShell / Command Prompt)"
        )
        sys.exit(1)

    # 1. Initialize an ephemeral isolated workspace
    with SandboxedWorkspace() as workspace:
        console.print(f"[bold blue]Step 1:[/bold blue] Ephemeral workspace mounted at: {workspace.workspace_dir}")

        # 2. Plant intentional bugs in the workspace
        broken_code = (
            "def fibonacci(n: int) -> int:\n"
            "    # BUG 1: Wrong base cases\n"
            "    if n <= 0:\n"
            "        return 1\n"
            "    if n == 1:\n"
            "        return 0\n"
            "    # BUG 2: Incorrect recursive logic\n"
            "    return fibonacci(n - 1) - fibonacci(n - 2)\n"
        )

        test_code = (
            "from math_lib import fibonacci\n"
            "import sys\n\n"
            "def test():\n"
            "    assert fibonacci(0) == 0, f'Expected 0, got {fibonacci(0)}'\n"
            "    assert fibonacci(1) == 1, f'Expected 1, got {fibonacci(1)}'\n"
            "    assert fibonacci(2) == 1, f'Expected 1, got {fibonacci(2)}'\n"
            "    assert fibonacci(5) == 5, f'Expected 5, got {fibonacci(5)}'\n"
            "    assert fibonacci(7) == 13, f'Expected 13, got {fibonacci(7)}'\n"
            "    print('ALL 5 TESTS PASSED SUCCESSFULLY!')\n\n"
            "if __name__ == '__main__':\n"
            "    test()\n"
        )

        workspace.write_file("math_lib.py", broken_code)
        workspace.write_file("test_math.py", test_code)

        console.print("[bold blue]Step 2:[/bold blue] Planted broken [yellow]math_lib.py[/yellow] and [yellow]test_math.py[/yellow].")

        # 3. Hand over control to the autonomous agent
        task = (
            "The file test_math.py contains unit tests for math_lib.py, but running "
            "'python3 test_math.py' fails. Inspect the files, run the test script inside the sandbox, "
            "fix the logic in math_lib.py, and verify until 'python3 test_math.py' exits cleanly with exit code 0."
        )

        agent = AutonomousCodingAgent(model="gpt-4o-mini")
        console.print("\n[bold magenta]Starting Agent Autonomous Loop...[/bold magenta]\n")

        final_summary = agent.solve_task(
            workspace=workspace,
            task_instruction=task,
            max_iterations=6,
            on_step_callback=log_tool_event,
        )

        # 4. Show the patched file
        fixed_code = workspace.read_file("math_lib.py")
        console.print("\n[bold green]Patched math_lib.py Result:[/bold green]")
        console.print(Syntax(fixed_code, "python", theme="monokai", line_numbers=True))

        console.print(
            Panel(
                final_summary,
                title="[bold green]Agent Solution Summary[/bold green]",
                border_style="green",
            )
        )

    console.print("\n[bold blue]Step 5:[/bold blue] Ephemeral workspace and container wiped cleanly.")


if __name__ == "__main__":
    run_demo()