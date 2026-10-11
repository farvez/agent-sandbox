# Changelog

All notable changes to `airlock-sandbox` on npm, the TypeScript SDK for [Airlock](https://github.com/farvez/agent-sandbox).

## 0.4.0 — 2026-10-12

- Secrets: `Sandbox.create({ secrets })`, `sbx.setSecrets()`, `sbx.deleteSecret()` and
  `sbx.secretNames()`: environment variables for every command, never returned and masked in output.

## 0.3.0 — 2026-10-11

- Long-running sandboxes: `Sandbox.create({ persistent: true })` keeps one container for the whole
  session, so background processes keep running between commands.
- Previews: `sbx.expose(port)` returns a public HTTPS link to a server in a long-running sandbox;
  `sbx.unexpose(port)` closes it.

## 0.2.0 — 2026-10-11

- Binary files: `sbx.files.upload(path, data)` (`Uint8Array` or string), `sbx.files.download(path)`
  and `sbx.files.downloadArchive(path = ".")` (a folder as `.tar.gz` bytes).
- Snapshots: `sbx.snapshot({ name, path })`, `sbx.restore(id, { path })`, `sbx.listSnapshots()`,
  `sbx.deleteSnapshot(id)`, and `Sandbox.create({ snapshot: id })`.

## 0.1.0 — 2026-10-08

- First release: `Sandbox` (`create`, `attach`, `start`, `close`, `await using`), `run()` returning
  structured `CommandResult`s with `check()`, `files.read` / `files.write`, `importRepo`, egress
  requests and log, `usage`, `audit`.
- Typed errors with automatic retries on 429/503 (honouring `Retry-After`) and on dropped
  connections for reads.
- Agent tools: `openaiTools()`, `anthropicTools()` and `handleToolCall()`.
- Zero dependencies; Node 18+, Bun and Deno; ES modules and CommonJS.
