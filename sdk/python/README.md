# airlock-sandbox

Python SDK for [agent-sandbox](https://github.com/farvez/agent-sandbox): run code from
AI agents in isolated gVisor containers — no network unless you allow it, hard memory,
CPU, process and disk limits, and every outbound connection logged.

```bash
pip install airlock-sandbox
```

No dependencies; Python 3.9+.

## Quick start

```python
from airlock_sandbox import Sandbox

with Sandbox(api_key="...", base_url="https://sandbox.example.com") as sbx:
    sbx.files.write("main.py", "print(2 ** 16)")
    result = sbx.run("python3 main.py")
    print(result.stdout)      # 65536
    print(result.exit_code)   # 0
```

Leaving the `with` block deletes the session and its files. Settings can come from the
environment instead: `SANDBOX_API_URL`, `SANDBOX_API_KEY`, and `SANDBOX_API_INSECURE=1`
for a server with a self-signed certificate (or pass `verify="/path/to/ca.pem"`).

## Running commands

```python
result = sbx.run("python3 -m pytest -q", timeout=60)   # 1–60 s
result.stdout, result.stderr, result.exit_code
result.ok            # exit code 0, not timed out, not killed
result.timed_out     # hit the timeout
result.oom_killed    # killed for exceeding the memory limit
result.warnings      # e.g. over the disk quota
result.check()       # raises CommandError unless ok; returns the result
str(result)          # text form with [STDOUT]/[STDERR]/[EXIT CODE] — handy for an LLM
```

Each command runs in a fresh container; files in the workspace persist between
commands, processes do not.

## Internet access (egress)

Sandboxes have no network by default. Ask for specific hosts — they must be allowed by
your tenant's policy on the server:

```python
with Sandbox(egress=["pypi"]) as sbx:              # presets: pypi, npm, github, huggingface
    sbx.run("pip install requests", timeout=60).check()
    for event in sbx.egress_log():
        print(event["decision"], event["host"], event.get("reason", ""))
```

Only HTTPS to the listed hosts works; everything else is refused and logged.
`sbx.egress_policy()` lists what your tenant may request.

## Use as tools for an LLM agent

```python
from airlock_sandbox import Sandbox
from airlock_sandbox.tools import openai_tools, anthropic_tools, handle_tool_call

# OpenAI
response = client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=openai_tools())
for call in response.choices[0].message.tool_calls or []:
    output = handle_tool_call(sbx, call.function.name, call.function.arguments)

# Anthropic
response = client.messages.create(model="claude-sonnet-5", max_tokens=2048, messages=messages, tools=anthropic_tools())
for block in response.content:
    if block.type == "tool_use":
        output = handle_tool_call(sbx, block.name, block.input)
```

Tools: `write_file`, `read_file`, `run_command`. `handle_tool_call` never raises — errors
come back as text the model can act on — and long output is trimmed to 8,000 characters,
keeping the beginning and the end.

## MCP server (Claude Code, Claude Desktop, other MCP clients)

The package includes an MCP server that gives any MCP client a sandbox as tools — no code
needed. Install with the `mcp` extra (Python 3.10+):

```bash
pip install "airlock-sandbox[mcp]"
```

**Claude Code:**

```bash
claude mcp add airlock --scope user -e SANDBOX_API_URL=https://<server> -e SANDBOX_API_KEY=<key> -e AIRLOCK_EGRESS=pypi -- airlock-sandbox-mcp
```

Add `-e SANDBOX_API_INSECURE=1` if the server uses a self-signed certificate. With
[uv](https://docs.astral.sh/uv/) you can skip the install and use
`-- uvx --from "airlock-sandbox[mcp]" airlock-sandbox-mcp` as the command instead.
Put the server name (`airlock`) right after `add`: `-e` takes several values and would
otherwise swallow the name. If `uvx` reports an old version, run it once with `--refresh`.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "airlock": {
      "command": "airlock-sandbox-mcp",
      "env": {
        "SANDBOX_API_URL": "https://<server>",
        "SANDBOX_API_KEY": "<key>",
        "AIRLOCK_EGRESS": "pypi"
      }
    }
  }
}
```

On Windows, if Claude Desktop can't find the command, use its full path (`where airlock-sandbox-mcp`).

**Tools:** `run_command`, `write_file`, `read_file`, `list_files`, `egress_log`,
`sandbox_info`, `reset_sandbox`. Each MCP server process gets one sandbox session, created
on first use and deleted when the client disconnects; files persist between commands
within it. `AIRLOCK_EGRESS` (or `--egress`) sets which hosts the sandbox may reach — it
must be allowed by your tenant's policy; leave it unset for no network.

## Errors

All errors inherit from `SandboxError`:

| Exception | When |
|-----------|------|
| `AuthenticationError` | Missing or invalid API key (401) |
| `PermissionDeniedError` | Path outside the workspace, egress outside your policy, someone else's session (403) |
| `NotFoundError` | Session doesn't exist or expired (404) |
| `QuotaExceededError` | Write would exceed the workspace disk quota (413) |
| `RateLimitError` | Tenant limit hit: sessions, requests/minute or concurrent commands (429); see `.retry_after` |
| `CapacityError` | Every workspace on the server is in use (503) |
| `ValidationError` | Invalid request, e.g. unknown template (400/422) |
| `APIConnectionError` | Server unreachable |
| `CommandError` | Raised by `CommandResult.check()` |

Rate limits (429) and a full server (503) are retried automatically, honouring the
server's `Retry-After` (`max_retries=3` by default).

## Account

```python
sbx.usage()    # {"limits": {...}, "sessions_open": 1, "commands_running": 0, "requests_available": 118}
sbx.health()
```

## License

MIT
