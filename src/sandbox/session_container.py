"""Long-running sessions: one container per session that stays up between commands.

A normal session starts a fresh container for every command, so nothing outlives a command.
A persistent session keeps one container running (`sleep infinity` under an init process) and
runs each command in it with `docker exec`, so background processes (a dev server, a worker)
keep running, an interactive terminal can attach to it, and the host can reach ports it
listens on (previews).

The container has the same hardening as per-command ones (gVisor, read-only root, no
capabilities, memory/CPU/pids limits, unprivileged user). Its network is fixed at start
(gVisor never sees networks connected later): the session's egress network when it has
internet access, otherwise a private `internal` network with no route out, so the host can
reach it but it can reach nothing.

Commands run through a small wrapper: output goes to files in the container's /tmp and is
printed after the command exits, so a background process that keeps stdout open can't make
the command hang; GNU `timeout` kills the command's process group when it overruns.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, Optional

import docker

LABEL = "agent-sandbox.session"
MAX_STDOUT = 1_000_000
MAX_STDERR = 250_000

# $AIRLOCK_CMD is the user's command (passed as an environment variable: no quoting issues).
WRAPPER = (
    'o=/tmp/.airlock-out-$$; e=/tmp/.airlock-err-$$; '
    'timeout -k 2 -s TERM "$AIRLOCK_TIMEOUT" bash -c "$AIRLOCK_CMD" </dev/null >"$o" 2>"$e"; rc=$?; '
    f'head -c {MAX_STDOUT} "$o"; head -c {MAX_STDERR} "$e" >&2; rm -f "$o" "$e"; exit $rc'
)


class SessionContainer:
    def __init__(self, client: docker.DockerClient, container, network_name: str, owns_network: bool):
        self.client = client
        self.container = container
        self.network_name = network_name
        self.owns_network = owns_network   # False when it's the egress gateway's network

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def start(cls, client: docker.DockerClient, session_id: str, *, image: str, workspace_dir: str,
              user: Optional[str], runtime: Optional[str], environment: Dict[str, str],
              network_name: Optional[str] = None) -> "SessionContainer":
        owns_network = network_name is None
        if owns_network:
            network_name = f"agent-sandbox-session-{session_id}"
            client.networks.create(network_name, driver="bridge", internal=True, labels={LABEL: session_id})
        kwargs = dict(
            image=image,
            command=["sleep", "infinity"],
            name=f"agent-sandbox-{session_id}",
            init=True,                                 # reaps orphaned background processes
            mem_limit="256m", memswap_limit="256m", nano_cpus=1_000_000_000, pids_limit=64,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            read_only=True, tmpfs={"/tmp": "size=64m"},
            environment=environment,
            volumes={workspace_dir: {"bind": "/workspace", "mode": "rw"}},
            working_dir="/workspace",
            network=network_name,
            labels={LABEL: session_id},
            detach=True,
        )
        if runtime:
            kwargs["runtime"] = runtime
        if user:
            kwargs["user"] = user
        try:
            container = client.containers.run(**kwargs)
        except Exception:
            if owns_network:
                _remove_network(client, network_name)
            raise
        return cls(client, container, network_name, owns_network)

    @classmethod
    def reconnect(cls, client: docker.DockerClient, session_id: str, container_id: str, network_name: str,
                  owns_network: bool) -> "SessionContainer":
        container = client.containers.get(container_id)
        if container.status != "running" or container.labels.get(LABEL) != session_id:
            raise RuntimeError(f"Session container for {session_id} is not running")
        return cls(client, container, network_name, owns_network)

    def state(self) -> dict:
        return {"container_id": self.container.id, "network_name": self.network_name, "owns_network": self.owns_network}

    @property
    def user(self) -> str:
        return self.container.attrs.get("Config", {}).get("User", "") or ""

    def ip_address(self) -> Optional[str]:
        self.container.reload()
        return (self.container.attrs["NetworkSettings"]["Networks"].get(self.network_name) or {}).get("IPAddress") or None

    def is_running(self) -> bool:
        try:
            self.container.reload()
        except Exception:
            return False
        return self.container.status == "running"

    def remove(self) -> None:
        try:
            self.container.remove(force=True)
        except Exception:
            pass
        if self.owns_network:
            _remove_network(self.client, self.network_name)

    # ------------------------------------------------------------------ commands

    def execute(self, command: str, timeout_seconds: int, env: Optional[Dict[str, str]] = None) -> dict:
        """Runs one command; returns {stdout, stderr, exit_code, timed_out, oom_killed, warnings}.
        `env` (the session's secrets) is added to this command's environment only."""
        api = self.client.api
        exec_id = api.exec_create(
            self.container.id, ["/bin/bash", "-c", WRAPPER], user=self.user, workdir="/workspace",
            environment={**(env or {}), "AIRLOCK_CMD": command, "AIRLOCK_TIMEOUT": str(int(timeout_seconds))},
        )["Id"]
        result: dict = {}

        def run():
            try:
                result["out"] = api.exec_start(exec_id, demux=True)
            except Exception as e:   # the container was removed meanwhile, ...
                result["error"] = e

        started = time.monotonic()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout_seconds + 10)   # `timeout` inside normally ends it well before this
        elapsed = time.monotonic() - started
        if worker.is_alive() or "error" in result:
            return {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True, "oom_killed": False,
                    "warnings": [f"Execution exceeded {timeout_seconds}s limit."]}
        stdout, stderr = result["out"] if isinstance(result["out"], tuple) else (result["out"], b"")
        exit_code = api.exec_inspect(exec_id).get("ExitCode")
        exit_code = -1 if exit_code is None else exit_code
        timed_out = exit_code == 124 or (exit_code == 137 and elapsed >= timeout_seconds)
        warnings = []
        if timed_out:
            warnings.append(f"Execution exceeded {timeout_seconds}s limit.")
        elif exit_code == 137:
            warnings.append("Process was killed (exit 137, SIGKILL), most likely for exceeding the memory limit.")
        if not self.is_running():
            warnings.append("The session's container stopped (most likely out of memory); start a new session.")
        return {
            "stdout": (stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (stderr or b"").decode("utf-8", errors="replace"),
            "exit_code": exit_code, "timed_out": timed_out, "oom_killed": False, "warnings": warnings,
        }

    # ------------------------------------------------------------------ interactive terminal

    def open_pty(self, rows: int = 24, cols: int = 80, env: Optional[Dict[str, str]] = None) -> "Pty":
        api = self.client.api
        exec_id = api.exec_create(
            self.container.id, ["/bin/bash", "-l"], user=self.user, workdir="/workspace",
            tty=True, stdin=True, environment={**(env or {}), "TERM": "xterm-256color"},
        )["Id"]
        sock = api.exec_start(exec_id, tty=True, socket=True)
        pty = Pty(api, exec_id, sock)
        pty.resize(rows, cols)
        return pty


class Pty:
    """A bash on a pseudo-terminal inside the session's container: raw bytes both ways."""

    def __init__(self, api, exec_id: str, sock):
        self.api = api
        self.exec_id = exec_id
        self._raw = getattr(sock, "_sock", sock)
        self._closed = False

    def read(self, size: int = 65536) -> bytes:
        """Blocks until output is available; b"" when the shell has exited."""
        try:
            return self._raw.recv(size)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        self._raw.sendall(data)

    def resize(self, rows: int, cols: int) -> None:
        try:
            self.api.exec_resize(self.exec_id, height=max(2, min(int(rows), 500)), width=max(2, min(int(cols), 1000)))
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._raw.sendall(b"\x04exit\n")
        except OSError:
            pass
        try:
            self._raw.close()
        except OSError:
            pass


def prune_orphans(client: docker.DockerClient, keep: set) -> None:
    """At API start: containers and networks of sessions that died with an earlier process."""
    for container in client.containers.list(all=True, filters={"label": LABEL}):
        if container.labels.get(LABEL) not in keep:
            try:
                container.remove(force=True)
            except Exception:
                pass
    for network in client.networks.list(filters={"label": LABEL}):
        if (network.attrs.get("Labels") or {}).get(LABEL) not in keep:
            _remove_network(client, network.name)


def _remove_network(client: docker.DockerClient, name: str) -> None:
    try:
        client.networks.get(name).remove()
    except Exception:
        pass


def preview_token() -> str:
    return uuid.uuid4().hex[:16]
