"""Developer console: GitHub sign-in, dashboard, API keys, and (for admins) invites.

Server-rendered on the API's own origin: the session is an HttpOnly, Secure,
SameSite=Lax signed cookie; every state-changing form carries a CSRF token that
must match the session's. The console uses the key store and account store
directly — no admin key in the browser.
"""
from __future__ import annotations

import functools
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional, Set

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.console.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    STATE_COOKIE,
    STATE_TTL,
    GitHubOAuth,
    Signer,
    new_session,
    tenant_for_login,
)

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https://avatars.githubusercontent.com; "
        "style-src 'self'; script-src 'self'; form-action 'self' https://github.com; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


@dataclass
class ConsoleConfig:
    base_url: str                       # e.g. https://airlock.complyoo.com (no trailing slash)
    github_client_id: str
    github_client_secret: str
    session_secret: str
    admins: Set[str] = field(default_factory=set)   # GitHub logins, lowercase
    signup: str = "invite"              # "invite" or "open"

    @classmethod
    def from_env(cls) -> Optional["ConsoleConfig"]:
        names = ("SANDBOX_CONSOLE_BASE_URL", "SANDBOX_GITHUB_CLIENT_ID", "SANDBOX_GITHUB_CLIENT_SECRET",
                 "SANDBOX_CONSOLE_SECRET")
        values = [os.getenv(n) for n in names]
        if not any(values):
            return None
        missing = [n for n, v in zip(names, values) if not v]
        if missing:
            raise RuntimeError(f"Console partly configured; also set {missing}.")
        signup = os.getenv("SANDBOX_CONSOLE_SIGNUP", "invite")
        if signup not in ("invite", "open"):
            raise RuntimeError("SANDBOX_CONSOLE_SIGNUP must be 'invite' or 'open'.")
        admins = {a.strip().lower() for a in os.getenv("SANDBOX_CONSOLE_ADMINS", "").split(",") if a.strip()}
        return cls(values[0].rstrip("/"), values[1], values[2], values[3], admins, signup)


def build_console_router(
    config: ConsoleConfig,
    *,
    keystore: Callable,          # () -> KeyStore
    accounts: Callable,          # () -> AccountStore
    limits: Callable,            # (tenant) -> usage/limits dict
    egress: Callable,            # (tenant) -> list of allowed hosts
    github: Optional[GitHubOAuth] = None,
) -> APIRouter:
    router = APIRouter(prefix="/console", include_in_schema=False)
    signer = Signer(config.session_secret)
    oauth = github or GitHubOAuth(config.github_client_id, config.github_client_secret,
                                  f"{config.base_url}/console/auth/callback")
    secure_cookies = config.base_url.startswith("https://")

    # ------------------------------------------------------------------ helpers

    def session_of(request: Request) -> Optional[dict]:
        return signer.loads(request.cookies.get(SESSION_COOKIE))

    def is_admin(session: dict) -> bool:
        return session["login"].lower() in config.admins

    def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        session = session_of(request)
        response = TEMPLATES.TemplateResponse(request, template, {
            "session": session, "is_admin": bool(session and is_admin(session)),
            "base_url": config.base_url, **context,
        }, status_code=status_code)
        response.headers.update(SECURITY_HEADERS)
        return response

    def redirect(url: str) -> RedirectResponse:
        response = RedirectResponse(url, status_code=303)
        response.headers.update(SECURITY_HEADERS)
        return response

    def require(request: Request, csrf: Optional[str] = None, admin: bool = False) -> dict:
        """The signed-in session, checking CSRF on POSTs and admin rights when asked."""
        session = session_of(request)
        if session is None:
            raise _Redirect("/console")
        if csrf is not None and not secrets.compare_digest(csrf, session["csrf"]):
            raise _Forbidden("This form expired. Reload the page and try again.")
        if admin and not is_admin(session):
            raise _Forbidden("Only console admins can do that.")
        return session

    def guarded(handler):
        """Turns `require()` failures into a redirect (signed out) or a 403 page."""
        @functools.wraps(handler)   # keeps the signature FastAPI reads parameters from
        async def wrapper(*args, **kwargs):
            request: Request = kwargs["request"]
            try:
                return await handler(*args, **kwargs)
            except _Redirect as r:
                return redirect(r.url)
            except _Forbidden as f:
                return render(request, "message.html", 403, title="Not allowed", message=str(f))
        return wrapper

    # ------------------------------------------------------------------ sign in / out

    @router.get("", response_class=HTMLResponse)
    @guarded
    async def home(request: Request):
        session = session_of(request)
        if session is None:
            return render(request, "landing.html", signup=config.signup)
        tenant = session["tid"]
        keys = [k for k in keystore().list(tenant) if k.active]
        return render(request, "dashboard.html", tenant=tenant, active_keys=len(keys),
                      usage=accounts().usage(tenant), limits=limits(tenant), egress=egress(tenant))

    @router.get("/login")
    async def login(request: Request):
        state = secrets.token_urlsafe(24)
        response = redirect(oauth.authorize_url(state))
        response.set_cookie(STATE_COOKIE, signer.dumps({"state": state}, STATE_TTL), max_age=STATE_TTL,
                            httponly=True, secure=secure_cookies, samesite="lax", path="/console")
        return response

    @router.get("/auth/callback", response_class=HTMLResponse)
    async def callback(request: Request, code: str = "", state: str = ""):
        expected = signer.loads(request.cookies.get(STATE_COOKIE))
        if not code or not expected or not secrets.compare_digest(state, expected.get("state", "")):
            return render(request, "message.html", 400, title="Sign-in failed",
                          message="The sign-in link expired or was tampered with. Please try again.")
        try:
            gh = oauth.fetch_user(code)
        except Exception:
            return render(request, "message.html", 502, title="Sign-in failed",
                          message="GitHub didn't confirm the sign-in. Please try again.")
        login_lower = gh["login"].lower()
        allowed = (login_lower in config.admins or config.signup == "open"
                   or accounts().get_user(gh["id"]) is not None or accounts().is_invited(login_lower))
        if not allowed:
            return render(request, "message.html", 403, title="Invite needed",
                          message=f"@{gh['login']} isn't invited yet. The console is invite-only for now — "
                                  "ask the operator to invite your GitHub username.")
        user = accounts().save_user(gh["id"], gh["login"], gh["name"], gh["avatar_url"], tenant_for_login(gh["login"]))
        response = redirect("/console")
        response.set_cookie(SESSION_COOKIE, signer.dumps(new_session(user), SESSION_TTL), max_age=SESSION_TTL,
                            httponly=True, secure=secure_cookies, samesite="lax", path="/console")
        response.delete_cookie(STATE_COOKIE, path="/console")
        return response

    @router.post("/logout")
    @guarded
    async def logout(request: Request, csrf: str = Form("")):
        require(request, csrf)
        response = redirect("/console")
        response.delete_cookie(SESSION_COOKIE, path="/console")
        return response

    # ------------------------------------------------------------------ API keys

    @router.get("/keys", response_class=HTMLResponse)
    @guarded
    async def keys_page(request: Request):
        session = require(request)
        return render(request, "keys.html", keys=keystore().list(session["tid"]), new_key=None, error=None)

    @router.post("/keys", response_class=HTMLResponse)
    @guarded
    async def create_key(request: Request, csrf: str = Form(""), name: str = Form("")):
        session = require(request, csrf)
        from src.api.keystore import KeyLimitReached

        new_key, error = None, None
        try:
            record, api_key = keystore().issue(session["tid"], name or "console")
            new_key = {"api_key": api_key, "key_id": record.key_id, "name": record.name}
        except KeyLimitReached as e:
            error = str(e)
        return render(request, "keys.html", keys=keystore().list(session["tid"]), new_key=new_key, error=error)

    @router.post("/keys/{key_id}/revoke", response_class=HTMLResponse)
    @guarded
    async def revoke_key(request: Request, key_id: str, csrf: str = Form("")):
        session = require(request, csrf)
        from src.api.keystore import LastActiveKey, UnknownKey

        error = None
        try:
            # The console is a separate login, so revoking the last key can't lock anyone out.
            keystore().revoke(key_id, tenant_id=session["tid"], keep_one=False)
        except (UnknownKey, LastActiveKey) as e:
            error = str(e)
        return render(request, "keys.html", keys=keystore().list(session["tid"]), new_key=None, error=error)

    # ------------------------------------------------------------------ admin: invites

    @router.get("/admin", response_class=HTMLResponse)
    @guarded
    async def admin_page(request: Request):
        require(request, admin=True)
        return render(request, "admin.html", invites=accounts().list_invites(), users=accounts().list_users(),
                      signup=config.signup, error=None)

    @router.post("/admin/invites", response_class=HTMLResponse)
    @guarded
    async def add_invite(request: Request, csrf: str = Form(""), login: str = Form("")):
        session = require(request, csrf, admin=True)
        login = login.strip().lstrip("@")
        error = None
        if not GITHUB_LOGIN_RE.match(login):
            error = f"'{login}' isn't a valid GitHub username."
        else:
            accounts().invite(login, invited_by=session["login"])
        return render(request, "admin.html", invites=accounts().list_invites(), users=accounts().list_users(),
                      signup=config.signup, error=error)

    @router.post("/admin/invites/{login}/delete", response_class=HTMLResponse)
    @guarded
    async def delete_invite(request: Request, login: str, csrf: str = Form("")):
        require(request, csrf, admin=True)
        accounts().uninvite(login)
        return redirect("/console/admin")

    return router


class _Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class _Forbidden(Exception):
    pass
