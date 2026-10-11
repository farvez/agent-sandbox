"""File upload/download, folder archives and snapshots, through the REST API."""
import io
import os
import tarfile

import pytest

import src.api.server as server
from src.api.snapshots import SnapshotStore, _DirBackend
from tests.test_api_server import AUTH, OTHER_TENANT, FakeWorkspace, client, create  # noqa: F401 (fixture)


@pytest.fixture
def snapshots(monkeypatch, tmp_path):
    store = SnapshotStore(_DirBackend(str(tmp_path / "snaps")), limit=3)
    monkeypatch.setattr(server, "SNAPSHOTS", store)
    return store


def test_upload_and_download_binary(client):
    sid = create(client)
    data = bytes(range(256)) * 10
    res = client.put(f"/v1/sessions/{sid}/files", params={"path": "data/blob.bin"}, content=data, headers=AUTH)
    assert res.status_code == 200, res.text
    assert res.json() == {"path": "data/blob.bin", "size_bytes": len(data)}

    res = client.get(f"/v1/sessions/{sid}/files", params={"path": "data/blob.bin"}, headers=AUTH)
    assert res.status_code == 200
    assert res.content == data
    assert res.headers["content-type"] == "application/octet-stream"
    assert 'filename="blob.bin"' in res.headers["content-disposition"]


def test_download_missing_file_is_404(client):
    sid = create(client)
    res = client.get(f"/v1/sessions/{sid}/files", params={"path": "nope.txt"}, headers=AUTH)
    assert res.status_code == 404


def test_upload_outside_workspace_is_refused(client):
    sid = create(client)
    res = client.put(f"/v1/sessions/{sid}/files", params={"path": "../escape.txt"}, content=b"x", headers=AUTH)
    assert res.status_code == 403


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(server, "UPLOAD_MAX_BYTES", 10)
    sid = create(client)
    res = client.put(f"/v1/sessions/{sid}/files", params={"path": "big.bin"}, content=b"x" * 11, headers=AUTH)
    assert res.status_code == 413


def test_download_size_limit(client, monkeypatch):
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/files", params={"path": "big.bin"}, content=b"x" * 100, headers=AUTH)
    monkeypatch.setattr(server, "DOWNLOAD_MAX_BYTES", 50)
    res = client.get(f"/v1/sessions/{sid}/files", params={"path": "big.bin"}, headers=AUTH)
    assert res.status_code == 413


def test_files_of_another_tenant_are_forbidden(client):
    sid = create(client)
    res = client.get(f"/v1/sessions/{sid}/files", params={"path": "a"}, headers=OTHER_TENANT)
    assert res.status_code == 403
    res = client.put(f"/v1/sessions/{sid}/files", params={"path": "a"}, content=b"x", headers=OTHER_TENANT)
    assert res.status_code == 403


def test_archive_download(client):
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/files", params={"path": "proj/a.txt"}, content=b"hello", headers=AUTH)
    res = client.get(f"/v1/sessions/{sid}/archive", params={"path": "proj"}, headers=AUTH)
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"
    assert 'filename="proj.tar.gz"' in res.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(res.content), mode="r:gz") as tar:
        assert tar.extractfile("proj/a.txt").read() == b"hello"


def test_snapshots_disabled_is_501(client, monkeypatch):
    monkeypatch.setattr(server, "SNAPSHOTS", None)
    sid = create(client)
    assert client.post(f"/v1/sessions/{sid}/snapshots", json={}, headers=AUTH).status_code == 501
    assert client.get("/v1/snapshots", headers=AUTH).status_code == 501


def test_snapshot_restore_into_new_session(client, snapshots):
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/files", params={"path": "notes/todo.md"}, content=b"- ship", headers=AUTH)
    res = client.post(f"/v1/sessions/{sid}/snapshots", json={"name": "before refactor"}, headers=AUTH)
    assert res.status_code == 201, res.text
    snap = res.json()
    assert snap["name"] == "before refactor" and snap["session_id"] == sid

    listed = client.get("/v1/snapshots", headers=AUTH).json()
    assert [s["id"] for s in listed["snapshots"]] == [snap["id"]]
    assert listed["limit"] == 3

    # Another tenant can't see or use it.
    assert client.get("/v1/snapshots", headers=OTHER_TENANT).json()["snapshots"] == []
    res = client.post("/v1/sessions", json={"snapshot_id": snap["id"]}, headers=OTHER_TENANT)
    assert res.status_code == 404

    res = client.post("/v1/sessions", json={"snapshot_id": snap["id"]}, headers=AUTH)
    assert res.status_code == 201
    new_sid = res.json()["session_id"]
    res = client.get(f"/v1/sessions/{new_sid}/files", params={"path": "notes/todo.md"}, headers=AUTH)
    assert res.content == b"- ship"


def test_restore_into_existing_session_folder(client, snapshots):
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/files", params={"path": "a.txt"}, content=b"one", headers=AUTH)
    snap = client.post(f"/v1/sessions/{sid}/snapshots", json={}, headers=AUTH).json()
    other = create(client)
    res = client.post(f"/v1/sessions/{other}/restore", json={"snapshot_id": snap["id"], "path": "old"}, headers=AUTH)
    assert res.status_code == 200, res.text
    assert res.json()["path"] == "old"
    assert client.get(f"/v1/sessions/{other}/files", params={"path": "old/a.txt"}, headers=AUTH).content == b"one"


def test_snapshot_limit_and_delete(client, snapshots):
    sid = create(client)
    ids = [client.post(f"/v1/sessions/{sid}/snapshots", json={}, headers=AUTH).json()["id"] for _ in range(3)]
    res = client.post(f"/v1/sessions/{sid}/snapshots", json={}, headers=AUTH)
    assert res.status_code == 409
    assert client.delete(f"/v1/snapshots/{ids[0]}", headers=OTHER_TENANT).status_code == 404
    assert client.delete(f"/v1/snapshots/{ids[0]}", headers=AUTH).status_code == 200
    assert client.post(f"/v1/sessions/{sid}/snapshots", json={}, headers=AUTH).status_code == 201


def test_unknown_snapshot_ids_are_404(client, snapshots):
    sid = create(client)
    for bad in ("snap_000000000000", "../../etc", "x"):
        res = client.post(f"/v1/sessions/{sid}/restore", json={"snapshot_id": bad}, headers=AUTH)
        assert res.status_code == 404


def test_transfers_are_audited(client, monkeypatch, snapshots):
    entries = []
    monkeypatch.setattr(server, "record_audit", lambda tenant, **e: entries.append(e))
    sid = create(client)
    client.put(f"/v1/sessions/{sid}/files", params={"path": "a.bin"}, content=b"12", headers=AUTH)
    client.get(f"/v1/sessions/{sid}/files", params={"path": "missing"}, headers=AUTH)
    client.post(f"/v1/sessions/{sid}/snapshots", json={}, headers=AUTH)
    kinds = [(e["kind"], e.get("exit_code")) for e in entries]
    assert ("upload", 0) in kinds and ("download", None) in kinds and ("snapshot", 0) in kinds
