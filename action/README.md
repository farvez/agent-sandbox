# Airlock GitHub Action

Run your tests, or any command, in an isolated [Airlock](https://github.com/farvez/agent-sandbox)
sandbox instead of on the runner: a gVisor container with **no network unless you allow hosts**,
a disk quota, and memory and CPU limits. Useful for code you don't fully trust: pull requests
from forks, AI-generated changes, third-party packages' install scripts.

```yaml
name: Tests in a sandbox
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: farvez/agent-sandbox/action@main
        with:
          api-key: ${{ secrets.AIRLOCK_API_KEY }}
          egress: pypi                 # only PyPI is reachable; leave empty for no network
          run: pip install -r requirements.txt && python -m pytest -q --junitxml=results/junit.xml
          download: results            # copy results back to the runner
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-results
          path: airlock-results/
```

Create the API key in the [console](https://airlock.complyoo.com/console) (API keys page) and add
it as a repository secret named `AIRLOCK_API_KEY`. Secrets aren't passed to workflows triggered by
pull requests from forks, so for those use `pull_request_target` only with care, or run the action
on `push`.

## How it works

1. The checked-out repository is packed (without `.git`, `node_modules`, virtualenvs and caches;
   at most 50 MB compressed), uploaded, and unpacked in the sandbox at `/workspace/repo`.
2. Your command runs there with bash, in the background of a long-running sandbox, so it can run
   longer than a single command's 60-second limit (up to `timeout-minutes`). Its output streams
   into the job log as it's produced.
3. Optionally a folder is downloaded back (`download`, into `download-to`). Only regular files and
   folders are copied; links are skipped.
4. The step's exit code is the command's, so a failing test fails the job. The sandbox is deleted.

## Inputs

| Input | Default | |
|-------|---------|---|
| `api-key` | required | Airlock API key |
| `run` | required | Command to run (bash) |
| `egress` | empty | Comma-separated hosts or presets the sandbox may reach over HTTPS (`pypi`, `npm`, `github`, `huggingface`, or host names your account allows). Empty = no network |
| `upload` | `true` | Upload the repository and run in it; `false` runs in an empty `/workspace` |
| `path` | `.` | Folder to upload |
| `download` | empty | Folder (relative to the repository) to copy back after the command |
| `download-to` | `airlock-results` | Where the downloaded files go on the runner |
| `timeout-minutes` | `20` | Longest the command may run |
| `api-url` | `https://airlock.complyoo.com` | Your Airlock server |

Outputs: `exit-code` and `session-id` (find it in the console's Activity page).

## Limits

Each run uses a long-running sandbox, and each account can have a couple of those open at once,
so very wide matrix builds may hit `Long-running session limit reached`; use `max-parallel` on the
matrix. The sandbox has the session limits of your account: 256 MB of memory, 1 CPU and a
512 MB workspace by default.
