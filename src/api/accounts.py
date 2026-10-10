"""Console accounts, invites and usage metering, in one table.

Items are addressed by a string key:
    user#<github_id>              a console user and the tenant they own
    invite#<github_login>         permission to sign up (invite-only mode)
    request#<github_login>        a signed-in GitHub user asking for an invite
    block#<github_login>          access removed by an admin: no sign-in, no requests
    ghapp#<tenant>                GitHub App installations the tenant connected (private repos)
    usage#<tenant>#<YYYY-MM>      monthly counters: sessions, commands, command_seconds

Backends (SANDBOX_ACCOUNTS): dynamodb:<table> on the server, sqlite:<path> locally.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

COUNTERS = ("sessions", "commands", "command_seconds")
MAX_PENDING_REQUESTS = 500   # keeps a flood of sign-ins from filling the table


class RequestsFull(Exception):
    """Too many invite requests are waiting; new ones are refused until some are handled."""


def current_period(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc).strftime("%Y-%m")


class _SqliteItems:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS items (pk TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def get(self, pk: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT data FROM items WHERE pk = ?", (pk,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, pk: str, item: dict) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO items VALUES (?, ?)", (pk, json.dumps(item)))

    def delete(self, pk: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM items WHERE pk = ?", (pk,))

    def prefix(self, prefix: str) -> List[dict]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM items WHERE pk LIKE ? ORDER BY pk", (prefix + "%",)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def add(self, pk: str, base: dict, increments: Dict[str, float]) -> None:
        with self._lock, self._db:
            row = self._db.execute("SELECT data FROM items WHERE pk = ?", (pk,)).fetchone()
            item = json.loads(row[0]) if row else dict(base)
            for name, amount in increments.items():
                item[name] = item.get(name, 0) + amount
            self._db.execute("INSERT OR REPLACE INTO items VALUES (?, ?)", (pk, json.dumps(item)))


class _DynamoItems:
    """Table with partition key `pk` (string)."""

    def __init__(self, table_name: str, region: Optional[str] = None):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    @staticmethod
    def _plain(item: Optional[dict]) -> Optional[dict]:
        if item is None:
            return None
        return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in item.items() if k != "pk"}

    def get(self, pk: str) -> Optional[dict]:
        return self._plain(self._table.get_item(Key={"pk": pk}).get("Item"))

    def put(self, pk: str, item: dict) -> None:
        clean = {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in item.items()}
        self._table.put_item(Item={"pk": pk, **clean})

    def delete(self, pk: str) -> None:
        self._table.delete_item(Key={"pk": pk})

    def prefix(self, prefix: str) -> List[dict]:
        from boto3.dynamodb.conditions import Attr

        kwargs: dict = {"FilterExpression": Attr("pk").begins_with(prefix)}
        items: list = []
        while True:
            page = self._table.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [self._plain(i) for i in sorted(items, key=lambda i: i["pk"])]

    def add(self, pk: str, base: dict, increments: Dict[str, float]) -> None:
        """Atomic counters: concurrent commands never lose an increment."""
        names, values, sets = {}, {}, []
        for i, (name, amount) in enumerate(increments.items()):
            names[f"#c{i}"] = name
            values[f":c{i}"] = Decimal(str(amount))
        for j, (name, value) in enumerate(base.items()):
            names[f"#b{j}"] = name
            values[f":b{j}"] = value
            sets.append(f"#b{j} = if_not_exists(#b{j}, :b{j})")
        expression = "ADD " + ", ".join(f"#c{i} :c{i}" for i in range(len(increments)))
        if sets:
            expression = "SET " + ", ".join(sets) + " " + expression
        self._table.update_item(
            Key={"pk": pk}, UpdateExpression=expression,
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )


class AccountStore:
    def __init__(self, items, clock=time.time):
        self._items = items
        self._clock = clock

    @classmethod
    def from_config(cls, spec: Optional[str], region: Optional[str] = None) -> Optional["AccountStore"]:
        if not spec:
            return None
        kind, _, target = spec.partition(":")
        if kind == "sqlite" and target:
            return cls(_SqliteItems(target))
        if kind == "dynamodb" and target:
            return cls(_DynamoItems(target, region))
        raise RuntimeError(f"SANDBOX_ACCOUNTS must be 'dynamodb:<table>' or 'sqlite:<path>', got {spec!r}.")

    # ---------------------------------------------------------------- users

    def get_user(self, github_id: int) -> Optional[dict]:
        return self._items.get(f"user#{github_id}")

    def save_user(self, github_id: int, login: str, name: str, avatar_url: str, tenant_id: str) -> dict:
        existing = self.get_user(github_id) or {}
        user = {
            "github_id": github_id, "login": login, "name": name or login, "avatar_url": avatar_url,
            "tenant_id": existing.get("tenant_id", tenant_id),       # a user's tenant never changes
            "created_at": existing.get("created_at", self._clock()), "last_login_at": self._clock(),
        }
        self._items.put(f"user#{github_id}", user)
        return user

    def list_users(self) -> List[dict]:
        return self._items.prefix("user#")

    def delete_user(self, github_id: int) -> None:
        self._items.delete(f"user#{github_id}")

    # ---------------------------------------------------------------- blocks

    def block(self, login: str, blocked_by: str, tenant_id: Optional[str] = None) -> dict:
        record = {"login": login.lower(), "blocked_by": blocked_by, "blocked_at": self._clock(), "tenant_id": tenant_id}
        self._items.put(f"block#{login.lower()}", record)
        return record

    def unblock(self, login: str) -> None:
        self._items.delete(f"block#{login.lower()}")

    def is_blocked(self, login: str) -> bool:
        return self._items.get(f"block#{login.lower()}") is not None

    def list_blocked(self) -> List[dict]:
        return self._items.prefix("block#")

    # ---------------------------------------------------------------- invites

    def invite(self, login: str, invited_by: str) -> dict:
        record = {"login": login.lower(), "invited_by": invited_by, "invited_at": self._clock()}
        self._items.put(f"invite#{login.lower()}", record)
        return record

    def uninvite(self, login: str) -> None:
        self._items.delete(f"invite#{login.lower()}")

    def is_invited(self, login: str) -> bool:
        return self._items.get(f"invite#{login.lower()}") is not None

    def list_invites(self) -> List[dict]:
        return self._items.prefix("invite#")

    # ---------------------------------------------------------------- invite requests

    def request_invite(self, github_id: int, login: str, name: str, avatar_url: str,
                       note: str = "", contact: str = "") -> dict:
        """Records (or updates) a request from a GitHub user who signed in without an invite."""
        key = f"request#{login.lower()}"
        existing = self._items.get(key)
        if existing is None and len(self.list_requests()) >= MAX_PENDING_REQUESTS:
            raise RequestsFull("Too many invite requests are waiting right now. Please try again in a few days.")
        now = self._clock()
        record = {
            "login": login.lower(), "display_login": login, "github_id": github_id, "name": name or login,
            "avatar_url": avatar_url, "note": note, "contact": contact,
            "requested_at": (existing or {}).get("requested_at", now), "updated_at": now,
        }
        self._items.put(key, record)
        return record

    def get_request(self, login: str) -> Optional[dict]:
        return self._items.get(f"request#{login.lower()}")

    def delete_request(self, login: str) -> None:
        self._items.delete(f"request#{login.lower()}")

    def list_requests(self) -> List[dict]:
        return sorted(self._items.prefix("request#"), key=lambda r: r.get("requested_at", 0))

    # ---------------------------------------------------------------- GitHub App installations

    def set_github_installations(self, tenant_id: str, installations: List[dict]) -> None:
        """Replaces the tenant's connected installations: [{id, account, account_type}, ...]."""
        self._items.put(f"ghapp#{tenant_id}", {
            "tenant_id": tenant_id, "linked_at": self._clock(),
            "installations": [{"id": int(i["id"]), "account": str(i["account"]),
                               "account_type": str(i.get("account_type", "User"))} for i in installations],
        })

    def github_installations(self, tenant_id: str) -> List[dict]:
        item = self._items.get(f"ghapp#{tenant_id}") or {}
        return [{**i, "id": int(i["id"])} for i in item.get("installations", [])]

    def clear_github_installations(self, tenant_id: str) -> None:
        self._items.delete(f"ghapp#{tenant_id}")

    # ---------------------------------------------------------------- usage metering

    def record_usage(self, tenant_id: str, **increments: float) -> None:
        unknown = set(increments) - set(COUNTERS)
        if unknown:
            raise ValueError(f"Unknown usage counters: {sorted(unknown)}")
        period = current_period(self._clock())
        self._items.add(f"usage#{tenant_id}#{period}", {"tenant_id": tenant_id, "period": period}, increments)

    def usage(self, tenant_id: str, period: Optional[str] = None) -> dict:
        period = period or current_period(self._clock())
        item = self._items.get(f"usage#{tenant_id}#{period}") or {}
        return {"tenant_id": tenant_id, "period": period, **{c: item.get(c, 0) for c in COUNTERS}}
