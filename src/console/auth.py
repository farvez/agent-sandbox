"""Console sessions (signed cookies) and GitHub OAuth."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

SESSION_COOKIE = "airlock_session"
STATE_COOKIE = "airlock_oauth_state"
SESSION_TTL = 7 * 86400
STATE_TTL = 600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """HMAC-SHA256 signed, expiring JSON values for cookies. Tampering or expiry -> None."""

    def __init__(self, secret: str, clock: Callable[[], float] = time.time):
        if len(secret) < 32:
            raise ValueError("The console session secret must be at least 32 characters.")
        self._key = hashlib.sha256(("airlock-console:" + secret).encode()).digest()
        self._clock = clock

    def dumps(self, value: dict, ttl: int) -> str:
        payload = json.dumps({**value, "exp": int(self._clock()) + ttl}, separators=(",", ":")).encode()
        return f"{_b64(payload)}.{_b64(hmac.new(self._key, payload, hashlib.sha256).digest())}"

    def loads(self, token: Optional[str]) -> Optional[dict]:
        if not token or "." not in token:
            return None
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload = _unb64(payload_b64)
            if not hmac.compare_digest(hmac.new(self._key, payload, hashlib.sha256).digest(), _unb64(sig_b64)):
                return None
            value = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            return None
        return value if value.get("exp", 0) > self._clock() else None


def new_session(user: dict) -> dict:
    """Session contents. The CSRF token travels in forms and must match the cookie's."""
    return {"gid": user["github_id"], "login": user["login"], "tid": user["tenant_id"],
            "name": user.get("name") or user["login"], "avatar": user.get("avatar_url", ""),
            "csrf": secrets.token_urlsafe(24)}


def tenant_for_login(login: str) -> str:
    """GitHub logins are 1-39 chars of letters, digits and single hyphens -> always a valid tenant name."""
    return "gh-" + login.lower()


class GitHubOAuth:
    """The three GitHub calls of the web flow. `http` is injectable for tests."""

    AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_URL = "https://api.github.com/user"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, http: Optional[Callable] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = http or self._urlopen_json

    def authorize_url(self, state: str) -> str:
        query = urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "state": state,
            "scope": "read:user",   # identity only; repo access comes with Phase 2 import
            "allow_signup": "true",
        })
        return f"{self.AUTHORIZE_URL}?{query}"

    def fetch_user(self, code: str) -> dict:
        """Exchanges the code for a token and returns {id, login, name, avatar_url}. The token is not kept."""
        token = self._http("POST", self.TOKEN_URL, {
            "client_id": self.client_id, "client_secret": self.client_secret,
            "code": code, "redirect_uri": self.redirect_uri,
        }, None)
        access_token = token.get("access_token")
        if not access_token:
            raise PermissionError(token.get("error_description") or "GitHub did not return an access token.")
        user = self._http("GET", self.USER_URL, None, access_token)
        return {"id": int(user["id"]), "login": user["login"], "name": user.get("name") or user["login"],
                "avatar_url": user.get("avatar_url", "")}

    @staticmethod
    def _urlopen_json(method: str, url: str, form: Optional[dict], bearer: Optional[str]) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "airlock-console"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
            headers["Accept"] = "application/vnd.github+json"
        data = urllib.parse.urlencode(form).encode() if form else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
