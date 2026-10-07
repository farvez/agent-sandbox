"""Developer console: sign-in, invites, keys, CSRF, security headers (GitHub faked)."""
import html
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.accounts import AccountStore, _SqliteItems
from src.api.keystore import KeyStore, SqliteBackend
from src.console.auth import SESSION_COOKIE, GitHubOAuth, Signer
from src.console.routes import ConsoleConfig, build_console_router

GITHUB_USERS = {   # OAuth code -> the GitHub account it signs in as
    "code-admin": {"id": 1, "login": "Farvez", "name": "Farvez", "avatar_url": "https://avatars.githubusercontent.com/u/1"},
    "code-dev": {"id": 2, "login": "some-dev", "name": None, "avatar_url": ""},
    "code-stranger": {"id": 3, "login": "stranger", "name": "Stranger", "avatar_url": ""},
}


def fake_github(method, url, form, bearer):
    if url == GitHubOAuth.TOKEN_URL:
        return {"access_token": f"token-{form['code']}"} if form["code"] in GITHUB_USERS else {"error_description": "bad code"}
    return GITHUB_USERS[bearer.removeprefix("token-")]


def make_app(tmp_path, signup="invite", audit=None, notify=None, sessions=None):
    config = ConsoleConfig("https://console.test", "client-id", "client-secret", "s" * 40, {"farvez"}, signup)
    keystore = KeyStore(SqliteBackend(str(tmp_path / "keys.db")))
    accounts = AccountStore(_SqliteItems(str(tmp_path / "accounts.db")))
    app = FastAPI()
    app.include_router(build_console_router(
        config, keystore=lambda: keystore, accounts=lambda: accounts,
        limits=lambda t: {"sessions_open": 0, "limits": {"max_sessions": 10, "requests_per_minute": 120, "max_concurrent_exec": 4}},
        egress=lambda t: ["pypi.org", "files.pythonhosted.org"],
        github=GitHubOAuth("client-id", "client-secret", "https://console.test/console/auth/callback", http=fake_github),
        audit=(lambda: audit) if audit is not None else None, notify=notify, sessions=sessions,
    ))
    return app, keystore, accounts


@pytest.fixture
def console(tmp_path):
    app, keystore, accounts = make_app(tmp_path)
    client = TestClient(app, base_url="https://console.test", follow_redirects=False)
    return client, keystore, accounts


def sign_in(client, code):
    start = client.get("/console/login")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/console/auth/callback?code={code}&state={state}")


def csrf_of(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_signed_out_sees_landing_with_security_headers(console):
    client, *_ = console
    res = client.get("/console")
    assert res.status_code == 200 and "Sign in with GitHub" in res.text and "Invite-only" in res.text
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert res.headers["x-content-type-options"] == "nosniff"


def test_login_redirects_to_github_with_state_and_identity_scope(console):
    client, *_ = console
    res = client.get("/console/login")
    query = parse_qs(urlparse(res.headers["location"]).query)
    assert res.headers["location"].startswith(GitHubOAuth.AUTHORIZE_URL)
    assert query["client_id"] == ["client-id"] and query["scope"] == ["read:user"]
    assert query["redirect_uri"] == ["https://console.test/console/auth/callback"]
    assert "httponly" in res.headers["set-cookie"].lower() and "secure" in res.headers["set-cookie"].lower()


def test_callback_rejects_a_wrong_state(console):
    client, *_ = console
    client.get("/console/login")
    assert client.get("/console/auth/callback?code=code-admin&state=forged").status_code == 400


def test_admin_signs_in_and_gets_a_tenant(console):
    client, _, accounts = console
    res = sign_in(client, "code-admin")
    assert res.status_code == 303 and res.headers["location"] == "/console"
    cookie = res.headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
    page = client.get("/console").text
    assert "Welcome, Farvez" in page and "gh-farvez" in page and "Create your first API key" in page
    assert accounts.get_user(1)["tenant_id"] == "gh-farvez"


def test_uninvited_user_is_turned_away_until_invited(console):
    client, _, accounts = console
    denied = sign_in(client, "code-dev")
    assert denied.status_code == 403 and "isn't invited" in html.unescape(denied.text)
    accounts.invite("Some-Dev", invited_by="farvez")
    assert sign_in(client, "code-dev").status_code == 303
    assert accounts.get_user(2)["tenant_id"] == "gh-some-dev"


def test_create_key_shows_it_once_and_it_authenticates(console):
    client, keystore, _ = console
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/keys").text)
    created = client.post("/console/keys", data={"csrf": csrf, "name": "laptop"})
    api_key = re.search(r'<pre id="newkey">(asb_[^<]+)</pre>', created.text).group(1)
    assert "won't be shown again" in html.unescape(created.text)
    assert keystore.authenticate(api_key) == "gh-farvez"
    listing = client.get("/console/keys").text
    assert "laptop" in listing and api_key not in listing            # never shown again


def test_revoke_key(console):
    client, keystore, _ = console
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/keys").text)
    page = client.post("/console/keys", data={"csrf": csrf, "name": "temp"}).text
    api_key = re.search(r'<pre id="newkey">(asb_[^<]+)</pre>', page).group(1)
    key_id = api_key.split("_")[1]
    after = client.post(f"/console/keys/{key_id}/revoke", data={"csrf": csrf})
    assert "Revoked" in after.text and keystore.authenticate(api_key) is None


def test_posts_without_the_session_csrf_token_are_refused(console):
    client, keystore, _ = console
    sign_in(client, "code-admin")
    assert client.post("/console/keys", data={"csrf": "wrong", "name": "x"}).status_code == 403
    assert client.post("/console/keys", data={"name": "x"}).status_code == 403
    assert keystore.list("gh-farvez") == []


def test_admin_page_is_for_admins_only(console):
    client, _, accounts = console
    accounts.invite("some-dev", invited_by="farvez")
    sign_in(client, "code-dev")
    assert client.get("/console/admin").status_code == 403
    assert "Admin" not in client.get("/console").text.split("<main>")[0]   # no nav link either


def test_admin_invites_through_the_console(console):
    client, _, accounts = console
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/admin").text)
    page = client.post("/console/admin/invites", data={"csrf": csrf, "login": "@NewPerson"})
    assert "@newperson" in page.text and accounts.is_invited("newperson")
    bad = client.post("/console/admin/invites", data={"csrf": csrf, "login": "not a user!"})
    assert "isn't a valid GitHub username" in html.unescape(bad.text)
    client.post("/console/admin/invites/newperson/delete", data={"csrf": csrf})
    assert not accounts.is_invited("newperson")


def test_tampered_or_missing_session_means_signed_out(console):
    client, *_ = console
    sign_in(client, "code-admin")
    client.cookies.set(SESSION_COOKIE, client.cookies.get(SESSION_COOKIE)[:-2] + "xx", domain="console.test")
    assert "Sign in with GitHub" in client.get("/console").text
    assert client.get("/console/keys").status_code == 303


def test_sign_out(console):
    client, *_ = console
    page = sign_in(client, "code-admin") and client.get("/console").text
    client.post("/console/logout", data={"csrf": csrf_of(page)})
    assert "Sign in with GitHub" in client.get("/console").text


def test_open_signup_lets_anyone_in(tmp_path):
    app, _, accounts = make_app(tmp_path, signup="open")
    client = TestClient(app, base_url="https://console.test", follow_redirects=False)
    assert sign_in(client, "code-stranger").status_code == 303
    assert accounts.get_user(3)["tenant_id"] == "gh-stranger"


def test_github_failure_is_a_clean_error(console):
    client, *_ = console
    start = client.get("/console/login")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    res = client.get(f"/console/auth/callback?code=unknown&state={state}")
    assert res.status_code == 502 and "didn't confirm" in html.unescape(res.text)


def test_signer_rejects_tampering_and_expiry():
    clock = [1000.0]
    signer = Signer("k" * 40, clock=lambda: clock[0])
    token = signer.dumps({"a": 1}, ttl=60)
    assert signer.loads(token)["a"] == 1
    assert signer.loads(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    clock[0] += 61
    assert signer.loads(token) is None
    with pytest.raises(ValueError):
        Signer("short")


def test_deploy_bundle_ships_console_assets():
    """Regression: the Terraform bundle once shipped only src/**/*.py, so the server crashed
    on boot without the console's templates and static files."""
    import os

    main_tf = open(os.path.join(os.path.dirname(__file__), "..", "terraform", "main.tf"), encoding="utf-8").read()
    assert 'fileset("${path.module}/..", "src/**")' in main_tf
    console_dir = os.path.join(os.path.dirname(__file__), "..", "src", "console")
    assert os.listdir(os.path.join(console_dir, "templates")) and os.listdir(os.path.join(console_dir, "static"))


# ------------------------------------------------------------------ invite requests

def request_form(page_text):
    return re.search(r'name="csrf" value="([^"]+)"', page_text).group(1)


def test_stranger_gets_a_request_form_tied_to_their_github_account(console):
    client, _, accounts = console
    denied = sign_in(client, "code-stranger")
    assert denied.status_code == 403 and "isn't invited" in html.unescape(denied.text)
    assert "Request an invite" in denied.text and "@stranger" in denied.text
    assert "airlock_pending" in denied.headers["set-cookie"] and "httponly" in denied.headers["set-cookie"].lower()

    res = client.post("/console/request-invite", data={
        "csrf": request_form(denied.text), "note": "testing my agent " + "x" * 600, "contact": "me@example.com"})
    assert res.status_code == 303 and res.headers["location"] == "/console/request-invite"
    page = client.get("/console/request-invite")
    assert "Request sent" in page.text and "Your request is in" in page.text
    saved = accounts.get_request("stranger")
    assert saved["github_id"] == 3 and saved["contact"] == "me@example.com" and len(saved["note"]) == 500


def test_request_needs_the_signed_in_identity_and_its_token(console):
    client, _, accounts = console
    denied = sign_in(client, "code-stranger")
    token = request_form(denied.text)
    assert client.post("/console/request-invite", data={"csrf": "forged"}).status_code == 400
    client.cookies.set("airlock_pending", "tampered", domain="console.test", path="/console")
    assert client.post("/console/request-invite", data={"csrf": token}).status_code == 400
    assert accounts.list_requests() == []


def test_request_page_without_github_sign_in_starts_it(console):
    client, *_ = console
    assert client.get("/console/request-invite").headers["location"] == "/console/login"


def test_admin_approves_a_request_and_the_user_can_sign_in(console):
    client, _, accounts = console
    denied = sign_in(client, "code-stranger")
    client.post("/console/request-invite", data={"csrf": request_form(denied.text), "note": "evals", "contact": "@stranger"})
    client.cookies.clear()

    sign_in(client, "code-admin")
    admin = client.get("/console/admin")
    assert "Invite requests" in admin.text and "evals" in admin.text and 'class="badge"' in admin.text
    csrf = csrf_of(admin.text)
    res = client.post("/console/admin/requests/stranger/approve", data={"csrf": csrf})
    assert res.status_code == 303
    assert accounts.is_invited("stranger") and accounts.get_request("stranger") is None
    assert "Invited @stranger" in client.get("/console/admin").text and "@stranger" in client.get("/console/admin").text
    client.cookies.clear()

    assert sign_in(client, "code-stranger").status_code == 303   # approved: straight in


def test_admin_dismisses_and_non_admins_cannot_decide(console):
    client, _, accounts = console
    accounts.request_invite(3, "stranger", "Stranger", "", "", "")
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/admin").text)
    client.cookies.clear()

    accounts.invite("some-dev", invited_by="farvez")
    sign_in(client, "code-dev")
    dev_csrf = csrf_of(client.get("/console").text)
    assert client.post("/console/admin/requests/stranger/approve", data={"csrf": dev_csrf}).status_code == 403
    assert not accounts.is_invited("stranger")
    client.cookies.clear()

    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/admin").text)
    assert client.post("/console/admin/requests/stranger/dismiss", data={"csrf": csrf}).status_code == 303
    assert accounts.get_request("stranger") is None and not accounts.is_invited("stranger")


def test_full_request_queue_is_explained(console, monkeypatch):
    from src.api import accounts as accounts_module

    client, _, accounts = console
    monkeypatch.setattr(accounts_module, "MAX_PENDING_REQUESTS", 0)
    denied = sign_in(client, "code-stranger")
    res = client.post("/console/request-invite", data={"csrf": request_form(denied.text)})
    assert res.status_code == 503 and "Too many invite requests" in res.text


def test_signing_in_as_an_uninvited_account_ends_the_previous_session(console):
    client, *_ = console
    sign_in(client, "code-admin")
    denied = sign_in(client, "code-stranger")
    assert "@farvez" not in denied.text.lower() and "Sign out" not in denied.text
    assert 'airlock_session=""' in denied.headers.get("set-cookie", "") or "airlock_session=;" in denied.headers.get("set-cookie", "")
    assert client.get("/console/keys").headers["location"] == "/console"


# ------------------------------------------------------------------ remove access, notifications, activity

def admin_client(tmp_path, **kw):
    app, keystore, accounts = make_app(tmp_path, **kw)
    return TestClient(app, base_url="https://console.test", follow_redirects=False), keystore, accounts


def test_admin_removes_a_user_everywhere(tmp_path):
    from tests.test_console_workspaces import FakeSessions

    sessions = FakeSessions()
    client, keystore, accounts = admin_client(tmp_path, sessions=sessions)
    accounts.invite("some-dev", invited_by="farvez")
    sign_in(client, "code-dev")
    dev_cookie = client.cookies.get("airlock_session")
    keystore.issue("gh-some-dev", "laptop")
    keystore.issue("gh-some-dev", "ci")
    sessions.create("gh-some-dev", [])
    client.cookies.clear()

    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/admin").text)
    res = client.post("/console/admin/users/2/remove", data={"csrf": csrf})
    assert res.status_code == 303
    notice = html.unescape(client.get("/console/admin").text)
    assert "Removed @some-dev: 2 key(s) revoked, 1 sandbox(es) closed" in notice and "Unblock" in notice
    assert accounts.get_user(2) is None and accounts.is_blocked("some-dev") and not accounts.is_invited("some-dev")
    assert not [k for k in keystore.list("gh-some-dev") if k.active] and sessions.list("gh-some-dev") == []
    client.cookies.clear()

    client.cookies.set("airlock_session", dev_cookie, domain="console.test", path="/console")
    assert client.get("/console/keys").headers["location"] == "/console"     # old login no longer works
    client.cookies.clear()
    denied = sign_in(client, "code-dev")
    assert denied.status_code == 403 and "no longer has access" in denied.text and "Request an invite" not in denied.text


def test_admins_cannot_be_removed_and_others_cannot_remove(tmp_path):
    client, _, accounts = admin_client(tmp_path)
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/admin").text)
    assert client.post("/console/admin/users/1/remove", data={"csrf": csrf}).status_code == 403
    assert accounts.get_user(1) is not None
    client.cookies.clear()

    accounts.invite("some-dev", invited_by="farvez")
    sign_in(client, "code-dev")
    dev_csrf = csrf_of(client.get("/console").text)
    assert client.post("/console/admin/users/1/remove", data={"csrf": dev_csrf}).status_code == 403


def test_unblocking_lets_them_request_again(tmp_path):
    client, _, accounts = admin_client(tmp_path)
    accounts.block("stranger", blocked_by="farvez")
    assert sign_in(client, "code-stranger").status_code == 403
    sign_in(client, "code-admin")
    csrf = csrf_of(client.get("/console/admin").text)
    client.post("/console/admin/blocked/stranger/unblock", data={"csrf": csrf})
    client.cookies.clear()
    assert "Request an invite" in sign_in(client, "code-stranger").text


def test_new_invite_request_emails_the_operator_once(tmp_path):
    sent = []
    client, _, _ = admin_client(tmp_path, notify=lambda subject, message: sent.append((subject, message)))
    denied = sign_in(client, "code-stranger")
    token = re.search(r'name="csrf" value="([^"]+)"', denied.text).group(1)
    client.post("/console/request-invite", data={"csrf": token, "note": "agents", "contact": "s@example.com"})
    client.post("/console/request-invite", data={"csrf": token, "note": "agents and evals"})   # an edit
    assert len(sent) == 1
    subject, message = sent[0]
    assert subject == "Airlock: invite request from @stranger"
    assert "agents" in message and "s@example.com" in message and "https://console.test/console/admin" in message


def test_a_failing_email_never_breaks_the_request(tmp_path):
    def broken(subject, message):
        raise RuntimeError("sns down")

    client, _, accounts = admin_client(tmp_path, notify=broken)
    denied = sign_in(client, "code-stranger")
    token = re.search(r'name="csrf" value="([^"]+)"', denied.text).group(1)
    assert client.post("/console/request-invite", data={"csrf": token}).status_code == 303
    assert accounts.get_request("stranger") is not None


def make_audit(tmp_path):
    from src.api.audit import AuditLog, _SqliteAudit

    return AuditLog(_SqliteAudit(str(tmp_path / "audit.db")))


def test_activity_page_shows_own_commands(tmp_path):
    audit = make_audit(tmp_path)
    audit.record("gh-farvez", session_id="sbx_1", actor="console @farvez", kind="exec", command="pytest -q",
                 exit_code=1, duration_s=2.5)
    audit.record("gh-someone", session_id="sbx_9", actor="key x", kind="exec", command="secret-of-someone")
    client, *_ = admin_client(tmp_path, audit=audit)
    sign_in(client, "code-admin")
    assert 'href="/console/activity"' in client.get("/console").text
    page = client.get("/console/activity").text
    assert "$ pytest -q" in page and "exit 1" in page and "console @farvez" in page
    assert "secret-of-someone" not in page


def test_admins_can_open_any_tenants_activity_others_cannot(tmp_path):
    audit = make_audit(tmp_path)
    audit.record("gh-someone", session_id="sbx_9", actor="key x", kind="exec", command="their-command")
    client, _, accounts = admin_client(tmp_path, audit=audit)
    sign_in(client, "code-admin")
    assert "their-command" in client.get("/console/activity?tenant=gh-someone").text
    client.cookies.clear()

    accounts.invite("some-dev", invited_by="farvez")
    sign_in(client, "code-dev")
    assert "their-command" not in client.get("/console/activity?tenant=gh-someone").text


def test_activity_is_hidden_without_an_audit_log(tmp_path):
    client, *_ = admin_client(tmp_path)
    sign_in(client, "code-admin")
    assert 'href="/console/activity"' not in client.get("/console").text
    assert client.get("/console/activity").status_code == 404


# ------------------------------------------------------------------ privacy and terms

def test_policy_pages_are_public_and_linked(console):
    client, *_ = console
    for path, heading in (("/console/privacy", "Privacy Policy"), ("/console/terms", "Terms of Use")):
        page = client.get(path)
        assert page.status_code == 200 and heading in page.text
        assert "github.com/farvez/agent-sandbox/issues" in page.text          # no contact configured
    landing = client.get("/console").text
    assert 'href="/console/privacy"' in landing and 'href="/console/terms"' in landing


def test_privacy_page_states_retention_and_shows_the_contact(tmp_path):
    config = ConsoleConfig("https://console.test", "client-id", "client-secret", "s" * 40, {"farvez"}, "invite",
                           "privacy@example.com")
    app = FastAPI()
    app.include_router(build_console_router(
        config, keystore=lambda: None, accounts=lambda: AccountStore(_SqliteItems(str(tmp_path / "a.db"))),
        limits=lambda t: {}, egress=lambda t: [],
        github=GitHubOAuth("client-id", "client-secret", "https://console.test/console/auth/callback", http=fake_github),
    ))
    page = html.unescape(TestClient(app, base_url="https://console.test").get("/console/privacy").text)
    assert "privacy@example.com" in page
    for promise in ("90 days", "30 days", "14 days", "Never the command's output", "read:user"):
        assert promise in page
