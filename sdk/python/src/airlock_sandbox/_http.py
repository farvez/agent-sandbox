"""Small HTTP client on the standard library, with retries for 429/503 and dropped connections."""
from __future__ import annotations

import json
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Union

from airlock_sandbox.errors import STATUS_ERRORS, APIConnectionError, RateLimitError, SandboxError

IDEMPOTENT = {"GET", "DELETE"}


class HTTPClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify: Union[bool, str] = True,
        timeout: float = 90,
        max_retries: int = 3,
        max_retry_wait: float = 30,
        user_agent: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.user_agent = user_agent or f"airlock-sandbox python/{platform.python_version()}"
        if verify is False:
            # For a server without a domain (self-signed certificate). Prefer passing the CA file.
            self._ssl: Optional[ssl.SSLContext] = ssl._create_unverified_context()
        elif isinstance(verify, str):
            self._ssl = ssl.create_default_context(cafile=verify)
        else:
            self._ssl = None

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        attempt = 0
        while True:
            try:
                return self._send(method, path, body)
            except SandboxError as err:
                wait = self._retry_wait(err, method, attempt)
                if wait is None:
                    raise
                attempt += 1
                time.sleep(wait)

    def _retry_wait(self, err: SandboxError, method: str, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None to give up."""
        if attempt >= self.max_retries:
            return None
        backoff = min(2 ** attempt, self.max_retry_wait)
        if isinstance(err, RateLimitError):
            # Not processed, so safe to retry any method. Honour Retry-After when sent.
            wait = err.retry_after if err.retry_after is not None else backoff
            return wait if wait <= self.max_retry_wait else None
        if err.status == 503:
            return backoff  # no free workspace yet; nothing was created
        if isinstance(err, APIConnectionError) and method in IDEMPOTENT:
            return backoff
        return None

    def _send(self, method: str, path: str, body: Optional[dict]) -> Any:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            raise self._http_error(err) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError) as err:
            reason = getattr(err, "reason", err)
            raise APIConnectionError(f"Could not reach {self.base_url}: {reason}") from None
        return json.loads(raw) if raw else None

    @staticmethod
    def _http_error(err: urllib.error.HTTPError) -> SandboxError:
        try:
            detail = json.loads(err.read().decode()).get("detail", err.reason)
        except Exception:
            detail = err.reason
        message = detail if isinstance(detail, str) else json.dumps(detail)
        cls = STATUS_ERRORS.get(err.code, SandboxError)
        if cls is RateLimitError:
            header = err.headers.get("Retry-After")
            return RateLimitError(message, detail=detail, retry_after=int(header) if header and header.isdigit() else None)
        return cls(message, status=err.code, detail=detail)
