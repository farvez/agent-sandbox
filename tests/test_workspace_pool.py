import os

import pytest

from src.sandbox.workspace_pool import (
    QuotaExceededError,
    WorkspaceCapacityError,
    WorkspacePool,
    disk_usage,
    quota_bytes_from_env,
)


@pytest.fixture
def pool_root(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    for i in (1, 2, 3):
        (root / f"slot-00{i}").mkdir()
    (root / "lost+found").mkdir()  # present on a real ext4 mount; never handed out
    return root


def test_claims_distinct_slots_until_full(pool_root):
    pool = WorkspacePool(str(pool_root))
    claimed = {pool.claim() for _ in range(3)}
    assert {os.path.basename(p) for p in claimed} == {"slot-001", "slot-002", "slot-003"}
    with pytest.raises(WorkspaceCapacityError):
        pool.claim()


def test_release_wipes_and_frees_the_slot(pool_root):
    pool = WorkspacePool(str(pool_root))
    slot = pool.claim()
    os.makedirs(os.path.join(slot, ".local", "lib"))
    with open(os.path.join(slot, "big.bin"), "wb") as f:
        f.write(b"x" * 1000)
    pool.release(slot)
    assert os.path.isdir(slot) and os.listdir(slot) == []   # directory (and its quota) kept
    assert pool.claim() == slot


def test_claim_marker_is_outside_the_slot(pool_root):
    pool = WorkspacePool(str(pool_root))
    slot = pool.claim()
    assert os.listdir(slot) == []
    assert os.path.isdir(os.path.join(f"{pool_root}.locks", os.path.basename(slot)))


def test_stale_claims_from_a_previous_process_are_reset(pool_root):
    old = WorkspacePool(str(pool_root))
    slot = old.claim()
    with open(os.path.join(slot, "leftover.txt"), "w") as f:
        f.write("from a session that died with the old API process")

    fresh = WorkspacePool(str(pool_root))   # e.g. after an API restart
    assert fresh.claim() == slot
    assert os.listdir(slot) == []


def test_release_ignores_paths_outside_the_pool(pool_root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    WorkspacePool(str(pool_root)).release(str(outside))
    assert (outside / "keep.txt").exists()


def test_disk_usage_counts_files_and_does_not_follow_symlinks(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 50_000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 50_000)
    outside = tmp_path.parent / f"{tmp_path.name}_outside.bin"
    outside.write_bytes(b"z" * 5_000_000)
    try:
        os.symlink(outside, tmp_path / "link")
    except OSError:
        pass  # no symlink privilege on Windows; the rest still applies
    used = disk_usage(str(tmp_path))
    assert 100_000 <= used < 1_000_000
    outside.unlink()


def test_quota_from_env(monkeypatch):
    monkeypatch.delenv("SANDBOX_WORKSPACE_QUOTA_MB", raising=False)
    assert quota_bytes_from_env() == 512 * 2**20
    monkeypatch.setenv("SANDBOX_WORKSPACE_QUOTA_MB", "64")
    assert quota_bytes_from_env() == 64 * 2**20
    assert quota_bytes_from_env(override_mb=1) == 2**20


# ---------------------------------------------------------------- soft quota on writes


def test_write_within_quota_succeeds(offline_workspace):
    offline_workspace.quota_bytes = 2**20
    offline_workspace.write_file("ok.txt", "x" * 100_000)


def test_write_past_quota_is_refused(offline_workspace):
    offline_workspace.quota_bytes = 2**20
    offline_workspace.write_file("a.txt", "x" * 600_000)
    with pytest.raises(QuotaExceededError, match="quota"):
        offline_workspace.write_file("b.txt", "y" * 600_000)
    assert not os.path.exists(os.path.join(offline_workspace.workspace_dir, "b.txt"))


def test_overwriting_a_file_counts_only_the_difference(offline_workspace):
    offline_workspace.quota_bytes = 2**20
    offline_workspace.write_file("a.txt", "x" * 700_000)
    offline_workspace.write_file("a.txt", "y" * 700_000)   # replaces, doesn't add


def test_recover_keeps_restored_slots_and_frees_the_rest(pool_root):
    old = WorkspacePool(str(pool_root))
    kept, dropped = old.claim(), old.claim()
    for slot, name in ((kept, "keep.txt"), (dropped, "drop.txt")):
        with open(os.path.join(slot, name), "w") as f:
            f.write("data")

    fresh = WorkspacePool(str(pool_root))   # the API restarted
    fresh.recover([kept])
    assert os.listdir(kept) == ["keep.txt"]          # the restored session keeps its files
    assert os.listdir(dropped) == []                 # the session that wasn't restored is wiped
    claimed = {fresh.claim(), fresh.claim()}
    assert kept not in claimed                       # still held by the restored session
    assert dropped in claimed


def test_recover_ignores_paths_outside_the_pool(pool_root, tmp_path):
    fresh = WorkspacePool(str(pool_root))
    fresh.recover([str(tmp_path / "elsewhere")])
    assert len({fresh.claim() for _ in range(3)}) == 3
