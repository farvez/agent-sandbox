"""Host-side file access (src/sandbox/files.py), snapshot storage and runtime config."""
import json
import os

import pytest

from src.api import runtime_config
from src.api.snapshots import SnapshotError, SnapshotStore, _DirBackend, tenant_dir
from src.sandbox import files


def _symlink_or_skip(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra privileges on this machine")


def test_write_and_read_bytes(tmp_path):
    root = str(tmp_path)
    assert files.write_bytes(root, "a/b/c.bin", b"\x00\xff") == 2
    assert files.read_bytes(root, "a/b/c.bin", 10) == b"\x00\xff"


@pytest.mark.parametrize("bad", ["", "  ", "..", "../x", "a/../../x", "a\0b", "."])
def test_paths_outside_or_empty_are_refused(tmp_path, bad):
    with pytest.raises(files.WorkspaceFileError):
        files.write_bytes(str(tmp_path), bad, b"x")


def test_read_refuses_directories_and_oversize(tmp_path):
    os.mkdir(tmp_path / "dir")
    with pytest.raises(files.WorkspaceFileError):
        files.read_bytes(str(tmp_path), "dir", 10)
    (tmp_path / "big").write_bytes(b"x" * 11)
    with pytest.raises(files.WorkspaceFileError) as e:
        files.read_bytes(str(tmp_path), "big", 10)
    assert e.value.status == 413


def test_symlink_to_outside_is_refused(tmp_path, outside_dir):
    ws = tmp_path / "ws"
    ws.mkdir()
    _symlink_or_skip(os.path.join(outside_dir, "secret.txt"), ws / "link")
    with pytest.raises(files.WorkspaceFileError):
        files.read_bytes(str(ws), "link", 100)
    with pytest.raises(files.WorkspaceFileError):
        files.write_bytes(str(ws), "link", b"overwrite")
    with open(os.path.join(outside_dir, "secret.txt")) as f:
        assert f.read() == "HOST SECRET"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.name == "nt", reason="O_NOFOLLOW is POSIX")
def test_symlink_inside_workspace_is_refused_too(tmp_path):
    (tmp_path / "real.txt").write_bytes(b"x")
    _symlink_or_skip(tmp_path / "real.txt", tmp_path / "link")
    # The resolved path is inside, but the final component is a symlink: never followed.
    with pytest.raises(files.WorkspaceFileError):
        files.read_bytes(str(tmp_path), "link", 100)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")
def test_fifos_are_refused_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(files.WorkspaceFileError):
        files.read_bytes(str(tmp_path), "pipe", 100)
    with pytest.raises(files.WorkspaceFileError):
        files.write_bytes(str(tmp_path), "pipe", b"x")


def test_create_new_never_replaces(tmp_path):
    files.create_new(str(tmp_path), "x.tar.gz", b"1")
    with pytest.raises(FileExistsError):
        files.create_new(str(tmp_path), "x.tar.gz", b"2")


# ------------------------------------------------------------------ snapshots

def store(tmp_path, **kw):
    return SnapshotStore(_DirBackend(str(tmp_path)), **kw)


def test_snapshot_round_trip_and_expiry(tmp_path):
    now = [1000.0]
    s = store(tmp_path, retention_days=1, clock=lambda: now[0])
    meta = s.create("acme", b"archive", name="  my   snap ", session_id="sbx_1")
    assert meta["name"] == "my snap" and meta["size_bytes"] == 7
    assert s.get("acme", meta["id"]) == (meta, b"archive")
    assert s.list("globex") == []
    now[0] += 2 * 86400
    assert s.list("acme") == []
    with pytest.raises(SnapshotError):
        s.get("acme", meta["id"])


def test_snapshot_size_and_count_limits(tmp_path):
    s = store(tmp_path, limit=1, max_bytes=5)
    with pytest.raises(SnapshotError) as e:
        s.create("acme", b"123456")
    assert e.value.status == 413
    s.create("acme", b"1")
    with pytest.raises(SnapshotError) as e:
        s.create("acme", b"1")
    assert e.value.status == 409


def test_tenant_dirs_are_safe():
    assert tenant_dir("acme") == "acme"
    for weird in ("../x", "a/b", "..", "gh user"):
        assert "/" not in tenant_dir(weird) and tenant_dir(weird).startswith("t-")


def test_snapshot_config():
    assert SnapshotStore.from_config(None) is None
    with pytest.raises(RuntimeError):
        SnapshotStore.from_config("ftp://x")


# ------------------------------------------------------------------ runtime config

def test_runtime_config_only_on_the_server():
    assert runtime_config.parameter_name({}) is None
    assert runtime_config.parameter_name({"AWS_REGION": "us-east-1"}) is None
    server_env = {"AWS_REGION": "us-east-1", "SANDBOX_KEYSTORE": "dynamodb:t"}
    assert runtime_config.parameter_name(server_env) == runtime_config.DEFAULT_PARAM
    assert runtime_config.parameter_name({**server_env, "SANDBOX_RUNTIME_CONFIG": "off"}) is None
    assert runtime_config.parameter_name({"SANDBOX_RUNTIME_CONFIG": "ssm:/x"}) == "/x"


def test_runtime_config_applies_only_unset_sandbox_vars():
    env = {"SANDBOX_RUNTIME_CONFIG": "ssm:/p", "SANDBOX_A": "keep"}
    raw = json.dumps({"SANDBOX_A": "new", "SANDBOX_B": "b", "PATH": "/evil"})
    applied = runtime_config.apply(env, fetch=lambda name, region: raw)
    assert applied == {"SANDBOX_B": "b"}
    assert env["SANDBOX_A"] == "keep" and "PATH" not in env


def test_runtime_config_failures_are_ignored():
    def boom(name, region):
        raise RuntimeError("no access")
    env = {"SANDBOX_RUNTIME_CONFIG": "ssm:/p"}
    assert runtime_config.apply(env, fetch=boom) == {}
    assert runtime_config.apply(env, fetch=lambda n, r: None) == {}
    assert runtime_config.apply(env, fetch=lambda n, r: "[1]") == {}
