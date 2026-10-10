"""Private repositories: connecting the GitHub App in the console, and importing through it."""
import html

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.server as server
from src.api.accounts import AccountStore, _SqliteItems
from src.api.github_app import GitHubAppError, Installation
from src.api.keystore import KeyStore, SqliteBackend
from src.api.repos import RepoImportError
from src.console.auth import GitHubOAuth
from src.console.routes import ConsoleConfig, build_console_router
from tests.test_api_server import AUTH, client, create  # noqa: F401  (pytest fixture)
from tests.test_console import csrf_of, fake_github, sign_in
from tests.test_console_workspaces import FakeSessions


class FakeApp:
    """Stands in for GitHubApp: the one-time code decides which GitHub user installed it."""

    slug = "airlock-sandbox"

    def __init__(self):
        self.codes = {"good": (1, [Installation(11, "Farvez", "User"), Installation(22, "acme-org", "Organization")]),
                      "someone-else": (999, [Installation(33, "intruder", "User")])}
        self.tokens = {(22, "secret-repo"): "ghs_secret"}

    def install_url(self, state):
        return f"https://github.com/apps/{self.slug}/installations/new?state={state}"

    def authorize_url(self, state, redirect_uri):
        return f"https://github.com/login/oauth/authorize?client_id=Iv1.app&redirect_uri={redirect_uri}&state={state}"

    def user_installations(self, code):
        if code not in self.codes:
            raise GitHubAppError("The code passed is incorrect or expired.")
        return self.codes[code]

    def repo_token(self, installation_id, repo_name):
        return self.tokens.get((installation_id, repo_name))


@pytest.fixture
def console(tmp_path):
    accounts = AccountStore(_SqliteItems(str(tmp_path / "accounts.db")))
    config = ConsoleConfig("https://console.test", "client-id", "client-secret", "s" * 40, {"farvez"}, "invite")
    app = FastAPI()
    app.include_router(build_console_router(
        config, keystore=lambda: KeyStore(SqliteBackend(str(tmp_path / "keys.db"))), accounts=lambda: accounts,
        limits=lambda t: {"sessions_open": 0, "limits": {"max_sessions": 10, "requests_per_minute": 120, "max_concurrent_exec": 4}},
        egress=lambda t: [], sessions=FakeSessions(), github_app=lambda: FakeApp(),
        github=GitHubOAuth("client-id", "client-secret", "https://console.test/console/auth/callback", http=fake_github),
    ))
    client = TestClient(app, base_url="https://console.test", follow_redirects=False)
    sign_in(client, "code-admin")   # GitHub user id 1, @Farvez
    return client, accounts


def test_connect_sends_people_to_the_app_installation(console):
    client, _ = console
    page = client.get("/console/workspaces").text
    assert "Private repositories" in page and 'href="/console/github/connect"' in page
    res = client.get("/console/github/connect")
    assert res.status_code == 303 and res.headers["location"].startswith(
        "https://github.com/apps/airlock-sandbox/installations/new?state=")


def test_callback_links_the_installations_of_the_signed_in_user(console):
    client, accounts = console
    res = client.get("/console/github/callback?code=good&installation_id=11&setup_action=install")
    assert res.status_code == 303 and res.headers["location"] == "/console/workspaces"
    assert [i["id"] for i in accounts.github_installations("gh-farvez")] == [11, 22]
    page = html.unescape(client.get("/console/workspaces").text)
    assert "Connected GitHub (@Farvez, @acme-org)" in page
    assert "https://github.com/organizations/acme-org/settings/installations/22" in page


def test_callback_refuses_a_code_from_another_github_user(console):
    client, accounts = console
    client.get("/console/github/callback?code=someone-else&installation_id=33&setup_action=install")
    assert accounts.github_installations("gh-farvez") == []
    assert "GitHub authorized a different account" in html.unescape(client.get("/console/workspaces").text)


@pytest.mark.parametrize("query, message", [
    ("code=stale&setup_action=install", "Couldn't connect GitHub"),
    ("setup_action=install", "didn't confirm the connection"),
    ("setup_action=request", "organisation owner to approve"),
])
def test_callback_explains_what_happened(console, query, message):
    client, accounts = console
    client.get(f"/console/github/callback?{query}")
    assert message in html.unescape(client.get("/console/workspaces").text)
    assert accounts.github_installations("gh-farvez") == []


def test_callback_needs_a_console_session(console):
    client, accounts = console
    client.cookies.clear()
    assert client.get("/console/github/callback?code=good").headers["location"] == "/console"
    assert accounts.github_installations("gh-farvez") == []


def test_disconnect(console):
    client, accounts = console
    client.get("/console/github/callback?code=good&setup_action=install")
    csrf = csrf_of(client.get("/console/workspaces").text)
    assert client.post("/console/github/disconnect", data={"csrf": "forged"}).status_code == 403
    client.post("/console/github/disconnect", data={"csrf": csrf})
    assert accounts.github_installations("gh-farvez") == []
    assert "Connect GitHub" in client.get("/console/workspaces").text


# ------------------------------------------------------------------ importing through the installation

@pytest.fixture
def private_setup(client, monkeypatch, tmp_path):
    accounts = AccountStore(_SqliteItems(str(tmp_path / "a.db")))
    accounts.set_github_installations("acme", [{"id": 22, "account": "acme-org", "account_type": "Organization"}])
    monkeypatch.setattr(server, "ACCOUNTS", accounts)
    monkeypatch.setattr(server, "GITHUB_APP", FakeApp())
    downloads = []

    def fetch(repo, token=None):
        downloads.append((repo.full_name, token))
        if token is None:
            raise RepoImportError(f"{repo.full_name} wasn't found.", 404)
        return b"tgz"

    monkeypatch.setattr(server, "fetch_archive", fetch)
    monkeypatch.setattr(server, "import_archive", lambda ws, repo, archive, dest: {
        "repo": repo.full_name, "ref": repo.ref, "path": f"/workspace/{dest}", "dest": dest, "files": 2, "archive_bytes": 3})
    return downloads


def test_private_repo_is_imported_with_a_one_repo_token(client, private_setup):
    sid = create(client)
    res = client.post(f"/v1/sessions/{sid}/import", json={"repo": "acme-org/secret-repo"}, headers=AUTH)
    assert res.status_code == 200 and res.json()["path"] == "/workspace/secret-repo"
    assert private_setup == [("acme-org/secret-repo", None), ("acme-org/secret-repo", "ghs_secret")]


@pytest.mark.parametrize("repo", ["someone-else/secret-repo", "acme-org/not-selected"])
def test_repos_outside_the_installation_stay_404(client, private_setup, repo):
    sid = create(client)
    res = client.post(f"/v1/sessions/{sid}/import", json={"repo": repo}, headers=AUTH)
    assert res.status_code == 404


def test_other_tenants_cannot_use_the_installation(client, private_setup):
    from tests.test_api_server import OTHER_TENANT

    sid = client.post("/v1/sessions", json={}, headers=OTHER_TENANT).json()["session_id"]
    res = client.post(f"/v1/sessions/{sid}/import", json={"repo": "acme-org/secret-repo"}, headers=OTHER_TENANT)
    assert res.status_code == 404 and private_setup[-1][1] is None   # never even asked for a token


def test_link_existing_installation_authorizes_without_installing(console):
    client, accounts = console
    assert 'href="/console/github/link"' in client.get("/console/workspaces").text
    res = client.get("/console/github/link")
    assert res.status_code == 303
    assert res.headers["location"].startswith("https://github.com/login/oauth/authorize?client_id=Iv1.app")
    assert "redirect_uri=https://console.test/console/github/callback" in res.headers["location"]
    client.get("/console/github/callback?code=good")      # GitHub comes back with a code, no installation_id
    assert [i["id"] for i in accounts.github_installations("gh-farvez")] == [11, 22]
