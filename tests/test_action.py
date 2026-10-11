"""The GitHub Action's runner (action/run.py) against a scripted fake sandbox."""
import base64
import io
import os
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "action"))
import run as action  # noqa: E402


class FakeSandbox:
    """Serves the background command's log in pieces, as the real sandbox's file would grow."""

    def __init__(self, log=b"", exit_after_polls=1, exit_code=0, archive=b""):
        self.log, self.polls, self.exit_after, self.exit_code = log, 0, exit_after_polls, exit_code
        self.archive = archive
        self.commands, self.written, self.uploaded = [], {}, {}
        self.files = SimpleNamespace(write=self._write, upload=self._upload, download_archive=self._archive)

    def _write(self, path, content):
        self.written[path] = content

    def _upload(self, path, data):
        self.uploaded[path] = data
        return len(data)

    def _archive(self, path):
        self.archive_path = path
        return self.archive

    def run(self, command, timeout=15):
        self.commands.append(command)
        out = ""
        if command.startswith("python3 -c") and action.LOG in command:
            offset = int(command.split("f.seek(")[1].split(")")[0])
            self.polls += 1
            visible = self.log if self.polls >= self.exit_after else self.log[: len(self.log) // 2]
            data = visible[offset: offset + action.CHUNK]
            done = self.polls >= self.exit_after and offset + len(data) >= len(self.log)
            out = base64.b64encode(data).decode() + "\n" + (str(self.exit_code) if done else "") + "\n"
        return SimpleNamespace(stdout=out, check=lambda: None)


def test_pack_skips_git_and_caches(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)")
    for skipped in (".git", "node_modules", "src/__pycache__"):
        (tmp_path / skipped).mkdir(parents=True, exist_ok=True)
        (tmp_path / skipped / "junk").write_text("x")
    with tarfile.open(fileobj=io.BytesIO(action.pack(str(tmp_path))), mode="r:gz") as tar:
        names = tar.getnames()
    assert "src/app.py" in names and not any(n.startswith((".git", "node_modules")) or "__pycache__" in n for n in names)


def test_pack_size_limit(tmp_path):
    (tmp_path / "big.bin").write_bytes(os.urandom(2000))
    with pytest.raises(SystemExit):
        action.pack(str(tmp_path), max_bytes=1000)


def test_upload_and_start(tmp_path):
    (tmp_path / "t.py").write_text("x")
    sbx = FakeSandbox()
    action.upload_repo(sbx, str(tmp_path))
    assert action.ARCHIVE in sbx.uploaded and "tar -xzf" in sbx.commands[-1]
    action.start(sbx, "echo 'quotes' && exit 3", "/workspace/repo")
    assert sbx.written[action.SCRIPT] == "echo 'quotes' && exit 3\n"        # the command never touches a shell string
    assert "cd /workspace/repo" in sbx.commands[-1] and "nohup" in sbx.commands[-1]


def test_follow_streams_output_and_returns_the_exit_code():
    log = b"collecting...\n" + b"x" * (action.CHUNK + 10) + b"\n3 passed\n"
    sbx = FakeSandbox(log=log, exit_after_polls=3, exit_code=7)
    out = []
    code = action.follow(sbx, out.append, deadline=float("inf"), sleep=lambda s: None)
    assert code == 7 and b"".join(out) == log


def test_follow_times_out():
    sbx = FakeSandbox(log=b"still going", exit_after_polls=10**9)
    clock = iter(range(100))
    assert action.follow(sbx, lambda d: None, deadline=5, sleep=lambda s: None, clock=lambda: next(clock)) is None


def test_download_strips_the_folder_and_skips_links(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("repo/results/report.xml", b"<ok/>"), ("repo/results/sub/a.txt", b"a")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("repo/results/leak")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    sbx = FakeSandbox(archive=buf.getvalue())
    files = action.download(sbx, "results/", str(tmp_path / "out"))
    assert sbx.archive_path == "repo/results"
    assert sorted(files) == ["report.xml", "sub/a.txt"]
    assert (tmp_path / "out" / "report.xml").read_bytes() == b"<ok/>"
    assert not (tmp_path / "out" / "leak").exists()


def test_main_needs_inputs(monkeypatch, capsys):
    monkeypatch.delenv("INPUT_RUN", raising=False)
    assert action.main() == 2 and "`run`" in capsys.readouterr().out
    monkeypatch.setenv("INPUT_RUN", "true")
    monkeypatch.delenv("INPUT_API_KEY", raising=False)
    assert action.main() == 2
