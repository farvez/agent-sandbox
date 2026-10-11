"""Long-running sessions: limits, previews (proxy + Caddy's TLS check) and the terminal WebSocket."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

import src.api.server as server
from src.api import previews
from src.api.limits import LimitTracker, load_tenant_limits
from tests.test_api_server import AUTH, OTHER_TENANT, FakeSessionContainer, client, create  # noqa: F401
from tests.test_sdk import api_url, fresh_state  # noqa: F401  (real HTTP server fixtures)
from tests.conftest import TENANT_KEYS

DOMAIN = "preview.test"


@pytest.fixture
def preview_domain(monkeypatch):
    monkeypatch.setattr(server, "PREVIEW_DOMAIN", DOMAIN)


@pytest.fixture
def upstream():
    """A tiny web app standing in for a server running in the session, on a free local port."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"], seen["path"] = self.headers.get("Host"), self.path
            body = f"hello from {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "app=1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(201)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[::-1])

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], seen
    httpd.shutdown()


def create_persistent(client, headers=AUTH):
    res = client.post("/v1/sessions", json={"persistent": True}, headers=headers)
    assert res.status_code == 201, res.text
    assert res.json()["persistent"] is True
    return res.json()["session_id"]


def test_regular_sessions_are_not_persistent(client):
    sid = create(client)
    listed = client.get("/v1/sessions", headers=AUTH).json()["sessions"]
    assert listed[0]["session_id"] == sid and listed[0]["persistent"] is False and listed[0]["previews"] == []


def test_persistent_session_limit(client, monkeypatch):
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits(
        '{"*": {"max_sessions": 10, "requests_per_minute": 1000, "max_persistent_sessions": 1}}')))
    sid = create_persistent(client)
    res = client.post("/v1/sessions", json={"persistent": True}, headers=AUTH)
    assert res.status_code == 429 and "Long-running" in res.json()["detail"]
    create(client)                                                    # regular sessions still fine
    assert server.LIMITS.usage("acme")["persistent_sessions_open"] == 1
    client.delete(f"/v1/sessions/{sid}", headers=AUTH)
    assert server.LIMITS.usage("acme")["persistent_sessions_open"] == 0
    create_persistent(client)


def test_host_wide_cap(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_PERSISTENT_SESSIONS", 1)
    create_persistent(client)
    assert client.post("/v1/sessions", json={"persistent": True}, headers=OTHER_TENANT).status_code == 503


def test_expose_needs_persistent_and_enabled_previews(client, monkeypatch):
    sid = create(client)
    monkeypatch.setattr(server, "PREVIEW_DOMAIN", DOMAIN)
    assert client.post(f"/v1/sessions/{sid}/previews", json={"port": 3000}, headers=AUTH).status_code == 409
    psid = create_persistent(client)
    monkeypatch.setattr(server, "PREVIEW_DOMAIN", "")
    assert client.post(f"/v1/sessions/{psid}/previews", json={"port": 3000}, headers=AUTH).status_code == 501
    monkeypatch.setattr(server, "PREVIEW_DOMAIN", DOMAIN)
    assert client.post(f"/v1/sessions/{psid}/previews", json={"port": 80}, headers=AUTH).status_code == 422
    assert client.post(f"/v1/sessions/{psid}/previews", json={"port": 3000}, headers=OTHER_TENANT).status_code == 403


def test_preview_proxies_to_the_session(client, preview_domain, upstream):
    port, seen = upstream
    sid = create_persistent(client)
    res = client.post(f"/v1/sessions/{sid}/previews", json={"port": port}, headers=AUTH)
    assert res.status_code == 200
    url = res.json()["url"]
    token = server.active_sessions[sid].workspace.preview_token
    assert url == f"https://{port}-{token}.{DOMAIN}"
    host = url.removeprefix("https://")

    page = client.get("/app/page?x=1", headers={"Host": host})
    assert page.status_code == 200 and page.text == "hello from /app/page?x=1"
    assert seen["host"] == f"localhost:{port}"                       # what dev servers expect
    assert page.headers["x-robots-tag"].startswith("noindex") and page.headers["set-cookie"] == "app=1"
    posted = client.post("/echo", content=b"abc", headers={"Host": host})
    assert posted.status_code == 201 and posted.content == b"cba"
    assert client.get("/v1/sessions", headers=AUTH).json()["sessions"][0]["previews"] == [{"port": port, "url": url}]

    # Unexposed, wrong token, other port, closed session: all 404.
    assert client.get("/", headers={"Host": f"{port}-{'0' * 16}.{DOMAIN}"}).status_code == 404
    assert client.get("/", headers={"Host": f"{port + 1}-{token}.{DOMAIN}"}).status_code == 404
    client.delete(f"/v1/sessions/{sid}/previews/{port}", headers=AUTH)
    assert client.get("/", headers={"Host": host}).status_code == 404
    # The API itself is untouched on its own hostname.
    assert client.get("/healthz").status_code == 200


def test_preview_with_nothing_listening_is_502(client, preview_domain):
    sid = create_persistent(client)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    url = client.post(f"/v1/sessions/{sid}/previews", json={"port": free}, headers=AUTH).json()["url"]
    res = client.get("/", headers={"Host": url.removeprefix("https://")})
    assert res.status_code == 502 and "0.0.0.0" in res.text


def test_preview_websockets_are_refused(client, preview_domain, upstream):
    port, _ = upstream
    sid = create_persistent(client)
    url = client.post(f"/v1/sessions/{sid}/previews", json={"port": port}, headers=AUTH).json()["url"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Host": url.removeprefix("https://")}) as ws:
            ws.receive_text()


@pytest.mark.parametrize("host,expected", [
    ("3000-0123456789abcdef.preview.test", (3000, "0123456789abcdef")),
    ("3000-0123456789ABCDEF.PREVIEW.TEST:443", (3000, "0123456789abcdef")),
    ("80-0123456789abcdef.preview.test", None),
    ("3000-0123456789abcdef.evil.test", None),
    ("x.3000-0123456789abcdef.preview.test", None),
    ("preview.test", None),
    ("3000-short.preview.test", None),
])
def test_parse_host(host, expected):
    assert previews.parse_host(host, DOMAIN) == expected


def test_caddy_tls_check(api_url, preview_domain, monkeypatch):
    with httpx.Client(base_url=api_url, headers={"X-API-Key": TENANT_KEYS["acme"]}) as http:
        sid = http.post("/v1/sessions", json={"persistent": True}).json()["session_id"]
        url = http.post(f"/v1/sessions/{sid}/previews", json={"port": 3000}).json()["url"]
        host = url.removeprefix("https://")
        assert http.get("/internal/preview-allowed", params={"domain": host}).status_code == 200
        assert http.get("/internal/preview-allowed", params={"domain": "3001" + host[4:]}).status_code == 404
        assert http.get("/internal/preview-allowed", params={"domain": "evil.example.com"}).status_code == 404
        # Through the public proxy (it adds X-Forwarded-For) the check never answers.
        proxied = http.get("/internal/preview-allowed", params={"domain": host}, headers={"X-Forwarded-For": "1.2.3.4"})
        assert proxied.status_code == 404
        http.delete(f"/v1/sessions/{sid}")


def test_caddy_tls_check_refuses_remote_clients(client, preview_domain):
    sid = create_persistent(client)
    url = client.post(f"/v1/sessions/{sid}/previews", json={"port": 3000}, headers=AUTH).json()["url"]
    res = client.get("/internal/preview-allowed", params={"domain": url.removeprefix("https://")})
    assert res.status_code == 404           # TestClient's address isn't 127.0.0.1


# ---------------------------------------------------------------- terminal


def test_terminal_round_trip(client):
    sid = create_persistent(client)
    with client.websocket_connect(f"/v1/sessions/{sid}/terminal?rows=30&cols=100", headers=AUTH) as ws:
        assert ws.receive_bytes() == b"$ [30x100] "
        ws.send_bytes(b"ls\r")
        assert ws.receive_bytes() == b"echo:ls\r"
        ws.send_text('{"type": "resize", "rows": 50, "cols": 160}')
        assert ws.receive_bytes() == b"[resized 50x160]"
        ws.send_bytes(b"exit")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert server.active_sessions[sid].workspace.session_container.pty.closed


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, OTHER_TENANT])
def test_terminal_needs_the_owners_key(client, headers):
    sid = create_persistent(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/sessions/{sid}/terminal", headers=headers) as ws:
            ws.receive_bytes()


def test_terminal_needs_a_persistent_session(client):
    sid = create(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/sessions/{sid}/terminal", headers=AUTH) as ws:
            ws.receive_bytes()


def test_terminal_accepts_bearer_auth(client):
    sid = create_persistent(client)
    bearer = {"Authorization": f"Bearer {TENANT_KEYS['acme']}"}
    with client.websocket_connect(f"/v1/sessions/{sid}/terminal", headers=bearer) as ws:
        assert ws.receive_bytes().startswith(b"$ ")


def test_persistent_state_survives_a_restart(client, preview_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SESSION_STATE_PATH", str(tmp_path / "sessions.json"))
    sid = create_persistent(client)
    client.post(f"/v1/sessions/{sid}/previews", json={"port": 3000}, headers=AUTH)
    token = server.active_sessions[sid].workspace.preview_token
    server.persist_sessions()
    server.active_sessions.clear()
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits('{"*": {"requests_per_minute": 1000}}')))
    assert server.restore_sessions() == 1
    rec = server.active_sessions[sid]
    assert rec.persistent and rec.workspace.preview_token == token and rec.workspace.exposed_ports == [3000]
    assert server.LIMITS.usage("acme")["persistent_sessions_open"] == 1


def test_limit_is_released_when_sessions_end_or_expire(client, monkeypatch):
    monkeypatch.setattr(server, "LIMITS", LimitTracker(load_tenant_limits(
        '{"*": {"max_sessions": 10, "requests_per_minute": 1000, "max_persistent_sessions": 1}}')))
    for _ in range(3):                                   # delete: the slot comes back every time
        sid = create_persistent(client)
        assert client.delete(f"/v1/sessions/{sid}", headers=AUTH).status_code == 200
    sid = create_persistent(client)                       # expiry releases it too
    server.active_sessions[sid].last_accessed_at -= server.SESSION_TTL_SECONDS + 1
    server.reap_expired_sessions()
    assert server.LIMITS.usage("acme")["persistent_sessions_open"] == 0
    create_persistent(client)
