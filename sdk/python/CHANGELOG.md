# Changelog

All notable changes to `airlock-sandbox`, the Python SDK for [Airlock](https://github.com/farvez/agent-sandbox).

## 0.5.0 — 2026-10-08

- `Sandbox.audit(limit=100, before=None)`: this tenant's command history (commands and outcomes,
  never output), newest first, with a cursor for older pages.
- MCP server: new `activity` tool.
- Docs: Claude Code can now connect to the server's **remote MCP endpoint** with just a URL and
  key (`claude mcp add --transport http …`), with nothing to install. The local
  `airlock-sandbox-mcp` keeps working; see the README for the startup-timeout workaround on Windows.

## 0.4.1 — 2026-10-04

- Package metadata: author is now listed as Farvez Anzam. No code changes; still MIT.

## 0.4.0 — 2026-10-04

- `Sandbox.import_repo(repo, ref=None, path=None)`: import a public GitHub repository
  (`"owner/repo"` or its URL) into the sandbox workspace. The server downloads it, so the
  session needs no internet access.
- MCP server: new `import_repo` tool.
- Needs a server with `POST /v1/sessions/{id}/import`.

## 0.3.0 — 2026-10-04

- `airlock_sandbox.keys.Keys` and the `airlock-sandbox-keys` CLI: create, list and revoke
  self-service API keys — tenants rotate their own keys, admins (`--admin`) manage any tenant's.

## 0.2.1 — 2026-10-04

- Docs: correct argument order for `claude mcp add` (the server name comes right after `add`).

## 0.2.0 — 2026-10-03

- `airlock-sandbox-mcp` (install with the `[mcp]` extra): an MCP server that gives Claude Code,
  Claude Desktop and other MCP clients a sandbox as tools — `run_command`, `write_file`,
  `read_file`, `list_files`, `egress_log`, `sandbox_info`, `reset_sandbox`. One session per
  server process, cleaned up on disconnect and on the next start after a crash.

## 0.1.1 — 2026-10-03

- Re-release of 0.1.0 with a corrected version number; no code changes.

## 0.1.0 — 2026-10-03

- First release: `Sandbox` (sessions as a context manager), `run()` returning structured
  `CommandResult`s, `files.read` / `files.write`, egress requests and the egress log, usage.
- Typed errors (`RateLimitError`, `QuotaExceededError`, `PermissionDeniedError`, …) with
  automatic retries on 429/503 honouring `Retry-After`.
- Agent tools: `openai_tools()`, `anthropic_tools()` and `handle_tool_call()`.
- Standard library only; Python 3.9+.
