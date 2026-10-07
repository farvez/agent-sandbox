// Compile-only check of the public types, as a TypeScript user would write it.
import { CommandResult, RateLimitError, Sandbox, anthropicTools, handleToolCall, openaiTools } from "airlock-sandbox";

export async function example(): Promise<string> {
  await using sbx = await Sandbox.create({ egress: ["pypi"] });
  const result: CommandResult = (await sbx.run("python3 -V", { timeout: 30 })).check();
  const repo = await sbx.importRepo("psf/requests", { ref: "main" });
  const page = await sbx.audit({ limit: 5 });
  const first: number | null = page.entries[0]?.exit_code ?? null;
  const tools = [openaiTools()[0].function.name, anthropicTools()[0].input_schema];
  try {
    await sbx.usage();
  } catch (err) {
    if (err instanceof RateLimitError) return `retry in ${err.retryAfter}`;
  }
  return `${result.stdout} ${repo.files} ${first} ${tools.length} ${await handleToolCall(sbx, "run_command", { command: "ls" })}`;
}
