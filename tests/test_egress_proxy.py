import asyncio
import base64
import json

import pytest

import src.egress.proxy as proxy_module
from src.egress.proxy import (
    EgressProxy,
    expand_rules,
    host_allowed,
    is_public_ip,
    pass_from_proxy_auth,
    rule_covered,
    sign_pass,
    verify_pass,
)

SECRET = b"s" * 32


# ------------------------------------------------------------------ rules


def test_presets_expand_and_dedupe():
    assert expand_rules(["pypi", "PyPI.org.", "api.example.com"]) == [
        "pypi.org", "files.pythonhosted.org", "api.example.com"
    ]


@pytest.mark.parametrize(
    "host, allowed",
    [
        ("pypi.org", True),
        ("PYPI.ORG.", True),
        ("evil-pypi.org", False),
        ("pypi.org.evil.com", False),
        ("a.example.com", True),
        ("a.b.example.com", True),
        ("example.com", False),       # wildcard does not include the apex
        ("badexample.com", False),
    ],
)
def test_host_matching(host, allowed):
    assert host_allowed(host, ["pypi.org", "*.example.com"]) is allowed


@pytest.mark.parametrize(
    "rule, covered",
    [("pypi.org", True), ("a.example.com", True), ("*.a.example.com", True), ("*.example.com", True),
     ("*.com", False), ("example.com", False), ("evil.com", False)],
)
def test_rule_coverage_by_policy(rule, covered):
    assert rule_covered(rule, ["pypi.org", "*.example.com"]) is covered


@pytest.mark.parametrize(
    "ip, public",
    [("151.101.0.223", True), ("10.0.0.5", False), ("172.17.0.1", False), ("192.168.1.1", False),
     ("127.0.0.1", False), ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False),
     ("::1", False), ("fd00::1", False), ("::ffff:169.254.169.254", False), ("2606:4700::1111", True)],
)
def test_public_ip_check(ip, public):
    assert is_public_ip(ip) is public


# ------------------------------------------------------------------ passes


def test_pass_roundtrip():
    claims = verify_pass(SECRET, sign_pass(SECRET, "sbx_1", "acme", ["pypi.org"], 60))
    assert claims["sid"] == "sbx_1" and claims["tid"] == "acme" and claims["allow"] == ["pypi.org"]


def test_pass_rejects_wrong_secret_tampering_and_expiry():
    token = sign_pass(SECRET, "sbx_1", "acme", ["pypi.org"], 60)
    assert verify_pass(b"x" * 32, token) is None

    payload, sig = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged["allow"] = ["*.com"]
    forged_payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    assert verify_pass(SECRET, f"{forged_payload}.{sig}") is None

    assert verify_pass(SECRET, sign_pass(SECRET, "sbx_1", "acme", ["pypi.org"], -1)) is None
    assert verify_pass(SECRET, "garbage") is None


def test_proxy_auth_header_parsing():
    header = "Basic " + base64.b64encode(b"sbx_1:the.token").decode()
    assert pass_from_proxy_auth(header) == "the.token"
    assert pass_from_proxy_auth("Bearer x") is None
    assert pass_from_proxy_auth("Basic !!!") is None


# ------------------------------------------------------------------ live proxy


def auth_header(rules, secret=SECRET, ttl=60):
    token = sign_pass(secret, "sbx_test", "acme", rules, ttl)
    return "Proxy-Authorization: Basic " + base64.b64encode(f"sbx_test:{token}".encode()).decode() + "\r\n"


async def _scenario(request: bytes, block_private: bool, tmp_path):
    """Starts a local echo server and the proxy, sends one request, returns (response, log events)."""

    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    echo_port = echo_server.sockets[0].getsockname()[1]
    log_path = tmp_path / "egress.jsonl"
    proxy = EgressProxy(SECRET, log_path=str(log_path), block_private=block_private)
    proxy_server = await proxy.serve("127.0.0.1", 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]

    original_ports = proxy_module.ALLOWED_PORTS
    proxy_module.ALLOWED_PORTS = {443, echo_port}
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(request.replace(b"{PORT}", str(echo_port).encode()))
        await writer.drain()
        response = await asyncio.wait_for(reader.read(200), 5)
        if b"200 Connection Established" in response:
            writer.write(b"hello")
            await writer.drain()
            response += await asyncio.wait_for(reader.read(200), 5)
        writer.close()
        await asyncio.sleep(0.2)  # let the proxy finish and write its log line
    finally:
        proxy_module.ALLOWED_PORTS = original_ports
        proxy_server.close()
        echo_server.close()
    events = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
    return response, events


def run(request, tmp_path, block_private=True):
    return asyncio.run(_scenario(request.encode(), block_private, tmp_path))


def test_allowed_host_is_tunnelled_and_logged(tmp_path):
    response, events = run("CONNECT localhost:{PORT} HTTP/1.1\r\n" + auth_header(["localhost"]) + "\r\n",
                           tmp_path, block_private=False)
    assert b"200 Connection Established" in response and b"echo:hello" in response
    assert events[-1]["decision"] == "allow" and events[-1]["session"] == "sbx_test"
    assert events[-1]["bytes_up"] == 5


def test_private_address_is_refused_even_if_allowlisted(tmp_path):
    response, events = run("CONNECT localhost:{PORT} HTTP/1.1\r\n" + auth_header(["localhost"]) + "\r\n", tmp_path)
    assert b"403" in response
    assert events[-1]["reason"] == "resolves to a private or reserved address"


def test_host_not_on_pass_is_refused(tmp_path):
    response, events = run("CONNECT localhost:{PORT} HTTP/1.1\r\n" + auth_header(["pypi.org"]) + "\r\n",
                           tmp_path, block_private=False)
    assert b"403" in response and events[-1]["reason"] == "host not on allowlist"


def test_missing_or_forged_pass_gets_407(tmp_path):
    response, events = run("CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", tmp_path)
    assert b"407" in response and events[-1]["decision"] == "deny"
    response, _ = run("CONNECT pypi.org:443 HTTP/1.1\r\n" + auth_header(["pypi.org"], secret=b"y" * 32) + "\r\n", tmp_path)
    assert b"407" in response


def test_expired_pass_gets_407(tmp_path):
    response, _ = run("CONNECT pypi.org:443 HTTP/1.1\r\n" + auth_header(["pypi.org"], ttl=-5) + "\r\n", tmp_path)
    assert b"407" in response


def test_plain_http_is_refused(tmp_path):
    response, events = run("GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\n" + auth_header(["pypi.org"]) + "\r\n", tmp_path)
    assert b"405" in response and "CONNECT" in events[-1]["reason"]


def test_non_443_port_is_refused(tmp_path):
    response, events = run("CONNECT pypi.org:22 HTTP/1.1\r\n" + auth_header(["pypi.org"]) + "\r\n", tmp_path)
    assert b"403" in response and events[-1]["reason"] == "port 22 not allowed"


# ------------------------------------------------------------------ proxy DNS


def test_upstream_nameservers_skips_loopback_and_falls_through(tmp_path):
    from src.egress.gateway import upstream_nameservers

    stub = tmp_path / "stub.conf"          # systemd-resolved stub: loopback only
    stub.write_text("nameserver 127.0.0.53\noptions edns0\n")
    real = tmp_path / "real.conf"
    real.write_text("# comment\nnameserver 10.0.0.2\nnameserver ::1\nnameserver 1.1.1.1\nsearch ec2.internal\n")

    assert upstream_nameservers(candidates=[str(stub), str(real)]) == ["10.0.0.2", "1.1.1.1"]
    assert upstream_nameservers(candidates=[str(tmp_path / "missing"), str(stub)]) == []
    assert upstream_nameservers(override=" 9.9.9.9, 8.8.8.8 ", candidates=[str(real)]) == ["9.9.9.9", "8.8.8.8"]
