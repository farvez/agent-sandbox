import { CommandError, FileNotFoundError, SandboxError } from "./errors.js";
import { HttpClient } from "./http.js";
import { VERSION } from "./version.js";

export const DEFAULT_TEMPLATE = "sandbox-base:latest";

// `await using` needs Symbol.asyncDispose; older runtimes (Node 18, 20) don't define it yet.
(Symbol as { asyncDispose?: symbol }).asyncDispose ??= Symbol.for("Symbol.asyncDispose");

declare const process: { env: Record<string, string | undefined> } | undefined;

export interface SandboxOptions {
  /** Defaults to the SANDBOX_API_KEY environment variable. */
  apiKey?: string;
  /** Defaults to the SANDBOX_API_URL environment variable. */
  baseUrl?: string;
  /** Hosts or presets this session may reach over HTTPS, e.g. ["pypi"]. Empty = no network. */
  egress?: string[];
  template?: string;
  /** Per-request timeout in milliseconds (default 90 000). */
  timeoutMs?: number;
  /** Retries for 429, 503 and dropped connections (default 3). */
  maxRetries?: number;
  /** Custom fetch (tests, proxies). */
  fetch?: typeof fetch;
}

export interface RunOptions {
  /** Seconds, 1-60 (default 15). */
  timeout?: number;
}

/** What a command did. `output` is the server's text form, handy to hand to an LLM. */
export class CommandResult {
  constructor(
    readonly command: string,
    readonly stdout: string,
    readonly stderr: string,
    readonly exitCode: number,
    readonly timedOut = false,
    readonly oomKilled = false,
    readonly warnings: string[] = [],
    readonly output = "",
  ) {}

  get ok(): boolean {
    return this.exitCode === 0 && !this.timedOut && !this.oomKilled;
  }

  /** Throws CommandError unless the command succeeded; returns this so it can be chained. */
  check(): this {
    if (!this.ok) {
      const reason = this.timedOut ? "timed out" : this.oomKilled ? "was killed for using too much memory"
        : `exited with code ${this.exitCode}`;
      throw new CommandError(`Command ${JSON.stringify(this.command)} ${reason}.\n${this.output}`, this);
    }
    return this;
  }

  toString(): string {
    return this.output;
  }

  static fromResponse(data: any): CommandResult {
    return new CommandResult(
      data.command ?? "", data.stdout ?? "", data.stderr ?? "", data.exit_code ?? -1,
      Boolean(data.timed_out), Boolean(data.oom_killed), [...(data.warnings ?? [])], data.output ?? "",
    );
  }
}

/** File operations inside the sandbox workspace (paths are relative to /workspace). */
export class Files {
  constructor(private readonly sandbox: Sandbox) {}

  async write(path: string, content: string): Promise<void> {
    await this.sandbox.call("POST", "/write", { path, content });
  }

  /** Returns the file's text. Throws FileNotFoundError if it doesn't exist. */
  async read(path: string): Promise<string> {
    const data = await this.sandbox.call<{ content: string }>("GET", `/read?${new URLSearchParams({ path })}`);
    if (data.content === `Error: File '${path}' does not exist.`) throw new FileNotFoundError(`File not found: ${path}`);
    return data.content;
  }
}

export interface ImportResult {
  repo: string;
  ref: string;
  path: string;
  files: number;
  archive_bytes: number;
}

export interface AuditEntry {
  ts: number;
  session_id: string;
  actor: string;
  kind: "exec" | "import";
  command: string;
  exit_code: number | null;
  duration_s: number | null;
  timed_out: boolean;
  oom_killed: boolean;
  detail?: string | null;
}

/**
 * An isolated, network-controlled sandbox session.
 *
 * ```ts
 * await using sbx = await Sandbox.create({ egress: ["pypi"] });
 * (await sbx.run("pip install requests", { timeout: 60 })).check();
 * ```
 */
export class Sandbox {
  readonly files = new Files(this);
  readonly template: string;
  readonly requestedEgress: string[];
  sessionId?: string;
  tenantId?: string;
  egress: string[] = [];
  diskQuotaMb?: number;
  private readonly http: HttpClient;

  constructor(options: SandboxOptions = {}) {
    const env = typeof process !== "undefined" ? process.env : {};
    const baseUrl = options.baseUrl ?? env.SANDBOX_API_URL;
    const apiKey = options.apiKey ?? env.SANDBOX_API_KEY;
    if (!baseUrl || !apiKey) {
      throw new SandboxError("Set baseUrl and apiKey, or the SANDBOX_API_URL and SANDBOX_API_KEY environment variables.");
    }
    this.http = new HttpClient({
      baseUrl, apiKey, timeoutMs: options.timeoutMs, maxRetries: options.maxRetries, fetch: options.fetch,
      userAgent: `airlock-sandbox-js/${VERSION}`,
    });
    this.template = options.template ?? DEFAULT_TEMPLATE;
    this.requestedEgress = [...(options.egress ?? [])];
  }

  /** Creates and starts a sandbox. Remember to close() it, or use `await using`. */
  static async create(options: SandboxOptions = {}): Promise<Sandbox> {
    return new Sandbox(options).start();
  }

  /** A handle to an existing session (e.g. one created by another process), without creating one. */
  static attach(sessionId: string, options: SandboxOptions = {}): Sandbox {
    const sandbox = new Sandbox(options);
    sandbox.sessionId = sessionId;
    return sandbox;
  }

  get baseUrl(): string {
    return this.http.baseUrl;
  }

  // ------------------------------------------------------------------ lifecycle

  async start(): Promise<this> {
    if (this.sessionId) return this;
    const data = await this.http.request("POST", "/v1/sessions", { template: this.template, egress: this.requestedEgress });
    this.sessionId = data.session_id;
    this.tenantId = data.tenant_id;
    this.egress = data.egress ?? [];
    this.diskQuotaMb = data.disk_quota_mb;
    return this;
  }

  /** Deletes the session and its files. Safe to call more than once. */
  async close(options: { timeoutMs?: number } = {}): Promise<void> {
    if (!this.sessionId) return;
    const sessionId = this.sessionId;
    this.sessionId = undefined;
    try {
      await this.http.request("DELETE", `/v1/sessions/${sessionId}`, undefined,
        options.timeoutMs ? { timeoutMs: options.timeoutMs, retries: 0 } : {});
    } catch (err) {
      if (!(err instanceof SandboxError && err.status === 404)) throw err; // already gone (expired) is fine
    }
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  // ------------------------------------------------------------------ running code

  /** Runs a shell command in a fresh container (1-60 s timeout). Files persist between runs. */
  async run(command: string, options: RunOptions = {}): Promise<CommandResult> {
    const data = await this.call("POST", "/exec", { command, timeout_seconds: options.timeout ?? 15 });
    return CommandResult.fromResponse(data);
  }

  /** Imports a public GitHub repository ("owner/repo" or its URL) into /workspace/<path>. */
  async importRepo(repo: string, options: { ref?: string; path?: string } = {}): Promise<ImportResult> {
    return this.call<ImportResult>("POST", "/import", { repo, ref: options.ref ?? null, path: options.path ?? null },
      { timeoutMs: Math.max(this.http.timeoutMs, 240_000) });
  }

  /** Every outbound connection this session attempted, allowed or denied. */
  async egressLog(limit = 200): Promise<Record<string, unknown>[]> {
    return (await this.call<{ events: Record<string, unknown>[] }>("GET", `/egress?limit=${Math.trunc(limit)}`)).events;
  }

  // ------------------------------------------------------------------ account

  /** This tenant's limits and current usage. */
  async usage(): Promise<Record<string, any>> {
    return this.http.request("GET", "/v1/usage");
  }

  /** Hosts this tenant's sessions may request. */
  async egressPolicy(): Promise<string[]> {
    return (await this.http.request<{ allowed: string[] }>("GET", "/v1/egress/policy")).allowed;
  }

  /** This tenant's command history, newest first (never output). Pass `next` back as `before`. */
  async audit(options: { limit?: number; before?: string } = {}): Promise<{ entries: AuditEntry[]; next: string | null }> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 100) });
    if (options.before) query.set("before", options.before);
    return this.http.request("GET", `/v1/audit?${query}`);
  }

  async health(): Promise<{ status: string; active_sessions: number }> {
    return this.http.request("GET", "/healthz");
  }

  // ------------------------------------------------------------------ internals

  /** @internal */
  async call<T = any>(method: string, suffix: string, body?: unknown, options?: { timeoutMs?: number }): Promise<T> {
    if (!this.sessionId) throw new SandboxError("Sandbox not started: use `await Sandbox.create(...)` or call start().");
    return this.http.request<T>(method, `/v1/sessions/${this.sessionId}${suffix}`, body, options);
  }
}
