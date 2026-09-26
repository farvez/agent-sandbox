import os
import sys
import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from evals.benchmarks import ALL_BENCHMARKS
from evals.evaluator import BenchmarkEvaluator

from dotenv import load_dotenv
load_dotenv()

console = Console()


def main():
    console.print(
        Panel(
            "[bold white]AGENT-SANDBOX BENCHMARK & EVALUATION HARNESS[/bold white]\n"
            "[dim]Evaluating Functional Self-Healing + Security Isolation Guardrails[/dim]",
            border_style="cyan",
        )
    )

    if not os.getenv("OPENAI_API_KEY"):
        console.print("[red]Error: OPENAI_API_KEY environment variable is not set.[/red]")
        sys.exit(1)

    evaluator = BenchmarkEvaluator(model="gpt-4o-mini")
    reports = []
    start_total = time.time()

    for task in ALL_BENCHMARKS:
        console.print(f"Running task: [bold yellow]{task.task_id}[/bold yellow] ({task.task_type})...")
        start_task = time.time()
        report = evaluator.run_eval(task)
        elapsed = time.time() - start_task
        reports.append((report, elapsed))

    # Render results table
    table = Table(title="\nBenchmark Evaluation Results")
    table.add_column("Task ID", style="cyan", no_wrap=True)
    table.add_column("Type", style="magenta")
    table.add_column("Functional Test", style="bold")
    table.add_column("Security Isolation", style="bold")
    table.add_column("Time (s)", justify="right")
    table.add_column("Agent Summary", style="dim")

    passed_count = 0
    for report, elapsed in reports:
        func_status = "[green]PASSED[/green]" if report.passed else "[red]FAILED[/red]"
        sec_status = "[green]ISOLATED[/green]" if report.security_clean else "[red]BREACHED[/red]"

        if report.passed:
            passed_count += 1

        table.add_row(
            report.task_id,
            report.task_type,
            func_status,
            sec_status,
            f"{elapsed:.1f}s",
            report.summary.replace("\n", " "),
        )

    console.print(table)

    total_time = time.time() - start_total
    pass_rate = (passed_count / len(reports)) * 100
    console.print(
        Panel(
            f"[bold]Pass Rate: {pass_rate:.1f}% ({passed_count}/{len(reports)})[/bold]\n"
            f"[dim]Total Elapsed Time: {total_time:.1f}s[/dim]",
            border_style="green" if pass_rate == 100 else "yellow",
        )
    )


if __name__ == "__main__":
    main()