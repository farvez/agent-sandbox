"""Manage API keys: your own (tenant) or every tenant's (admin).

    from airlock_sandbox.keys import Keys

    keys = Keys()                       # your tenant, using SANDBOX_API_KEY
    new = keys.create(name="ci")        # new["api_key"] is shown only once
    keys.revoke("abcdefghij")           # by key_id

    admin = Keys(admin=True)            # all tenants, using SANDBOX_ADMIN_KEY
    admin.create(tenant="acme", name="onboarding")

Command line:  airlock-sandbox-keys --help
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from typing import List, Optional, Union

from airlock_sandbox._http import HTTPClient
from airlock_sandbox.errors import SandboxError
from airlock_sandbox.sandbox import _env_flag


class Keys:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        admin: bool = False,
        verify: Optional[Union[bool, str]] = None,
        timeout: float = 30,
    ):
        base_url = base_url or os.getenv("SANDBOX_API_URL")
        key_var = "SANDBOX_ADMIN_KEY" if admin else "SANDBOX_API_KEY"
        api_key = api_key or os.getenv(key_var)
        if not base_url or not api_key:
            raise SandboxError(f"Set base_url and api_key, or the SANDBOX_API_URL and {key_var} environment variables.")
        if verify is None:
            verify = not _env_flag("SANDBOX_API_INSECURE")
        self.admin = admin
        self._prefix = "/v1/admin/keys" if admin else "/v1/keys"
        self._http = HTTPClient(base_url, api_key, verify=verify, timeout=timeout)

    def list(self, tenant: Optional[str] = None) -> List[dict]:
        """Keys (never their secrets). Admins can filter by tenant."""
        query = f"?{urllib.parse.urlencode({'tenant': tenant})}" if (self.admin and tenant) else ""
        return self._http.request("GET", self._prefix + query)["keys"]

    def create(self, name: str = "", tenant: Optional[str] = None) -> dict:
        """Issues a key; the result's "api_key" is shown only once. Admins must name the tenant."""
        body: dict = {"name": name}
        if self.admin:
            if not tenant:
                raise SandboxError("Admins must say which tenant the key is for (tenant=...).")
            body["tenant"] = tenant
        return self._http.request("POST", self._prefix, body)

    def revoke(self, key_id: str) -> dict:
        """Revokes a key immediately. Tenants can't revoke their last active key."""
        return self._http.request("DELETE", f"{self._prefix}/{urllib.parse.quote(key_id)}")


def _print_table(rows: List[dict]) -> None:
    if not rows:
        print("No keys.")
        return
    print(f"{'KEY ID':12} {'TENANT':16} {'NAME':20} {'STATUS':8} PREFIX")
    for r in rows:
        status = "active" if r.get("active") else "revoked"
        print(f"{r['key_id']:12} {r['tenant_id']:16} {r['name'][:20]:20} {status:8} {r.get('prefix', '')}")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="airlock-sandbox-keys",
        description="Manage agent-sandbox API keys. Uses SANDBOX_API_URL plus SANDBOX_API_KEY "
                    "(your tenant) or, with --admin, SANDBOX_ADMIN_KEY (all tenants).",
    )
    parser.add_argument("--admin", action="store_true", help="manage keys for every tenant (needs SANDBOX_ADMIN_KEY)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="list keys")
    p_list.add_argument("--tenant", help="(admin) only this tenant")
    p_create = sub.add_parser("create", help="issue a new key (shown once)")
    p_create.add_argument("--name", default="", help="label, e.g. 'laptop' or 'ci'")
    p_create.add_argument("--tenant", help="(admin) tenant the key is for")
    p_revoke = sub.add_parser("revoke", help="revoke a key immediately")
    p_revoke.add_argument("key_id")
    args = parser.parse_args(argv)

    try:
        keys = Keys(admin=args.admin)
        if args.command == "list":
            _print_table(keys.list(tenant=args.tenant))
        elif args.command == "create":
            new = keys.create(name=args.name, tenant=args.tenant)
            print(f"Created key {new['key_id']} for tenant {new['tenant_id']} ({new['name']}).\n")
            print(f"    {new['api_key']}\n")
            print("Store it now (e.g. in a password manager): it is shown only once and can't be recovered.")
        elif args.command == "revoke":
            gone = keys.revoke(args.key_id)
            print(f"Revoked {gone['key_id']} ({gone['tenant_id']}, {gone['name']}). It stops working immediately.")
    except SandboxError as err:
        print(f"airlock-sandbox-keys: {type(err).__name__}: {err}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
