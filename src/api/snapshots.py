"""Workspace snapshots: a saved copy of a session's /workspace that a later session can restore.

The archive is built and unpacked inside the sandbox (see SandboxedWorkspace.archive/extract);
this module only stores the bytes. Each snapshot is two objects under <prefix>/<tenant>/:
<id>.tar.gz and <id>.json (name, size, source session, time). Snapshots expire after
SANDBOX_SNAPSHOT_RETENTION_DAYS (an S3 lifecycle rule deletes them; listings hide expired ones
sooner), and a tenant keeps at most SANDBOX_SNAPSHOT_LIMIT of them.

Backends (SANDBOX_SNAPSHOTS): s3://<bucket>[/<prefix>] on the server, dir:<path> for local
development.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from typing import List, Optional, Tuple

DEFAULT_LIMIT = 10
DEFAULT_MAX_MB = 200
DEFAULT_RETENTION_DAYS = 30

SNAPSHOT_ID = re.compile(r"^snap_[0-9a-f]{12}$")
SAFE_TENANT = re.compile(r"^[A-Za-z0-9_.@-]{1,100}$")


class SnapshotError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def tenant_dir(tenant_id: str) -> str:
    if SAFE_TENANT.match(tenant_id) and tenant_id not in (".", ".."):
        return tenant_id
    return "t-" + hashlib.sha256(tenant_id.encode()).hexdigest()[:32]


def clean_name(name: Optional[str]) -> str:
    name = " ".join((name or "").split())[:80]
    return name or "snapshot"


class _DirBackend:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.split("/"))

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def get(self, key: str) -> Optional[bytes]:
        try:
            with open(self._path(key), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass

    def list(self, prefix: str) -> List[str]:
        folder = self._path(prefix.rstrip("/"))
        if not os.path.isdir(folder):
            return []
        return [f"{prefix.rstrip('/')}/{n}" for n in sorted(os.listdir(folder))]


class _S3Backend:
    def __init__(self, bucket: str, prefix: str, region: Optional[str] = None):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = boto3.client("s3", region_name=region)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ServerSideEncryption="AES256")

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self._s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            return None

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self._key(key))

    def list(self, prefix: str) -> List[str]:
        full = self._key(prefix)
        keys: List[str] = []
        for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][len(self._key("")):] if self.prefix else obj["Key"])
        return keys


class SnapshotStore:
    def __init__(self, backend, limit: int = DEFAULT_LIMIT, max_bytes: int = DEFAULT_MAX_MB * 2**20,
                 retention_days: int = DEFAULT_RETENTION_DAYS, clock=time.time):
        self._backend = backend
        self.limit = limit
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self._clock = clock

    @classmethod
    def from_config(cls, spec: Optional[str], region: Optional[str] = None, env=os.environ) -> Optional["SnapshotStore"]:
        if not spec:
            return None
        kwargs = dict(
            limit=int(env.get("SANDBOX_SNAPSHOT_LIMIT", DEFAULT_LIMIT)),
            max_bytes=int(env.get("SANDBOX_SNAPSHOT_MAX_MB", DEFAULT_MAX_MB)) * 2**20,
            retention_days=int(env.get("SANDBOX_SNAPSHOT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        )
        if spec.startswith("s3://"):
            bucket, _, prefix = spec[5:].partition("/")
            if bucket:
                return cls(_S3Backend(bucket, prefix, region), **kwargs)
        kind, _, target = spec.partition(":")
        if kind == "dir" and target:
            return cls(_DirBackend(target), **kwargs)
        raise RuntimeError(f"SANDBOX_SNAPSHOTS must be 's3://<bucket>[/<prefix>]' or 'dir:<path>', got {spec!r}.")

    def _expired(self, meta: dict) -> bool:
        return meta.get("created_at", 0) + self.retention_days * 86400 < self._clock()

    def list(self, tenant_id: str) -> List[dict]:
        """Newest first, without expired ones."""
        folder = tenant_dir(tenant_id)
        found = []
        for key in self._backend.list(f"{folder}/"):
            if not key.endswith(".json"):
                continue
            raw = self._backend.get(key)
            if raw is None:
                continue
            try:
                meta = json.loads(raw)
            except ValueError:
                continue
            if isinstance(meta, dict) and SNAPSHOT_ID.match(str(meta.get("id", ""))) and not self._expired(meta):
                found.append(meta)
        return sorted(found, key=lambda m: m.get("created_at", 0), reverse=True)

    def create(self, tenant_id: str, data: bytes, name: Optional[str] = None,
               session_id: Optional[str] = None) -> dict:
        if len(data) > self.max_bytes:
            raise SnapshotError(f"The workspace archive is larger than the {self.max_bytes // 2**20} MB snapshot limit.", 413)
        if len(self.list(tenant_id)) >= self.limit:
            raise SnapshotError(f"You already have {self.limit} snapshots, the most allowed. Delete one first.", 409)
        now = self._clock()
        meta = {
            "id": f"snap_{secrets.token_hex(6)}", "name": clean_name(name), "size_bytes": len(data),
            "session_id": session_id, "created_at": now,
            "expires_at": now + self.retention_days * 86400,
        }
        folder = tenant_dir(tenant_id)
        self._backend.put(f"{folder}/{meta['id']}.tar.gz", data)
        self._backend.put(f"{folder}/{meta['id']}.json", json.dumps(meta).encode())
        return meta

    def get(self, tenant_id: str, snapshot_id: str) -> Tuple[dict, bytes]:
        meta = self.meta(tenant_id, snapshot_id)
        data = self._backend.get(f"{tenant_dir(tenant_id)}/{snapshot_id}.tar.gz")
        if data is None:
            raise SnapshotError(f"Snapshot {snapshot_id} not found.", 404)
        return meta, data

    def meta(self, tenant_id: str, snapshot_id: str) -> dict:
        if not SNAPSHOT_ID.match(snapshot_id or ""):
            raise SnapshotError(f"Snapshot {snapshot_id} not found.", 404)
        raw = self._backend.get(f"{tenant_dir(tenant_id)}/{snapshot_id}.json")
        meta = json.loads(raw) if raw else None
        if not meta or self._expired(meta):
            raise SnapshotError(f"Snapshot {snapshot_id} not found.", 404)
        return meta

    def delete(self, tenant_id: str, snapshot_id: str) -> dict:
        meta = self.meta(tenant_id, snapshot_id)
        folder = tenant_dir(tenant_id)
        self._backend.delete(f"{folder}/{snapshot_id}.tar.gz")
        self._backend.delete(f"{folder}/{snapshot_id}.json")
        return meta
