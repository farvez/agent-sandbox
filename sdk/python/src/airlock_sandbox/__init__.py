"""Python SDK for agent-sandbox: isolated, network-controlled code execution for AI agents.

    from airlock_sandbox import Sandbox

    with Sandbox(egress=["pypi"]) as sbx:
        sbx.run("pip install requests", timeout=60).check()
        sbx.files.write("main.py", "import requests; print(requests.__version__)")
        print(sbx.run("python3 main.py").stdout)
"""
from airlock_sandbox.errors import (
    APIConnectionError,
    AuthenticationError,
    CapacityError,
    CommandError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    SandboxError,
    ValidationError,
)
from airlock_sandbox.sandbox import CommandResult, Files, Sandbox

__version__ = "0.1.1"

__all__ = [
    "Sandbox",
    "CommandResult",
    "Files",
    "SandboxError",
    "APIConnectionError",
    "AuthenticationError",
    "CapacityError",
    "CommandError",
    "NotFoundError",
    "PermissionDeniedError",
    "QuotaExceededError",
    "RateLimitError",
    "ValidationError",
    "__version__",
]
