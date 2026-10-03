import sys

from src.step1_runner.runner import run_command

# Quote the interpreter path: shlex.split runs in POSIX mode, and Windows paths
# contain backslashes and sometimes spaces.
PY = f'"{sys.executable}"'


def test_captures_stdout_and_exit_code():
    res = run_command(f"{PY} -c \"print('hello')\"")
    assert res.exit_code == 0
    assert res.stdout.strip() == "hello"
    assert not res.timed_out


def test_nonzero_exit_code_and_stderr():
    res = run_command(f"{PY} -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\"")
    assert res.exit_code == 3
    assert "boom" in res.stderr


def test_timeout_is_enforced():
    res = run_command(f"{PY} -c \"import time; time.sleep(5)\"", timeout_seconds=1)
    assert res.timed_out
    assert res.exit_code == -1


def test_missing_binary_returns_127():
    res = run_command("definitely-not-a-real-binary-xyz")
    assert res.exit_code == 127
    assert "not found" in res.stderr
