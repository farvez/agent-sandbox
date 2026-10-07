"""The TypeScript SDK (sdk/typescript) against the real API server over HTTP (containers faked)."""
import os
import shutil
import subprocess

import pytest

from tests.test_sdk import KEY, api_url, fresh_state  # noqa: F401  (pytest fixtures)

SDK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "typescript")
NODE = shutil.which("node")
BUILT = os.path.exists(os.path.join(SDK_DIR, "dist", "esm", "index.js"))

pytestmark = pytest.mark.skipif(
    not NODE or not BUILT, reason="needs Node.js and a built TypeScript SDK (cd sdk/typescript && npm ci && npm run build)"
)


def test_typescript_sdk_against_the_api(api_url):
    env = {**os.environ, "SANDBOX_API_URL": api_url, "SANDBOX_API_KEY": KEY}
    proc = subprocess.run([NODE, "--test", "test/integration.test.mjs"], cwd=SDK_DIR, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "# pass 1" in proc.stdout
