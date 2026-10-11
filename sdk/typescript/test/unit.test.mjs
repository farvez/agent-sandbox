// Offline tests against a fake server (a fetch stand-in). Run: npm test
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

import {
  APIConnectionError, AuthenticationError, CommandError, FileNotFoundError, NotFoundError, RateLimitError,
  Sandbox, VERSION, anthropicTools, handleToolCall, openaiTools, truncate,
} from "../dist/esm/index.js";

/** A scripted fake server: routes "METHOD /path" to handlers; records every call. */
function fakeServer(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    const { pathname, search } = new URL(url);
    const key = `${init.method} ${pathname}`;
    const sent = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ key, search, body: sent, headers: init.headers });
    const handler = routes[key];
    if (!handler) return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
    const out = await handler({ search, body: sent, calls });
    if (out instanceof Response) return out;
    if (out instanceof Error) throw out;
    const [status, body, headers] = Array.isArray(out) ? out : [200, out];
    return new Response(body === undefined ? "" : JSON.stringify(body), { status, headers });
  };
  return { fetch, calls };
}

const SESSION = { "POST /v1/sessions": () => [201, { session_id: "sbx_1", tenant_id: "acme", egress: ["pypi.org"], disk_quota_mb: 512 }] };
const opts = (server, extra = {}) => ({ baseUrl: "https://sandbox.test/", apiKey: "k", fetch: server.fetch, maxRetries: 3, ...extra });
const result = (over = {}) => ({ command: "x", stdout: "", stderr: "", exit_code: 0, timed_out: false, oom_killed: false, warnings: [], output: "[EXIT CODE]: 0", ...over });

test("create sends the template and egress, and records the session", async () => {
  const server = fakeServer(SESSION);
  const sbx = await Sandbox.create(opts(server, { egress: ["pypi"] }));
  assert.equal(sbx.sessionId, "sbx_1");
  assert.equal(sbx.tenantId, "acme");
  assert.deepEqual(sbx.egress, ["pypi.org"]);
  assert.equal(sbx.diskQuotaMb, 512);
  assert.deepEqual(server.calls[0].body, { template: "sandbox-base:latest", egress: ["pypi"] });
  assert.equal(server.calls[0].headers["X-API-Key"], "k");
  assert.equal(server.calls[0].headers["User-Agent"], `airlock-sandbox-js/${VERSION}`);
});

test("run returns a structured result; check() throws on failure", async () => {
  const server = fakeServer({
    ...SESSION,
    "POST /v1/sessions/sbx_1/exec": ({ body }) => result(body.command === "fail"
      ? { command: "fail", stderr: "boom\n", exit_code: 3, output: "[STDERR]:\nboom\n[EXIT CODE]: 3" }
      : { command: body.command, stdout: "hi\n", output: "[STDOUT]:\nhi\n[EXIT CODE]: 0" }),
  });
  const sbx = await Sandbox.create(opts(server));
  const ok = await sbx.run("echo hi", { timeout: 30 });
  assert.equal(ok.stdout, "hi\n");
  assert.ok(ok.ok);
  assert.equal(ok.check(), ok);
  assert.equal(String(ok), "[STDOUT]:\nhi\n[EXIT CODE]: 0");
  assert.equal(server.calls.at(-1).body.timeout_seconds, 30);
  const bad = await sbx.run("fail");
  assert.equal(bad.exitCode, 3);
  assert.throws(() => bad.check(), (err) => err instanceof CommandError && /exited with code 3/.test(err.message));
});

test("files: write, read, and a missing file", async () => {
  const stored = {};
  const server = fakeServer({
    ...SESSION,
    "POST /v1/sessions/sbx_1/write": ({ body }) => { stored[body.path] = body.content; return { status: "success" }; },
    "GET /v1/sessions/sbx_1/read": ({ search }) => {
      const path = new URLSearchParams(search).get("path");
      return { path, content: stored[path] ?? `Error: File '${path}' does not exist.` };
    },
  });
  const sbx = await Sandbox.create(opts(server));
  await sbx.files.write("src/a b.py", "print(1)");
  assert.equal(await sbx.files.read("src/a b.py"), "print(1)");
  await assert.rejects(sbx.files.read("nope.py"), FileNotFoundError);
});

test("429 is retried after Retry-After, then succeeds", async () => {
  let n = 0;
  const server = fakeServer({ ...SESSION, "GET /v1/usage": () => (++n < 3 ? [429, { detail: "slow down" }, { "Retry-After": "0" }] : { ok: true }) });
  const sbx = new Sandbox(opts(server));
  assert.deepEqual(await sbx.usage(), { ok: true });
  assert.equal(n, 3);
});

test("429 with a long Retry-After is not waited out", async () => {
  const server = fakeServer({ "GET /v1/usage": () => [429, { detail: "limit" }, { "Retry-After": "3600" }] });
  await assert.rejects(new Sandbox(opts(server)).usage(), (err) => err instanceof RateLimitError && err.retryAfter === 3600);
});

test("401 is not retried and becomes AuthenticationError", async () => {
  const server = fakeServer({ "GET /v1/usage": () => [401, { detail: "Invalid or missing X-API-Key header" }] });
  await assert.rejects(new Sandbox(opts(server)).usage(), (err) => err instanceof AuthenticationError && err.status === 401);
  assert.equal(server.calls.length, 1);
});

test("dropped connections are retried for GET but never for POST", async () => {
  let gets = 0;
  const server = fakeServer({
    "GET /v1/usage": () => (++gets < 2 ? new TypeError("socket hang up") : { ok: true }),
    "POST /v1/sessions": () => new TypeError("socket hang up"),
  });
  assert.deepEqual(await new Sandbox(opts(server)).usage(), { ok: true });
  await assert.rejects(Sandbox.create(opts(server)), APIConnectionError);
  assert.equal(server.calls.filter((c) => c.key === "POST /v1/sessions").length, 1);
});

test("close is idempotent and ignores an already-expired session", async () => {
  const server = fakeServer({ ...SESSION, "DELETE /v1/sessions/sbx_1": () => [404, { detail: "Session not found" }] });
  const sbx = await Sandbox.create(opts(server));
  await sbx.close();
  await sbx.close();
  assert.equal(server.calls.filter((c) => c.key.startsWith("DELETE")).length, 1);
});

test("Symbol.asyncDispose (what `await using` calls) closes the sandbox", async () => {
  const server = fakeServer({ ...SESSION, "DELETE /v1/sessions/sbx_1": () => ({ status: "terminated" }) });
  const sbx = await Sandbox.create(opts(server));
  await sbx[Symbol.asyncDispose]();
  assert.ok(server.calls.some((c) => c.key === "DELETE /v1/sessions/sbx_1"));
});

test("calls before start explain what to do; attach reuses a session", async () => {
  const server = fakeServer({ "POST /v1/sessions/sbx_9/exec": () => result({ command: "ls" }) });
  await assert.rejects(new Sandbox(opts(server)).run("ls"), /not started/);
  assert.equal((await Sandbox.attach("sbx_9", opts(server)).run("ls")).exitCode, 0);
});

test("importRepo, audit and egress endpoints", async () => {
  const server = fakeServer({
    ...SESSION,
    "POST /v1/sessions/sbx_1/import": ({ body }) => ({ repo: body.repo, ref: body.ref ?? "HEAD", path: `/workspace/${body.path ?? "r"}`, files: 5, archive_bytes: 1 }),
    "GET /v1/audit": ({ search }) => ({ entries: [{ command: "ls" }], next: new URLSearchParams(search).get("before") ? null : "cursor" }),
    "GET /v1/sessions/sbx_1/egress": () => ({ events: [{ decision: "allow", host: "pypi.org" }] }),
    "GET /v1/egress/policy": () => ({ allowed: ["pypi.org"] }),
  });
  const sbx = await Sandbox.create(opts(server));
  assert.equal((await sbx.importRepo("psf/requests", { ref: "main", path: "lib" })).path, "/workspace/lib");
  const page = await sbx.audit({ limit: 1 });
  assert.equal(page.next, "cursor");
  assert.equal((await sbx.audit({ before: page.next })).next, null);
  assert.deepEqual(await sbx.egressPolicy(), ["pypi.org"]);
  assert.equal((await sbx.egressLog(10))[0].host, "pypi.org");
});

test("errors carry the server's detail; 404 becomes NotFoundError", async () => {
  const server = fakeServer({ ...SESSION });
  const sbx = await Sandbox.create(opts(server));
  await assert.rejects(sbx.egressLog(), (err) => err instanceof NotFoundError && err.message === "Not Found");
});

test("reads SANDBOX_API_URL and SANDBOX_API_KEY when options are omitted", () => {
  process.env.SANDBOX_API_URL = "https://env.test";
  process.env.SANDBOX_API_KEY = "env-key";
  try {
    assert.equal(new Sandbox().baseUrl, "https://env.test");
  } finally {
    delete process.env.SANDBOX_API_URL;
    delete process.env.SANDBOX_API_KEY;
  }
  assert.throws(() => new Sandbox(), /SANDBOX_API_URL/);
});

// ---------------------------------------------------------------- agent tools

test("tool definitions for OpenAI and Anthropic", () => {
  assert.deepEqual(openaiTools().map((t) => t.function.name), ["write_file", "read_file", "run_command"]);
  assert.equal(openaiTools()[0].type, "function");
  assert.deepEqual(anthropicTools()[2].input_schema.required, ["command"]);
});

test("handleToolCall runs tools and turns every problem into text", async () => {
  const server = fakeServer({
    ...SESSION,
    "POST /v1/sessions/sbx_1/exec": ({ body }) => result({ output: `ran ${body.command} (${body.timeout_seconds}s)` }),
    "POST /v1/sessions/sbx_1/write": () => ({ status: "success" }),
    "GET /v1/sessions/sbx_1/read": () => ({ content: "Error: File 'x.py' does not exist." }),
  });
  const sbx = await Sandbox.create(opts(server));
  assert.equal(await handleToolCall(sbx, "run_command", '{"command": "ls", "timeout_seconds": 20}'), "ran ls (20s)");
  assert.equal(await handleToolCall(sbx, "write_file", { path: "a.py", content: "abc" }), "Wrote 3 characters to a.py");
  assert.equal(await handleToolCall(sbx, "read_file", { path: "x.py" }), "Error: file not found: x.py");
  assert.match(await handleToolCall(sbx, "run_command", "{not json"), /not valid JSON/);
  assert.match(await handleToolCall(sbx, "run_command", "[1]"), /must be a JSON object/);
  assert.match(await handleToolCall(sbx, "run_command", {}), /missing required argument 'command'/);
  assert.match(await handleToolCall(sbx, "delete_everything", {}), /unknown tool/);
});

test("truncate keeps the head and the tail", () => {
  const text = "a".repeat(10_000) + "END";
  const out = truncate(text, 1000);
  assert.ok(out.length < 1100 && out.startsWith("a") && out.endsWith("END") && out.includes("TRUNCATED"));
  assert.equal(truncate("short", 1000), "short");
});

test("the CommonJS build loads with require()", () => {
  const require = createRequire(import.meta.url);
  const cjs = require("../dist/cjs/index.js");
  assert.equal(typeof cjs.Sandbox, "function");
  assert.equal(cjs.VERSION, VERSION);
});

test("files upload and download raw bytes", async () => {
  let stored;
  const server = fakeServer({
    ...SESSION,
    "PUT /v1/sessions/sbx_1/files": ({ body }) => { stored = body; return { path: "a.bin", size_bytes: body.length }; },
    "GET /v1/sessions/sbx_1/files": ({ search }) => search.includes("missing")
      ? [404, { detail: "'missing' doesn't exist." }] : new Response(stored, { status: 200 }),
    "GET /v1/sessions/sbx_1/archive": () => new Response(new Uint8Array([31, 139]), { status: 200 }),
  });
  const sbx = await Sandbox.create(opts(server));
  assert.equal(await sbx.files.upload("dir/a.bin", new Uint8Array([0, 255, 7])), 3);
  const put = server.calls.at(-1);
  assert.equal(put.search, "?path=dir%2Fa.bin");
  assert.equal(put.headers["Content-Type"], "application/octet-stream");
  assert.deepEqual([...(await sbx.files.download("dir/a.bin"))], [0, 255, 7]);
  assert.deepEqual([...(await sbx.files.downloadArchive())], [31, 139]);
  assert.equal(await sbx.files.upload("t.txt", "hé"), 3);
  await assert.rejects(sbx.files.download("missing"), NotFoundError);
});

test("snapshots: save, list, restore, start from one, delete", async () => {
  const snap = { id: "snap_0123456789ab", name: "v1", size_bytes: 10, session_id: "sbx_1", created_at: 1, expires_at: 2 };
  const server = fakeServer({
    ...SESSION,
    "POST /v1/sessions/sbx_1/snapshots": () => [201, snap],
    "POST /v1/sessions/sbx_1/restore": ({ body }) => ({ restored: snap, path: body.path }),
    "GET /v1/snapshots": () => ({ snapshots: [snap] }),
    [`DELETE /v1/snapshots/${snap.id}`]: () => ({ deleted: snap }),
  });
  const sbx = await Sandbox.create(opts(server));
  assert.deepEqual(await sbx.snapshot({ name: "v1" }), snap);
  assert.deepEqual(server.calls.at(-1).body, { name: "v1", path: "." });
  assert.deepEqual(await sbx.listSnapshots(), [snap]);
  assert.equal((await sbx.restore(snap.id, { path: "old" })).path, "old");
  await sbx.deleteSnapshot(snap.id);
  await Sandbox.create(opts(server, { snapshot: snap.id }));
  assert.equal(server.calls.at(-1).body.snapshot_id, snap.id);
});
test("persistent sandboxes and previews", async () => {
  const server = fakeServer({
    "POST /v1/sessions": ({ body }) => [201, { session_id: "sbx_1", tenant_id: "acme", egress: [], disk_quota_mb: 512, persistent: Boolean(body.persistent) }],
    "POST /v1/sessions/sbx_1/previews": ({ body }) => ({ port: body.port, url: `https://${body.port}-0123456789abcdef.preview.test` }),
    "DELETE /v1/sessions/sbx_1/previews/8000": () => ({ port: 8000, status: "closed" }),
  });
  const sbx = await Sandbox.create(opts(server, { persistent: true }));
  assert.equal(server.calls[0].body.persistent, true);
  assert.equal(sbx.persistent, true);
  assert.equal(await sbx.expose(8000), "https://8000-0123456789abcdef.preview.test");
  await sbx.unexpose(8000);
  const regular = await Sandbox.create(opts(server));
  assert.equal("persistent" in server.calls.at(-1).body, false);
  assert.equal(regular.persistent, false);
});
