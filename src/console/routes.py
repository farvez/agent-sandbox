"""Developer console: GitHub sign-in, dashboard, API keys, and (for admins) invites.

Server-rendered on the API's own origin: the session is an HttpOnly, Secure,
SameSite=Lax signed cookie; every state-changing form carries a CSRF token that
must match the session's. The console uses the key store and account store
directly — no admin key in the browser.
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional, Set

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
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
from src.console.terminal import HOME, MAX_COMMAND_CHARS, clean_cwd, split_cwd, wrap_command

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def asset_version(directory: str = STATIC_DIR) -> str:
    """A short hash of the console's CSS and JS. Pages link `console.css?v=<hash>`, so every
    change gets a new URL and browsers never keep using a stale copy after a deploy."""
    digest = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), "rb") as f:
            digest.update(name.encode() + b"/" + f.read())
    return digest.hexdigest()[:12]


ASSET_VERSION = asset_version()
FLASH_COOKIE = "airlock_flash"
# A GitHub user who signed in without an invite: their verified identity, so they can
# request one without a console session (and nobody can request in someone else's name).
PENDING_COOKIE = "airlock_pending"
PENDING_TTL = 30 * 60
NOTE_MAX, CONTACT_MAX = 500, 200
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
    contact: str = ""                   # operator contact shown on the privacy and terms pages

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
        return cls(values[0].rstrip("/"), values[1], values[2], values[3], admins, signup,
                   os.getenv("SANDBOX_CONSOLE_CONTACT", "").strip())


def build_console_router(
    config: ConsoleConfig,
    *,
    keystore: Callable,          # () -> KeyStore
    accounts: Callable,          # () -> AccountStore
    limits: Callable,            # (tenant) -> usage/limits dict
    egress: Callable,            # (tenant) -> list of allowed hosts
    github: Optional[GitHubOAuth] = None,
    sessions=None,               # ConsoleSessions (server.py); None hides the workspace pages
    audit: Optional[Callable] = None,    # () -> AuditLog or None; None hides the Activity page
    notify: Optional[Callable] = None,   # (subject, message) -> None, e.g. email to the operator
    github_app: Optional[Callable] = None,   # () -> GitHubApp or None; None hides private-repo import
) -> APIRouter:
    router = APIRouter(prefix="/console", include_in_schema=False)
    signer = Signer(config.session_secret)
    oauth = github or GitHubOAuth(config.github_client_id, config.github_client_secret,
                                  f"{config.base_url}/console/auth/callback")
    secure_cookies = config.base_url.startswith("https://")

    # ------------------------------------------------------------------ helpers

    def session_of(request: Request) -> Optional[dict]:
        """The signed-in session, as long as the account still exists (an admin can remove it)."""
        if not hasattr(request.state, "console_session"):
            session = signer.loads(request.cookies.get(SESSION_COOKIE))
            if session is not None and accounts().get_user(session["gid"]) is None:
                session = None
            request.state.console_session = session
        return request.state.console_session

    def audit_log():
        return audit() if audit is not None else None

    def send_notice(subject: str, message: str) -> None:
        """Best effort: a failed email never fails the request."""
        if notify is None:
            return
        try:
            notify(subject, message)
        except Exception:
            import logging

            logging.getLogger("agent_sandbox.console").exception("Notification failed: %s", subject)

    def is_admin(session: dict) -> bool:
        return session["login"].lower() in config.admins

    def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        session = session_of(request)
        flash = signer.loads(request.cookies.get(FLASH_COOKIE))
        admin = bool(session and is_admin(session))
        response = TEMPLATES.TemplateResponse(request, template, {
            "session": session, "is_admin": admin,
            "base_url": config.base_url, "workspaces_enabled": sessions is not None,
            "audit_enabled": audit_log() is not None, "asset_version": ASSET_VERSION,
            "pending_requests": len(accounts().list_requests()) if admin else 0,
            "flash": flash, **context,
        }, status_code=status_code)
        response.headers.update(SECURITY_HEADERS)
        if flash is not None:
            response.delete_cookie(FLASH_COOKIE, path="/console")
        return response

    def set_flash(response: Response, kind: str, text: str) -> Response:
        """A one-time notice shown on the next page (signed, so it can't be forged into the page)."""
        response.set_cookie(FLASH_COOKIE, signer.dumps({"kind": kind, "text": text[:600]}, 120), max_age=120,
                            httponly=True, secure=secure_cookies, samesite="lax", path="/console")
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

    # ------------------------------------------------------------------ policies (public)

    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy(request: Request):
        return render(request, "privacy.html", contact=config.contact, updated="11 October 2026")

    @router.get("/terms", response_class=HTMLResponse)
    async def terms(request: Request):
        return render(request, "terms.html", contact=config.contact, updated="8 October 2026")

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
        if accounts().is_blocked(login_lower) and login_lower not in config.admins:
            response = render(request, "message.html", 403, title="Access removed", session=None, is_admin=False,
                              pending_requests=0, message=f"@{gh['login']} no longer has access to this console.")
            response.delete_cookie(STATE_COOKIE, path="/console")
            response.delete_cookie(SESSION_COOKIE, path="/console")
            return response
        allowed = (login_lower in config.admins or config.signup == "open"
                   or accounts().get_user(gh["id"]) is not None or accounts().is_invited(login_lower))
        if not allowed:
            pending = {"gid": gh["id"], "login": gh["login"], "name": gh["name"] or gh["login"],
                       "avatar": gh["avatar_url"], "csrf": secrets.token_urlsafe(24)}
            response = invite_page(request, pending, 403)
            response.set_cookie(PENDING_COOKIE, signer.dumps(pending, PENDING_TTL), max_age=PENDING_TTL,
                                httponly=True, secure=secure_cookies, samesite="lax", path="/console")
            response.delete_cookie(STATE_COOKIE, path="/console")
            response.delete_cookie(SESSION_COOKIE, path="/console")   # a different GitHub account signed in
            return response
        user = accounts().save_user(gh["id"], gh["login"], gh["name"], gh["avatar_url"], tenant_for_login(gh["login"]))
        accounts().delete_request(login_lower)   # they're in; nothing left to approve
        response = redirect("/console")
        response.set_cookie(SESSION_COOKIE, signer.dumps(new_session(user), SESSION_TTL), max_age=SESSION_TTL,
                            httponly=True, secure=secure_cookies, samesite="lax", path="/console")
        response.delete_cookie(STATE_COOKIE, path="/console")
        response.delete_cookie(PENDING_COOKIE, path="/console")
        return response

    # ------------------------------------------------------------------ invite requests

    def invite_page(request: Request, pending: dict, status_code: int = 200, error: Optional[str] = None):
        login = pending["login"]
        # Shown to someone without an account: never with another account's session in the header.
        return render(request, "invite_needed.html", status_code, session=None, is_admin=False, pending_requests=0,
                      pending=pending,
                      existing=accounts().get_request(login), invited=accounts().is_invited(login),
                      note_max=NOTE_MAX, contact_max=CONTACT_MAX, error=error)

    @router.get("/request-invite", response_class=HTMLResponse)
    async def request_invite_page(request: Request):
        pending = signer.loads(request.cookies.get(PENDING_COOKIE))
        if pending is None:
            return redirect("/console/login")   # who's asking is confirmed by signing in with GitHub
        return invite_page(request, pending)

    @router.post("/request-invite", response_class=HTMLResponse)
    async def request_invite(request: Request, csrf: str = Form(""), note: str = Form(""), contact: str = Form("")):
        from src.api.accounts import RequestsFull

        pending = signer.loads(request.cookies.get(PENDING_COOKIE))
        if pending is None or not secrets.compare_digest(csrf, pending.get("csrf", "")) \
                or accounts().is_blocked(pending["login"]):
            return render(request, "message.html", 400, title="Sign in again",
                          message="This form expired. Sign in with GitHub again to request an invite.")
        is_new = accounts().get_request(pending["login"]) is None
        note, contact = note.strip()[:NOTE_MAX], contact.strip()[:CONTACT_MAX]
        try:
            accounts().request_invite(pending["gid"], pending["login"], pending["name"], pending["avatar"], note, contact)
        except RequestsFull as e:
            return invite_page(request, pending, 503, error=str(e))
        if is_new:   # one email per person, not per edit
            login = pending["login"]
            await run_in_threadpool(send_notice, f"Airlock: invite request from @{login}", (
                f"@{login} ({pending['name']}) asked for an invite to the Airlock console.\n\n"
                f"Use case: {note or '(not given)'}\n"
                f"Contact:  {contact or '(not given)'}\n"
                f"GitHub:   https://github.com/{login}\n\n"
                f"Approve or dismiss: {config.base_url}/console/admin\n"))
        return set_flash(redirect("/console/request-invite"), "ok",
                         "Request sent. You'll be able to sign in once it's approved.")

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

    def admin_view(request: Request, error: Optional[str] = None):
        return render(request, "admin.html", invites=accounts().list_invites(), users=accounts().list_users(),
                      requests=accounts().list_requests(), blocked=accounts().list_blocked(),
                      admins=config.admins, signup=config.signup, error=error)

    @router.get("/admin", response_class=HTMLResponse)
    @guarded
    async def admin_page(request: Request):
        require(request, admin=True)
        return admin_view(request)

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
            accounts().delete_request(login)
        return admin_view(request, error)

    @router.post("/admin/requests/{login}/approve")
    @guarded
    async def approve_request(request: Request, login: str, csrf: str = Form("")):
        session = require(request, csrf, admin=True)
        if not GITHUB_LOGIN_RE.match(login):
            raise _Forbidden("That isn't a valid GitHub username.")
        found = accounts().get_request(login)
        accounts().invite(login, invited_by=session["login"])
        accounts().delete_request(login)
        reach = f" Contact they left: {found['contact']}" if found and found.get("contact") else ""
        return set_flash(redirect("/console/admin"), "ok",
                         f"Invited @{login}. Let them know they can sign in now.{reach}")

    @router.post("/admin/requests/{login}/dismiss")
    @guarded
    async def dismiss_request(request: Request, login: str, csrf: str = Form("")):
        require(request, csrf, admin=True)
        accounts().delete_request(login)
        return set_flash(redirect("/console/admin"), "ok", f"Dismissed the request from @{login}.")

    @router.post("/admin/users/{github_id}/remove")
    @guarded
    async def remove_user(request: Request, github_id: int, csrf: str = Form("")):
        """Removes an account: keys revoked, sandboxes closed, console sign-in ended, and blocked
        from signing in or requesting again until unblocked. Usage history is kept."""
        session = require(request, csrf, admin=True)
        user = accounts().get_user(github_id)
        if user is None:
            return set_flash(redirect("/console/admin"), "error", "That account no longer exists.")
        login, tenant = user["login"], user["tenant_id"]
        if login.lower() in config.admins:
            raise _Forbidden("Console admins can't be removed here; take them out of console_admins first.")
        revoked = 0
        for key in keystore().list(tenant):
            if key.active:
                keystore().revoke(key.key_id, tenant_id=tenant, keep_one=False)
                revoked += 1
        closed = await run_in_threadpool(sessions.close_all, tenant) if sessions is not None else 0
        accounts().block(login, blocked_by=session["login"], tenant_id=tenant)
        accounts().uninvite(login)
        accounts().delete_request(login)
        accounts().delete_user(github_id)
        return set_flash(redirect("/console/admin"), "ok",
                         f"Removed @{login}: {revoked} key(s) revoked, {closed} sandbox(es) closed. "
                         "They can't sign in or request access again unless you unblock them.")

    @router.post("/admin/blocked/{login}/unblock")
    @guarded
    async def unblock(request: Request, login: str, csrf: str = Form("")):
        require(request, csrf, admin=True)
        accounts().unblock(login)
        return set_flash(redirect("/console/admin"), "ok",
                         f"Unblocked @{login}. They can request access again, or invite them to let them straight in.")

    @router.post("/admin/invites/{login}/delete", response_class=HTMLResponse)
    @guarded
    async def delete_invite(request: Request, login: str, csrf: str = Form("")):
        require(request, csrf, admin=True)
        accounts().uninvite(login)
        return redirect("/console/admin")


    # ------------------------------------------------------------------ GitHub App (private repositories)

    def gh_app():
        return github_app() if github_app is not None else None

    def not_enabled(request: Request):
        return render(request, "message.html", 404, title="Not available",
                      message="Private repositories aren't enabled on this server.")

    @router.get("/github/connect")
    @guarded
    async def github_connect(request: Request):
        require(request)
        app = gh_app()
        if app is None:
            return not_enabled(request)
        # Who installed it is confirmed on the way back (the user id behind GitHub's one-time code).
        return redirect(app.install_url(state=secrets.token_urlsafe(16)))

    @router.get("/github/link")
    @guarded
    async def github_link(request: Request):
        require(request)
        app = gh_app()
        if app is None:
            return not_enabled(request)
        # Already installed on GitHub: just authorize, and the callback links the installations.
        return redirect(app.authorize_url(secrets.token_urlsafe(16), f"{config.base_url}/console/github/callback"))

    @router.get("/github/callback")
    @guarded
    async def github_callback(request: Request, code: str = "", setup_action: str = ""):
        from src.api.github_app import GitHubAppError

        session = require(request)
        app = gh_app()
        if app is None:
            return not_enabled(request)
        response = redirect("/console/workspaces")
        if setup_action == "request":
            return set_flash(response, "ok", "GitHub asked an organisation owner to approve Airlock. "
                                             "Once they have, click Connect GitHub again.")
        if not code:
            return set_flash(response, "error", "GitHub didn't confirm the connection. Click Connect GitHub to try again.")
        try:
            github_user_id, installations = await run_in_threadpool(app.user_installations, code)
        except GitHubAppError as e:
            return set_flash(response, "error", f"Couldn't connect GitHub: {e}")
        if github_user_id != int(session["gid"]):
            return set_flash(response, "error", f"GitHub authorized a different account than @{session['login']}. "
                                                f"Sign in to GitHub as @{session['login']} and connect again.")
        accounts().set_github_installations(session["tid"], [vars(i) for i in installations])
        names = ", ".join(f"@{i.account}" for i in installations)
        if not installations:
            return set_flash(response, "error", "Airlock isn't installed on any GitHub account you can access yet.")
        return set_flash(response, "ok", f"Connected GitHub ({names}). You can now import the private repositories "
                                         "you gave Airlock access to.")

    @router.post("/github/disconnect")
    @guarded
    async def github_disconnect(request: Request, csrf: str = Form("")):
        session = require(request, csrf)
        accounts().clear_github_installations(session["tid"])
        return set_flash(redirect("/console/workspaces"), "ok",
                         "Disconnected. Airlock no longer uses your GitHub installation; to remove its access on "
                         "GitHub too, uninstall the app from your GitHub settings.")

    # ------------------------------------------------------------------ activity (command audit log)

    @router.get("/activity", response_class=HTMLResponse)
    @guarded
    async def activity(request: Request, before: str = "", tenant: str = ""):
        session = require(request)
        log = audit_log()
        if log is None:
            return render(request, "message.html", 404, title="Not available",
                          message="The activity log isn't enabled on this server.")
        # Admins can look at any tenant (e.g. to investigate abuse); everyone else sees their own.
        target = tenant.strip() if tenant.strip() and is_admin(session) else session["tid"]
        entries, cursor = await run_in_threadpool(log.list, target, 100, before or None)
        return render(request, "activity.html", entries=entries, cursor=cursor, target=target,
                      own=target == session["tid"], retention_days=log.retention_days, first_page=not before)

    # ------------------------------------------------------------------ workspaces: sessions, repo import, terminal

    if sessions is None:
        return router

    from src.api.limits import LimitExceeded

    def failure(error: Exception):
        """(status, message) for the errors session operations raise on purpose."""
        if isinstance(error, HTTPException):
            return error.status_code, str(error.detail)
        if isinstance(error, LimitExceeded):
            return 429, str(error)
        raise error

    def json_response(status_code: int, body: dict) -> JSONResponse:
        return JSONResponse(body, status_code=status_code, headers=SECURITY_HEADERS)

    def require_api(request: Request) -> Optional[dict]:
        """For the terminal's fetch() calls: the session, with the CSRF token in a header."""
        session = session_of(request)
        token = request.headers.get("X-CSRF-Token", "")
        if session is None or not secrets.compare_digest(token, session["csrf"]):
            return None
        return session

    def workspaces_page(request: Request, session: dict, error: Optional[str] = None, status_code: int = 200):
        from src.api.github_app import Installation

        tenant = session["tid"]
        installations = [Installation(i["id"], i["account"], i.get("account_type", "User"))
                         for i in accounts().github_installations(tenant)] if gh_app() is not None else []
        return render(request, "workspaces.html", status_code, workspaces=sessions.list(tenant),
                      egress=egress(tenant), limits=limits(tenant), ttl_minutes=sessions.ttl_seconds // 60, error=error,
                      github_enabled=gh_app() is not None, installations=installations)

    @router.get("/workspaces", response_class=HTMLResponse)
    @guarded
    async def list_workspaces(request: Request):
        session = require(request)
        return workspaces_page(request, session)

    @router.post("/workspaces", response_class=HTMLResponse)
    @guarded
    async def create_workspace(request: Request, csrf: str = Form(""), internet: str = Form(""),
                               repo: str = Form(""), ref: str = Form("")):
        session = require(request, csrf)
        tenant = session["tid"]
        try:
            created = await run_in_threadpool(sessions.create, tenant, egress(tenant) if internet else [])
        except Exception as e:
            status_code, message = failure(e)
            return workspaces_page(request, session, error=message, status_code=status_code)
        response = redirect(f"/console/workspaces/{created['session_id']}")
        if repo.strip():
            try:
                result = await run_in_threadpool(sessions.import_repo, tenant, created["session_id"],
                                                 repo, ref.strip() or None, None, actor=f"console @{session['login']}")
                set_flash(response, "ok", f"Imported {result['repo']} ({result['files']} files) into {result['path']}.")
                response.headers["location"] += f"?cwd={result['path']}"
            except Exception as e:
                set_flash(response, "error", f"The workspace is ready, but the import failed: {failure(e)[1]}")
        return response

    @router.get("/workspaces/{session_id}", response_class=HTMLResponse)
    @guarded
    async def open_workspace(request: Request, session_id: str, cwd: str = HOME):
        session = require(request)
        try:
            workspace = sessions.get(session["tid"], session_id)
        except Exception as e:
            status_code, message = failure(e)
            return render(request, "message.html", status_code, title="Workspace not available",
                          message=f"{message}. Workspaces close after {sessions.ttl_seconds // 60} minutes without use.")
        return render(request, "workspace.html", workspace=workspace, home=HOME, start_cwd=clean_cwd(cwd),
                      ttl_minutes=sessions.ttl_seconds // 60)

    @router.post("/workspaces/{session_id}/delete")
    @guarded
    async def delete_workspace(request: Request, session_id: str, csrf: str = Form("")):
        session = require(request, csrf)
        response = redirect("/console/workspaces")
        try:
            await run_in_threadpool(sessions.destroy, session["tid"], session_id)
            set_flash(response, "ok", f"Closed {session_id} and wiped its files.")
        except Exception as e:
            set_flash(response, "error", failure(e)[1])
        return response

    @router.post("/workspaces/{session_id}/exec")
    async def workspace_exec(request: Request, session_id: str):
        session = require_api(request)
        if session is None:
            return json_response(401, {"error": "Your console session expired. Reload the page."})
        try:
            body = await request.json()
            command = str(body.get("command", ""))
            cwd = str(body.get("cwd") or HOME)
            timeout = max(1, min(int(body.get("timeout", 30)), 60))
        except Exception:
            return json_response(400, {"error": "Malformed request."})
        if not command.strip():
            return json_response(400, {"error": "Type a command."})
        if len(command) > MAX_COMMAND_CHARS:
            return json_response(400, {"error": f"Commands are limited to {MAX_COMMAND_CHARS} characters."})
        try:
            result = await run_in_threadpool(sessions.run, session["tid"], session_id, wrap_command(command, cwd), timeout,
                                             actor=f"console @{session['login']}", audit_command=command)
        except Exception as e:
            status_code, message = failure(e)
            return json_response(status_code, {"error": message})
        stdout, new_cwd = split_cwd(result["stdout"], cwd)
        return json_response(200, {
            "stdout": stdout, "stderr": result["stderr"], "exit_code": result["exit_code"],
            "timed_out": result["timed_out"], "warnings": result["warnings"], "cwd": new_cwd,
        })

    @router.post("/workspaces/{session_id}/import")
    async def workspace_import(request: Request, session_id: str):
        session = require_api(request)
        if session is None:
            return json_response(401, {"error": "Your console session expired. Reload the page."})
        try:
            body = await request.json()
            repo, ref, path = (str(body.get(k) or "").strip()[:300] for k in ("repo", "ref", "path"))
        except Exception:
            return json_response(400, {"error": "Malformed request."})
        try:
            result = await run_in_threadpool(sessions.import_repo, session["tid"], session_id,
                                             repo, ref or None, path or None, actor=f"console @{session['login']}")
        except Exception as e:
            status_code, message = failure(e)
            return json_response(status_code, {"error": message})
        return json_response(200, result)

    return router


class _Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class _Forbidden(Exception):
    pass
