import os
import json
import time
import uuid
import asyncio
import secrets
import threading
import logging
from typing import Dict, List, Optional, Set, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from src.egress.gateway import shutdown_gateway
from src.egress.proxy import expand_rules, rule_covered
from src.step5_agent.sandbox import SandboxedWorkspace
from src.step5_agent.workspace_pool import QuotaExceededError, WorkspaceCapacityError



def load_api_keys(multi: Optional[str], single: Optional[str]) -> List[Tuple[bytes, str]]:
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
        raise RuntimeError("No API keys configured. Set SANDBOX_API_KEYS or SANDBOX_API_KEY.")
    keys = [key for _, key in pairs]
    if len(set(keys)) != len(keys):
        raise RuntimeError("The same API key is assigned to more than one tenant.")
    return [(key.encode(), tenant) for tenant, key in pairs]


API_KEYS = load_api_keys(os.getenv("SANDBOX_API_KEYS"), os.getenv("SANDBOX_API_KEY"))


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


class CreateSessionResponse(BaseModel):
    session_id: str
    tenant_id: str
    egress: List[str]
    disk_quota_mb: int
    created_at: float
    status: str


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
    output: str


class SessionRecord:
    def __init__(self, session_id: str, workspace: SandboxedWorkspace, tenant_id: str):
        self.session_id = session_id
        self.workspace = workspace
        self.tenant_id = tenant_id
        self.created_at = time.time()
        self.last_accessed_at = time.time()

    def touch(self):
        self.last_accessed_at = time.time()


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
    sweeper_task = asyncio.create_task(session_ttl_sweeper())
    yield
    sweeper_task.cancel()
    with sessions_lock:
        remaining = list(active_sessions.values())
        active_sessions.clear()
    for rec in remaining:
        rec.workspace.cleanup()
    shutdown_gateway()


app = FastAPI(
    title="Agent Sandbox Execution API",
    version="1.0.0",
    description="Multi-tenant, isolated execution substrate for AI coding agents.",
    lifespan=lifespan,
)


def verify_api_key(header_key: Optional[str] = Security(api_key_header)) -> str:
    """Authenticates the caller and returns their tenant ID."""
    tenant_id = None
    if header_key:
        presented = header_key.encode()
        # Compare against every key without returning early, so response time
        # doesn't reveal which (or whether any) configured key was close.
        for key, tenant in API_KEYS:
            if secrets.compare_digest(presented, key):
                tenant_id = tenant
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return tenant_id


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


@app.get("/healthz")
def health_check():
    with sessions_lock:
        count = len(active_sessions)
    return {"status": "healthy", "active_sessions": count}


@app.post("/v1/sessions", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(request: CreateSessionRequest, tenant_id: str = Security(verify_api_key)):
    if request.template not in ALLOWED_TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template '{request.template}' not permitted. Allowed: {list(ALLOWED_TEMPLATES)}"
        )

    egress = expand_rules(request.egress)
    allowed = EGRESS_POLICY.get(tenant_id, [])
    refused = [rule for rule in egress if not rule_covered(rule, allowed)]
    if refused:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Egress not permitted for this tenant: {refused}. Allowed: {allowed}",
        )

    session_id = f"sbx_{uuid.uuid4().hex[:12]}"
    try:
        workspace = SandboxedWorkspace(
            base_image=request.template, egress=egress, session_id=session_id, tenant_id=tenant_id
        )
    except WorkspaceCapacityError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))

    with sessions_lock:
        active_sessions[session_id] = SessionRecord(
            session_id=session_id,
            workspace=workspace,
            tenant_id=tenant_id,
        )

    return CreateSessionResponse(
        session_id=session_id,
        tenant_id=tenant_id,
        egress=egress,
        disk_quota_mb=workspace.quota_bytes // 2**20,
        created_at=time.time(),
        status="ready",
    )


@app.get("/v1/egress/policy")
def egress_policy(tenant_id: str = Security(verify_api_key)):
    """The hosts this tenant's sessions may request."""
    return {"tenant_id": tenant_id, "allowed": EGRESS_POLICY.get(tenant_id, [])}


@app.get("/v1/sessions/{session_id}/egress")
def egress_log(session_id: str, limit: int = 200, tenant_id: str = Security(verify_api_key)):
    """Every outbound connection this session attempted, allowed or denied."""
    rec = get_authorized_session(session_id, tenant_id)
    return {"session_id": session_id, "events": rec.workspace.egress_events(limit=max(1, min(limit, 1000)))}


@app.post("/v1/sessions/{session_id}/write")
def write_file(session_id: str, request: WriteFileRequest, tenant_id: str = Security(verify_api_key)):
    rec = get_authorized_session(session_id, tenant_id)
    try:
        msg = rec.workspace.write_file(request.path, request.content)
        return {"status": "success", "message": msg}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except QuotaExceededError as e:
        raise HTTPException(status_code=413, detail=str(e))


@app.get("/v1/sessions/{session_id}/read", response_model=ReadFileResponse)
def read_file(session_id: str, path: str, tenant_id: str = Security(verify_api_key)):
    rec = get_authorized_session(session_id, tenant_id)
    try:
        content = rec.workspace.read_file(path)
        return ReadFileResponse(path=path, content=content)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/v1/sessions/{session_id}/exec", response_model=RunCommandResponse)
def run_command(session_id: str, request: RunCommandRequest, tenant_id: str = Security(verify_api_key)):
    rec = get_authorized_session(session_id, tenant_id)
    raw_output = rec.workspace.run_command(
        command=request.command,
        timeout_seconds=request.timeout_seconds,
    )
    return RunCommandResponse(command=request.command, output=raw_output)


@app.delete("/v1/sessions/{session_id}", status_code=status.HTTP_200_OK)
def destroy_session(session_id: str, tenant_id: str = Security(verify_api_key)):
    with sessions_lock:
        rec = active_sessions.get(session_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Session not found")
        if rec.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        active_sessions.pop(session_id, None)

    rec.workspace.cleanup()

    return {"status": "terminated", "session_id": session_id}