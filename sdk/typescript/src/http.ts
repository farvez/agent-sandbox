import { APIConnectionError, RateLimitError, SandboxError, STATUS_ERRORS } from "./errors.js";

const IDEMPOTENT = new Set(["GET", "DELETE"]);

export interface HttpOptions {
  baseUrl: string;
  apiKey: string;
  /** Per-request timeout in milliseconds (default 90 000). */
  timeoutMs?: number;
  /** Retries for 429, 503 and dropped connections on GET/DELETE (default 3). */
  maxRetries?: number;
  /** Longest wait between retries in milliseconds (default 30 000). */
  maxRetryWaitMs?: number;
  userAgent?: string;
  /** Custom fetch (tests, proxies). Defaults to the global fetch. */
  fetch?: typeof fetch;
}

export interface RequestOptions {
  timeoutMs?: number;
  retries?: number;
}

/** Small client on fetch, with the same retry rules as the Python SDK. */
export class HttpClient {
  readonly baseUrl: string;
  readonly timeoutMs: number;
  private readonly apiKey: string;
  private readonly maxRetries: number;
  private readonly maxRetryWaitMs: number;
  private readonly userAgent: string;
  private readonly fetchFn: typeof fetch;

  constructor(options: HttpOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.timeoutMs = options.timeoutMs ?? 90_000;
    this.maxRetries = options.maxRetries ?? 3;
    this.maxRetryWaitMs = options.maxRetryWaitMs ?? 30_000;
    this.userAgent = options.userAgent ?? "airlock-sandbox-js";
    const fetchFn = options.fetch ?? globalThis.fetch;
    if (!fetchFn) throw new SandboxError("No fetch available: use Node 18+ or pass options.fetch.");
    this.fetchFn = fetchFn;
  }

  async request<T = any>(method: string, path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.send<T>(method, path, body, options.timeoutMs);
      } catch (err) {
        const wait = this.retryWait(err, method, attempt, options.retries);
        if (wait === undefined) throw err;
        await new Promise((resolve) => setTimeout(resolve, wait));
      }
    }
  }

  /** Milliseconds to wait before retrying, or undefined to give up. */
  private retryWait(err: unknown, method: string, attempt: number, retries?: number): number | undefined {
    if (!(err instanceof SandboxError) || attempt >= (retries ?? this.maxRetries)) return undefined;
    const backoff = Math.min(1000 * 2 ** attempt, this.maxRetryWaitMs);
    if (err instanceof RateLimitError) {
      // Not processed, so safe to retry any method. Honour Retry-After when sent.
      const wait = err.retryAfter !== undefined ? err.retryAfter * 1000 : backoff;
      return wait <= this.maxRetryWaitMs ? wait : undefined;
    }
    if (err.status === 503) return backoff; // no free workspace yet; nothing was created
    if (err instanceof APIConnectionError && IDEMPOTENT.has(method)) return backoff;
    return undefined;
  }

  private async send<T>(method: string, path: string, body: unknown, timeoutMs?: number): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchFn(this.baseUrl + path, {
        method,
        headers: {
          "X-API-Key": this.apiKey,
          "Content-Type": "application/json",
          Accept: "application/json",
          "User-Agent": this.userAgent,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs ?? this.timeoutMs),
      });
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      throw new APIConnectionError(`Could not reach ${this.baseUrl}: ${reason}`);
    }
    const text = await response.text();
    if (!response.ok) throw this.httpError(response, text);
    return (text ? JSON.parse(text) : undefined) as T;
  }

  private httpError(response: Response, text: string): SandboxError {
    let detail: unknown = response.statusText;
    try {
      detail = JSON.parse(text).detail ?? detail;
    } catch {
      // not JSON: keep the status text
    }
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    if (response.status === 429) {
      const header = response.headers.get("Retry-After");
      return new RateLimitError(message, detail, header && /^\d+$/.test(header) ? Number(header) : undefined);
    }
    const ErrorClass = STATUS_ERRORS[response.status] ?? SandboxError;
    return new ErrorClass(message, response.status, detail);
  }
}
