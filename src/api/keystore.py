"""Self-service API keys: issued and revoked at runtime, without a redeploy.

A key looks like  asb_<key_id>_<secret>  and is shown once, when created. Only
the SHA-256 of the full key is stored, so a leaked database contains no usable
keys. <key_id> is public and identifies the key for listing and revocation.

Backends (SANDBOX_KEYSTORE):
    dynamodb:<table>   production; survives redeploys (the instance is replaced on each one)
    sqlite:<path>      local development and tests
    unset              no key store; only the static SANDBOX_API_KEYS from configuration
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

KEY_PREFIX = "asb"
KEY_RE = re.compile(r"^asb_([a-z2-7]{10})_([A-Za-z0-9_-]{43})$")
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_ACTIVE_KEYS_PER_TENANT = 10
CACHE_TTL_SECONDS = 30


@dataclass
class KeyRecord:
    key_id: str
    tenant_id: str
    name: str
    key_hash: str
    created_at: float
    revoked_at: Optional[float] = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def public(self) -> dict:
        """What listing shows: never the hash."""
        data = asdict(self)
        data.pop("key_hash")
        data["active"] = self.active
        data["prefix"] = f"{KEY_PREFIX}_{self.key_id}_…"
        return data


class KeyManagementError(Exception):
    """Base for key-management errors (409/404 at the API)."""


class KeyLimitReached(KeyManagementError):
    pass


class LastActiveKey(KeyManagementError):
    pass


class UnknownKey(KeyManagementError):
    pass


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def new_key() -> Tuple[str, str]:
    """Returns (key_id, api_key). 50 bits of id (public) + 256 bits of secret."""
    key_id = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(10))
    secret = secrets.token_urlsafe(32)  # 43 characters
    return key_id, f"{KEY_PREFIX}_{key_id}_{secret}"


def parse_key(api_key: str) -> Optional[str]:
    """The key_id of a well-formed self-service key, else None (e.g. a static key)."""
    match = KEY_RE.match(api_key)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- backends


class _Backend:
    def put(self, record: KeyRecord) -> None: ...
    def get(self, key_id: str) -> Optional[KeyRecord]: ...
    def list(self, tenant_id: Optional[str]) -> List[KeyRecord]: ...
    def revoke(self, key_id: str, at: float) -> None: ...


class SqliteBackend(_Backend):
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS api_keys (key_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
                "name TEXT NOT NULL, key_hash TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL)"
            )

    def put(self, record: KeyRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?)",
                (record.key_id, record.tenant_id, record.name, record.key_hash, record.created_at, record.revoked_at),
            )

    def get(self, key_id: str) -> Optional[KeyRecord]:
        with self._lock:
            row = self._db.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
        return KeyRecord(*row) if row else None

    def list(self, tenant_id: Optional[str]) -> List[KeyRecord]:
        with self._lock:
            if tenant_id is None:
                rows = self._db.execute("SELECT * FROM api_keys ORDER BY created_at").fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM api_keys WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)
                ).fetchall()
        return [KeyRecord(*row) for row in rows]

    def revoke(self, key_id: str, at: float) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL", (at, key_id))


class DynamoBackend(_Backend):
    """Table with partition key `key_id` (string). Listing scans: fine for hundreds of keys."""

    def __init__(self, table_name: str, region: Optional[str] = None):
        import boto3  # only needed on the server

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @staticmethod
    def _from_item(item: dict) -> KeyRecord:
        return KeyRecord(
            key_id=item["key_id"], tenant_id=item["tenant_id"], name=item["name"], key_hash=item["key_hash"],
            created_at=float(item["created_at"]),
            revoked_at=float(item["revoked_at"]) if item.get("revoked_at") is not None else None,
        )

    def put(self, record: KeyRecord) -> None:
        from decimal import Decimal

        item = {k: v for k, v in asdict(record).items() if v is not None}
        item["created_at"] = Decimal(str(record.created_at))
        self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(key_id)")

    def get(self, key_id: str) -> Optional[KeyRecord]:
        item = self._table.get_item(Key={"key_id": key_id}, ConsistentRead=True).get("Item")
        return self._from_item(item) if item else None

    def list(self, tenant_id: Optional[str]) -> List[KeyRecord]:
        kwargs: dict = {}
        if tenant_id is not None:
            from boto3.dynamodb.conditions import Attr

            kwargs["FilterExpression"] = Attr("tenant_id").eq(tenant_id)
        items: list = []
        while True:
            page = self._table.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return sorted((self._from_item(i) for i in items), key=lambda r: r.created_at)

    def revoke(self, key_id: str, at: float) -> None:
        from decimal import Decimal

        self._table.update_item(
            Key={"key_id": key_id},
            UpdateExpression="SET revoked_at = :at",
            ConditionExpression="attribute_exists(key_id) AND attribute_not_exists(revoked_at)",
            ExpressionAttributeValues={":at": Decimal(str(at))},
        )


# --------------------------------------------------------------------------- key store


class KeyStore:
    def __init__(self, backend: _Backend, clock=time.time):
        self._backend = backend
        self._clock = clock
        self._cache: Dict[str, Tuple[float, Optional[KeyRecord]]] = {}  # key_id -> (expires, record|None)
        self._lock = threading.Lock()  # serialises issue/revoke so per-tenant checks hold

    @classmethod
    def from_config(cls, spec: Optional[str], region: Optional[str] = None) -> Optional["KeyStore"]:
        if not spec:
            return None
        kind, _, target = spec.partition(":")
        if kind == "sqlite" and target:
            return cls(SqliteBackend(target))
        if kind == "dynamodb" and target:
            return cls(DynamoBackend(target, region))
        raise RuntimeError(f"SANDBOX_KEYSTORE must be 'dynamodb:<table>' or 'sqlite:<path>', got {spec!r}.")

    # ---------------------------------------------------------------- auth path

    def authenticate(self, api_key: str) -> Optional[str]:
        """The tenant for a valid, active self-service key; None otherwise."""
        key_id = parse_key(api_key)
        if key_id is None:
            return None
        record = self._cached_get(key_id)
        if record is None or not record.active:
            return None
        return record.tenant_id if secrets.compare_digest(hash_key(api_key), record.key_hash) else None

    def _cached_get(self, key_id: str) -> Optional[KeyRecord]:
        now = self._clock()
        hit = self._cache.get(key_id)
        if hit and hit[0] > now:
            return hit[1]
        record = self._backend.get(key_id)   # misses are cached too: guessed ids can't hammer the table
        self._cache[key_id] = (now + CACHE_TTL_SECONDS, record)
        return record

    # ---------------------------------------------------------------- management

    def issue(self, tenant_id: str, name: str) -> Tuple[KeyRecord, str]:
        if not TENANT_RE.match(tenant_id):
            raise ValueError("Tenant names are 1-63 chars of lowercase letters, digits, '-' or '_'.")
        with self._lock:
            active = [r for r in self._backend.list(tenant_id) if r.active]
            if len(active) >= MAX_ACTIVE_KEYS_PER_TENANT:
                raise KeyLimitReached(
                    f"Tenant '{tenant_id}' already has {len(active)} active keys (max {MAX_ACTIVE_KEYS_PER_TENANT}). "
                    "Revoke one first."
                )
            key_id, api_key = new_key()
            record = KeyRecord(key_id, tenant_id, name.strip()[:100] or "unnamed", hash_key(api_key), self._clock())
            self._backend.put(record)
            self._cache.pop(key_id, None)
            return record, api_key

    def list(self, tenant_id: Optional[str] = None) -> List[KeyRecord]:
        return self._backend.list(tenant_id)

    def revoke(self, key_id: str, tenant_id: Optional[str] = None, keep_one: bool = False) -> KeyRecord:
        """Revokes a key. `tenant_id` restricts it to that tenant's keys; `keep_one`
        refuses to revoke a tenant's last active key (self-service lock-out guard)."""
        with self._lock:
            record = self._backend.get(key_id)
            if record is None or (tenant_id is not None and record.tenant_id != tenant_id):
                raise UnknownKey(f"No key '{key_id}'" + (" for this tenant." if tenant_id else "."))
            if record.active:
                if keep_one and len([r for r in self._backend.list(record.tenant_id) if r.active]) <= 1:
                    raise LastActiveKey("This is the tenant's last active key; create a new one before revoking it.")
                record.revoked_at = self._clock()
                self._backend.revoke(key_id, record.revoked_at)
            self._cache.pop(key_id, None)   # effective immediately on this server
            return record
