# airlock-sandbox (TypeScript)

TypeScript / JavaScript SDK for [Airlock](https://github.com/farvez/agent-sandbox): isolated,
network-controlled sandboxes for AI coding agents. Every command runs in a throwaway gVisor
container with no network by default, hard memory/CPU/process limits and a disk quota, while
files persist in the session's workspace.

Zero dependencies; uses the built-in `fetch`. Node 18+, Bun and Deno. ES modules and CommonJS.

```bash
npm install airlock-sandbox
```

## Quick start

Get an API key from the [console](https://airlock.complyoo.com/console), then:

```ts
import { Sandbox } from "airlock-sandbox";

await using sbx = await Sandbox.create({
  baseUrl: "https://airlock.complyoo.com",
  apiKey: process.env.AIRLOCK_KEY,
  egress: ["pypi"],                       // internet only to PyPI; omit for no network
});

await sbx.importRepo("pallets/itsdangerous");
(await sbx.run("pip install pytest freezegun", { timeout: 60 })).check();
const tests = await sbx.run("cd itsdangerous && PYTHONPATH=src python -m pytest -q", { timeout: 60 });
console.log(tests.stdout);                 // "297 passed in 1.04s"
```

`await using` (TypeScript 5.2+) closes the sandbox and deletes its files at the end of the block.
Without it, call `await sbx.close()` yourself. `baseUrl` and `apiKey` default to the
`SANDBOX_API_URL` and `SANDBOX_API_KEY` environment variables.

## Running commands

```ts
const result = await sbx.run("python3 -m pytest -q", { timeout: 60 });   // 1-60 s
result.stdout; result.stderr; result.exitCode; result.ok; result.timedOut; result.oomKilled
result.check();          // throws CommandError unless it succeeded; returns result
String(result);          // text form ([STDOUT]/[STDERR]/[EXIT CODE]), handy for an LLM
```

Each command runs in a fresh container; files in `/workspace` persist between commands,
processes don't.

## Files and repositories

```ts
await sbx.files.write("src/main.py", "print('hi')");
await sbx.files.read("src/main.py");                 // throws FileNotFoundError if missing
await sbx.importRepo("psf/requests", { ref: "v2.32.3", path: "lib" });   // public GitHub repos
```

## Give an LLM agent a sandbox

```ts
import OpenAI from "openai";
import { Sandbox, openaiTools, handleToolCall } from "airlock-sandbox";

const openai = new OpenAI();
await using sbx = await Sandbox.create();
const messages = [{ role: "user", content: "Write fizzbuzz.py and run it." }];

while (true) {
  const reply = (await openai.chat.completions.create({ model: "gpt-4o-mini", messages, tools: openaiTools() })).choices[0].message;
  messages.push(reply);
  if (!reply.tool_calls?.length) break;
  for (const call of reply.tool_calls) {
    const content = await handleToolCall(sbx, call.function.name, call.function.arguments);
    messages.push({ role: "tool", tool_call_id: call.id, content });
  }
}
```

`anthropicTools()` gives the same tools (`write_file`, `read_file`, `run_command`) in the
Anthropic Messages API format. `handleToolCall` never throws: problems come back as text for
the model, and long output is cut to 8,000 characters (keeping the head and the tail).

## Errors and retries

Every error extends `SandboxError` (with `status` and `detail`): `AuthenticationError` (401),
`PermissionDeniedError` (403), `NotFoundError` (404), `QuotaExceededError` (413),
`ValidationError` (400/422), `RateLimitError` (429, with `retryAfter`), `CapacityError` (503),
`APIConnectionError`, `CommandError` and `FileNotFoundError`.

429 and 503 are retried automatically (honouring `Retry-After`), and dropped connections are
retried for reads. Tune with `maxRetries` and `timeoutMs` in the options.

## More

```ts
await sbx.egressLog();        // outbound connections this session attempted, allowed or denied
await sbx.egressPolicy();     // hosts your tenant may request
await sbx.usage();            // limits and current usage
await sbx.audit({ limit: 50 }); // command history (never output); pass `next` back as `before`
Sandbox.attach("sbx_...");    // a handle to an existing session
```

For Claude Code and other MCP clients, use the server's remote MCP endpoint; no SDK needed.
See the [main README](https://github.com/farvez/agent-sandbox#readme).

## License

MIT © 2026 Farvez Anzam. The Airlock server is AGPL-3.0; see the
[main repository](https://github.com/farvez/agent-sandbox).
