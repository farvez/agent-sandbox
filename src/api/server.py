import os
import time
import uuid
import asyncio
from typing import Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from src.step5_agent.sandbox import SandboxedWorkspace

# Configuration
API_KEY_ENV = os.getenv("SANDBOX_API_KEY", "sb_live_secret_key_123")
SESSION_TTL_SECONDS = int(os.getenv("SANDBOX_SESSION_TTL", "1800"))  # 30 min default
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# Request / Response Schemas
class CreateSessionRequest(BaseModel):
    template: str = Field(default="sandbox-base:latest", description="Base docker image tag")
    metadata: Optional[Dict[str, str]] = None


class CreateSessionResponse(BaseModel):
    session_id: str
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


# In-Memory Session Registry
class SessionRecord:
    def __init__(self, session_id: str, workspace: SandboxedWorkspace):
        self.session_id = session_id
        self.workspace = workspace
        self.created_at = time.time()
        self.last_accessed_at = time.time()

    def touch(self):
        self.last_accessed_at = time.time()


active_sessions: Dict[str, SessionRecord] = {}


async def session_ttl_sweeper():
    """Background task that reaps sessions exceeding TTL limits."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired_ids = [
            sid for sid, rec in active_sessions.items()
            if now - rec.last_accessed_at > SESSION_TTL_SECONDS
        ]
        for sid in expired_ids:
            record = active_sessions.pop(sid, None)
            if record:
                record.workspace.cleanup()


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweeper_task = asyncio.create_task(session_ttl_sweeper())
    yield
    sweeper_task.cancel()
    # Teardown all sessions on shutdown
    for rec in active_sessions.values():
        rec.workspace.cleanup()
    active_sessions.clear()


app = FastAPI(
    title="Agent Sandbox Execution API",
    version="1.0.0",
    description="Multi-tenant, isolated execution substrate for AI coding agents.",
    lifespan=lifespan,
)


def verify_api_key(header_key: Optional[str] = Security(api_key_header)) -> str:
    """Enforces API Key authentication on protected endpoints."""
    if not header_key or header_key != API_KEY_ENV:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return header_key


@app.get("/healthz")
def health_check():
    """Liveness probe for AWS Load Balancer target health checks."""
    return {"status": "healthy", "active_sessions": len(active_sessions)}


@app.post("/v1/sessions", response_model=CreateSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(request: CreateSessionRequest, _key: str = Security(verify_api_key)):
    """Allocates a dedicated ephemeral directory and container execution boundary."""
    session_id = f"sbx_{uuid.uuid4().hex[:12]}"
    workspace = SandboxedWorkspace(base_image=request.template)
    active_sessions[session_id] = SessionRecord(session_id=session_id, workspace=workspace)

    return CreateSessionResponse(
        session_id=session_id,
        created_at=time.time(),
        status="ready",
    )


@app.post("/v1/sessions/{session_id}/write")
def write_file(session_id: str, request: WriteFileRequest, _key: str = Security(verify_api_key)):
    """Writes files securely into the tenant workspace."""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    record = active_sessions[session_id]
    record.touch()
    try:
        msg = record.workspace.write_file(request.path, request.content)
        return {"status": "success", "message": msg}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.get("/v1/sessions/{session_id}/read", response_model=ReadFileResponse)
def read_file(session_id: str, path: str, _key: str = Security(verify_api_key)):
    """Reads files from the tenant workspace."""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    record = active_sessions[session_id]
    record.touch()
    try:
        content = record.workspace.read_file(path)
        return ReadFileResponse(path=path, content=content)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/v1/sessions/{session_id}/exec", response_model=RunCommandResponse)
def run_command(session_id: str, request: RunCommandRequest, _key: str = Security(verify_api_key)):
    """Executes a command inside the isolated container runtime."""
    if session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    record = active_sessions[session_id]
    record.touch()
    raw_output = record.workspace.run_command(
        command=request.command,
        timeout_seconds=request.timeout_seconds,
    )
    return RunCommandResponse(command=request.command, output=raw_output)


@app.delete("/v1/sessions/{session_id}", status_code=status.HTTP_200_OK)
def destroy_session(session_id: str, _key: str = Security(verify_api_key)):
    """Immediately terminates the session and wipes workspace files from disk."""
    record = active_sessions.pop(session_id, None)
    if not record:
        raise HTTPException(status_code=404, detail="Session not found")

    record.workspace.cleanup()
    return {"status": "terminated", "session_id": session_id}