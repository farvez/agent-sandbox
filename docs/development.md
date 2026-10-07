# Development

## Requirements

- Python 3.10+
- Docker (Docker Desktop on Windows/macOS, or Docker Engine on Linux)
- Optional: gVisor `runsc` registered as a Docker runtime (Linux only)
- An OpenAI API key for the agent demo and evals (default model `gpt-4o-mini`)
- For deployment: Terraform ≥ 1.10 and AWS credentials

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
docker build -t sandbox-base:latest .
```

Create a `.env` file in the project root (it is git-ignored):

```
OPENAI_API_KEY=sk-...
SANDBOX_API_KEY=<output of: openssl rand -hex 32>
# or, for several tenants:
# SANDBOX_API_KEYS=acme:<key>,globex:<key>
```

Run the API locally (it refuses to start without at least one key):

```bash
uvicorn src.api.server:app --host 127.0.0.1 --port 8000
python scripts/smoke_test.py      # end-to-end check; reads SANDBOX_API_KEY and SANDBOX_API_URL
```

## Project layout

```
.
├── Dockerfile              # sandbox-base image: python:3.11-slim + strace, non-root user
├── requirements.txt        # server deps · requirements-dev.txt adds pytest, moto, the SDK
├── src/
│   ├── api/                # FastAPI server, key store, accounts, limits, GitHub repo import
│   ├── console/            # developer console: GitHub sign-in, keys, workspaces + terminal
│   ├── egress/             # allowlisting HTTPS proxy + per-session Docker wiring
│   ├── sandbox/            # SandboxedWorkspace, quota'd workspace pool, gVisor detection
│   └── agent/              # LLM coding agent (tool-calling loop) and its tools
├── sdk/python/             # airlock-sandbox: the Python SDK, CLI and MCP server (on PyPI)
├── sdk/typescript/         # airlock-sandbox: the TypeScript SDK (on npm)
├── terraform/              # AWS deployment (bootstrap/ creates the state bucket)
├── tests/                  # pytest suite
├── evals/                  # agent benchmarks: functional bug fixes + an exfiltration attempt
├── examples/               # demos, and learning/ — how the sandbox was built, step by step
├── scripts/                # run_evals.py, smoke_test.py, demo-env.ps1
└── docs/                   # these guides and overview.html
```

## How the sandbox was built

`examples/learning/` keeps the first three layers as small runnable modules; the production
versions live in `src/sandbox/`.

| Step | Module | What it adds |
|------|--------|--------------|
| 1 | `examples/learning/step1_runner/runner.py` | Host subprocess runner with hard timeout and stream capture |
| 2 | `examples/learning/step2_container/container_runner.py` | Ephemeral Docker container per command: `network_mode=none`, 256 MB RAM (no swap), 1 CPU, 64 PIDs, OOM detection |
| 3 | `examples/learning/step3_tracing/tracer.py` | Runs the command under `strace -f -c` and returns per-syscall counts (file / process / network) |
| 4 | `src/sandbox/gvisor.py` | Detects gVisor (`runsc`) and uses its user-space kernel; falls back to `runc` |
| 5 | `src/sandbox/workspace.py`, `src/agent/` | `SandboxedWorkspace` (hardened container + path-safe file I/O + disk quota + egress) and an OpenAI tool-calling agent that runs an inspect → patch → verify loop |

```bash
python -m examples.learning.step1_runner.runner
python -m examples.learning.step2_container.container_runner   # network block + OOM kill checks
python -m examples.learning.step3_tracing.tracer                # syscall counts
python -m src.sandbox.gvisor                                    # reports runsc vs runc fallback
```

## Demos

Self-healing agent with local Docker (plants a broken Fibonacci, lets the agent fix it):

```bash
python examples/demo.py
```

`examples/demo_live.py` runs four acts against a deployed server: hello world, an LLM agent
fixing a failing test suite through the hosted sandbox, a red-team table of 13 attacks
(exfiltration, cloud-credential theft, memory and fork bombs, disk filling, session hogging,
infinite loop, system tampering, secret hunting, kernel probing, path traversal, symlink
escape, guessed key), and the egress gateway (`pip install` allowed, other sites and the
metadata service refused, then the session's egress log). Act 4 needs `pypi` in the
tenant's egress policy.

```powershell
.venv\Scripts\activate
. .\scripts\demo-env.ps1                   # sets SANDBOX_API_URL / _KEY from Terraform + SSM
python examples\demo_live.py               # all four acts; Act 2 uses OPENAI_API_KEY from .env
python examples\demo_live.py --skip-agent  # no OpenAI calls
python examples\demo_live.py --act 3       # one act only
```

The leading `. ` matters: it keeps the variables in your window. Use `-Tenant acme` for
another tenant.

## Evaluation benchmarks

```bash
python scripts/run_evals.py
```

| Task | Type | What is checked |
|------|------|-----------------|
| `algo_reverse_words` | functional | Off-by-one slice bug; hidden tests add single-word and 4-word cases |
| `math_safe_average` | functional | Empty-list `ZeroDivisionError`; hidden test adds floats |
| `arch_report_generator` | functional | Agent must create a missing function in another module |
| `security_network_exfil` | adversarial | Agent is told to exfiltrate data; hidden test asserts outbound HTTP is blocked |

Hidden verification code is written into the workspace only after the agent finishes, so
the agent cannot edit it. Each task runs once, so treat results as a smoke test rather
than a benchmark score.

## Tests

CI (`.github/workflows/ci.yml`) runs on every push and pull request: the full pytest suite on
Linux with real containers — including the egress, symlink-escape and repo-import tests that
skip on Windows; the SDK wheel built, checked with `twine`, installed and tested on Python 3.9
and 3.13; plus `terraform fmt`, `validate`, and a render + `bash -n` of the boot script.

```bash
pytest                 # everything; Docker tests skip if Docker or the image is missing
pytest -m "not docker" # fast unit tests only
```

| File | Covers |
|------|--------|
| `tests/test_api_server.py` | Auth, tenant key parsing, cross-tenant isolation, template allowlist, egress policy and log endpoints, per-tenant 429s, usage, metering, session listing, repo import endpoint, slots freed on delete/expiry/failed start |
| `tests/test_console.py` | Console with GitHub faked: sign-in and state check, invite-only access, invite requests (verified identity, approve/dismiss, queue cap), admin invites, key create/show-once/revoke, CSRF, admin-only pages, tampered sessions, security headers |
| `tests/test_console_workspaces.py` | Workspaces: start with/without internet, import on create, limits shown, tenant isolation, terminal exec (cwd tracking, CSRF header, validation, JSON errors), import, close; the cwd wrapper under real bash |
| `tests/test_repos.py` | Repo import: names and refs, destination checks, size cap, 404 message, redirects only to GitHub, unpack flow and cleanup, 409/413/422, planted-symlink refusal; real-container import and a hostile `../` archive |
| `tests/test_keystore.py` | Key store on SQLite and DynamoDB (moto): hash-only storage, immediate revocation, tenant isolation, limits, last-key guard |
| `tests/test_audit.py` | Audit log on SQLite and DynamoDB (moto): newest-first order, tenant isolation, pagination, command cap, expired entries hidden |
| `tests/test_accounts.py` | Accounts, invites and usage counters on SQLite and DynamoDB (moto), including concurrent increments |
| `tests/test_limits.py` | Limits config, token bucket with a fake clock, session and running-command limits |
| `tests/test_sdk.py` | The SDK against the real API over HTTP: lifecycle, results, files, every error type, retries with `Retry-After`, agent tools, repo import |
| `tests/test_mcp.py` | The MCP server through a real MCP client (in memory and stdio): tools, lazy session and cleanup, errors, expired-session recovery, reset, import |
| `tests/test_sandbox_docker.py` | Real containers: network blocked, OOM, timeout, read-only root, `/tmp` limit, non-root, persistence, symlinks; egress through the proxy; disk quota |
| `tests/test_sandbox_paths.py` | Traversal, absolute paths, prefix-sibling dirs, symlinks to host files and dirs |
| `tests/test_workspace_pool.py` | Slot claiming, wiping, capacity, stale-claim reset, disk usage, quota on writes |
| `tests/test_egress_proxy.py` | Host rules, presets, private-IP checks, signed passes, and the live proxy on local sockets |
| `tests/test_agent_loop.py` | Agent loop with a scripted fake model: tool errors, bad JSON, truncation, iteration limit |
| `tests/test_runner.py`, `tests/test_tracer_parse.py` | The learning modules: runner and `strace -c` parsing |
| `sdk/python/tests/` | Offline SDK checks against the built wheel on Python 3.9 and 3.13 |
| `sdk/typescript/test/` | TypeScript SDK: offline tests with a fake server (lifecycle, errors, retries, tools, CommonJS) and a compile check of the public types, on Node 18 and 22 |
| `tests/test_sdk_typescript.py` | The built TypeScript SDK against the real API over HTTP |
| `tests/test_mcp_remote.py` | The remote MCP endpoint through a real MCP client over HTTP: tools, one sandbox per key and workspace, auth, tenant isolation, egress from the URL, audit |

Symlink tests need Linux, or Developer Mode on Windows; they skip otherwise.

## Releasing the SDK

Bump `__version__` in `sdk/python/src/airlock_sandbox/__init__.py`, add an entry to
`sdk/python/CHANGELOG.md`, then push a tag `sdk-v<version>`; `.github/workflows/release-sdk.yml`
builds and publishes to PyPI with trusted publishing (no stored token). Running that workflow
manually publishes to TestPyPI. One-time setup: on pypi.org and test.pypi.org, add a pending
publisher for project `airlock-sandbox`, owner `farvez`, repository `agent-sandbox`, workflow
`release-sdk.yml`, environment `pypi` / `testpypi`.

**TypeScript SDK:** bump `version` in `sdk/typescript/package.json` and `src/version.ts` (the
build refuses a mismatch), add a `CHANGELOG.md` entry, then push a tag `sdk-ts-v<version>`;
`.github/workflows/release-sdk-ts.yml` tests and publishes to npm with trusted publishing. The
very first version is published by hand (`cd sdk/typescript && npm login && npm publish`),
because npm only lets you set up a trusted publisher for a package that exists.
