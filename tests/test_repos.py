"""GitHub repository import: parsing, the size-capped download, and unpacking in the sandbox."""
import io
import os
import re
import tarfile
import urllib.error

import pytest

from src.api.repos import (
    RepoImportError,
    RepoRef,
    _GitHubOnlyRedirects,
    clean_destination,
    fetch_archive,
    import_archive,
    parse_repo,
)
from tests.conftest import make_offline_workspace, requires_docker


def make_tar(files, top="repo-main"):
    """A GitHub-style archive: everything under one top-level folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("spec, ref, expected", [
    ("psf/requests", None, RepoRef("psf", "requests", "HEAD")),
    ("https://github.com/psf/requests", None, RepoRef("psf", "requests", "HEAD")),
    ("https://github.com/psf/requests.git", "v2.32.3", RepoRef("psf", "requests", "v2.32.3")),
    ("https://www.github.com/psf/requests/", None, RepoRef("psf", "requests", "HEAD")),
    ("https://github.com/psf/requests/tree/feature/x", None, RepoRef("psf", "requests", "feature/x")),
    ("  octo-cat/my.repo_1  ", " dev ", RepoRef("octo-cat", "my.repo_1", "dev")),
])
def test_parse_repo_accepts_common_forms(spec, ref, expected):
    assert parse_repo(spec, ref) == expected


@pytest.mark.parametrize("spec, ref", [
    ("", None), ("requests", None), ("https://gitlab.com/a/b", None), ("http://evil.test/github.com/a/b", None),
    ("-bad/repo", None), ("a/..", None), ("a/b c", None), ("a/b", "../../etc"), ("a/b", "x y"),
    ("https://github.com/a/b/blob/main/x.py", None), ("a/b", "/abs"),
])
def test_parse_repo_rejects_anything_else(spec, ref):
    with pytest.raises(RepoImportError):
        parse_repo(spec, ref)


def test_archive_url_points_at_github():
    assert parse_repo("psf/requests", "release/2.x").archive_url == \
        "https://github.com/psf/requests/archive/release/2.x.tar.gz"


@pytest.mark.parametrize("dest, expected", [(None, "requests"), ("", "requests"), ("src/app", "src/app"), ("/x/", "x")])
def test_destination_defaults_to_repo_name(dest, expected):
    assert clean_destination(dest, RepoRef("psf", "requests")) == expected


@pytest.mark.parametrize("dest", ["../up", "a/../b", "a b", "a/./b", "$(id)", "a;b"])
def test_destination_must_stay_inside_the_workspace(dest):
    with pytest.raises(RepoImportError):
        clean_destination(dest, RepoRef("psf", "requests"))


# ------------------------------------------------------------------ download

class FakeResponse(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}


def test_fetch_returns_the_archive():
    data = make_tar({"a.py": "print(1)"})
    seen = []
    assert fetch_archive(RepoRef("o", "r"), 2**20, lambda url: seen.append(url) or FakeResponse(data)) == data
    assert seen == ["https://github.com/o/r/archive/HEAD.tar.gz"]


def test_fetch_refuses_a_declared_oversize_archive():
    with pytest.raises(RepoImportError, match="larger than") as e:
        fetch_archive(RepoRef("o", "r"), 100, lambda url: FakeResponse(b"x" * 10, length=10_000))
    assert e.value.status == 413


def test_fetch_stops_reading_past_the_cap_even_without_a_length():
    response = FakeResponse(b"x" * 5000)
    response.headers = {}
    with pytest.raises(RepoImportError) as e:
        fetch_archive(RepoRef("o", "r"), 1000, lambda url: response)
    assert e.value.status == 413


def test_fetch_maps_404_to_a_friendly_error():
    def missing(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(RepoImportError, match="public repositories") as e:
        fetch_archive(RepoRef("o", "private"), 100, missing)
    assert e.value.status == 404


def test_redirects_only_to_github_hosts():
    handler = _GitHubOnlyRedirects()
    req = __import__("urllib.request").request.Request("https://github.com/o/r/archive/HEAD.tar.gz")
    ok = handler.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/o/r/tar.gz/HEAD")
    assert ok.full_url.startswith("https://codeload.github.com/")
    for target in ("https://169.254.169.254/latest/meta-data", "http://codeload.github.com/x", "https://evil.test/x"):
        with pytest.raises(RepoImportError):
            handler.redirect_request(req, None, 302, "Found", {}, target)


# ------------------------------------------------------------------ unpacking (sandbox faked)

class SimulatedSandbox:
    """An offline workspace whose execute() does what the in-sandbox script would."""

    def __init__(self, fail_with=None):
        self._ws = make_offline_workspace()
        self.workspace_dir = self._ws.workspace_dir
        self.quota_bytes = self._ws.quota_bytes
        self.container_user = None
        self.fail_with = fail_with
        self.scripts = []

    def disk_usage_bytes(self):
        return self._ws.disk_usage_bytes()

    def execute(self, script, timeout_seconds=15):
        self.scripts.append(script)
        tar_name = re.search(r"tar -xzf (\S+)", script).group(1)
        dest = re.search(r"mkdir -p -- (\S+)", script).group(1)
        tar_path = os.path.join(self.workspace_dir, tar_name)
        assert os.path.isfile(tar_path)
        try:
            if self.fail_with:
                return {"stdout": "", "stderr": self.fail_with, "exit_code": 2, "timed_out": False,
                        "oom_killed": False, "warnings": []}
            target = os.path.join(self.workspace_dir, dest)
            if os.path.exists(target):
                return {"stdout": "", "stderr": "already exists", "exit_code": 17, "timed_out": False,
                        "oom_killed": False, "warnings": []}
            count = 0
            with tarfile.open(tar_path) as tar:
                for m in tar.getmembers():
                    rel = m.name.split("/", 1)[1]
                    os.makedirs(os.path.dirname(os.path.join(target, rel)), exist_ok=True)
                    with open(os.path.join(target, rel), "wb") as f:
                        f.write(tar.extractfile(m).read())
                    count += 1
            return {"stdout": f"{count}\n", "stderr": "", "exit_code": 0, "timed_out": False,
                    "oom_killed": False, "warnings": []}
        finally:
            os.unlink(tar_path)   # the script's EXIT trap

    def cleanup(self):
        self._ws.cleanup()


@pytest.fixture
def sandbox():
    sbx = SimulatedSandbox()
    yield sbx
    sbx.cleanup()


def test_import_unpacks_into_the_destination_and_leaves_no_archive(sandbox):
    data = make_tar({"README.md": "# hi", "pkg/main.py": "print(1)"})
    result = import_archive(sandbox, RepoRef("o", "r"), data, "r")
    assert result == {"repo": "o/r", "ref": "HEAD", "path": "/workspace/r", "dest": "r", "files": 2,
                      "archive_bytes": len(data)}
    with open(os.path.join(sandbox.workspace_dir, "r", "pkg", "main.py")) as f:
        assert f.read() == "print(1)"
    assert os.listdir(sandbox.workspace_dir) == ["r"]


def test_import_quotes_the_destination_and_runs_tar_unprivileged(sandbox):
    import_archive(sandbox, RepoRef("o", "r"), make_tar({"a": "1"}), "src/app")
    script = sandbox.scripts[0]
    assert "--no-same-owner" in script and "--strip-components=1" in script and "trap" in script


def test_existing_destination_is_a_409(sandbox):
    os.makedirs(os.path.join(sandbox.workspace_dir, "r"))
    with pytest.raises(RepoImportError, match="already exists") as e:
        import_archive(sandbox, RepoRef("o", "r"), make_tar({"a": "1"}), "r")
    assert e.value.status == 409


def test_unpack_failure_reports_the_error_and_cleans_up():
    sbx = SimulatedSandbox(fail_with="tar: Disk quota exceeded")
    try:
        with pytest.raises(RepoImportError, match="Disk quota exceeded") as e:
            import_archive(sbx, RepoRef("o", "r"), make_tar({"a": "1"}), "r")
        assert e.value.status == 422
        assert os.listdir(sbx.workspace_dir) == []
    finally:
        sbx.cleanup()


def test_archive_larger_than_free_space_is_refused_before_writing(sandbox):
    sandbox.quota_bytes = 10
    with pytest.raises(RepoImportError, match="free disk space") as e:
        import_archive(sandbox, RepoRef("o", "r"), make_tar({"a": "1"}), "r")
    assert e.value.status == 413 and sandbox.scripts == []


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="needs POSIX symlinks")
def test_archive_file_never_follows_a_planted_symlink(sandbox, outside_dir, monkeypatch):
    # If sandbox code guessed the archive name and planted a symlink there, O_EXCL refuses it.
    monkeypatch.setattr("src.api.repos.secrets.token_hex", lambda n: "guessed")
    os.symlink(os.path.join(outside_dir, "secret.txt"),
               os.path.join(sandbox.workspace_dir, ".airlock-import-guessed.tar.gz"))
    with pytest.raises(FileExistsError):
        import_archive(sandbox, RepoRef("o", "r"), make_tar({"a": "1"}), "r")
    with open(os.path.join(outside_dir, "secret.txt")) as f:
        assert f.read() == "HOST SECRET"


# ------------------------------------------------------------------ real containers

@pytest.mark.docker
@requires_docker
def test_import_in_a_real_sandbox_contains_hostile_paths():
    from src.step5_agent.sandbox import SandboxedWorkspace

    hostile = make_tar({"ok.py": "print('fine')", "../../escape.txt": "x"}, top="repo-main")
    with SandboxedWorkspace() as ws:
        parent = os.path.dirname(ws.workspace_dir)
        before = set(os.listdir(parent))
        result = import_archive(ws, RepoRef("o", "r"), hostile, "r")
        assert result["files"] >= 1
        assert ws.run_command("python r/ok.py").startswith("[STDOUT]:\nfine")
        assert set(os.listdir(parent)) == before                  # nothing landed next to the workspace
        assert not any(n.startswith(".airlock-import-") for n in os.listdir(ws.workspace_dir))
