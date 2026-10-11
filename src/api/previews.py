"""Preview URLs: https://<port>-<token>.<SANDBOX_PREVIEW_DOMAIN> → a port inside a long-running session.

A session's code can listen on a port (a web app, a dev server); after the session exposes that
port, requests to its preview hostname are proxied from the host to the session container's
private address. The token is random per session, so a preview link works for whoever has it
(like a share link) and stops working when the session closes or the port is unexposed.

Each preview hostname gets its own TLS certificate from Caddy's on-demand TLS; Caddy first asks
/internal/preview-allowed whether the name belongs to an open preview, so nobody can make the
server request certificates for arbitrary names.

Preview pages are someone's sandboxed code, served on a subdomain: the console's cookies are
host-only (never sent to previews), and responses are marked noindex. WebSocket upgrades
(dev-server hot reload) aren't proxied yet.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

import httpx

HOST_RE = re.compile(r"^(?P<port>[1-9][0-9]{0,4})-(?P<token>[0-9a-f]{16})$")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
MIN_PORT, MAX_PORT = 1024, 65535
MAX_BODY = 50 * 2**20


def parse_host(host: str, domain: str) -> Optional[Tuple[int, str]]:
    """(port, token) for '<port>-<token>.<domain>' (any :port suffix ignored), else None."""
    if not domain:
        return None
    host = (host or "").split(":")[0].lower().rstrip(".")
    suffix = "." + domain.lower()
    if not host.endswith(suffix):
        return None
    m = HOST_RE.match(host[: -len(suffix)])
    if not m:
        return None
    port = int(m.group("port"))
    return (port, m.group("token")) if MIN_PORT <= port <= MAX_PORT else None


def preview_url(domain: str, port: int, token: str) -> str:
    return f"https://{port}-{token}.{domain}"


class PreviewMiddleware:
    """ASGI middleware: requests for a preview hostname are proxied; everything else goes to the app.

    `resolve(port, token)` returns the upstream base URL ("http://10.210.3.2:3000") for an open,
    exposed preview (and marks the session as used), or None.
    """

    def __init__(self, app, domain: Callable[[], str], resolve: Callable[[int, str], Optional[str]],
                 timeout: float = 60.0, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.app = app
        self.domain = domain
        self.resolve = resolve
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), transport=transport,
                                        follow_redirects=False, trust_env=False)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        target = parse_host(headers.get(b"host", b"").decode("latin-1"), self.domain())
        if target is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        from starlette.concurrency import run_in_threadpool

        upstream = await run_in_threadpool(self.resolve, *target)
        if upstream is None:
            return await _plain(send, 404, b"This preview isn't available: the session closed, or the port isn't exposed.")
        await self._proxy(scope, receive, send, upstream, target[0])

    async def _proxy(self, scope, receive, send, upstream: str, port: int):
        path = scope.get("raw_path") or scope["path"].encode()
        url = upstream + path.decode("latin-1") + (("?" + scope["query_string"].decode("latin-1")) if scope.get("query_string") else "")
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]
                   if k.decode("latin-1").lower() not in HOP_BY_HOP and not k.lower().startswith(b"x-forwarded-")]
        original_host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
        # Dev servers (Vite, ...) refuse unknown Host headers; they see localhost, like on a laptop.
        headers += [("host", f"localhost:{port}"), ("x-forwarded-host", original_host), ("x-forwarded-proto", "https")]

        # Buffered (with a cap) so the app gets a Content-Length: simple servers can't read chunked bodies.
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > MAX_BODY:
                return await _plain(send, 413, f"Preview request bodies are limited to {MAX_BODY // 2**20} MB.".encode())
            if not message.get("more_body"):
                break

        request = self.client.build_request(scope["method"], url, headers=headers, content=body or None)
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError:
            return await _plain(send, 502, f"Nothing answered on port {port} in the session. Is the server running "
                                           "and listening on 0.0.0.0?".encode())
        try:
            out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.multi_items()
                   if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding"]
            out += [(b"x-robots-tag", b"noindex, nofollow"), (b"referrer-policy", b"no-referrer")]
            await send({"type": "http.response.start", "status": response.status_code, "headers": out})
            async for chunk in response.aiter_bytes():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()


async def _plain(send, status: int, text: bytes):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"x-robots-tag", b"noindex")]})
    await send({"type": "http.response.body", "body": text})
