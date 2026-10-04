"""Live demo against a deployed agent-sandbox server.

  Act 1  Hello sandbox      create a session, write code, run it remotely
  Act 2  Agent at work      an LLM agent fixes a failing test suite on the hosted sandbox
  Act 3  Red team           real attacks against the sandbox, and what stopped each one
  Act 4  Egress gateway     pip install through the allowlist; everything else refused and logged

Needs SANDBOX_API_URL and SANDBOX_API_KEY (plus SANDBOX_API_INSECURE=1 for a
self-signed certificate). Act 2 also needs OPENAI_API_KEY; skip it with --skip-agent.

    python examples/demo_live.py
    python examples/demo_live.py --skip-agent
    python examples/demo_live.py --act 3

Act 4 needs the tenant's egress policy to include "pypi".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `src` and `evals` import

import argparse
import os
import time

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from airlock_sandbox import AuthenticationError, RateLimitError, Sandbox

load_dotenv()
console = Console()


def act_header(number: int, title: str, subtitle: str) -> None:
    console.print()
    console.print(Panel(f"[bold]{subtitle}[/bold]", title=f"[bold magenta]Act {number} · {title}[/bold magenta]",
                        border_style="magenta"))


# ---------------------------------------------------------------- Act 1

def act_hello() -> None:
    act_header(1, "Hello sandbox", "Untrusted code runs on the server, not on this laptop")
    with Sandbox() as sbx:
        console.print(f"Server  [cyan]{sbx.base_url}[/cyan]  ·  health {sbx.health()}")
        console.print(f"Session [cyan]{sbx.session_id}[/cyan] for tenant [cyan]{sbx.tenant_id}[/cyan]")

        code = "import platform, os\nprint('2**16 =', 2 ** 16)\nprint('running as uid', os.getuid(), 'on', platform.system())\n"
        console.print(Syntax(code, "python", theme="monokai", line_numbers=True))
        sbx.write_file("hello.py", code)

        started = time.perf_counter()
        out = sbx.run_command("python3 hello.py")
        console.print(Panel(out, title=f"exec · {time.perf_counter() - started:.2f}s round trip", border_style="green"))
        console.print("[dim]Fresh container per command · files persist in the session workspace · "
                      "session deleted on exit[/dim]")


# ---------------------------------------------------------------- Act 2

BROKEN_LIB = (
    "def fibonacci(n: int) -> int:\n"
    "    if n <= 0:\n"
    "        return 1          # bug: should be 0\n"
    "    if n == 1:\n"
    "        return 0          # bug: should be 1\n"
    "    return fibonacci(n - 1) - fibonacci(n - 2)   # bug: should be +\n"
)
TESTS = (
    "from math_lib import fibonacci\n\n"
    "for n, want in [(0, 0), (1, 1), (2, 1), (5, 5), (10, 55)]:\n"
    "    got = fibonacci(n)\n"
    "    assert got == want, f'fibonacci({n}) = {got}, expected {want}'\n"
    "print('ALL TESTS PASSED')\n"
)


def act_agent() -> None:
    act_header(2, "Agent at work", "An LLM agent fixes a failing test suite, executing only inside the hosted sandbox")
    if not os.getenv("OPENAI_API_KEY"):
        console.print("[yellow]OPENAI_API_KEY not set; skipping Act 2.[/yellow]")
        return

    from src.agent.agent import AutonomousCodingAgent

    with Sandbox() as sbx:
        sbx.write_file("math_lib.py", BROKEN_LIB)
        sbx.write_file("test_math.py", TESTS)
        console.print(Syntax(BROKEN_LIB, "python", theme="monokai", line_numbers=True))
        console.print(Panel(sbx.run_command("python3 test_math.py"), title="before", border_style="red"))

        def on_step(tool: str, args: str) -> None:
            color = {"read_file": "cyan", "write_file": "green"}.get(tool, "yellow")
            console.print(Panel(args, title=f"[bold {color}]agent → {tool}[/bold {color}]", border_style=color))

        summary = AutonomousCodingAgent(model=os.getenv("DEMO_MODEL", "gpt-4o-mini")).solve_task(
            workspace=sbx,
            task_instruction=(
                "`python3 test_math.py` fails. Inspect math_lib.py and test_math.py, fix math_lib.py, "
                "and re-run the tests until they pass. Do not modify test_math.py."
            ),
            max_iterations=8,
            on_step_callback=on_step,
        )

        console.print(Syntax(sbx.read_file("math_lib.py"), "python", theme="monokai", line_numbers=True))
        after = sbx.run_command("python3 test_math.py")
        console.print(Panel(after, title="after (verified by the demo, not the agent)",
                            border_style="green" if "[EXIT CODE]: 0" in after else "red"))
        console.print(Panel(summary, title="agent summary", border_style="blue"))


# ---------------------------------------------------------------- Act 3

# (attack, what it tries, command, timeout, how to tell it was blocked, control that stopped it)
SHELL_ATTACKS = [
    ("Exfiltrate data", "send workspace contents to an outside server",
     "python3 -c \"import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=3)\" 2>&1 | tail -1",
     15, lambda o: "unreachable" in o.lower() or "error" in o.lower(), "network_mode=none"),
    ("Steal cloud credentials", "read the server's AWS role from the metadata service",
     "python3 -c \"import urllib.request; print(urllib.request.urlopen('http://169.254.169.254/latest/meta-data/iam/', timeout=3).read())\" 2>&1 | tail -1",
     15, lambda o: "unreachable" in o.lower() or "error" in o.lower(), "network_mode=none"),
    ("Memory bomb", "allocate until the host runs out of RAM",
     "python3 -c \"b=[]\nwhile True: b.append(' '*10**7)\"",
     30, lambda o: "memory limit" in o.lower(), "256 MB cgroup, no swap"),
    # Hard-stopped by the kernel quota on the server ("Disk quota exceeded");
    # elsewhere the workspace reports going over its quota.
    ("Fill the disk", "write until the server's disk is full",
     # 2>&1 before > : the error goes to the output, the data to the file.
     "head -c 4G /dev/zero 2>&1 > fill.bin | tail -1; stat -c 'stopped at %s bytes' fill.bin; rm -f fill.bin",
     45, lambda o: "quota" in o.lower(), "per-workspace disk quota"),
    ("Infinite loop", "hold a CPU forever",
     "while true; do :; done",
     3, lambda o: "[TIMEOUT]" in o, "per-command timeout"),
    ("Tamper with the system", "overwrite system binaries or config",
     "echo pwned > /usr/bin/python3 2>&1; touch /etc/cron.d/backdoor 2>&1",
     15, lambda o: "[EXIT CODE]: 0" not in o, "read-only rootfs · non-root"),
    # GPG_KEY is excluded: it is the public fingerprint the official Python image
    # uses to verify its own download, not a secret.
    ("Look for secrets", "find API keys in the environment",
     "env | grep -v '^GPG_KEY=' | grep -iE 'key|token|secret|aws' || echo 'nothing found'",
     15, lambda o: "nothing found" in o, "clean container environment"),
    ("Identify the kernel", "probe the host kernel for exploits",
     "dmesg 2>&1 | head -1",
     15, lambda o: "gVisor" in o, "gVisor user-space kernel"),
]


def act_red_team() -> None:
    act_header(3, "Red team", "Hostile code tries to break out. Every attempt runs for real on the server.")
    table = Table(show_lines=False, header_style="bold")
    table.add_column("Attack")
    table.add_column("Tries to…", style="dim")
    table.add_column("Result")
    table.add_column("Stopped by", style="cyan")
    blocked = 0

    with Sandbox() as sbx:
        for name, intent, command, timeout, is_blocked, control in SHELL_ATTACKS:
            with console.status(f"{name}…"):
                out = sbx.run_command(command, timeout_seconds=timeout)
            ok = is_blocked(out)
            blocked += ok
            table.add_row(name, intent, "[green]BLOCKED[/green]" if ok else "[red]NOT BLOCKED[/red]", control)

        # Fork bomb: what matters is that the sandbox and the service survive it.
        # Under gVisor the 256 MB limit ends the sandbox after ~10-20 processes.
        with console.status("Fork bomb…"):
            sbx.run_command(":(){ :|:& };:", timeout_seconds=10)
            ok = "alive" in sbx.run_command("echo alive") and sbx.health()["status"] == "healthy"
        blocked += ok
        table.add_row("Fork bomb", "spawn processes until the host locks up",
                      "[green]CONTAINED[/green]" if ok else "[red]NOT CONTAINED[/red]", "memory + pids cgroups")

        # Attacks on the API boundary rather than inside the container
        with console.status("Path traversal…"):
            try:
                sbx.read_file("../../../../etc/passwd")
                ok = False
            except PermissionError:
                ok = True
        blocked += ok
        table.add_row("Path traversal", "read host files with ../../", "[green]BLOCKED[/green]" if ok else "[red]NOT BLOCKED[/red]",
                      "realpath + commonpath check")

        with console.status("Symlink escape…"):
            sbx.run_command("ln -sf /etc/passwd leak")
            try:
                content = sbx.read_file("leak")
                ok = "root:" not in content
            except PermissionError:
                ok = True
        blocked += ok
        table.add_row("Symlink escape", "plant a link to a host file, then read it", "[green]BLOCKED[/green]" if ok else "[red]NOT BLOCKED[/red]",
                      "symlinks resolved before access")

    # One tenant tries to take every sandbox on the server for itself.
    with console.status("Grabbing every sandbox…"):
        grabbed, ok = [], False
        try:
            for _ in range(200):
                grabbed.append(Sandbox(max_retries=0).start())   # no retries: we want to see the 429
        except RateLimitError:
            ok = True
        finally:
            for sbx in grabbed:
                sbx.close()
    blocked += ok
    table.add_row("Hog the server", "open sessions until no one else can get one",
                  f"[green]STOPPED at {len(grabbed)}[/green]" if ok else "[red]NOT STOPPED[/red]",
                  "per-tenant session limit")

    with console.status("Stolen / guessed key…"):
        try:
            Sandbox(api_key="guessed-key").start()
            ok = False
        except AuthenticationError:
            ok = True
    blocked += ok
    table.add_row("Guessed API key", "use the service without a valid key", "[green]BLOCKED[/green]" if ok else "[red]NOT BLOCKED[/red]",
                  "API key auth (constant time)")

    total = len(SHELL_ATTACKS) + 5
    console.print(table)
    style = "green" if blocked == total else "red"
    console.print(Panel(f"[bold]{blocked}/{total} attacks blocked[/bold]", border_style=style))


# ---------------------------------------------------------------- Act 4

def act_egress() -> None:
    act_header(4, "Egress gateway", "Internet access only to approved sites, with every connection logged")
    policy = Sandbox().egress_policy()   # tenant-level call; no session needed
    if "pypi.org" not in policy:
        console.print(f"[yellow]This tenant may reach {policy or 'nothing'}; Act 4 needs 'pypi' in its egress policy "
                      "(Terraform: -var 'egress_policy={default=[\"pypi\"]}').[/yellow]")
        return

    with Sandbox(egress=["pypi"]) as sbx:
        console.print(f"Session [cyan]{sbx.session_id}[/cyan] may reach: [cyan]{', '.join(sbx.egress)}[/cyan]")
        steps = [
            ("pip install from PyPI", "pip install --quiet requests==2.32.3 && python3 -c 'import requests; print(\"requests\", requests.__version__)'", 60),
            ("a site not on the list", "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\" 2>&1 | tail -1", 15),
            ("the cloud metadata service", "python3 -c \"import urllib.request; urllib.request.urlopen('https://169.254.169.254/latest/meta-data/', timeout=5)\" 2>&1 | tail -1", 15),
            ("going around the proxy", "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)\" 2>&1 | tail -1", 15),
        ]
        for label, command, timeout in steps:
            with console.status(f"{label}…"):
                out = sbx.run_command(command, timeout_seconds=timeout)
            console.print(Panel(out, title=label, border_style="green" if label.startswith("pip") else "yellow"))

        table = Table(title="Egress log for this session", header_style="bold")
        for column in ("Decision", "Host", "Reason", "Bytes in"):
            table.add_column(column)
        for e in sbx.egress_log():
            allowed = e["decision"] == "allow"
            table.add_row("[green]allow[/green]" if allowed else "[red]deny[/red]",
                          e.get("host") or e.get("target", ""), e.get("reason", ""),
                          f"{e.get('bytes_down', 0):,}" if allowed else "")
        console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--act", type=int, choices=[1, 2, 3, 4], help="run a single act")
    parser.add_argument("--skip-agent", action="store_true", help="skip Act 2 (no OpenAI calls)")
    args = parser.parse_args()

    for var in ("SANDBOX_API_URL", "SANDBOX_API_KEY"):
        if not os.getenv(var):
            console.print(f"[red]{var} is not set.[/red] See the README 'Deploy to AWS' section.")
            sys.exit(1)

    console.print(Panel("[bold white]AGENT-SANDBOX · LIVE DEMO[/bold white]\n"
                        "[dim]Hosted, isolated code execution for AI agents[/dim]", border_style="cyan"))
    acts = {1: act_hello, 2: act_agent, 3: act_red_team, 4: act_egress}
    for number in ([args.act] if args.act else [1, 2, 3, 4]):
        if number == 2 and args.skip_agent:
            continue
        acts[number]()


if __name__ == "__main__":
    main()
