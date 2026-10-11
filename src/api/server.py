import os
import re
import json
import time
import uuid
import asyncio
import secrets
import threading
import logging
from typing import Dict, List, Optional, Set, Tuple
from contextlib import AsyncExitStack, asynccontextmanager

from src.api import runtime_config

runtime_config.apply()  # before anything below reads its settings from the environment

from fastapi import FastAPI, HTTPException, Request, Security, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from src.api.accounts import AccountStore
from src.api.audit import AuditLog
from src.api.github_app import GitHubApp, GitHubAppError
from src.api.keystore import KeyLimitReached, KeyStore, LastActiveKey, UnknownKey
from src.api.limits import LimitExceeded, LimitTracker, load_tenant_limits
from src.api import session_state
from src.egress.gateway import shutdown_gateway
from src.egress.proxy import expand_rules, rule_covered
from src.api.repos import RepoImportError, clean_destination, fetch_archive, import_archive, parse_repo
from src.api.snapshots import SnapshotError, SnapshotStore
from src.api import previews, terminal_ws
from src.sandbox.files import WorkspaceFileError
from src.sandbox.workspace import SandboxedWorkspace, format_result
from src.sandbox.workspace_pool import QuotaExceededError, WorkspaceCapacityError, get_pool



def load_api_keys(multi: Optional[str], single: Optional[str], required: bool = True) -> List[Tuple[bytes, str]]:
    """Builds the (key, tenant_id) table from the environment.

    SANDBOX_API_KEYS="acme:<key>,globex:<key>" gives each tenant its own key;
    SANDBOX_API_KEY=<key> is a shorthand for a single tenant named "default".
    """
    pairs: List[Tuple[str, str]] = []
    for entry in (multi or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        tenant, sep, key = entry.partition(":")
        if not sep or not tenant.strip() or not key.strip():
            raise RuntimeError("SANDBOX_API_KEYS entries must look like 'tenant:key'.")
        pairs.append((tenant.strip(), key.strip()))
    if single:
        pairs.append(("default", single))

    if not pairs:
        if not required:
            return []
        raise RuntimeError("No API keys configured. Set SANDBOX_API_KEYS or SANDBOX_API_KEY, or SANDBOX_KEYSTORE.")
    keys = [key for _, key in pairs]
    if len(set(keys)) != len(keys):
        raise RuntimeError("The same API key is assigned to more than one tenant.")
    return [(key.encode(), tenant) for tenant, key in pairs]


# Self-service keys (issued/revoked at runtime) live in the key store; static keys from
# configuration keep working alongside them.
KEYSTORE = KeyStore.from_config(os.getenv("SANDBOX_KEYSTORE"), region=os.getenv("AWS_REGION"))
API_KEYS = load_api_keys(os.getenv("SANDBOX_API_KEYS"), os.getenv("SANDBOX_API_KEY"), required=KEYSTORE is None)

# The admin key manages keys for every tenant. It is not a tenant key: it can't run sandboxes.
ADMIN_KEY = os.getenv("SANDBOX_ADMIN_KEY") or None
if ADMIN_KEY is not None and len(ADMIN_KEY) < 32:
    raise RuntimeError("SANDBOX_ADMIN_KEY must be at least 32 characters.")


def load_egress_policy(raw: Optional[str], path: Optional[str]) -> Dict[str, List[str]]:
    """Which hosts each tenant's sessions may reach, as {"tenant": ["pypi", "api.example.com"]}.

    From SANDBOX_EGRESS_POLICY (JSON) or the JSON file at SANDBOX_EGRESS_POLICY_FILE.
    Tenants not listed get no internet access at all.
    """
    if path and not raw:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    if not raw or not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(
        isinstance(v, list) and all(isinstance(h, str) for h in v) for v in data.values()
    ):
        raise RuntimeError('Egress policy must be a JSON object like {"tenant": ["pypi", "api.example.com"]}.')
    return {tenant: expand_rules(hosts) for tenant, hosts in data.items()}


EGRESS_POLICY = load_egress_policy(os.getenv("SANDBOX_EGRESS_POLICY"), os.getenv("SANDBOX_EGRESS_POLICY_FILE"))


def tenant_egress(tenant_id: str) -> List[str]:
    """The tenant's own policy, else the "*" default (e.g. for console sign-ups), else nothing."""
    return EGRESS_POLICY.get(tenant_id, EGRESS_POLICY.get("*", []))


# Console accounts, invites and usage metering (optional).
ACCOUNTS = AccountStore.from_config(os.getenv("SANDBOX_ACCOUNTS"), region=os.getenv("AWS_REGION"))


def record_usage(tenant_id: str, **counters: float) -> None:
    """Metering must never fail a request."""
    if ACCOUNTS is None:
        return
    try:
        ACCOUNTS.record_usage(tenant_id, **counters)
    except Exception:
        logger.exception("Usage metering failed for %s", tenant_id)
# Per-tenant command audit log (optional): every exec and import, never the output.
AUDIT = AuditLog.from_config(os.getenv("SANDBOX_AUDIT"), region=os.getenv("AWS_REGION"),
                             retention_days=int(os.getenv("SANDBOX_AUDIT_RETENTION_DAYS", "90")))


# Workspace snapshots (optional; see src/api/snapshots.py) and file transfer limits.
SNAPSHOTS = SnapshotStore.from_config(os.getenv("SANDBOX_SNAPSHOTS"), region=os.getenv("AWS_REGION"))
UPLOAD_MAX_BYTES = int(os.getenv("SANDBOX_UPLOAD_MAX_MB", "50")) * 2**20
# Long-running sessions on this host at once (each keeps a 256 MB container), and the domain
# preview URLs live under (unset = previews off).
MAX_PERSISTENT_SESSIONS = int(os.getenv("SANDBOX_MAX_PERSISTENT_SESSIONS", "8"))
PREVIEW_DOMAIN = (os.getenv("SANDBOX_PREVIEW_DOMAIN") or "").strip().lower().rstrip(".")
DOWNLOAD_MAX_BYTES = int(os.getenv("SANDBOX_DOWNLOAD_MAX_MB", "100")) * 2**20


# GitHub App for private repository import (optional; see src/api/github_app.py).
GITHUB_APP = GitHubApp.from_env()


def private_repo_token(tenant_id: str, repo) -> Optional[str]:
    """A one-hour, read-only token for one private repository, from an installation of the
    GitHub App that this tenant connected on the repository's owner; None if there isn't one."""
    if GITHUB_APP is None or ACCOUNTS is None:
        return None
    for installation in ACCOUNTS.github_installations(tenant_id):
        if installation["account"].lower() == repo.owner.lower():
            try:
                token = GITHUB_APP.repo_token(installation["id"], repo.name)
            except GitHubAppError as e:
                raise RepoImportError(str(e), 502)
            if token:
                return token
    return None


def record_audit(tenant_id: str, **entry) -> None:
    """Auditing, like metering, must never fail a request."""
    if AUDIT is None:
        return
    try:
        AUDIT.record(tenant_id, **entry)
    except Exception:
        logger.exception("Audit logging failed for %s", tenant_id)


def actor_for_key(header_key: Optional[str]) -> str:
    """Which credential ran something, without revealing it: the key id of a self-service
    key (asb_<id>_<secret>), or "static key" for one from configuration."""
    if header_key and header_key.startswith("asb_") and header_key.count("_") >= 2:
        return f"key {header_key.split('_')[1][:16]}"
    return "static key"


LIMITS = LimitTracker(load_tenant_limits(os.getenv("SANDBOX_TENANT_LIMITS"), os.getenv("SANDBOX_TENANT_LIMITS_FILE")))

SESSION_TTL_SECONDS = int(os.getenv("SANDBOX_SESSION_TTL", "1800"))
ALLOWED_TEMPLATES: Set[str] = {
    "sandbox-base:latest",
    "python:3.11-slim",
}

logger = logging.getLogger("agent_sandbox.api")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
# Endpoints are sync (run in FastAPI's thread pool) so blocking Docker calls
# never stall the event loop; guard the registry with a thread lock.
sessions_lock = threading.Lock()


class CreateSessionRequest(BaseModel):
    template: str = Field(default="sandbox-base:latest", description="Base docker image tag")
    metadata: Optional[Dict[str, str]] = None
    egress: List[str] = Field(
        default_factory=list,
        max_length=20,
        description='Hosts or presets this session may reach over HTTPS, e.g. ["pypi"]. Empty = no network.',
    )
    snapshot_id: Optional[str] = Field(default=None, max_length=40,
                                       description="Start with this snapshot's files restored into /workspace")
    secrets: Dict[str, str] = Field(default_factory=dict, description="Environment variables for every command, "
                                    "e.g. an API key; never returned or logged (see /v1/sessions/{id}/secrets)")
    persistent: bool = Field(default=False, description="Keep one container running for the whole session: "
                             "background processes survive between commands, and the session gets an "
                             "interactive terminal and preview URLs")


class CreateSessionResponse(BaseModel):
    session_id: str
    tenant_id: str
    egress: List[str]
    disk_quota_mb: int
    created_at: float
    status: str
    persistent: bool = False


class SetSecretsRequest(BaseModel):
    secrets: Dict[str, str] = Field(..., description='{"NAME": "value"}; added to (or replacing) the session\'s secrets')


class ExposePortRequest(BaseModel):
    port: int = Field(..., ge=previews.MIN_PORT, le=previews.MAX_PORT, description="Port the session's server listens on")


class WriteFileRequest(BaseModel):
    path: str = Field(..., description="Relative path inside the sandbox")
    content: str = Field(..., description="File content to write")


class ReadFileResponse(BaseModel):
    path: str
    content: str


class RunCommandRequest(BaseModel):
    command: str = Field(..., description="Shell command to execute")
    timeout_seconds: int = Field(default=15, ge=1, le=60)


class RunCommandResponse(BaseModel):
    command: str
    output: str = Field(description="Text form: [STDOUT]/[STDERR]/[WARNING]/[TIMEOUT]/[EXIT CODE]")
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    oom_killed: bool
    warnings: List[str]


class ImportRepoRequest(BaseModel):
    repo: str = Field(..., max_length=300, description='Public GitHub repository: "owner/repo" or its https://github.com URL')
    ref: Optional[str] = Field(default=None, max_length=200, description="Branch, tag or commit (default: the default branch)")
    path: Optional[str] = Field(default=None, max_length=200, description="Folder inside /workspace (default: the repo name)")


class CreateSnapshotRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80, description="A label to recognise it by")
    path: str = Field(default=".", max_length=200, description='Folder to save (default "." = all of /workspace)')


class RestoreSnapshotRequest(BaseModel):
    snapshot_id: str = Field(..., max_length=40)
    path: str = Field(default=".", max_length=200, description="Folder inside /workspace to restore into")


class SessionRecord:
    def __init__(self, session_id: str, workspace: SandboxedWorkspace, tenant_id: str, egress: Optional[List[str]] = None):
        self.session_id = session_id
        self.workspace = workspace
        self.tenant_id = tenant_id
        self.egress = list(egress or [])
        # Fixed at creation: the workspace forgets its container once cleaned up, and the
        # long-running-session limit must still be released after that.
        self.persistent = bool(getattr(workspace, "persistent", False))
        self.created_at = time.time()
        self.last_accessed_at = time.time()

    def touch(self):
        self.last_accessed_at = time.time()

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "egress": list(self.egress),
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
            "expires_at": self.last_accessed_at + SESSION_TTL_SECONDS,
            "disk_quota_mb": self.workspace.quota_bytes // 2**20,
            "persistent": self.persistent,
            "previews": self.previews(),
        }

    def previews(self) -> List[dict]:
        if not PREVIEW_DOMAIN:
            return []
        return [{"port": port, "url": previews.preview_url(PREVIEW_DOMAIN, port, self.workspace.preview_token)}
                for port in getattr(self.workspace, "exposed_ports", [])]



active_sessions: Dict[str, SessionRecord] = {}


def reap_expired_sessions() -> None:
    """Removes sessions idle longer than the TTL and wipes their workspaces."""
    now = time.time()
    with sessions_lock:
        expired = [
            active_sessions.pop(sid)
            for sid, rec in list(active_sessions.items())
            if now - rec.last_accessed_at > SESSION_TTL_SECONDS
        ]
    for rec in expired:
        rec.workspace.cleanup()
        LIMITS.close_session(rec.tenant_id, persistent=rec.persistent)
    persist_sessions()


# ------------------------------------------------------------------ sessions survive API restarts
# With SANDBOX_SESSION_STATE set, a restart (e.g. a code deploy) keeps every open session:
# workspaces and egress proxies keep running on the host, the registry is saved on the way
# down (and every minute) and taken back on the way up.

SESSION_STATE_PATH = os.getenv("SANDBOX_SESSION_STATE") or None


def persist_sessions() -> None:
    if not SESSION_STATE_PATH:
        return
    with sessions_lock:
        records = list(active_sessions.values())
    try:
        session_state.save(SESSION_STATE_PATH, [{
            **rec.workspace.state(), "session_egress": rec.egress,
            "created_at": rec.created_at, "last_accessed_at": rec.last_accessed_at,
        } for rec in records])
    except Exception:
        logger.exception("Saving the session registry failed")


def restore_sessions() -> int:
    """At startup: takes back the sessions the previous API process left running.
    Anything that can't be restored (expired, workspace or proxy gone) is cleaned up."""
    now = time.time()
    restored: List[SessionRecord] = []
    for saved in session_state.load(SESSION_STATE_PATH):
        try:
            if now - float(saved["last_accessed_at"]) > SESSION_TTL_SECONDS:
                continue
            workspace = SandboxedWorkspace.restore(saved)
        except Exception as e:
            logger.warning("Could not restore session %s: %s", saved.get("session_id"), e)
            continue
        rec = SessionRecord(saved["session_id"], workspace, saved["tenant_id"], egress=saved.get("session_egress"))
        rec.created_at = float(saved["created_at"])
        rec.last_accessed_at = float(saved["last_accessed_at"])
        restored.append(rec)
    pool = get_pool()
    if pool is not None:
        pool.recover([rec.workspace.workspace_dir for rec in restored])   # frees every other slot
    with sessions_lock:
        for rec in restored:
            active_sessions[rec.session_id] = rec
    for rec in restored:
        LIMITS.restore_session(rec.tenant_id, persistent=rec.persistent)
    persist_sessions()
    return len(restored)


async def session_ttl_sweeper():
    """Background task that reaps expired sessions every minute."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(reap_expired_sessions)
        except Exception:
            logger.exception("Session sweeper iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SESSION_STATE_PATH:
        restored = await asyncio.to_thread(restore_sessions)
        logger.info("Restored %d session(s) after restart", restored)
        await asyncio.to_thread(prune_session_containers)
    sweeper_task = asyncio.create_task(session_ttl_sweeper())
    async with AsyncExitStack() as stack:
        if REMOTE_MCP is not None:
            await stack.enter_async_context(REMOTE_MCP.lifespan())
        yield
    sweeper_task.cancel()
    if SESSION_STATE_PATH:
        # A restart, not the end: leave workspaces and proxies running for the next process.
        await asyncio.to_thread(persist_sessions)
        with sessions_lock:
            active_sessions.clear()
        shutdown_gateway()
        return
    with sessions_lock:
        remaining = list(active_sessions.values())
        active_sessions.clear()
    for rec in remaining:
        rec.workspace.cleanup()
        LIMITS.close_session(rec.tenant_id, persistent=rec.persistent)
    shutdown_gateway()


app = FastAPI(
    title="Agent Sandbox Execution API",
    version="1.0.0",
    description="Multi-tenant, isolated execution substrate for AI coding agents.",
    lifespan=lifespan,
)


def tenant_for_key(header_key: Optional[str]) -> Optional[str]:
    """The tenant an API key belongs to, or None."""
    tenant_id = None
    if header_key:
        if KEYSTORE is not None:
            tenant_id = KEYSTORE.authenticate(header_key)
        presented = header_key.encode()
        # Compare against every static key without returning early, so response time
        # doesn't reveal which (or whether any) configured key was close.
        for key, tenant in API_KEYS:
            if secrets.compare_digest(presented, key):
                tenant_id = tenant
    return tenant_id


def verify_api_key(header_key: Optional[str] = Security(api_key_header)) -> str:
    """Authenticates the caller and returns their tenant ID."""
    tenant_id = tenant_for_key(header_key)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return tenant_id


def authorize(tenant_id: str = Security(verify_api_key)) -> str:
    """Every authenticated request: valid key, then the tenant's request-rate limit."""
    LIMITS.check_rate(tenant_id)
    return tenant_id


@app.exception_handler(LimitExceeded)
def limit_exceeded(request: Request, exc: LimitExceeded) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse(status_code=429, content={"detail": str(exc)}, headers=headers)


def get_authorized_session(session_id: str, tenant_id: str) -> SessionRecord:
    """Ensures the session exists and belongs to the caller's tenant."""
    with sessions_lock:
        rec = active_sessions.get(session_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Session not found or expired")
        if rec.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Forbidden: Access denied to this session")
        rec.touch()
        return rec


# ------------------------------------------------------------------ session operations
# Shared by the REST API (API-key auth) and the console (GitHub sign-in): callers
# authenticate and rate-limit first, then pass the tenant in.

def prune_session_containers() -> None:
    """Long-running session containers left by an earlier process that weren't taken back."""
    try:
        import docker

        from src.sandbox.session_container import prune_orphans

        with sessions_lock:
            keep = {sid for sid, rec in active_sessions.items() if rec.persistent}
        prune_orphans(docker.from_env(), keep)
    except Exception:
        logger.exception("Pruning old session containers failed")


def persistent_sessions_open() -> int:
    with sessions_lock:
        return sum(1 for rec in active_sessions.values() if rec.persistent)


def start_session(tenant_id: str, template: str = "sandbox-base:latest",
                  egress_request: Optional[List[str]] = None, persistent: bool = False) -> SessionRecord:
    if template not in ALLOWED_TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template '{template}' not permitted. Allowed: {list(ALLOWED_TEMPLATES)}"
        )

    egress = expand_rules(egress_request or [])
    allowed = tenant_egress(tenant_id)
    refused = [rule for rule in egress if not rule_covered(rule, allowed)]
    if refused:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Egress not permitted for this tenant: {refused}. Allowed: {allowed}",
        )

    if persistent and persistent_sessions_open() >= MAX_PERSISTENT_SESSIONS:
        raise HTTPException(status_code=503, detail="The server is running as many long-running sessions as it can; "
                                                    "try again shortly, or start a regular session.")
    LIMITS.open_session(tenant_id, persistent=persistent)  # 429 when the tenant is at a session limit
    session_id = f"sbx_{uuid.uuid4().hex[:12]}"
    kwargs = {"persistent": True} if persistent else {}
    try:
        workspace = SandboxedWorkspace(
            base_image=template, egress=egress, session_id=session_id, tenant_id=tenant_id, **kwargs
        )
    except WorkspaceCapacityError as e:
        LIMITS.close_session(tenant_id, persistent=persistent)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception:
        LIMITS.close_session(tenant_id, persistent=persistent)
        raise

    rec = SessionRecord(session_id=session_id, workspace=workspace, tenant_id=tenant_id, egress=egress)
    with sessions_lock:
        active_sessions[session_id] = rec
    record_usage(tenant_id, sessions=1)
    persist_sessions()
    return rec


def run_in_session(rec: SessionRecord, command: str, timeout_seconds: int,
                   actor: str = "api", audit_command: Optional[str] = None) -> dict:
    """`audit_command` is what the user typed when `command` wraps it (the console terminal)."""
    with LIMITS.running_command(rec.tenant_id):  # 429 when too many are already running
        started = time.monotonic()
        result = rec.workspace.execute(command=command, timeout_seconds=timeout_seconds)
    elapsed = round(time.monotonic() - started, 3)
    record_usage(rec.tenant_id, commands=1, command_seconds=elapsed)
    record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="exec",
                 command=audit_command if audit_command is not None else command,
                 exit_code=result["exit_code"], duration_s=elapsed,
                 timed_out=result["timed_out"], oom_killed=result["oom_killed"])
    return result


def import_repo_into(rec: SessionRecord, repo: str, ref: Optional[str] = None, path: Optional[str] = None,
                     actor: str = "api") -> dict:
    """Downloads a public GitHub repository and unpacks it into the session's workspace."""
    described = f"import {repo}" + (f" @{ref}" if ref else "") + (f" into {path}" if path else "")
    started = time.monotonic()
    try:
        parsed = parse_repo(repo, ref)
        dest = clean_destination(path, parsed)
        try:
            archive = fetch_archive(parsed)
        except RepoImportError as public_error:
            # Not public (or not there): try the tenant's connected GitHub App installation.
            token = private_repo_token(rec.tenant_id, parsed) if public_error.status == 404 else None
            if token is None:
                raise
            archive = fetch_archive(parsed, token=token)
        with LIMITS.running_command(rec.tenant_id):
            result = import_archive(rec.workspace, parsed, archive, dest)
    except (RepoImportError, QuotaExceededError) as e:
        status_code = e.status if isinstance(e, RepoImportError) else 413
        record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="import", command=described,
                     duration_s=round(time.monotonic() - started, 3), detail=f"failed ({status_code}): {e}"[:300])
        raise HTTPException(status_code=status_code, detail=str(e))
    elapsed = round(time.monotonic() - started, 3)
    record_usage(rec.tenant_id, commands=1, command_seconds=elapsed)
    record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="import", command=described,
                 exit_code=0, duration_s=elapsed, detail=f"{result.get('files', 0)} files into {result.get('path', '')}")
    return result


def _transfer_error(e: Exception) -> HTTPException:
    if isinstance(e, (WorkspaceFileError, SnapshotError)):
        return HTTPException(status_code=e.status, detail=str(e))
    if isinstance(e, QuotaExceededError):
        return HTTPException(status_code=413, detail=str(e))
    if isinstance(e, PermissionError):
        return HTTPException(status_code=403, detail=str(e))
    raise e


def _audited(rec: SessionRecord, actor: str, kind: str, described: str, action):
    """Runs a file or snapshot operation, recording it (and any failure) in the audit log."""
    started = time.monotonic()
    try:
        result = action()
    except (WorkspaceFileError, SnapshotError, QuotaExceededError, PermissionError) as e:
        error = _transfer_error(e)
        record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind=kind, command=described,
                     duration_s=round(time.monotonic() - started, 3), detail=f"failed ({error.status_code}): {e}"[:300])
        raise error
    record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind=kind, command=described,
                 exit_code=0, duration_s=round(time.monotonic() - started, 3))
    return result


def upload_file(rec: SessionRecord, path: str, data: bytes, actor: str = "api") -> dict:
    size = _audited(rec, actor, "upload", f"upload {path} ({len(data)} bytes)",
                    lambda: rec.workspace.write_bytes(path, data))
    return {"path": path, "size_bytes": size}


def download_file(rec: SessionRecord, path: str, actor: str = "api") -> bytes:
    return _audited(rec, actor, "download", f"download {path}",
                    lambda: rec.workspace.read_bytes(path, DOWNLOAD_MAX_BYTES))


def download_archive(rec: SessionRecord, path: str = ".", actor: str = "api") -> bytes:
    def build():
        with LIMITS.running_command(rec.tenant_id):
            return rec.workspace.archive(path, DOWNLOAD_MAX_BYTES)
    return _audited(rec, actor, "download", f"download archive of {path or '.'}", build)


def require_snapshots() -> SnapshotStore:
    if SNAPSHOTS is None:
        raise HTTPException(status_code=501, detail="Snapshots are not enabled on this server (SANDBOX_SNAPSHOTS).")
    return SNAPSHOTS


def create_snapshot(rec: SessionRecord, name: Optional[str] = None, path: str = ".", actor: str = "api") -> dict:
    store = require_snapshots()

    def save():
        with LIMITS.running_command(rec.tenant_id):
            archive = rec.workspace.archive(path, store.max_bytes)
        return store.create(rec.tenant_id, archive, name=name, session_id=rec.session_id)
    meta = _audited(rec, actor, "snapshot", f"snapshot {path or '.'}" + (f" as {name!r}" if name else ""), save)
    return meta


def restore_snapshot(rec: SessionRecord, snapshot_id: str, path: str = ".", actor: str = "api") -> dict:
    store = require_snapshots()

    def restore():
        meta, archive = store.get(rec.tenant_id, snapshot_id)
        with LIMITS.running_command(rec.tenant_id):
            rec.workspace.extract(archive, path)
        return meta
    meta = _audited(rec, actor, "restore", f"restore {snapshot_id} into {path or '.'}", restore)
    return {"restored": meta, "path": path or "."}


SECRET_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
RESERVED_SECRETS = {"PATH", "HOME", "PYTHONUSERBASE", "PYTHONPATH", "PYTHONHOME", "TERM", "SHELL", "USER",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "BASH_ENV", "ENV", "IFS"}
MAX_SECRETS, MAX_SECRET_BYTES = 20, 8192


def check_secrets(current: Dict[str, str], new: Dict[str, str]) -> None:
    for name, value in new.items():
        if not SECRET_NAME_RE.match(name):
            raise HTTPException(status_code=422, detail=f"Secret names are UPPER_CASE letters, digits and _ "
                                                        f"(max 64), like OPENAI_API_KEY; got {name[:70]!r}.")
        if name in RESERVED_SECRETS or name.startswith(("AIRLOCK_", "PIP_", "LD_", "SANDBOX_")):
            raise HTTPException(status_code=422, detail=f"{name} is used by the sandbox itself; pick another name.")
        if not isinstance(value, str) or "\0" in value or len(value.encode()) > MAX_SECRET_BYTES:
            raise HTTPException(status_code=422, detail=f"Secret {name}: values are text up to {MAX_SECRET_BYTES} bytes.")
    if len({**current, **new}) > MAX_SECRETS:
        raise HTTPException(status_code=422, detail=f"A session can have at most {MAX_SECRETS} secrets.")


def set_secrets(rec: SessionRecord, secrets_in: Dict[str, str], actor: str = "api") -> List[str]:
    """Adds or replaces secrets; returns every secret's name (never values)."""
    current = rec.workspace.secrets
    check_secrets(current, secrets_in)
    current.update(secrets_in)
    persist_sessions()
    if secrets_in:
        record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="secret",
                     command="set secret " + ", ".join(sorted(secrets_in)), exit_code=0)
    return sorted(current)


def delete_secret(rec: SessionRecord, name: str, actor: str = "api") -> List[str]:
    if rec.workspace.secrets.pop(name, None) is None:
        raise HTTPException(status_code=404, detail=f"No secret named {name[:70]}.")
    persist_sessions()
    record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="secret",
                 command=f"delete secret {name}", exit_code=0)
    return sorted(rec.workspace.secrets)


def require_persistent(rec: SessionRecord, feature: str) -> None:
    if not rec.persistent:
        raise HTTPException(status_code=409, detail=f"{feature} needs a long-running session: "
                                                    "create the session with \"persistent\": true.")


def expose_port(rec: SessionRecord, port: int, actor: str = "api") -> dict:
    require_persistent(rec, "Previews")
    if not PREVIEW_DOMAIN:
        raise HTTPException(status_code=501, detail="Previews are not enabled on this server (SANDBOX_PREVIEW_DOMAIN).")
    if not previews.MIN_PORT <= port <= previews.MAX_PORT:
        raise HTTPException(status_code=422, detail=f"Ports {previews.MIN_PORT}-{previews.MAX_PORT} can be previewed.")
    ports = rec.workspace.exposed_ports
    if port not in ports:
        if len(ports) >= 5:
            raise HTTPException(status_code=409, detail="A session can expose at most 5 ports.")
        ports.append(port)
        persist_sessions()
        record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="preview",
                     command=f"expose port {port}", exit_code=0)
    return {"port": port, "url": previews.preview_url(PREVIEW_DOMAIN, port, rec.workspace.preview_token)}


def unexpose_port(rec: SessionRecord, port: int, actor: str = "api") -> None:
    if port in getattr(rec.workspace, "exposed_ports", []):
        rec.workspace.exposed_ports.remove(port)
        persist_sessions()
        record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="preview",
                     command=f"unexpose port {port}", exit_code=0)


def resolve_preview(port: int, token: str) -> Optional[str]:
    """Upstream URL for an open, exposed preview (and keeps its session alive), else None."""
    with sessions_lock:
        rec = next((r for r in active_sessions.values()
                    if r.persistent and secrets.compare_digest(r.workspace.preview_token, token)), None)
        if rec is None or port not in rec.workspace.exposed_ports:
            return None
        rec.touch()
    ip = rec.workspace.session_container.ip_address()
    return f"http://{ip}:{port}" if ip else None


def end_session(session_id: str, tenant_id: str) -> None:
    with sessions_lock:
        rec = active_sessions.get(session_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Session not found")
        if rec.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        active_sessions.pop(session_id, None)

    rec.workspace.cleanup()
    LIMITS.close_session(tenant_id, persistent=rec.persistent)
    persist_sessions()


def close_tenant_sessions(tenant_id: str) -> int:
    """Closes every open session of a tenant (an admin removing its access). Returns how many."""
    closed = 0
    for rec in tenant_sessions(tenant_id):
        try:
            end_session(rec.session_id, tenant_id)
            closed += 1
        except HTTPException:
            pass   # already gone
    return closed


def tenant_sessions(tenant_id: str) -> List[SessionRecord]:
    with sessions_lock:
        return sorted((r for r in active_sessions.values() if r.tenant_id == tenant_id),
                      key=lambda r: r.created_at, reverse=True)


@app.get("/healthz")
def health_check():
    with sessions_lock:
        count = len(active_sessions)
    return {"status": "healthy", "active_sessions": count}


@app.post("/v1/sessions", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(request: CreateSessionRequest, tenant_id: str = Security(authorize),
                   header_key: Optional[str] = Security(api_key_header)):
    check_secrets({}, request.secrets)
    if request.snapshot_id:
        try:
            require_snapshots().meta(tenant_id, request.snapshot_id)
        except SnapshotError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
    rec = start_session(tenant_id, request.template, request.egress, persistent=request.persistent)
    if request.secrets:
        set_secrets(rec, request.secrets, actor=actor_for_key(header_key))
    if request.snapshot_id:
        try:
            restore_snapshot(rec, request.snapshot_id, actor=actor_for_key(header_key))
        except Exception:
            end_session(rec.session_id, tenant_id)
            raise
    return CreateSessionResponse(
        session_id=rec.session_id,
        tenant_id=tenant_id,
        egress=rec.egress,
        disk_quota_mb=rec.workspace.quota_bytes // 2**20,
        created_at=rec.created_at,
        status="ready",
        persistent=rec.persistent,
    )


@app.get("/v1/sessions")
def list_sessions(tenant_id: str = Security(authorize)):
    """This tenant's open sessions, newest first (including ones started from the console)."""
    return {"tenant_id": tenant_id, "sessions": [r.summary() for r in tenant_sessions(tenant_id)]}


# ------------------------------------------------------------------ API keys

class CreateKeyRequest(BaseModel):
    name: str = Field(default="", max_length=100, description="Label to recognise the key by, e.g. 'laptop' or 'ci'")


class AdminCreateKeyRequest(CreateKeyRequest):
    tenant: str = Field(..., description="Tenant the key belongs to; created implicitly by its first key")


def require_keystore() -> KeyStore:
    if KEYSTORE is None:
        raise HTTPException(status_code=501, detail="Self-service keys are not enabled on this server (SANDBOX_KEYSTORE).")
    return KEYSTORE


def verify_admin(header_key: Optional[str] = Security(api_key_header)) -> None:
    if ADMIN_KEY is None:
        raise HTTPException(status_code=404, detail="The admin API is not enabled on this server.")
    if not header_key or not secrets.compare_digest(header_key.encode(), ADMIN_KEY.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing admin key")


def _issue(tenant_id: str, name: str) -> dict:
    try:
        record, api_key = require_keystore().issue(tenant_id, name)
    except KeyLimitReached as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**record.public(), "api_key": api_key,
            "note": "Store this key now: it is shown only once and can't be recovered."}


def _revoke(key_id: str, tenant_id: Optional[str], keep_one: bool) -> dict:
    try:
        record = require_keystore().revoke(key_id, tenant_id=tenant_id, keep_one=keep_one)
    except UnknownKey as e:
        raise HTTPException(status_code=404, detail=str(e))
    except LastActiveKey as e:
        raise HTTPException(status_code=409, detail=str(e))
    return record.public()


@app.get("/v1/keys")
def list_own_keys(tenant_id: str = Security(authorize)):
    """This tenant's self-service keys (never the secrets). Static keys from configuration aren't listed."""
    return {"tenant_id": tenant_id, "keys": [r.public() for r in require_keystore().list(tenant_id)]}


@app.post("/v1/keys", status_code=status.HTTP_201_CREATED)
def create_own_key(request: CreateKeyRequest, tenant_id: str = Security(authorize)):
    """Issues another key for this tenant, e.g. to rotate: create new, switch over, revoke old."""
    return _issue(tenant_id, request.name)


@app.delete("/v1/keys/{key_id}")
def revoke_own_key(key_id: str, tenant_id: str = Security(authorize)):
    """Revokes one of this tenant's keys, effective immediately. Refuses the last active one."""
    return _revoke(key_id, tenant_id=tenant_id, keep_one=True)


@app.get("/v1/admin/keys", dependencies=[Security(verify_admin)])
def admin_list_keys(tenant: Optional[str] = None):
    return {"keys": [r.public() for r in require_keystore().list(tenant)]}


@app.post("/v1/admin/keys", status_code=status.HTTP_201_CREATED, dependencies=[Security(verify_admin)])
def admin_create_key(request: AdminCreateKeyRequest):
    return _issue(request.tenant, request.name)


@app.delete("/v1/admin/keys/{key_id}", dependencies=[Security(verify_admin)])
def admin_revoke_key(key_id: str):
    return _revoke(key_id, tenant_id=None, keep_one=False)


@app.get("/v1/usage")
def usage(tenant_id: str = Security(authorize)):
    """This tenant's limits and current usage."""
    return {"tenant_id": tenant_id, **LIMITS.usage(tenant_id)}


@app.get("/v1/egress/policy")
def egress_policy(tenant_id: str = Security(authorize)):
    """The hosts this tenant's sessions may request."""
    return {"tenant_id": tenant_id, "allowed": tenant_egress(tenant_id)}


@app.get("/v1/sessions/{session_id}/egress")
def egress_log(session_id: str, limit: int = 200, tenant_id: str = Security(authorize)):
    """Every outbound connection this session attempted, allowed or denied."""
    rec = get_authorized_session(session_id, tenant_id)
    return {"session_id": session_id, "events": rec.workspace.egress_events(limit=max(1, min(limit, 1000)))}


@app.post("/v1/sessions/{session_id}/write")
def write_file(session_id: str, request: WriteFileRequest, tenant_id: str = Security(authorize)):
    rec = get_authorized_session(session_id, tenant_id)
    try:
        msg = rec.workspace.write_file(request.path, request.content)
        return {"status": "success", "message": msg}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except QuotaExceededError as e:
        raise HTTPException(status_code=413, detail=str(e))


@app.get("/v1/sessions/{session_id}/read", response_model=ReadFileResponse)
def read_file(session_id: str, path: str, tenant_id: str = Security(authorize)):
    rec = get_authorized_session(session_id, tenant_id)
    try:
        content = rec.workspace.read_file(path)
        return ReadFileResponse(path=path, content=content)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/v1/sessions/{session_id}/exec", response_model=RunCommandResponse)
def run_command(session_id: str, request: RunCommandRequest, tenant_id: str = Security(authorize),
                header_key: Optional[str] = Security(api_key_header)):
    rec = get_authorized_session(session_id, tenant_id)
    result = run_in_session(rec, request.command, request.timeout_seconds, actor=actor_for_key(header_key))
    return RunCommandResponse(command=request.command, output=format_result(result), **result)


@app.post("/v1/sessions/{session_id}/import")
def import_repo(session_id: str, request: ImportRepoRequest, tenant_id: str = Security(authorize),
                header_key: Optional[str] = Security(api_key_header)):
    """Imports a public GitHub repository into /workspace/<path> (default: the repo name).

    The server downloads the archive (size-capped) and it is unpacked inside the
    sandbox, within the workspace disk quota. The session needs no egress for this.
    """
    rec = get_authorized_session(session_id, tenant_id)
    return import_repo_into(rec, request.repo, request.ref, request.path, actor=actor_for_key(header_key))


@app.put("/v1/sessions/{session_id}/files")
async def upload(session_id: str, path: str, request: Request, tenant_id: str = Security(authorize),
                 header_key: Optional[str] = Security(api_key_header)):
    """Uploads the raw request body (any bytes) to /workspace/<path>, creating folders as needed."""
    rec = get_authorized_session(session_id, tenant_id)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Uploads are limited to {UPLOAD_MAX_BYTES // 2**20} MB.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"Uploads are limited to {UPLOAD_MAX_BYTES // 2**20} MB.")
        chunks.append(chunk)
    return await run_in_threadpool(upload_file, rec, path, b"".join(chunks), actor_for_key(header_key))


def _attachment(data: bytes, filename: str, media_type: str) -> Response:
    safe = "".join(c for c in filename if c.isalnum() or c in "._-") or "download"
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{safe}"',
                             "X-Content-Type-Options": "nosniff"})


@app.get("/v1/sessions/{session_id}/files")
def download(session_id: str, path: str, tenant_id: str = Security(authorize),
             header_key: Optional[str] = Security(api_key_header)):
    """Downloads one file from /workspace as raw bytes."""
    rec = get_authorized_session(session_id, tenant_id)
    data = download_file(rec, path, actor=actor_for_key(header_key))
    return _attachment(data, os.path.basename(path.rstrip("/")), "application/octet-stream")


@app.get("/v1/sessions/{session_id}/archive")
def download_folder(session_id: str, path: str = ".", tenant_id: str = Security(authorize),
                    header_key: Optional[str] = Security(api_key_header)):
    """Downloads a folder (default: the whole workspace) as a .tar.gz built inside the sandbox."""
    rec = get_authorized_session(session_id, tenant_id)
    data = download_archive(rec, path, actor=actor_for_key(header_key))
    base = "workspace" if path.strip() in ("", ".") else os.path.basename(path.strip().rstrip("/"))
    return _attachment(data, f"{base}.tar.gz", "application/gzip")


@app.post("/v1/sessions/{session_id}/snapshots", status_code=status.HTTP_201_CREATED)
def snapshot_session(session_id: str, request: CreateSnapshotRequest, tenant_id: str = Security(authorize),
                     header_key: Optional[str] = Security(api_key_header)):
    """Saves /workspace (or one folder of it) as a snapshot that outlives the session."""
    rec = get_authorized_session(session_id, tenant_id)
    return create_snapshot(rec, request.name, request.path, actor=actor_for_key(header_key))


@app.post("/v1/sessions/{session_id}/restore")
def restore_into_session(session_id: str, request: RestoreSnapshotRequest, tenant_id: str = Security(authorize),
                         header_key: Optional[str] = Security(api_key_header)):
    """Unpacks a snapshot into this session's workspace (existing files with the same names are replaced)."""
    rec = get_authorized_session(session_id, tenant_id)
    return restore_snapshot(rec, request.snapshot_id, request.path, actor=actor_for_key(header_key))


@app.get("/v1/snapshots")
def list_snapshots(tenant_id: str = Security(authorize)):
    store = require_snapshots()
    return {"tenant_id": tenant_id, "snapshots": store.list(tenant_id), "limit": store.limit,
            "max_mb": store.max_bytes // 2**20, "retention_days": store.retention_days}


@app.delete("/v1/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str, tenant_id: str = Security(authorize)):
    try:
        return {"deleted": require_snapshots().delete(tenant_id, snapshot_id)}
    except SnapshotError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@app.put("/v1/sessions/{session_id}/secrets")
def put_secrets(session_id: str, request: SetSecretsRequest, tenant_id: str = Security(authorize),
                header_key: Optional[str] = Security(api_key_header)):
    """Adds or replaces secrets: environment variables for every later command (and new terminals),
    masked if a command prints them. Values are never returned."""
    rec = get_authorized_session(session_id, tenant_id)
    return {"session_id": session_id, "names": set_secrets(rec, request.secrets, actor=actor_for_key(header_key))}


@app.get("/v1/sessions/{session_id}/secrets")
def list_secrets(session_id: str, tenant_id: str = Security(authorize)):
    return {"session_id": session_id, "names": sorted(get_authorized_session(session_id, tenant_id).workspace.secrets)}


@app.delete("/v1/sessions/{session_id}/secrets/{name}")
def remove_secret(session_id: str, name: str, tenant_id: str = Security(authorize),
                  header_key: Optional[str] = Security(api_key_header)):
    rec = get_authorized_session(session_id, tenant_id)
    return {"session_id": session_id, "names": delete_secret(rec, name, actor=actor_for_key(header_key))}


@app.post("/v1/sessions/{session_id}/previews")
def expose(session_id: str, request: ExposePortRequest, tenant_id: str = Security(authorize),
           header_key: Optional[str] = Security(api_key_header)):
    """Gives a port the session listens on a public preview URL (long-running sessions only).
    Anyone with the URL can open it while the session is open; the server must listen on 0.0.0.0."""
    return expose_port(get_authorized_session(session_id, tenant_id), request.port, actor=actor_for_key(header_key))


@app.delete("/v1/sessions/{session_id}/previews/{port}")
def unexpose(session_id: str, port: int, tenant_id: str = Security(authorize),
             header_key: Optional[str] = Security(api_key_header)):
    unexpose_port(get_authorized_session(session_id, tenant_id), port, actor=actor_for_key(header_key))
    return {"port": port, "status": "closed"}


@app.get("/internal/preview-allowed", include_in_schema=False)
def preview_allowed(request: Request, domain: str = ""):
    """Caddy's on-demand TLS check: may it get a certificate for this hostname? Only answers Caddy
    itself (a direct local request; proxied requests carry X-Forwarded-For)."""
    direct = request.client is not None and request.client.host in ("127.0.0.1", "::1") \
        and "x-forwarded-for" not in request.headers
    target = previews.parse_host(domain, PREVIEW_DOMAIN)
    if not direct or target is None:
        raise HTTPException(status_code=404)
    with sessions_lock:
        ok = any(r.persistent and secrets.compare_digest(r.workspace.preview_token, target[1])
                 and target[0] in r.workspace.exposed_ports for r in active_sessions.values())
    if not ok:
        raise HTTPException(status_code=404)
    return {"ok": True}


async def open_terminal(websocket: WebSocket, rec: SessionRecord, actor: str) -> None:
    """Accepts the socket and runs a shell in the session until either side ends."""
    rows = int(websocket.query_params.get("rows", 24) or 24)
    cols = int(websocket.query_params.get("cols", 80) or 80)
    try:
        pty = await asyncio.to_thread(rec.workspace.session_container.open_pty, rows, cols, rec.workspace.secrets)
    except Exception:
        logger.exception("Opening a terminal failed")
        await websocket.close(1011)
        return
    await websocket.accept()
    started = time.monotonic()
    record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="terminal", command="open terminal")
    try:
        await terminal_ws.bridge(websocket, pty, on_input=rec.touch)
    finally:
        record_audit(rec.tenant_id, session_id=rec.session_id, actor=actor, kind="terminal", command="close terminal",
                     duration_s=round(time.monotonic() - started, 3))


@app.websocket("/v1/sessions/{session_id}/terminal")
async def terminal(websocket: WebSocket, session_id: str):
    """Interactive bash on a PTY in a long-running session (protocol: src/api/terminal_ws.py).
    Authenticate with the X-API-Key header (or Authorization: Bearer)."""
    key = websocket.headers.get("x-api-key") or ""
    auth = websocket.headers.get("authorization") or ""
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    tenant_id = tenant_for_key(key) if key else None
    if tenant_id is None:
        await websocket.close(1008)
        return
    try:
        LIMITS.check_rate(tenant_id)
        rec = get_authorized_session(session_id, tenant_id)
        require_persistent(rec, "The interactive terminal")
    except (HTTPException, LimitExceeded):
        await websocket.close(1008)
        return
    await open_terminal(websocket, rec, actor_for_key(key))


@app.get("/v1/audit")
def audit_log(limit: int = 100, before: Optional[str] = None, tenant_id: str = Security(authorize)):
    """This tenant's command history, newest first: commands and outcomes, never output.
    Pass `next` back as `before` for the next page."""
    if AUDIT is None:
        raise HTTPException(status_code=501, detail="The audit log is not enabled on this server (SANDBOX_AUDIT).")
    entries, cursor = AUDIT.list(tenant_id, limit, before)
    hidden = {"sk", "expires_at"}
    return {"tenant_id": tenant_id, "entries": [{k: v for k, v in e.items() if k not in hidden} for e in entries],
            "next": cursor, "retention_days": AUDIT.retention_days}


@app.delete("/v1/sessions/{session_id}", status_code=status.HTTP_200_OK)
def destroy_session(session_id: str, tenant_id: str = Security(authorize)):
    end_session(session_id, tenant_id)
    return {"status": "terminated", "session_id": session_id}


# ------------------------------------------------------------------ remote MCP endpoint (/mcp)

class McpOps:
    """What the remote MCP endpoint does with sessions: the same checks, metering and audit
    log as the REST API. Tenants come from RemoteMcp's per-request authentication."""

    actor_for_key = staticmethod(actor_for_key)

    @staticmethod
    def authenticate(key: str) -> Optional[str]:
        return tenant_for_key(key)

    @staticmethod
    def check_rate(tenant_id: str) -> None:
        from src.api.mcp_remote import AuthError

        try:
            LIMITS.check_rate(tenant_id)
        except LimitExceeded as e:
            raise AuthError(429, str(e), e.retry_after)

    @staticmethod
    def start(tenant_id: str, egress: List[str], persistent: bool = False) -> str:
        return start_session(tenant_id, egress_request=egress, persistent=persistent).session_id

    @staticmethod
    def expose(tenant_id: str, session_id: str, port: int, actor: str) -> dict:
        return expose_port(get_authorized_session(session_id, tenant_id), port, actor=actor)

    @staticmethod
    def exists(tenant_id: str, session_id: str) -> bool:
        with sessions_lock:
            rec = active_sessions.get(session_id)
        return rec is not None and rec.tenant_id == tenant_id

    @staticmethod
    def end(tenant_id: str, session_id: str) -> None:
        try:
            end_session(session_id, tenant_id)
        except HTTPException:
            pass

    @staticmethod
    def run(tenant_id: str, session_id: str, command: str, timeout: int, actor: str) -> str:
        rec = get_authorized_session(session_id, tenant_id)
        return format_result(run_in_session(rec, command, timeout, actor=actor))

    @staticmethod
    def write_file(tenant_id: str, session_id: str, path: str, content: str) -> str:
        return get_authorized_session(session_id, tenant_id).workspace.write_file(path, content)

    @staticmethod
    def read_file(tenant_id: str, session_id: str, path: str) -> str:
        return get_authorized_session(session_id, tenant_id).workspace.read_file(path)

    @staticmethod
    def import_repo(tenant_id: str, session_id: str, repo: str, ref: Optional[str], path: Optional[str], actor: str) -> dict:
        return import_repo_into(get_authorized_session(session_id, tenant_id), repo, ref, path, actor=actor)

    @staticmethod
    def snapshot(tenant_id: str, session_id: str, name: Optional[str], path: str, actor: str) -> dict:
        return create_snapshot(get_authorized_session(session_id, tenant_id), name, path, actor=actor)

    @staticmethod
    def snapshots(tenant_id: str) -> List[dict]:
        return require_snapshots().list(tenant_id)

    @staticmethod
    def restore(tenant_id: str, session_id: str, snapshot_id: str, path: str, actor: str) -> dict:
        return restore_snapshot(get_authorized_session(session_id, tenant_id), snapshot_id, path, actor=actor)

    @staticmethod
    def egress_events(tenant_id: str, session_id: str, limit: int) -> List[dict]:
        return get_authorized_session(session_id, tenant_id).workspace.egress_events(limit=limit)

    @staticmethod
    def audit(tenant_id: str, limit: int) -> List[dict]:
        return AUDIT.list(tenant_id, limit)[0] if AUDIT is not None else []

    @staticmethod
    def info(tenant_id: str, session_id: str) -> str:
        rec = get_authorized_session(session_id, tenant_id)
        usage = LIMITS.usage(tenant_id)
        limits = usage.get("limits", {})
        return "\n".join([
            f"Session: {session_id} (tenant {tenant_id})",
            f"Internet: {', '.join(rec.egress) if rec.egress else 'none'}",
            f"Long-running: {'yes (background processes keep running; preview_url works)' if rec.persistent else 'no (add ?persistent=1 to the MCP URL)'}",
            f"Allowed to request: {', '.join(tenant_egress(tenant_id)) or 'nothing'} (add ?egress=… to the MCP URL)",
            f"Disk quota: {rec.workspace.quota_bytes // 2**20} MB",
            f"Limits: {limits.get('max_sessions')} sessions, {limits.get('requests_per_minute')} requests/min, "
            f"{limits.get('max_concurrent_exec')} concurrent commands",
        ])

    @staticmethod
    def describe_error(e: Exception) -> str:
        if isinstance(e, HTTPException):
            return str(e.detail)
        if isinstance(e, (LimitExceeded, PermissionError, QuotaExceededError, WorkspaceFileError, SnapshotError)):
            return str(e)
        logger.exception("MCP tool failed")
        return "The sandbox failed to run that; try again, or reset_sandbox."


try:
    from src.api.mcp_remote import RemoteMcp
except ImportError:          # the mcp package isn't installed: no remote MCP endpoint
    RemoteMcp = None

REMOTE_MCP = RemoteMcp(McpOps(), version=app.version) if RemoteMcp is not None else None
if REMOTE_MCP is not None:
    from starlette.routing import Route

    app.router.routes.append(Route("/mcp", endpoint=REMOTE_MCP, methods=["GET", "POST", "DELETE"]))


# Preview hostnames (<port>-<token>.<SANDBOX_PREVIEW_DOMAIN>) are proxied before routing.
app.add_middleware(previews.PreviewMiddleware, domain=lambda: PREVIEW_DOMAIN, resolve=resolve_preview)


# ------------------------------------------------------------------ developer console

from src.console.routes import ConsoleConfig, build_console_router  # noqa: E402  (after the API is defined)

class ConsoleSessions:
    """What the console's workspace pages may do with sessions, for a signed-in tenant."""

    ttl_seconds = SESSION_TTL_SECONDS

    def list(self, tenant_id: str) -> List[dict]:
        return [r.summary() for r in tenant_sessions(tenant_id)]

    def get(self, tenant_id: str, session_id: str) -> dict:
        return get_authorized_session(session_id, tenant_id).summary()

    def create(self, tenant_id: str, egress: List[str], persistent: bool = False) -> dict:
        LIMITS.check_rate(tenant_id)
        return start_session(tenant_id, egress_request=egress, persistent=persistent).summary()

    def run(self, tenant_id: str, session_id: str, command: str, timeout_seconds: int,
            actor: str = "console", audit_command: Optional[str] = None) -> dict:
        LIMITS.check_rate(tenant_id)
        return run_in_session(get_authorized_session(session_id, tenant_id), command, timeout_seconds,
                              actor=actor, audit_command=audit_command)

    def import_repo(self, tenant_id: str, session_id: str, repo: str, ref: Optional[str], path: Optional[str],
                    actor: str = "console") -> dict:
        LIMITS.check_rate(tenant_id)
        return import_repo_into(get_authorized_session(session_id, tenant_id), repo, ref, path, actor=actor)

    def destroy(self, tenant_id: str, session_id: str) -> None:
        LIMITS.check_rate(tenant_id)
        end_session(session_id, tenant_id)

    # Files and snapshots. Errors come back as HTTPException (see _transfer_error).
    @property
    def upload_max_bytes(self) -> int:
        return UPLOAD_MAX_BYTES

    @property
    def snapshots_enabled(self) -> bool:
        return SNAPSHOTS is not None

    def upload(self, tenant_id: str, session_id: str, path: str, data: bytes, actor: str = "console") -> dict:
        LIMITS.check_rate(tenant_id)
        return upload_file(get_authorized_session(session_id, tenant_id), path, data, actor=actor)

    def download(self, tenant_id: str, session_id: str, path: str, actor: str = "console") -> bytes:
        LIMITS.check_rate(tenant_id)
        return download_file(get_authorized_session(session_id, tenant_id), path, actor=actor)

    def download_archive(self, tenant_id: str, session_id: str, path: str, actor: str = "console") -> bytes:
        LIMITS.check_rate(tenant_id)
        return download_archive(get_authorized_session(session_id, tenant_id), path, actor=actor)

    def snapshot(self, tenant_id: str, session_id: str, name: Optional[str], actor: str = "console") -> dict:
        LIMITS.check_rate(tenant_id)
        return create_snapshot(get_authorized_session(session_id, tenant_id), name, ".", actor=actor)

    def snapshots(self, tenant_id: str) -> List[dict]:
        return SNAPSHOTS.list(tenant_id) if SNAPSHOTS is not None else []

    def create_from_snapshot(self, tenant_id: str, egress: List[str], snapshot_id: str, actor: str = "console",
                             persistent: bool = False) -> dict:
        LIMITS.check_rate(tenant_id)
        try:
            require_snapshots().meta(tenant_id, snapshot_id)
        except SnapshotError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        rec = start_session(tenant_id, egress_request=egress, persistent=persistent)
        try:
            restore_snapshot(rec, snapshot_id, actor=actor)
        except Exception:
            end_session(rec.session_id, tenant_id)
            raise
        return rec.summary()

    def secrets(self, tenant_id: str, session_id: str) -> List[str]:
        return sorted(get_authorized_session(session_id, tenant_id).workspace.secrets)

    def set_secret(self, tenant_id: str, session_id: str, name: str, value: str, actor: str = "console") -> List[str]:
        LIMITS.check_rate(tenant_id)
        return set_secrets(get_authorized_session(session_id, tenant_id), {name: value}, actor=actor)

    def delete_secret(self, tenant_id: str, session_id: str, name: str, actor: str = "console") -> List[str]:
        LIMITS.check_rate(tenant_id)
        return delete_secret(get_authorized_session(session_id, tenant_id), name, actor=actor)

    # Long-running sessions: previews and the interactive terminal.
    @property
    def previews_enabled(self) -> bool:
        return bool(PREVIEW_DOMAIN)

    def expose(self, tenant_id: str, session_id: str, port: int, actor: str = "console") -> dict:
        LIMITS.check_rate(tenant_id)
        return expose_port(get_authorized_session(session_id, tenant_id), port, actor=actor)

    def unexpose(self, tenant_id: str, session_id: str, port: int, actor: str = "console") -> None:
        LIMITS.check_rate(tenant_id)
        unexpose_port(get_authorized_session(session_id, tenant_id), port, actor=actor)

    async def terminal(self, websocket: WebSocket, tenant_id: str, session_id: str, actor: str = "console") -> None:
        """Closes with 1008 unless the session is the tenant's and long-running; else runs the terminal."""
        try:
            LIMITS.check_rate(tenant_id)
            rec = get_authorized_session(session_id, tenant_id)
            require_persistent(rec, "The interactive terminal")
        except (HTTPException, LimitExceeded):
            await websocket.close(1008)
            return
        await open_terminal(websocket, rec, actor)

    def delete_snapshot(self, tenant_id: str, snapshot_id: str) -> dict:
        LIMITS.check_rate(tenant_id)
        try:
            return require_snapshots().delete(tenant_id, snapshot_id)
        except SnapshotError as e:
            raise HTTPException(status_code=e.status, detail=str(e))

    def close_all(self, tenant_id: str) -> int:
        return close_tenant_sessions(tenant_id)


def sns_notifier(topic_arn: Optional[str]):
    """(subject, message) -> None, publishing to an SNS topic (its email subscribers get it)."""
    if not topic_arn:
        return None
    import boto3

    client = boto3.client("sns", region_name=os.getenv("AWS_REGION"))

    def notify(subject: str, message: str) -> None:
        client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)

    return notify


CONSOLE = ConsoleConfig.from_env()
if CONSOLE is not None:
    if KEYSTORE is None or ACCOUNTS is None:
        raise RuntimeError("The console needs SANDBOX_KEYSTORE and SANDBOX_ACCOUNTS.")
    app.mount("/console/static",
              StaticFiles(directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "console", "static")),
              name="console-static")
    app.include_router(build_console_router(
        CONSOLE,
        keystore=lambda: KEYSTORE,
        accounts=lambda: ACCOUNTS,
        limits=lambda tenant: LIMITS.usage(tenant),
        egress=tenant_egress,
        sessions=ConsoleSessions(),
        audit=lambda: AUDIT,
        github_app=lambda: GITHUB_APP,
        notify=sns_notifier(os.getenv("SANDBOX_NOTIFY_TOPIC_ARN")),
    ))

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/console")
