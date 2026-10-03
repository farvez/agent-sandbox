"""Runs the egress proxy container and wires sandbox sessions to it.

Each session that is granted internet access gets its own `internal` Docker
network (no route out) containing only that session's containers and the
proxy. Sessions therefore can't reach each other, and code that ignores the
proxy settings has nowhere to send packets.
"""
import json
import os
import secrets
import tempfile
import threading
from typing import List, Optional, Tuple

import docker
from docker.errors import NotFound

from src.egress.proxy import sign_pass

PROXY_PORT = 3128
PROXY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.py")
LABEL = "agent-sandbox.egress"


class EgressGateway:
    container_name = "agent-sandbox-egress"

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
        # A fresh signing key per API process: passes from a previous run stop working.
        self.secret = secrets.token_bytes(32)
        self.log_dir = os.path.realpath(
            log_dir or os.getenv("SANDBOX_EGRESS_LOG_DIR") or os.path.join(tempfile.gettempdir(), "agent_sandbox_egress")
        )
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, "egress.jsonl")
        self._container = None
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- proxy container

    def _ensure_running(self) -> None:
        if self._container is not None:
            try:
                self._container.reload()
                if self._container.status == "running":
                    return
            except NotFound:
                pass

        # A container left by an earlier API process holds an old signing key.
        try:
            self.client.containers.get(self.container_name).remove(force=True)
        except NotFound:
            pass
        if not self._started:
            # First start in this process: leftover session networks belong to
            # sessions that died with an earlier process. (Never prune later on:
            # live sessions would lose their networks.)
            for network in self.client.networks.list(filters={"label": LABEL}):
                try:
                    network.remove()
                except Exception:
                    pass
            self._started = True

        kwargs = dict(
            image=self.image,
            name=self.container_name,
            command=["python3", "/app/proxy.py"],
            environment={
                "EGRESS_SECRET": self.secret.hex(),
                "EGRESS_LOG": "/logs/egress.jsonl",
                "EGRESS_PORT": str(PROXY_PORT),
            },
            volumes={
                PROXY_SCRIPT: {"bind": "/app/proxy.py", "mode": "ro"},
                self.log_dir: {"bind": "/logs", "mode": "rw"},
            },
            network="bridge",  # the proxy's own route to the internet
            read_only=True,
            tmpfs={"/tmp": "size=16m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="128m",
            pids_limit=128,
            labels={LABEL: "proxy"},
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
            detach=True,
        )
        if self.runtime:
            kwargs["runtime"] = self.runtime
        if self.user:
            kwargs["user"] = self.user
        self._container = self.client.containers.create(**kwargs)
        self._container.start()

    # ------------------------------------------------------------- per session

    def attach(self, session_id: str) -> Tuple[str, str]:
        """Creates the session's private network and returns (network name, proxy IP on it)."""
        with self._lock:
            self._ensure_running()
            name = f"agent-sandbox-egress-{session_id}"
            network = self.client.networks.create(
                name, driver="bridge", internal=True, labels={LABEL: session_id}
            )
            try:
                network.connect(self._container)
                self._container.reload()
                ip = self._container.attrs["NetworkSettings"]["Networks"][name]["IPAddress"]
            except Exception:
                network.remove()
                raise
            return name, ip

    def detach(self, network_name: str) -> None:
        try:
            network = self.client.networks.get(network_name)
        except NotFound:
            return
        try:
            network.disconnect(self.container_name, force=True)
        except Exception:
            pass
        try:
            network.remove()
        except Exception:
            pass

    def issue_pass(self, session_id: str, tenant_id: str, rules: List[str], ttl_seconds: int = 86400) -> str:
        return sign_pass(self.secret, session_id, tenant_id, rules, ttl_seconds)

    @staticmethod
    def proxy_url(session_id: str, token: str, proxy_ip: str) -> str:
        return f"http://{session_id}:{token}@{proxy_ip}:{PROXY_PORT}"

    # ------------------------------------------------------------- audit log

    def events(self, session_id: str, limit: int = 200) -> List[dict]:
        if not os.path.exists(self.log_path):
            return []
        matches = []
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("session") == session_id:
                    matches.append(event)
        return matches[-limit:]

    def shutdown(self) -> None:
        if self._container is not None:
            try:
                self._container.remove(force=True)
            except Exception:
                pass
            self._container = None


_gateway: Optional[EgressGateway] = None
_gateway_lock = threading.Lock()


def get_gateway(client: docker.DockerClient, **kwargs) -> EgressGateway:
    """One gateway (one proxy container, one signing key) per process."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = EgressGateway(client, **kwargs)
        return _gateway


def shutdown_gateway() -> None:
    global _gateway
    with _gateway_lock:
        if _gateway is not None:
            _gateway.shutdown()
            _gateway = None
