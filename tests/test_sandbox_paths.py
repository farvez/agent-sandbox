import os

import pytest


def test_write_then_read_roundtrip(offline_workspace):
    offline_workspace.write_file("pkg/main.py", "print('hi')")
    assert offline_workspace.read_file("pkg/main.py") == "print('hi')"


def test_missing_file_returns_error_message(offline_workspace):
    assert "does not exist" in offline_workspace.read_file("nope.py")


@pytest.mark.parametrize("path", ["../escape.txt", "a/../../escape.txt", "../../../../etc/passwd"])
def test_relative_traversal_is_blocked(offline_workspace, path):
    with pytest.raises(PermissionError):
        offline_workspace.write_file(path, "x")
    with pytest.raises(PermissionError):
        offline_workspace.read_file(path)


def test_absolute_path_is_blocked(offline_workspace, outside_dir):
    target = os.path.join(outside_dir, "secret.txt")
    with pytest.raises(PermissionError):
        offline_workspace.read_file(target)


def test_sibling_dir_with_shared_prefix_is_blocked(offline_workspace):
    # A plain startswith() check would accept "<workspace>_evil"
    sibling = os.path.basename(offline_workspace.workspace_dir) + "_evil/x.txt"
    with pytest.raises(PermissionError):
        offline_workspace.write_file("../" + sibling, "x")


def _symlink_or_skip(src, dst):
    try:
        os.symlink(src, dst)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks needs Developer Mode or admin rights on Windows")


def test_symlink_to_host_file_cannot_be_read(offline_workspace, outside_dir):
    # Simulates sandboxed code running `ln -s /host/secret leak` in /workspace
    _symlink_or_skip(os.path.join(outside_dir, "secret.txt"), os.path.join(offline_workspace.workspace_dir, "leak"))
    with pytest.raises(PermissionError):
        offline_workspace.read_file("leak")


def test_symlink_to_host_file_cannot_be_written(offline_workspace, outside_dir):
    secret = os.path.join(outside_dir, "secret.txt")
    _symlink_or_skip(secret, os.path.join(offline_workspace.workspace_dir, "leak"))
    with pytest.raises(PermissionError):
        offline_workspace.write_file("leak", "overwritten")
    with open(secret) as f:
        assert f.read() == "HOST SECRET"


def test_symlinked_directory_cannot_be_traversed(offline_workspace, outside_dir):
    _symlink_or_skip(outside_dir, os.path.join(offline_workspace.workspace_dir, "hostdir"))
    with pytest.raises(PermissionError):
        offline_workspace.read_file("hostdir/secret.txt")


def test_symlink_inside_workspace_is_allowed(offline_workspace):
    offline_workspace.write_file("real.txt", "ok")
    _symlink_or_skip(
        os.path.join(offline_workspace.workspace_dir, "real.txt"),
        os.path.join(offline_workspace.workspace_dir, "alias.txt"),
    )
    assert offline_workspace.read_file("alias.txt") == "ok"
