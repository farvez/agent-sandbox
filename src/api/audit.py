"""Per-tenant command audit log: what ran, where, by whom, and how it ended.

One entry per command (API exec, console terminal, repo import): time, session, actor
(which API key or console user), the command as typed (capped), exit code, duration and
whether it timed out or ran out of memory. Command output is never stored.

Backends (SANDBOX_AUDIT): dynamodb:<table> on the server — partition key `tenant`,
sort key `sk` ("<time>#<id>", so a tenant's history is one newest-first Query), with
DynamoDB TTL on `expires_at` deleting entries after SANDBOX_AUDIT_RETENTION_DAYS
(default 90) — and sqlite:<path> for local development.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from decimal import Decimal
from typing import List, Optional, Tuple

MAX_COMMAND_CHARS = 2000
DEFAULT_RETENTION_DAYS = 90


def _sort_key(ts: float) -> str:
    # Fixed-width microseconds, so string order is time order.
    return f"{int(ts * 1_000_000):020d}#{secrets.token_hex(4)}"


class _SqliteAudit:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS audit (tenant TEXT, sk TEXT, expires_at REAL, data TEXT, "
                             "PRIMARY KEY (tenant, sk))")

    def put(self, tenant: str, sk: str, item: dict) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (tenant, sk, item["expires_at"], json.dumps(item)))

    def query(self, tenant: str, limit: int, before: Optional[str], now: float) -> List[dict]:
        sql = "SELECT data FROM audit WHERE tenant = ? AND expires_at > ?"
        args: list = [tenant, now]
        if before:
            sql += " AND sk < ?"
            args.append(before)
        sql += " ORDER BY sk DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [json.loads(r[0]) for r in self._db.execute(sql, args).fetchall()]


class _DynamoAudit:
    def __init__(self, table_name: str, region: Optional[str] = None):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, tenant: str, sk: str, item: dict) -> None:
        clean = {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in item.items() if v is not None}
        self._table.put_item(Item={"tenant": tenant, "sk": sk, **clean})

    def query(self, tenant: str, limit: int, before: Optional[str], now: float) -> List[dict]:
        from boto3.dynamodb.conditions import Attr, Key

        condition = Key("tenant").eq(tenant)
        if before:
            condition = condition & Key("sk").lt(before)
        kwargs: dict = {"KeyConditionExpression": condition, "ScanIndexForward": False,
                        # TTL deletion can lag by hours; hide entries that have expired.
                        "FilterExpression": Attr("expires_at").gt(Decimal(str(now)))}
        items: list = []
        while len(items) < limit:
            page = self._table.query(Limit=limit, **kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [{k: _number(v) for k, v in i.items() if k != "tenant"} for i in items[:limit]]


def _number(value):
    """DynamoDB returns every number as Decimal: exit codes come back as ints, times as floats."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class AuditLog:
    def __init__(self, backend, retention_days: int = DEFAULT_RETENTION_DAYS, clock=time.time):
        self._backend = backend
        self.retention_days = retention_days
        self._clock = clock

    @classmethod
    def from_config(cls, spec: Optional[str], region: Optional[str] = None,
                    retention_days: int = DEFAULT_RETENTION_DAYS) -> Optional["AuditLog"]:
        if not spec:
            return None
        kind, _, target = spec.partition(":")
        if kind == "sqlite" and target:
            return cls(_SqliteAudit(target), retention_days)
        if kind == "dynamodb" and target:
            return cls(_DynamoAudit(target, region), retention_days)
        raise RuntimeError(f"SANDBOX_AUDIT must be 'dynamodb:<table>' or 'sqlite:<path>', got {spec!r}.")

    def record(self, tenant_id: str, *, session_id: str, actor: str, kind: str, command: str,
               exit_code: Optional[int] = None, duration_s: Optional[float] = None,
               timed_out: bool = False, oom_killed: bool = False, detail: Optional[str] = None) -> dict:
        now = self._clock()
        truncated = len(command) > MAX_COMMAND_CHARS
        entry = {
            "sk": _sort_key(now), "ts": now, "session_id": session_id, "actor": actor, "kind": kind,
            "command": command[:MAX_COMMAND_CHARS], "truncated": truncated, "exit_code": exit_code,
            "duration_s": duration_s, "timed_out": timed_out, "oom_killed": oom_killed, "detail": detail,
            "expires_at": now + self.retention_days * 86400,
        }
        self._backend.put(tenant_id, entry["sk"], entry)
        return entry

    def list(self, tenant_id: str, limit: int = 100, before: Optional[str] = None) -> Tuple[List[dict], Optional[str]]:
        """Newest first. Returns (entries, cursor for the next page or None)."""
        limit = max(1, min(int(limit), 500))
        entries = self._backend.query(tenant_id, limit + 1, before, self._clock())
        more = len(entries) > limit
        entries = entries[:limit]
        return entries, (entries[-1]["sk"] if more and entries else None)
