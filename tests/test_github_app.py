"""GitHub App: linking installations, one-repo read-only tokens, and private repo download."""
import io

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.api.github_app import GitHubApp, GitHubAppError, Installation
from src.api.repos import RepoRef, fetch_archive


@pytest.fixture(scope="module")
def keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


class FakeGitHub:
    """Records requests; answers from a table of (method, url-prefix) -> (status, body)."""

    def __init__(self, answers):
        self.answers = answers
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        for (m, prefix), answer in self.answers.items():
            if m == method and url.startswith(prefix):
                return answer
        return 404, {"message": "Not Found"}


def make_app(keys, http, clock=lambda: 1_790_000_000):
    return GitHubApp("123", keys[0], "Iv1.client", "client-secret", "airlock-sandbox", http=http, clock=clock)


def test_from_env(monkeypatch):
    for name in ("SANDBOX_GITHUB_APP_ID", "SANDBOX_GITHUB_APP_PRIVATE_KEY", "SANDBOX_GITHUB_APP_CLIENT_ID",
                 "SANDBOX_GITHUB_APP_CLIENT_SECRET", "SANDBOX_GITHUB_APP_SLUG"):
        monkeypatch.delenv(name, raising=False)
    assert GitHubApp.from_env() is None
    monkeypatch.setenv("SANDBOX_GITHUB_APP_ID", "123")
    with pytest.raises(RuntimeError, match="partly configured"):
        GitHubApp.from_env()


def test_install_url(keys):
    url = make_app(keys, FakeGitHub({})).install_url("st4te")
    assert url == "https://github.com/apps/airlock-sandbox/installations/new?state=st4te"


def test_user_installations_confirms_who_installed_it(keys):
    http = FakeGitHub({
        ("POST", "https://github.com/login/oauth/access_token"): (200, {"access_token": "ghu_user"}),
        ("GET", "https://api.github.com/user/installations"): (200, {"installations": [
            {"id": 11, "account": {"login": "farvez", "type": "User"}},
            {"id": 22, "account": {"login": "acme-org", "type": "Organization"}},
        ]}),
        ("GET", "https://api.github.com/user"): (200, {"id": 42, "login": "farvez"}),
    })
    user_id, installations = make_app(keys, http).user_installations("one-time-code")
    assert user_id == 42
    assert installations == [Installation(11, "farvez", "User"), Installation(22, "acme-org", "Organization")]
    assert http.requests[0][3] == {"client_id": "Iv1.client", "client_secret": "client-secret", "code": "one-time-code"}
    assert all(r[2].get("Authorization") == "Bearer ghu_user" for r in http.requests[1:])


def test_a_bad_code_is_refused(keys):
    http = FakeGitHub({("POST", "https://github.com/login/oauth/access_token"):
                       (200, {"error": "bad_verification_code", "error_description": "The code passed is incorrect."})})
    with pytest.raises(GitHubAppError, match="incorrect"):
        make_app(keys, http).user_installations("stale")


def test_repo_token_is_one_repo_read_only_and_signed_by_the_app(keys):
    http = FakeGitHub({("POST", "https://api.github.com/app/installations/22/access_tokens"): (201, {"token": "ghs_x"})})
    assert make_app(keys, http, clock=lambda: 1_790_000_000).repo_token(22, "secret-repo") == "ghs_x"
    method, url, headers, body = http.requests[0]
    assert body == {"repositories": ["secret-repo"], "permissions": {"contents": "read"}}
    claims = jwt.decode(headers["Authorization"].removeprefix("Bearer "), keys[1], algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 600


@pytest.mark.parametrize("status", [404, 422])
def test_repo_token_is_none_when_the_repo_is_not_included(keys, status):
    http = FakeGitHub({("POST", "https://api.github.com/app/installations/22/access_tokens"): (status, {})})
    assert make_app(keys, http).repo_token(22, "other") is None


def test_repo_token_errors_are_reported(keys):
    http = FakeGitHub({("POST", "https://api.github.com/app/installations/22/access_tokens"): (500, {"message": "boom"})})
    with pytest.raises(GitHubAppError, match="500"):
        make_app(keys, http).repo_token(22, "r")


def test_installation_settings_links():
    assert Installation(1, "farvez", "User").settings_url() == "https://github.com/settings/installations/1"
    assert Installation(2, "acme", "Organization").settings_url() == \
        "https://github.com/organizations/acme/settings/installations/2"


def test_private_download_uses_the_api_with_the_token():
    seen = []

    class Resp(io.BytesIO):
        headers = {}

    def opener(url, headers=None):
        seen.append((url, headers))
        return Resp(b"tgz")

    assert fetch_archive(RepoRef("acme-org", "secret", "HEAD"), 1000, opener, token="ghs_x") == b"tgz"
    assert seen == [("https://api.github.com/repos/acme-org/secret/tarball",
                     {"Authorization": "Bearer ghs_x", "Accept": "application/vnd.github+json"})]
    assert RepoRef("o", "r", "v1.2").api_tarball_url == "https://api.github.com/repos/o/r/tarball/v1.2"


def test_authorize_url_for_an_existing_installation(keys):
    from urllib.parse import parse_qs, urlparse

    url = make_app(keys, FakeGitHub({})).authorize_url("st", "https://x.test/console/github/callback")
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert parse_qs(urlparse(url).query) == {"client_id": ["Iv1.client"], "state": ["st"],
                                             "redirect_uri": ["https://x.test/console/github/callback"]}
