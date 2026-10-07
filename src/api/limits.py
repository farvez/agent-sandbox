"""Per-tenant limits: concurrent sessions, request rate, and concurrent commands.

Configured as JSON (SANDBOX_TENANT_LIMITS, or a file via SANDBOX_TENANT_LIMITS_FILE):

    {"*": {"max_sessions": 10, "requests_per_minute": 120, "max_concurrent_exec": 4},
     "acme": {"max_sessions": 50}}

"*" sets the defaults for every tenant; a tenant entry overrides individual fields.
Anything unspecified falls back to the built-in defaults below.
"""
import json
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from typing import Callable, Dict, Iterator, Optional

BUILTIN_DEFAULTS = {"max_sessions": 10, "requests_per_minute": 120, "max_concurrent_exec": 4}


@dataclass(frozen=True)
class TenantLimits:
    max_sessions: int = BUILTIN_DEFAULTS["max_sessions"]
    requests_per_minute: int = BUILTIN_DEFAULTS["requests_per_minute"]
    max_concurrent_exec: int = BUILTIN_DEFAULTS["max_concurrent_exec"]


class LimitExceeded(Exception):
    """A tenant hit one of its limits. `retry_after` is set for rate limiting."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


def load_tenant_limits(raw: Optional[str], path: Optional[str] = None) -> Dict[str, TenantLimits]:
    """Parses the limits config into {"*": defaults, tenant: limits, ...}."""
    if path and not raw:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    data = json.loads(raw) if raw and raw.strip() else {}
    if not isinstance(data, dict):
        raise RuntimeError('Tenant limits must be a JSON object like {"*": {"max_sessions": 10}}.')

    allowed = {f.name for f in fields(TenantLimits)}
    for tenant, values in data.items():
        if not isinstance(values, dict):
            raise RuntimeError(f"Tenant limits for '{tenant}' must be an object.")
        unknown = set(values) - allowed
        if unknown:
            raise RuntimeError(f"Unknown limit(s) for '{tenant}': {sorted(unknown)}. Known: {sorted(allowed)}.")
        for name, value in values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise RuntimeError(f"Limit '{name}' for '{tenant}' must be a positive integer.")

    defaults = replace(TenantLimits(), **data.get("*", {}))
    table = {"*": defaults}
    for tenant, values in data.items():
        if tenant != "*":
            table[tenant] = replace(defaults, **values)
    return table


class _TokenBucket:
    """Allows bursts up to `capacity`, refilling at `capacity` tokens per minute."""

    def __init__(self, capacity: int, now: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated = now

    def take(self, now: float) -> Optional[float]:
        """Takes a token; returns None on success, else seconds until one is available."""
        rate = self.capacity / 60.0
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return None
        return (1 - self.tokens) / rate


class LimitTracker:
    def __init__(self, limits: Dict[str, TenantLimits], clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, _TokenBucket] = {}
        self._sessions: Dict[str, int] = {}
        self._running: Dict[str, int] = {}

    def for_tenant(self, tenant_id: str) -> TenantLimits:
        return self.limits.get(tenant_id, self.limits.get("*", TenantLimits()))

    # ------------------------------------------------------------------ requests

    def check_rate(self, tenant_id: str) -> None:
        limit = self.for_tenant(tenant_id).requests_per_minute
        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None or bucket.capacity != limit:
                bucket = self._buckets[tenant_id] = _TokenBucket(limit, self.clock())
            wait = bucket.take(self.clock())
        if wait is not None:
            raise LimitExceeded(
                f"Rate limit of {limit} requests per minute exceeded; retry in {math.ceil(wait)} s.",
                retry_after=math.ceil(wait),
            )

    # ------------------------------------------------------------------ sessions

    def open_session(self, tenant_id: str) -> None:
        """Reserves a session slot; call close_session when the session ends or fails to start."""
        limit = self.for_tenant(tenant_id).max_sessions
        with self._lock:
            current = self._sessions.get(tenant_id, 0)
            if current >= limit:
                raise LimitExceeded(
                    f"Session limit reached: {current} of {limit} sessions are open. Delete one first."
                )
            self._sessions[tenant_id] = current + 1

    def restore_session(self, tenant_id: str) -> None:
        """Counts a session taken back after an API restart (it existed already, so no limit check)."""
        with self._lock:
            self._sessions[tenant_id] = self._sessions.get(tenant_id, 0) + 1

    def close_session(self, tenant_id: str) -> None:
        with self._lock:
            self._sessions[tenant_id] = max(0, self._sessions.get(tenant_id, 0) - 1)

    # ------------------------------------------------------------------ commands

    @contextmanager
    def running_command(self, tenant_id: str) -> Iterator[None]:
        limit = self.for_tenant(tenant_id).max_concurrent_exec
        with self._lock:
            current = self._running.get(tenant_id, 0)
            if current >= limit:
                raise LimitExceeded(
                    f"Too many commands running at once: {limit} allowed. Wait for one to finish."
                )
            self._running[tenant_id] = current + 1
        try:
            yield
        finally:
            with self._lock:
                self._running[tenant_id] = max(0, self._running.get(tenant_id, 0) - 1)

    # ------------------------------------------------------------------ reporting

    def usage(self, tenant_id: str) -> dict:
        limits = self.for_tenant(tenant_id)
        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is not None:
                now = self.clock()
                tokens = min(bucket.capacity, bucket.tokens + (now - bucket.updated) * bucket.capacity / 60.0)
            else:
                tokens = limits.requests_per_minute
            return {
                "limits": asdict(limits),
                "sessions_open": self._sessions.get(tenant_id, 0),
                "commands_running": self._running.get(tenant_id, 0),
                "requests_available": int(tokens),
            }
