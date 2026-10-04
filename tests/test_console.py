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


def make_app(tmp_path, signup="invite"):
    config = ConsoleConfig("https://console.test", "client-id", "client-secret", "s" * 40, {"farvez"}, signup)
    keystore = KeyStore(SqliteBackend(str(tmp_path / "keys.db")))
    accounts = AccountStore(_SqliteItems(str(tmp_path / "accounts.db")))
    app = FastAPI()
    app.include_router(build_console_router(
        config, keystore=lambda: keystore, accounts=lambda: accounts,
        limits=lambda t: {"sessions_open": 0, "limits": {"max_sessions": 10, "requests_per_minute": 120, "max_concurrent_exec": 4}},
        egress=lambda t: ["pypi.org", "files.pythonhosted.org"],
        github=GitHubOAuth("client-id", "client-secret", "https://console.test/console/auth/callback", http=fake_github),
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
