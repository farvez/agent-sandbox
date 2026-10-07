"""Keeps the session registry across API restarts (SANDBOX_SESSION_STATE=<file>).

A code deploy restarts only the API process: sandbox workspaces and egress proxies keep
running on the host. The API saves its open sessions here when it stops (and every minute,
in case it crashes) and takes them back when it starts, so users keep their sessions.

The file holds each session's proxy credentials, so it is written with owner-only access.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import List, Optional


def save(path: Optional[str], sessions: List[dict]) -> None:
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sessions-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "sessions": sessions}, f)
        if hasattr(os, "fchmod"):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)   # atomic: a crash mid-write never leaves a half file
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path: Optional[str]) -> List[dict]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return sessions if isinstance(sessions, list) else []
