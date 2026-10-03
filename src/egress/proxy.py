"""Egress gateway: an allowlisting HTTPS proxy for sandbox containers.

Sandboxes that are granted internet access sit on a private Docker network
whose only reachable host is this proxy. Each sandbox gets a signed pass
(session, tenant, allowed hosts, expiry) as its proxy credentials. For every
connection the proxy:

  1. accepts only HTTPS tunnels (CONNECT) to port 443,
  2. verifies the pass signature and expiry,
  3. checks the host against the pass's allowlist ("pypi.org", "*.example.com"),
  4. resolves the host itself and refuses private, loopback, link-local
     (cloud metadata) and other non-public addresses,
  5. connects to the vetted IP, pipes bytes both ways, and
  6. writes one JSON line per connection to the egress log.

TLS is never decrypted: the proxy sees the hostname, not the traffic.

Standard library only, so it runs unchanged inside the proxy container:
    EGRESS_SECRET=<hex> EGRESS_LOG=/logs/egress.jsonl python3 proxy.py
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import time
from typing import Dict, List, Optional, Tuple

ALLOWED_PORTS = {443}
MAX_HEADER_BYTES = 16 * 1024
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 120

# Friendly names a session can ask for instead of listing hosts.
PRESETS: Dict[str, List[str]] = {
    "pypi": ["pypi.org", "files.pythonhosted.org"],
    "npm": ["registry.npmjs.org"],
    "github": ["github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"],
    "huggingface": ["huggingface.co", "*.huggingface.co", "*.hf.co"],
}


# ------------------------------------------------------------------ host rules

def normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def expand_rules(names: List[str]) -> List[str]:
    """Expands presets ("pypi") into host rules and normalizes everything else."""
    rules: List[str] = []
    for name in names:
        name = normalize_host(name)
        for rule in PRESETS.get(name, [name]):
            if rule not in rules:
                rules.append(rule)
    return rules


def host_allowed(host: str, rules: List[str]) -> bool:
    """Exact match, or "*.example.com" matching any subdomain (not the apex)."""
    host = normalize_host(host)
    for rule in rules:
        if rule.startswith("*."):
            if host.endswith(rule[1:]) and host != rule[2:]:
                return True
        elif host == rule:
            return True
    return False


def rule_covered(rule: str, policy: List[str]) -> bool:
    """True if every host matched by `rule` is also allowed by `policy`."""
    if rule.startswith("*."):
        return rule in policy or any(p.startswith("*.") and rule[1:].endswith(p[1:]) for p in policy)
    return host_allowed(rule, policy)


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


# ---------------------------------------------------------------- signed passes

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_pass(secret: bytes, session_id: str, tenant_id: str, rules: List[str], ttl_seconds: int) -> str:
    payload = json.dumps(
        {"sid": session_id, "tid": tenant_id, "allow": rules, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_pass(secret: bytes, token: str) -> Optional[dict]:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), _unb64(sig_b64)):
            return None
        claims = json.loads(payload)
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None
    if claims.get("exp", 0) < time.time():
        return None
    return claims


def pass_from_proxy_auth(header_value: str) -> Optional[str]:
    """Extracts the pass from "Proxy-Authorization: Basic base64(session:pass)"."""
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        _, _, password = base64.b64decode(value.strip()).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return password or None


# ---------------------------------------------------------------------- server

class EgressProxy:
    def __init__(self, secret: bytes, log_path: Optional[str] = None, block_private: bool = True):
        self.secret = secret
        self.log_path = log_path
        # Tests turn this off to tunnel to a local echo server; production never does.
        self.block_private = block_private

    def log(self, event: dict) -> None:
        event = {"ts": round(time.time(), 3), **event}
        line = json.dumps(event, separators=(",", ":"))
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        else:
            print(line, flush=True)

    async def _respond(self, writer: asyncio.StreamWriter, code: int, reason: str, extra: str = "") -> None:
        writer.write(f"HTTP/1.1 {code} {reason}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        try:
            await writer.drain()
        finally:
            writer.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        event: dict = {"decision": "deny"}
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=CONNECT_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                writer.close()
                return
            lines = head.decode("latin-1").split("\r\n")
            method, target, *_ = (lines[0].split(" ") + ["", ""])[:3]
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}

            claims = None
            token = pass_from_proxy_auth(headers.get("proxy-authorization", ""))
            if token:
                claims = verify_pass(self.secret, token)
            if claims:
                event.update(session=claims["sid"], tenant=claims["tid"])

            if method.upper() != "CONNECT":
                event.update(target=target[:200], reason="only HTTPS (CONNECT) is supported")
                return await self._respond(writer, 405, "Method Not Allowed")

            host, _, port_text = target.rpartition(":")
            host = normalize_host(host.strip("[]"))
            port = int(port_text) if port_text.isdigit() else 0
            event.update(host=host, port=port)

            if not claims:
                event["reason"] = "missing, invalid or expired pass"
                return await self._respond(writer, 407, "Proxy Authentication Required",
                                           'Proxy-Authenticate: Basic realm="agent-sandbox"\r\n')
            if port not in ALLOWED_PORTS:
                event["reason"] = f"port {port} not allowed"
                return await self._respond(writer, 403, "Forbidden")
            if not host_allowed(host, claims["allow"]):
                event["reason"] = "host not on allowlist"
                return await self._respond(writer, 403, "Forbidden")

            # Resolve here and connect to a vetted IP, so DNS can't point an
            # allowed name at internal addresses (or change between check and use).
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                event["reason"] = "DNS lookup failed"
                return await self._respond(writer, 502, "Bad Gateway")
            ips = [info[4][0] for info in infos]
            if self.block_private and not all(is_public_ip(ip) for ip in ips):
                event.update(reason="resolves to a private or reserved address", ips=ips[:4])
                return await self._respond(writer, 403, "Forbidden")

            # Try each vetted address in order (e.g. IPv6 first on a host with no IPv6 route).
            upstream = None
            for ip in dict.fromkeys(ips):
                try:
                    up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
                    upstream = ip
                    break
                except (OSError, asyncio.TimeoutError):
                    continue
            if upstream is None:
                event.update(reason="upstream connection failed", ips=ips[:4])
                return await self._respond(writer, 502, "Bad Gateway")

            event.update(decision="allow", ip=upstream)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            sent, received = await asyncio.gather(
                self._pipe(reader, up_writer), self._pipe(up_reader, writer)
            )
            event.update(bytes_up=sent, bytes_down=received)
        except Exception as err:  # never let one connection take the proxy down
            event.setdefault("reason", f"proxy error: {type(err).__name__}")
            writer.close()
        finally:
            event["duration_ms"] = int((time.monotonic() - started) * 1000)
            self.log(event)

    async def _pipe(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> int:
        total = 0
        try:
            while True:
                chunk = await asyncio.wait_for(src.read(65536), timeout=IDLE_TIMEOUT)
                if not chunk:
                    break
                total += len(chunk)
                dst.write(chunk)
                await dst.drain()
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass
        return total

    async def serve(self, host: str = "0.0.0.0", port: int = 3128) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEADER_BYTES)


async def _main() -> None:
    secret = bytes.fromhex(os.environ["EGRESS_SECRET"])
    proxy = EgressProxy(secret, log_path=os.getenv("EGRESS_LOG"))
    server = await proxy.serve(port=int(os.getenv("EGRESS_PORT", "3128")))
    print(f"egress proxy listening on {server.sockets[0].getsockname()}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
