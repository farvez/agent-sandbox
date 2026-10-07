/**
 * Ready-made tools that give an LLM agent a sandbox: write_file, read_file, run_command.
 *
 * ```ts
 * const response = await openai.chat.completions.create({ model, messages, tools: openaiTools() });
 * for (const call of response.choices[0].message.tool_calls ?? []) {
 *   const result = await handleToolCall(sbx, call.function.name, call.function.arguments);
 * }
 * ```
 */
import { FileNotFoundError, SandboxError } from "./errors.js";
import type { Sandbox } from "./sandbox.js";

export const MAX_TOOL_OUTPUT_CHARS = 8000;

interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export const TOOLS: readonly ToolSpec[] = [
  {
    name: "write_file",
    description: "Create or overwrite a text file in the sandbox workspace.",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Path relative to the workspace, e.g. 'src/main.py'" },
        content: { type: "string", description: "Full file content" },
      },
      required: ["path", "content"],
    },
  },
  {
    name: "read_file",
    description: "Read a text file from the sandbox workspace.",
    parameters: {
      type: "object",
      properties: { path: { type: "string", description: "Path relative to the workspace" } },
      required: ["path"],
    },
  },
  {
    name: "run_command",
    description:
      "Run a shell command in an isolated Linux sandbox (bash, Python 3.11). Files in the workspace " +
      "persist between commands; processes do not. Returns stdout, stderr and the exit code.",
    parameters: {
      type: "object",
      properties: {
        command: { type: "string", description: "Shell command, e.g. 'python3 -m pytest -q'" },
        timeout_seconds: { type: "integer", description: "1-60, default 15", minimum: 1, maximum: 60 },
      },
      required: ["command"],
    },
  },
];

/** Tool definitions for OpenAI Chat Completions (`tools:`). */
export function openaiTools() {
  return TOOLS.map((t) => ({ type: "function" as const, function: { ...t } }));
}

/** Tool definitions for the Anthropic Messages API (`tools:`). */
export function anthropicTools() {
  return TOOLS.map((t) => ({ name: t.name, description: t.description, input_schema: t.parameters }));
}

/** Keeps the head and the tail; errors and exit codes are usually at the end. */
export function truncate(text: string, limit = MAX_TOOL_OUTPUT_CHARS): string {
  if (text.length <= limit) return text;
  const head = Math.floor((limit * 2) / 5);
  const tail = limit - head;
  return `${text.slice(0, head)}\n\n... [TRUNCATED: ${text.length - head - tail} characters omitted] ...\n\n${text.slice(-tail)}`;
}

/** Runs one tool call against `sandbox` and returns text for the model. Never throws. */
export async function handleToolCall(
  sandbox: Sandbox,
  name: string,
  args: string | Record<string, unknown> | null | undefined,
): Promise<string> {
  let parsed: unknown;
  try {
    parsed = typeof args === "string" ? JSON.parse(args) : (args ?? {});
  } catch (err) {
    return `Error: tool arguments were not valid JSON: ${(err as Error).message}`;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return "Error: tool arguments must be a JSON object.";
  }
  const a = parsed as Record<string, unknown>;
  const need = (key: string): string => {
    if (typeof a[key] !== "string") throw new MissingArgument(key);
    return a[key] as string;
  };

  try {
    let result: string;
    if (name === "write_file") {
      const content = need("content");
      await sandbox.files.write(need("path"), content);
      result = `Wrote ${content.length} characters to ${a.path}`;
    } else if (name === "read_file") {
      result = await sandbox.files.read(need("path"));
    } else if (name === "run_command") {
      const timeout = Number(a.timeout_seconds ?? 15);
      result = (await sandbox.run(need("command"), { timeout: Number.isFinite(timeout) ? Math.trunc(timeout) : 15 })).output;
    } else {
      return `Error: unknown tool '${name}'. Available: ${TOOLS.map((t) => t.name).join(", ")}`;
    }
    return truncate(result);
  } catch (err) {
    if (err instanceof MissingArgument) return `Error: missing required argument '${err.message}' for ${name}.`;
    if (err instanceof FileNotFoundError) return `Error: file not found: ${a.path}`;
    if (err instanceof SandboxError) return `Error from sandbox (${err.name}): ${err.message}`;
    return `Error: ${name} failed (${(err as Error).name}): ${(err as Error).message}`; // never crash the agent loop
  }
}

class MissingArgument extends Error {}
