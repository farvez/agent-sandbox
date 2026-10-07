"""The saved session registry that lets sessions survive an API restart."""
import os
import stat
import sys

import pytest

from src.api import session_state


def test_round_trip(tmp_path):
    path = str(tmp_path / "state" / "sessions.json")
    session_state.save(path, [{"session_id": "sbx_1", "tenant_id": "acme"}])
    assert session_state.load(path) == [{"session_id": "sbx_1", "tenant_id": "acme"}]
    assert [n for n in os.listdir(tmp_path / "state") if n.startswith(".sessions-")] == []   # no temp leftovers


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_file_is_owner_only(tmp_path):
    path = str(tmp_path / "sessions.json")
    session_state.save(path, [])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("content", ["", "not json", '{"version": 1}', '["a list"]', '{"sessions": "nope"}'])
def test_missing_or_damaged_file_means_no_sessions(tmp_path, content):
    path = tmp_path / "sessions.json"
    path.write_text(content)
    assert session_state.load(str(path)) == []
    assert session_state.load(str(tmp_path / "absent.json")) == []
    assert session_state.load(None) == []
