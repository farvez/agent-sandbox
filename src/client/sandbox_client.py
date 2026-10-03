"""Minimal Python client for a deployed agent-sandbox API.

    with RemoteSandbox(base_url="https://...", api_key="...") as sbx:
        sbx.write_file("main.py", "print(2 ** 16)")
        print(sbx.run_command("python3 main.py"))

    with RemoteSandbox(egress=["pypi"]) as sbx:     # HTTPS to PyPI only, every connection logged
        sbx.run_command("pip install requests", timeout_seconds=60)
        print(sbx.egress_log())

It exposes the same read_file / write_file / run_command methods as the local
SandboxedWorkspace, so AutonomousCodingAgent can use a hosted sandbox unchanged.
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional


class SandboxAPIError(RuntimeError):
    def __init__(self, status: int, detail: str, retry_after: Optional[int] = None):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        # Seconds to wait before retrying, when the server sent Retry-After (rate limit).
        self.retry_after = retry_after


class RemoteSandbox:
    """One sandbox session on a remote agent-sandbox server."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        template: str = "sandbox-base:latest",
        egress: Optional[List[str]] = None,
        insecure: Optional[bool] = None,
        timeout: float = 90,
    ):
        self.base_url = (base_url or os.environ["SANDBOX_API_URL"]).rstrip("/")
        self.api_key = api_key or os.environ["SANDBOX_API_KEY"]
        if insecure is None:
            insecure = os.getenv("SANDBOX_API_INSECURE") == "1"
        # insecure=True accepts a self-signed certificate (a deployment without a domain).
        self._ssl = ssl._create_unverified_context() if insecure else None
        self.timeout = timeout
        self.template = template
        self.egress = list(egress or [])
        self.session_id: Optional[str] = None
        self.tenant_id: Optional[str] = None

    # Session lifecycle -----------------------------------------------------

    def start(self) -> "RemoteSandbox":
        res = self._request("POST", "/v1/sessions", {"template": self.template, "egress": self.egress})
        self.session_id, self.tenant_id = res["session_id"], res["tenant_id"]
        self.egress = res.get("egress", [])
        return self

    def close(self) -> None:
        if self.session_id:
            try:
                self._request("DELETE", f"/v1/sessions/{self.session_id}")
            finally:
                self.session_id = None

    def __enter__(self) -> "RemoteSandbox":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # Workspace operations (same shape as SandboxedWorkspace) -----------------

    def write_file(self, path: str, content: str) -> str:
        return self._request("POST", self._s("/write"), {"path": path, "content": content})["message"]

    def read_file(self, path: str) -> str:
        query = urllib.parse.urlencode({"path": path})
        return self._request("GET", self._s(f"/read?{query}"))["content"]

    def run_command(self, command: str, timeout_seconds: int = 15) -> str:
        body = {"command": command, "timeout_seconds": timeout_seconds}
        return self._request("POST", self._s("/exec"), body)["output"]

    def egress_log(self, limit: int = 200) -> List[dict]:
        """Every outbound connection this session attempted, allowed or denied."""
        return self._request("GET", self._s(f"/egress?limit={limit}"))["events"]

    def egress_policy(self) -> List[str]:
        """Hosts this tenant's sessions may request."""
        return self._request("GET", "/v1/egress/policy")["allowed"]

    def usage(self) -> dict:
        """This tenant's limits and current usage (sessions, running commands, request budget)."""
        return self._request("GET", "/v1/usage")

    def health(self) -> dict:
        return self._request("GET", "/healthz")

    # Internals ---------------------------------------------------------------

    def _s(self, suffix: str) -> str:
        if not self.session_id:
            raise RuntimeError("Sandbox session not started; use `with RemoteSandbox(...)` or call start().")
        return f"/v1/sessions/{self.session_id}{suffix}"

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read().decode()).get("detail", err.reason)
            except Exception:
                detail = err.reason
            # Path traversal and similar refusals surface as PermissionError, like the local workspace.
            if err.code == 403:
                raise PermissionError(detail) from None
            retry_after = err.headers.get("Retry-After")
            raise SandboxAPIError(err.code, str(detail), int(retry_after) if retry_after else None) from None

