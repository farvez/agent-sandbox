// Runs the built SDK against a real Airlock API server. Started by tests/test_sdk_typescript.py
// (pytest), which provides SANDBOX_API_URL and SANDBOX_API_KEY for a test server.
import assert from "node:assert/strict";
import { test } from "node:test";

import { FileNotFoundError, NotFoundError, PermissionDeniedError, Sandbox, handleToolCall } from "../dist/esm/index.js";

test("full lifecycle against the real API", async () => {
  const sbx = await Sandbox.create({ egress: ["pypi"] });
  try {
    assert.match(sbx.sessionId, /^sbx_/);
    assert.equal(sbx.tenantId, "acme");
    assert.deepEqual(sbx.egress, ["pypi.org", "files.pythonhosted.org"]);

    await sbx.files.write("hello.py", "print('hi')");
    assert.equal(await sbx.files.read("hello.py"), "print('hi')");
    await assert.rejects(sbx.files.read("missing.py"), FileNotFoundError);
    await assert.rejects(sbx.files.write("../escape.txt", "x"), PermissionDeniedError);

    const ran = await sbx.run("python3 hello.py");
    assert.ok(ran.ok);
    assert.match(ran.stdout, /ran python3 hello.py/);
    assert.match(await handleToolCall(sbx, "run_command", { command: "ls" }), /ran ls/);

    assert.ok((await sbx.usage()).limits.max_sessions >= 1);
    assert.deepEqual((await sbx.egressPolicy()).includes("pypi.org"), true);
    assert.equal((await sbx.health()).status, "healthy");
  } finally {
    await sbx.close();
  }
  await assert.rejects(Sandbox.attach(sbx.sessionId ?? "sbx_gone").run("ls"), (err) => err instanceof NotFoundError || /not started/.test(err.message));
});
