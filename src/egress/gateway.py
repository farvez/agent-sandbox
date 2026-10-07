"""Runs egress proxy containers and wires sandbox sessions to them.

Each session that is granted internet access gets:
  * its own `internal` Docker network (no route out), and
  * its own proxy container, attached to that network and to the default
    bridge *before* it starts, with its own signing key.

Attaching both networks before start matters under gVisor: runsc fixes a
sandbox's network interfaces at start and never sees networks connected later.
A proxy per session also means tenants never share a proxy process or key.
Sessions can't reach each other, and code that ignores the proxy settings has
nowhere to send packets.
"""
import json
import os
import secrets
import tempfile
import threading
import time
from typing import List, Optional

import docker
from docker.errors import NotFound

from src.egress.proxy import sign_pass

PROXY_PORT = 3128
PROXY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.py")
LABEL = "agent-sandbox.egress"
READY_TIMEOUT = 20


RESOLV_CONF_CANDIDATES = ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf")


def upstream_nameservers(override: Optional[str] = None, candidates=RESOLV_CONF_CANDIDATES) -> List[str]:
    """The host's real DNS servers, for the proxy's own resolv.conf.

    Containers on custom Docker networks get Docker's embedded resolver
    (127.0.0.11), which relies on iptables rules that gVisor's network stack
    ignores, so under runsc every lookup fails. The proxy is pointed at the
    upstream servers directly instead. Loopback entries (systemd-resolved's
    127.0.0.53 stub) are skipped because they aren't reachable from a container.
    SANDBOX_EGRESS_DNS="10.0.0.2,1.1.1.1" overrides detection.
    """
    if override:
        return [ns.strip() for ns in override.split(",") if ns.strip()]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        servers = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                ns = parts[1]
                if not (ns.startswith("127.") or ns == "::1"):
                    servers.append(ns)
        if servers:
            return servers
    return []


class EgressSession:
    """The network, proxy and proxy URL belonging to one sandbox session."""

    def __init__(self, network_name: str, container, proxy_url: str):
        self.network_name = network_name
        self.container = container
        self.proxy_url = proxy_url


class EgressGateway:
    def __init__(
        self,
        client: docker.DockerClient,
        image: str = "sandbox-base:latest",
        log_dir: Optional[str] = None,
        runtime: Optional[str] = None,
        user: Optional[str] = None,
    ):
        self.client = client
        self.image = image
        self.runtime = runtime
        self.user = user
        self.log_dir = os.path.realpath(
            log_dir or os.getenv("SANDBOX_EGRESS_LOG_DIR") or os.path.join(tempfile.gettempdir(), "agent_sandbox_egress")
        )
        os.makedirs(self.log_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._pruned = False
        self._keep: set = set()   # sessions restored after an API restart; their proxies stay

        # On a Linux host, give proxies the host's upstream DNS (see upstream_nameservers).
        # On Docker Desktop there is no such file, and Docker's embedded DNS works there.
        self.resolv_conf: Optional[str] = None
        servers = upstream_nameservers(os.getenv("SANDBOX_EGRESS_DNS"))
        if servers:
            conf_dir = os.path.join(self.log_dir, "_conf")
            os.makedirs(conf_dir, exist_ok=True)
            self.resolv_conf = os.path.join(conf_dir, "resolv.conf")
            with open(self.resolv_conf, "w", encoding="utf-8") as f:
                f.write("".join(f"nameserver {ns}\n" for ns in servers))

    def _prune_orphans(self) -> None:
        """Once per process: proxies and networks left by an earlier API process
        belong to sessions that died with it. (Never later: those would be live.)"""
        if self._pruned:
            return
        for container in self.client.containers.list(all=True, filters={"label": LABEL}):
            if container.labels.get(LABEL) in self._keep:
                continue
            try:
                container.remove(force=True)
            except Exception:
                pass
        for network in self.client.networks.list(filters={"label": LABEL}):
            if (network.attrs.get("Labels") or {}).get(LABEL) in self._keep:
                continue
            try:
                network.remove()
            except Exception:
                pass
        self._pruned = True

    def reconnect(self, session_id: str, network_name: str, container_id: str, proxy_url: str) -> EgressSession:
        """After an API restart: takes back a session's still-running proxy, so the session
        keeps its internet access. Raises if the proxy or its network is gone."""
        container = self.client.containers.get(container_id)
        if container.status != "running" or container.labels.get(LABEL) != session_id:
            raise RuntimeError(f"Egress proxy for {session_id} is not running")
        self.client.networks.get(network_name)
        with self._lock:
            self._keep.add(session_id)
        return EgressSession(network_name, container, proxy_url)

    def log_path(self, session_id: str) -> str:
        return os.path.join(self.log_dir, f"{session_id}.jsonl")

    # ------------------------------------------------------------- per session

    def attach(self, session_id: str, tenant_id: str, rules: List[str], ttl_seconds: int = 86400) -> EgressSession:
        """Creates the session's private network and proxy; returns once the proxy is listening."""
        with self._lock:
            self._prune_orphans()

        name = f"agent-sandbox-egress-{session_id}"
        secret = secrets.token_bytes(32)  # this session's proxy only accepts passes signed with it
        network = self.client.networks.create(name, driver="bridge", internal=True, labels={LABEL: session_id})
        container = None
        try:
            kwargs = dict(
                image=self.image,
                name=name,
                command=["python3", "/app/proxy.py"],
                environment={
                    "EGRESS_SECRET": secret.hex(),
                    "EGRESS_LOG": f"/logs/{session_id}.jsonl",
                    "EGRESS_PORT": str(PROXY_PORT),
                },
                volumes={
                    PROXY_SCRIPT: {"bind": "/app/proxy.py", "mode": "ro"},
                    self.log_dir: {"bind": "/logs", "mode": "rw"},
                },
                network=name,  # the session side
                read_only=True,
                tmpfs={"/tmp": "size=16m"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="128m",
                pids_limit=64,
                labels={LABEL: session_id},
                detach=True,
            )
            if self.runtime:
                kwargs["runtime"] = self.runtime
            if self.user:
                kwargs["user"] = self.user
            if self.resolv_conf:
                kwargs["volumes"][self.resolv_conf] = {"bind": "/etc/resolv.conf", "mode": "ro"}
            container = self.client.containers.create(**kwargs)
            # The internet side, connected before start so gVisor sees both interfaces.
            self.client.networks.get("bridge").connect(container)
            container.start()
            self._wait_until_listening(container)
            container.reload()
            ip = container.attrs["NetworkSettings"]["Networks"][name]["IPAddress"]
        except Exception:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            network.remove()
            raise

        token = sign_pass(secret, session_id, tenant_id, rules, ttl_seconds)
        return EgressSession(name, container, f"http://{session_id}:{token}@{ip}:{PROXY_PORT}")

    @staticmethod
    def _wait_until_listening(container) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if b"listening" in container.logs():
                return
            container.reload()
            if container.status == "exited":
                raise RuntimeError(f"Egress proxy exited: {container.logs()[-500:].decode(errors='replace')}")
            time.sleep(0.2)
        raise RuntimeError("Egress proxy did not start in time")

    def detach(self, egress: EgressSession) -> None:
        try:
            egress.container.remove(force=True)
        except Exception:
            pass
        try:
            self.client.networks.get(egress.network_name).remove()
        except Exception:
            pass

    # ------------------------------------------------------------- audit log

    def events(self, session_id: str, limit: int = 200) -> List[dict]:
        path = self.log_path(session_id)
        if not os.path.exists(path):
            return []
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events[-limit:]


_gateway: Optional[EgressGateway] = None
_gateway_lock = threading.Lock()


def get_gateway(client: docker.DockerClient, **kwargs) -> EgressGateway:
    """One gateway per process (it owns orphan cleanup and the log directory)."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = EgressGateway(client, **kwargs)
        return _gateway


def shutdown_gateway() -> None:
    """Forgets the gateway so the next one prunes orphans again (used by API shutdown and tests).

    Live sessions remove their own proxy and network in SandboxedWorkspace.cleanup().
    """
    global _gateway
    with _gateway_lock:
        _gateway = None
