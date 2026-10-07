/** Errors thrown by the SDK. All extend SandboxError. */

export class SandboxError extends Error {
  /** HTTP status when the server answered, else undefined. */
  readonly status?: number;
  readonly detail: unknown;

  constructor(message: string, status?: number, detail?: unknown) {
    super(message);
    this.name = new.target.name;
    this.status = status;
    this.detail = detail ?? message;
  }
}

/** The server could not be reached (DNS, TLS, refused connection, timeout). */
export class APIConnectionError extends SandboxError {}
/** 401: missing or invalid API key. */
export class AuthenticationError extends SandboxError {}
/** 403: path outside the workspace, egress outside the tenant policy, or another tenant's session. */
export class PermissionDeniedError extends SandboxError {}
/** 404: the session doesn't exist or has expired. */
export class NotFoundError extends SandboxError {}
/** 413: the write would take the workspace past its disk quota. */
export class QuotaExceededError extends SandboxError {}
/** 400/422: the request was rejected as invalid. */
export class ValidationError extends SandboxError {}
/** 503: every workspace on the server is in use. */
export class CapacityError extends SandboxError {}
/** files.read() of a file that doesn't exist. */
export class FileNotFoundError extends SandboxError {}

/** 429: a per-tenant limit was hit. `retryAfter` is the server's suggested wait in seconds. */
export class RateLimitError extends SandboxError {
  readonly retryAfter?: number;

  constructor(message: string, detail?: unknown, retryAfter?: number) {
    super(message, 429, detail);
    this.retryAfter = retryAfter;
  }
}

/** Thrown by CommandResult.check() when a command failed, timed out or was killed. */
export class CommandError extends SandboxError {
  readonly result: unknown;

  constructor(message: string, result: unknown) {
    super(message);
    this.result = result;
  }
}

export const STATUS_ERRORS: Record<number, new (message: string, status?: number, detail?: unknown) => SandboxError> = {
  400: ValidationError,
  401: AuthenticationError,
  403: PermissionDeniedError,
  404: NotFoundError,
  413: QuotaExceededError,
  422: ValidationError,
  503: CapacityError,
};
