"""GitHub App integration: private repository import.

A user installs the Airlock GitHub App on their account or organisation and picks which
repositories it may read (Contents: read-only). GitHub sends them back to the console with a
one-time code ("Request user authorization during installation"); we exchange it for a
user token only to confirm that the GitHub user who installed the app is the console user,
and list the installations they can access. That token is then dropped.

When a private repository is imported, the server signs a short JWT with the app's private
key, asks GitHub for an installation token limited to that one repository with read-only
contents (valid for an hour), downloads the archive, and drops the token. Nothing long-lived
is stored except the installation id and account name.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

API = "https://api.github.com"
TOKEN_URL = "https://github.com/login/oauth/access_token"


class GitHubAppError(Exception):
    pass


def _default_http(method: str, url: str, headers: Dict[str, str], body: Optional[dict]) -> Tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "airlock-sandbox", **headers,
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


@dataclass
class Installation:
    id: int
    account: str          # the user or organisation login it's installed on
    account_type: str     # "User" or "Organization"

    def settings_url(self) -> str:
        if self.account_type == "Organization":
            return f"https://github.com/organizations/{self.account}/settings/installations/{self.id}"
        return f"https://github.com/settings/installations/{self.id}"


class GitHubApp:
    def __init__(self, app_id: str, private_key: str, client_id: str, client_secret: str, slug: str,
                 http: Callable = _default_http, clock: Callable[[], float] = time.time):
        self.app_id = str(app_id)
        self.private_key = private_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.slug = slug
        self._http = http
        self._clock = clock

    @classmethod
    def from_env(cls) -> Optional["GitHubApp"]:
        names = ("SANDBOX_GITHUB_APP_ID", "SANDBOX_GITHUB_APP_PRIVATE_KEY", "SANDBOX_GITHUB_APP_CLIENT_ID",
                 "SANDBOX_GITHUB_APP_CLIENT_SECRET", "SANDBOX_GITHUB_APP_SLUG")
        values = [os.getenv(n, "").strip() for n in names]
        if not any(values):
            return None
        missing = [n for n, v in zip(names, values) if not v]
        if missing:
            raise RuntimeError(f"GitHub App partly configured; also set {missing}.")
        return cls(*values)

    def install_url(self, state: str) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new?" + urllib.parse.urlencode({"state": state})

    # ------------------------------------------------------------------ linking (console)

    def user_installations(self, code: str) -> Tuple[int, List[Installation]]:
        """Exchanges the one-time code from GitHub's redirect for a user token, and returns
        (GitHub user id, installations of this app that user can access). The token is not kept."""
        status, data = self._http("POST", TOKEN_URL, {"Accept": "application/json"},
                                  {"client_id": self.client_id, "client_secret": self.client_secret, "code": code})
        token = data.get("access_token") if status == 200 else None
        if not token:
            raise GitHubAppError(data.get("error_description") or "GitHub didn't accept the authorization code.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        status, user = self._http("GET", f"{API}/user", headers, None)
        if status != 200 or "id" not in user:
            raise GitHubAppError("Couldn't read the GitHub account that installed the app.")
        status, listing = self._http("GET", f"{API}/user/installations?per_page=100", headers, None)
        if status != 200:
            raise GitHubAppError("Couldn't list the app's installations.")
        installations = [
            Installation(int(i["id"]), i["account"]["login"], i["account"].get("type", "User"))
            for i in listing.get("installations", []) if i.get("account", {}).get("login")
        ]
        return int(user["id"]), installations

    # ------------------------------------------------------------------ importing (server)

    def _jwt(self) -> str:
        import jwt  # PyJWT, with the cryptography backend for RS256

        now = int(self._clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    def repo_token(self, installation_id: int, repo_name: str) -> Optional[str]:
        """A read-only token for one repository of an installation (valid for an hour), or None
        when the installation doesn't include that repository."""
        status, data = self._http(
            "POST", f"{API}/app/installations/{installation_id}/access_tokens",
            {"Authorization": f"Bearer {self._jwt()}", "Accept": "application/vnd.github+json"},
            {"repositories": [repo_name], "permissions": {"contents": "read"}},
        )
        if status == 201 and data.get("token"):
            return data["token"]
        if status in (404, 422):   # not installed there, or that repo isn't selected
            return None
        raise GitHubAppError(f"GitHub refused an access token ({status}): {data.get('message', '')}".strip())
