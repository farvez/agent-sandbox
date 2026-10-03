# agent-sandbox

[![CI](https://github.com/farvez/agent-sandbox/actions/workflows/ci.yml/badge.svg)](https://github.com/farvez/agent-sandbox/actions/workflows/ci.yml)

An isolated execution substrate for autonomous AI coding agents. An LLM agent gets
three tools (`read_file`, `write_file`, `run_command`) and every command it issues runs
inside a throwaway container with no network, hard memory/CPU/PID limits, no Linux
capabilities, a read-only root filesystem and a non-root user — under gVisor's user-space kernel when the host has
it. The same sandbox is exposed as a REST API, deployable to AWS with Terraform, and
measured by an evaluation harness that checks both *did the agent fix the bug* and
*did the sandbox hold*.

The project is built up in steps, each a self-contained module you can run on its own:

| Step | Module | What it adds |
|------|--------|--------------|
| 1 | `src/step1_runner/runner.py` | Host subprocess runner with hard timeout and stream capture |
| 2 | `src/step2_container/container_runner.py` | Ephemeral Docker container per command: `network_mode=none`, 256 MB RAM (no swap), 1 CPU, 64 PIDs, OOM detection |
| 3 | `src/step3_tracing/tracer.py` | Runs the command under `strace -f -c` and returns per-syscall counts (file / process / network) |
| 4 | `src/step4_gvisor/gvisor_runner.py` | Detects gVisor (`runsc`) and uses its user-space kernel; falls back to `runc` |
| 5 | `src/step5_agent/` | `SandboxedWorkspace` (hardened container + path-safe file I/O) and an OpenAI tool-calling agent that runs an inspect → patch → verify loop |
| API | `src/api/server.py` | FastAPI gateway: per-tenant API keys, sessions, file read/write, exec, TTL reaper, image allowlist |
| Evals | `evals/`, `run_evals.py` | 3 functional bug-fix tasks + 1 adversarial exfiltration task, scored with hidden tests |
| Deploy | `terraform/` | EC2 host with Docker + gVisor, Caddy TLS, generated tenant keys in SSM, code bundle from S3 |

## Architecture

```
            ┌──────────────────────────────── host ───────────────────────────────┐
 client ─TLS─► Caddy :443 ─► FastAPI 127.0.0.1:8000 (key → tenant, image allowlist)   │
            │                   └─ session registry (in-memory, thread lock, TTL)  │
 LLM  ◄──►  │ AutonomousCodingAgent ── tools ──► SandboxedWorkspace               │
            │                                     │  temp dir, realpath-checked    │
            │                                     ▼                               │
            │   ┌────── container (one per command, removed afterwards) ──────┐   │
            │   │ runtime: runsc (gVisor) if present, else runc               │   │
            │   │ network none · mem 256m · swap 0 · cpu 1 · pids 64          │   │
            │   │ cap_drop ALL · no-new-privileges · non-root uid             │   │
            │   │ read-only rootfs · /tmp tmpfs 64m (noexec)                  │   │
            │   │ /workspace bind-mounted rw                                  │   │
            │   └─────────────────────────────────────────────────────────────┘   │
            └─────────────────────────────────────────────────────────────────────┘
```

Files persist across commands through the bind-mounted workspace directory;
processes do not — every `run_command` starts a fresh container.

### Security model

| Threat | Control |
|--------|---------|
| Data exfiltration / calling home | `network_mode=none` by default; with egress enabled, HTTPS only to the tenant-approved hosts through the egress gateway (below) |
| Cloud-credential theft / SSRF to internal services | No network by default; the egress gateway resolves DNS itself and refuses private, loopback and link-local (metadata) addresses |
| Fork bombs, memory or CPU exhaustion | `pids_limit=64`, `mem_limit=memswap_limit=256m`, 1 CPU quota, OOM kill reported to the caller |
| Runaway commands | Per-command timeout (1–60 s via API); container is killed and removed |
| Filling the disk | Per-workspace quota (default 512 MB): kernel-enforced ext4 project quota on the server, on a size-capped filesystem separate from the main disk; checked by the workspace everywhere (warning after a command, `413` on further writes) |
| Privilege escalation inside the container | `cap_drop=ALL`, `no-new-privileges`, non-root user |
| Tampering with the image / persisting outside the workspace | Read-only root filesystem; only `/workspace` and a 64 MB `noexec` `/tmp` are writable (`HOME=/tmp`) |
| Kernel exploits | gVisor `runsc` intercepts syscalls in user space; set `SANDBOX_REQUIRE_GVISOR=1` to refuse to run without it (on by default in the Terraform deploy) |
| `../` traversal or symlinks planted by sandboxed code | Host-side paths are resolved with `realpath` and must stay inside the workspace (`commonpath`) |
| Arbitrary images | `template` must be in `ALLOWED_TEMPLATES` |
| One tenant reaching another's sessions | Each API key maps to a tenant; sessions are bound to the tenant that created them (403 otherwise) |
| One tenant starving the others | Per-tenant limits (429): concurrent sessions (default 10), requests per minute (default 120, with `Retry-After`), concurrent running commands (default 4); the slot pool caps all sessions together |
| Key guessing / leakage | Keys required at startup, constant-time comparison against every key, generated by Terraform and stored in SSM |
| Agent derailed by a tool failure or huge output | Tool errors are returned to the model as text; output is cut to 8,000 characters, keeping head and tail |

Known limits: limits and sessions are tracked in memory by one API process (not shared
across servers), keys live in an environment variable rather than a database, egress
logs are not rotated.

## Requirements

- Python 3.10+
- Docker (Docker Desktop on Windows/macOS, or Docker Engine on Linux)
- An OpenAI API key for the agent, demo and evals (default model `gpt-4o-mini`)
- Optional: gVisor `runsc` registered as a Docker runtime (Linux only)
- For deployment: Terraform ≥ 1.5 and AWS credentials

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

Build the sandbox base image every runner expects:

```bash
docker build -t sandbox-base:latest .
```

Create a `.env` file in the project root (it is git-ignored):

```
OPENAI_API_KEY=sk-...
SANDBOX_API_KEY=<output of: openssl rand -hex 32>
# or, for several tenants:
# SANDBOX_API_KEYS=acme:<key>,globex:<key>
```

## Usage

Run each isolation step on its own:

```bash
python -m src.step1_runner.runner
python -m src.step2_container.container_runner   # network block + OOM kill checks
python -m src.step3_tracing.tracer                # syscall counts
python -m src.step4_gvisor.gvisor_runner          # reports runsc vs runc fallback
```

Self-healing agent demo (plants a broken Fibonacci, lets the agent fix it):

```bash
python demo.py
```

Evaluation harness:

```bash
python run_evals.py
```

REST API (the server refuses to start without at least one key):

```bash
uvicorn src.api.server:app --host 127.0.0.1 --port 8000
python test_api.py        # end-to-end smoke test; reads SANDBOX_API_KEY and SANDBOX_API_URL
```

### Python SDK

`sdk/python` is the client developers install — `pip install airlock-sandbox`
(standard library only, Python 3.9+). Full guide: [sdk/python/README.md](sdk/python/README.md).

```python
from airlock_sandbox import Sandbox
from airlock_sandbox.tools import openai_tools, anthropic_tools, handle_tool_call

with Sandbox(base_url="https://<host>", api_key="<key>", egress=["pypi"]) as sbx:
    sbx.run("pip install requests", timeout=60).check()
    sbx.files.write("main.py", "import requests; print(requests.__version__)")
    result = sbx.run("python3 main.py")
    print(result.stdout, result.exit_code, result.ok)
```

- `run()` returns a `CommandResult` (`stdout`, `stderr`, `exit_code`, `ok`, `timed_out`,
  `oom_killed`, `warnings`, `check()`); `str(result)` is the text form for an LLM.
- Typed errors (`RateLimitError`, `QuotaExceededError`, `PermissionDeniedError`, …); 429 and
  503 are retried automatically, honouring `Retry-After`.
- `openai_tools()` / `anthropic_tools()` + `handle_tool_call()` give an agent
  `write_file` / `read_file` / `run_command` in one line; errors come back as text.
- A `Sandbox` also has `read_file` / `write_file` / `run_command`, so the repo's
  `AutonomousCodingAgent` runs against a hosted sandbox unchanged.
- Reads `SANDBOX_API_URL`, `SANDBOX_API_KEY` and `SANDBOX_API_INSECURE=1` when arguments
  are omitted. For development: `pip install -e ./sdk/python` (included in `requirements-dev.txt`).

**Releasing:** bump `__version__` in `sdk/python/src/airlock_sandbox/__init__.py`, then push a
tag `sdk-v<version>`; `.github/workflows/release-sdk.yml` builds and publishes to PyPI with
trusted publishing (no stored token). Running that workflow manually publishes to TestPyPI.
One-time setup: on pypi.org and test.pypi.org, add a pending publisher for project
`airlock-sandbox`, owner `farvez`, repository `agent-sandbox`, workflow `release-sdk.yml`,
environment `pypi` / `testpypi`.

### MCP server

`pip install "airlock-sandbox[mcp]"` adds `airlock-sandbox-mcp`, an MCP server (stdio) that
gives Claude Code, Claude Desktop or any MCP client a sandbox as tools: `run_command`,
`write_file`, `read_file`, `list_files`, `egress_log`, `sandbox_info`, `reset_sandbox`.

```bash
claude mcp add airlock --scope user -e SANDBOX_API_URL=https://<server> -e SANDBOX_API_KEY=<key> -e AIRLOCK_EGRESS=pypi -- airlock-sandbox-mcp
```

One session per server process, created on first use and deleted on disconnect. MCP
clients kill servers about 2 s after disconnecting, so the session ID is also recorded in
the user's local state directory and anything a killed run left behind is deleted on the
next start. Setup for Claude Desktop: [sdk/python/README.md](sdk/python/README.md#mcp-server-claude-code-claude-desktop-other-mcp-clients).

### Live demo

`demo_live.py` runs four acts against a deployed server: hello world, an LLM agent fixing
a failing test suite through the hosted sandbox, a red-team table of 13 attacks
(exfiltration, cloud-credential theft, memory and fork bombs, disk filling, session hogging, infinite loop, system
tampering, secret hunting, kernel probing, path traversal, symlink escape, guessed key),
and the egress gateway (`pip install` allowed, other sites and the metadata service
refused, then the session's egress log). Act 4 needs `pypi` in the tenant's egress policy.

**Running it** (Windows PowerShell, from the repo root, after `terraform apply`):

```powershell
.venv\Scripts\activate
. .\scripts\demo-env.ps1          # sets SANDBOX_API_URL / _KEY / _INSECURE from Terraform + SSM
python demo_live.py               # all four acts; Act 2 uses OPENAI_API_KEY from .env
python demo_live.py --skip-agent  # no OpenAI calls
python demo_live.py --act 3       # one act only
```

The leading `. ` matters: it keeps the variables in your window. Use `-Tenant acme` for
another tenant. The firewall only admits `allowed_ingress_cidrs`; if your home IP
changes, re-run `terraform apply` with the new one or requests will time out.

Under gVisor a fork bomb is stopped by the 256 MB memory limit after roughly 10–20
processes (the sandbox exits with code 2); the host and other sessions keep running.

### Egress gateway

Sandboxes have no network unless a session asks for it. A session can request HTTPS access
to specific hosts, limited to what its tenant's policy allows, and every connection
attempt is logged:

```
sandbox (private network, no route out) ──► egress proxy ──► pypi.org           allowed + logged
          HTTPS_PROXY set automatically        checks pass  ──► example.com        403 + logged
                                                            ──► 169.254.169.254   403 + logged
```

- **Policy per tenant:** `SANDBOX_EGRESS_POLICY='{"acme": ["pypi", "api.openai.com"]}'`
  (or a JSON file via `SANDBOX_EGRESS_POLICY_FILE`). Tenants not listed get no internet.
  Rules are exact hosts or `*.example.com` (subdomains only); presets: `pypi`, `npm`,
  `github`, `huggingface`.
- **Per session:** `POST /v1/sessions {"egress": ["pypi"]}` must be covered by the policy
  (403 otherwise). Leave it empty and the session has no network at all.
- **Isolation:** each such session gets its own `internal` Docker network and its own
  proxy container (same hardening as sandboxes, under gVisor on the server). Sessions
  can't reach each other or share a proxy, and code that ignores the proxy has no route out.
  Both of the proxy's networks are attached before it starts, because gVisor never sees
  networks connected to a running container. For the same reason Docker's embedded DNS
  (127.0.0.11) doesn't work under gVisor, so on Linux each proxy gets a `resolv.conf`
  pointing at the host's upstream DNS (override with `SANDBOX_EGRESS_DNS`).
- **Signed passes:** the session's proxy credentials are an HMAC-signed pass naming the
  session, tenant, allowed hosts and expiry, signed with a key unique to that session's
  proxy — a pass from one session is refused (407) by every other session's proxy.
- **What the proxy enforces:** HTTPS tunnels (`CONNECT`) to port 443 only; host on the
  pass; DNS resolved by the proxy with private/reserved addresses refused, then connects
  to the vetted IP. TLS is not decrypted.
- **Audit log:** one JSON line per connection (session, tenant, host, decision, reason,
  bytes, duration) in `SANDBOX_EGRESS_LOG_DIR/<session_id>.jsonl`, kept after the session
  ends; per session via `GET /v1/sessions/{id}/egress`.
- **Cost:** each internet-enabled session runs one extra small container (128 MB cap) and
  takes about a second longer to create.
- `pip install` works: packages go to `/workspace/.local` (persistent, on `sys.path`).

```python
with Sandbox(egress=["pypi"]) as sbx:
    sbx.run("pip install requests", timeout=60).check()
    print(sbx.egress_log())
```

### Self-service API keys

Keys can be issued and revoked at runtime — no redeploy. They look like
`asb_<key_id>_<secret>`, are shown **once** when created, and only their SHA-256 is stored
(DynamoDB on the server, `SANDBOX_KEYSTORE=dynamodb:<table>`; SQLite for local development,
`sqlite:<path>`). Revocation takes effect immediately. Static keys from configuration
(`SANDBOX_API_KEYS`) keep working alongside them.

| Who | Can | With |
|-----|-----|------|
| **Admin** (`SANDBOX_ADMIN_KEY`) | issue a key for any tenant, list all keys, revoke any key | `airlock-sandbox-keys --admin …` or `/v1/admin/keys` |
| **Tenant** (its own key) | create more keys, list and revoke its own — i.e. rotate | `airlock-sandbox-keys …` or `/v1/keys` |

```bash
# admin: onboard a developer (prints the key once)
airlock-sandbox-keys --admin create --tenant acme --name "acme onboarding"
airlock-sandbox-keys --admin list --tenant acme
airlock-sandbox-keys --admin revoke <key_id>

# developer: rotate their own key
airlock-sandbox-keys create --name laptop-2026     # with the old key in SANDBOX_API_KEY
airlock-sandbox-keys revoke <old_key_id>           # with the new key
```

Guards: at most 10 active keys per tenant; a tenant can't revoke its last active key (an
admin can); the admin key manages keys only and can't run sandboxes. A new tenant comes into
existence with its first key and gets the `"*"` limits and no egress until its policy is set.

### Per-tenant limits

| Limit | Default | When exceeded |
|-------|---------|---------------|
| `max_sessions` — sessions open at once | 10 | `POST /v1/sessions` → 429 until one is deleted or expires |
| `requests_per_minute` — all `/v1` calls (token bucket: bursts allowed, refills continuously) | 120 | 429 with `Retry-After: <seconds>` |
| `max_concurrent_exec` — commands running at once | 4 | `exec` → 429 until one finishes |

```bash
SANDBOX_TENANT_LIMITS='{"*": {"max_sessions": 10}, "acme": {"max_sessions": 50, "requests_per_minute": 600}}'
```

`"*"` sets the defaults for every tenant; a tenant entry overrides individual fields.
Invalid keys or values stop the server at startup. `GET /v1/usage` (or `sbx.usage()` in
the client) shows a tenant its limits and current usage; client errors carry
`retry_after` for 429s.

### API reference

All `/v1` routes require the `X-API-Key` header. Each key belongs to a tenant, and a
session can only be used by the tenant that created it. Any `/v1` call can return `429`
when the tenant hits a limit (see **Per-tenant limits**).

| Method | Path | Body / query | Result |
|--------|------|--------------|--------|
| GET | `/healthz` | — | `{status, active_sessions}` |
| POST | `/v1/sessions` | `{template?, metadata?, egress?}` | `201 {session_id, tenant_id, egress, disk_quota_mb, created_at, status}` · `400` template not allowed · `403` egress outside policy · `429` session limit · `503` no free workspace |
| POST | `/v1/sessions/{id}/write` | `{path, content}` | `{status, message}` · `403` on traversal · `413` over disk quota |
| GET | `/v1/sessions/{id}/read` | `?path=` | `{path, content}` · `403` on traversal |
| POST | `/v1/sessions/{id}/exec` | `{command, timeout_seconds (1–60)}` | `{command, stdout, stderr, exit_code, timed_out, oom_killed, warnings, output}` · `429` too many commands running |
| DELETE | `/v1/sessions/{id}` | — | `{status: "terminated"}` |
| GET | `/v1/sessions/{id}/egress` | `?limit=` | `{session_id, events: [...]}` — the session's egress log |
| GET | `/v1/egress/policy` | — | `{tenant_id, allowed}` — hosts this tenant may request |
| GET | `/v1/keys` | — | this tenant's keys (no secrets) |
| POST | `/v1/keys` | `{name?}` | `201` new key incl. `api_key` (shown once) · `409` 10 active keys |
| DELETE | `/v1/keys/{key_id}` | — | revoked key · `409` last active key · `404` not this tenant's |
| GET/POST | `/v1/admin/keys` | `?tenant=` / `{tenant, name?}` | admin key only: list all / issue for any tenant |
| DELETE | `/v1/admin/keys/{key_id}` | — | admin key only: revoke any key |
| GET | `/v1/usage` | — | `{tenant_id, limits, sessions_open, commands_running, requests_available}` |

`exec` output is plain text with `[STDOUT]:`, `[STDERR]:`, `[WARNING]:` (OOM kill or over disk quota),
`[TIMEOUT]:` and a final `[EXIT CODE]: n` line.

| Env var | Default | Meaning |
|---------|---------|---------|
| `SANDBOX_API_KEYS` | — | Per-tenant keys: `tenant:key,tenant:key` |
| `SANDBOX_API_KEY` | — | Single key for a tenant named `default` (at least one of the two is required) |
| `SANDBOX_SESSION_TTL` | `1800` | Idle seconds before a session is reaped |
| `SANDBOX_REQUIRE_GVISOR` | unset | `1` = fail session creation if `runsc` is missing |
| `SANDBOX_KEYSTORE` | unset | `dynamodb:<table>` or `sqlite:<path>`: enables self-service keys |
| `SANDBOX_ADMIN_KEY` | unset | Enables the admin API (≥ 32 chars) |
| `SANDBOX_TENANT_LIMITS` | built-in | JSON `{"*": {...defaults}, "tenant": {...overrides}}`; keys `max_sessions`, `requests_per_minute`, `max_concurrent_exec` |
| `SANDBOX_TENANT_LIMITS_FILE` | — | Same, read from a file |
| `SANDBOX_WORKSPACE_QUOTA_MB` | `512` | Disk quota per session workspace |
| `SANDBOX_WORKSPACE_POOL` | unset | Directory of pre-made, kernel-quota'd `slot-NNN` workspaces (set by the Terraform deploy); unset = temp dirs with the soft check only |
| `SANDBOX_EGRESS_POLICY` | — | JSON `{"tenant": ["pypi", "host"]}`; empty = no tenant gets internet |
| `SANDBOX_EGRESS_POLICY_FILE` | — | Same, read from a file |
| `SANDBOX_EGRESS_LOG_DIR` | system temp dir | Where proxies write `<session_id>.jsonl` |
| `SANDBOX_EGRESS_DNS` | detected | Comma-separated DNS servers for the proxies (default: host's non-loopback nameservers) |

Allowed templates: `sandbox-base:latest`, `python:3.11-slim`.

## Tests

CI (GitHub Actions, `.github/workflows/ci.yml`) runs on every push and pull request:
the full pytest suite on Linux with real containers — including the egress and
symlink-escape tests that skip on Windows; the SDK wheel built, checked with `twine`,
installed and tested on Python 3.9 and 3.13; plus `terraform fmt`, `validate`, and a
render + `bash -n` of the boot script.

```bash
pytest                 # everything; Docker tests skip if Docker or the image is missing
pytest -m "not docker" # fast unit tests only
```

| File | Covers |
|------|--------|
| `tests/test_runner.py` | Step 1 runner: output, exit codes, timeout, missing binary |
| `tests/test_sandbox_paths.py` | Traversal, absolute paths, prefix-sibling dirs, symlinks to host files and dirs |
| `tests/test_tracer_parse.py` | `strace -c` parsing with and without the errors column |
| `tests/test_keystore.py` | Key store on SQLite and DynamoDB (moto): issue/authenticate, hash-only storage, immediate revocation through the cache, tenant isolation, limits, last-key guard |
| `tests/test_limits.py` | Limits config (defaults, overrides, validation), token bucket with a fake clock, session and running-command limits, usage report |
| `tests/test_sdk.py` | The SDK against the real API over HTTP: lifecycle, structured results, files, every error type, retries on 429/503 with `Retry-After`, agent tools in both formats, legacy text parsing, use with `AutonomousCodingAgent` |
| `tests/test_mcp.py` | The MCP server through a real MCP client (in memory and as a real stdio process): tool list and hints, lazy session and cleanup on disconnect, tool errors, expired-session recovery, reset, info and egress log, sessions left by a killed run deleted on next start |
| `sdk/python/tests/` | Offline SDK checks run against the built wheel on Python 3.9 and 3.13 in CI |
| `tests/test_workspace_pool.py` | Slot claiming, wiping, capacity, stale-claim reset after restart, disk usage measurement, quota on API writes |
| `tests/test_egress_proxy.py` | Host rules, wildcards, presets, private-IP checks, signed passes (forgery, expiry), and the live proxy on local sockets: tunnel, 403/405/407 cases, logging |
| `tests/test_api_server.py` | Auth, tenant key parsing, cross-tenant isolation, template allowlist, egress policy and log endpoints, per-tenant 429s (sessions, rate with `Retry-After`, concurrent exec), usage endpoint, slots freed on delete/expiry/failed start, lifecycle, slow exec not blocking other requests |
| `tests/test_agent_loop.py` | Agent loop with a scripted fake model: tool errors, bad JSON, missing arguments, output truncation, iteration limit |
| `tests/test_sandbox_docker.py` | Real containers: network blocked, OOM detected, timeout, read-only root, `/tmp` size limit, non-root, persistence, container-made symlinks; egress: `pip install` through the proxy (needs internet), refused hosts, metadata, bypass, per-session networks, cleanup; disk quota warning and write refusal |

Symlink tests need Linux, or Developer Mode on Windows; they skip otherwise.

## Deploy to AWS

`terraform/` provisions a VPC, one EC2 instance (Ubuntu 22.04, c7i-flex.large by default — 2 vCPU / 4 GB, eligible on the AWS Free plan),
a private S3 bucket holding the code bundle, one generated API key per tenant (stored
together as an SSM SecureString), and an instance role. At boot the host installs Docker, gVisor from its signed apt repository, AWS CLI
and Caddy; builds `sandbox-base:latest` from this repo's Dockerfile; and runs the API
as a non-root `sandbox` user behind Caddy on port 443, with `SANDBOX_REQUIRE_GVISOR=1`.

**First time only — state bucket.** Terraform state contains the API keys, so it lives in an
encrypted, versioned S3 bucket with native locking, not on a laptop:

```bash
cd terraform/bootstrap
terraform init && terraform apply                      # creates agent-sandbox-tfstate-<account-id>
terraform output -raw backend_config > ../backend.hcl  # git-ignored
```

**Deploy:**

```bash
cd terraform
terraform init -backend-config=backend.hcl
terraform apply -var 'allowed_ingress_cidrs=["<your-ip>/32"]' -var 'tenants=["default","acme"]' \
  -var 'egress_policy={acme=["pypi"]}' -var 'alert_email=you@example.com'
$(terraform output -raw fetch_api_keys_command)   # prints tenant:key pairs
```

On a new machine, `terraform init -backend-config=backend.hcl` is all it takes to pick up the
existing state. Old state versions are kept for 90 days in the bucket.

**Monitoring.** Every minute the host checks `/healthz` through Caddy and TLS and reports
`ApiHealthy`, `ActiveSessions`, `RootDiskUsedPercent` and `WorkspacesDiskUsedPercent` to
CloudWatch (namespace `AgentSandbox`, dimension `Service=agent-sandbox`), and deletes egress
logs older than 30 days. Alarms email `alert_email` (confirm AWS's subscription email first):

| Alarm | Fires when |
|-------|-----------|
| `agent-sandbox-api-down` | health check failing or no data for 3 min (also during the ~8 min of a redeploy) |
| `agent-sandbox-root-disk-80pct` | main disk over 80% |
| `agent-sandbox-workspaces-disk-80pct` | workspace filesystem over 80% |
| `agent-sandbox-status-check-failed` | AWS instance status check failing |
| `agent-sandbox-cpu-high` | CPU over 90% for 15 min |

A monthly cost budget (`monthly_budget_usd`, default $30) emails at 80% of actual spend and
when the month's forecast exceeds 100%.

- **Settings file:** put your variables in `terraform/terraform.tfvars` (git-ignored) so every
  `plan` / `apply` / `destroy` uses the same values — forgetting `domain_name` on one apply
  would switch the server back to a self-signed certificate.
- **TLS:** set `-var domain_name=sandbox.example.com` (DNS A record → instance IP) for a
  Let's Encrypt certificate; without it Caddy serves a self-signed certificate on the IP
  (`curl -k`).
- **Shell access:** no SSH. Use `aws ssm start-session --target <instance_id>`.
  Provisioning log: `/var/log/user_data.log`; service log: `journalctl -u agent-sandbox`.
- **Stable address:** the API sits on an Elastic IP, so its URL (and the self-signed
  certificate's IP) stays the same across redeploys; `terraform destroy` releases it.
- **Shipping code changes:** `terraform apply` re-zips `src/`, so a code change replaces
  the instance (sessions are in memory and are lost); the URL and API keys stay the same.
- **Adding a tenant:** add its name to `tenants` and `terraform apply`, then restart the
  service (`systemctl restart agent-sandbox` via SSM). Removing a name revokes its key.
- **Key rotation:** `terraform apply -replace='random_password.tenant_key["acme"]'`, then restart.
- The keys are also stored in Terraform state; keep state private.
- Only `src/**/*.py`, `Dockerfile` and `requirements.txt` are uploaded — never `.env`.
- **Disk quotas:** workspaces live on `/var/lib/agent-sandbox/workspaces`, a loop-mounted
  ext4 filesystem of `workspaces_disk_gb` (default 15 GB) with project quotas, holding
  `workspace_slots` (default 100 = max concurrent sessions) directories of
  `workspace_quota_mb` (default 512 MB) each. Check usage with `sudo repquota -P /var/lib/agent-sandbox/workspaces`.
- **Self-service keys:** a DynamoDB table (`agent-sandbox-api-keys`, deletion protection and
  point-in-time recovery on) and a generated admin key in SSM. Load it with
  `. .\scripts\demo-env.ps1 -Admin`, or print it with `terraform output -raw fetch_admin_key_command`.
- **Tenant limits:** `-var 'tenant_limits={"*"={max_sessions=10}, acme={max_sessions=50}}'`
  (written to `/etc/agent-sandbox/tenant-limits.json`). Unset = built-in defaults.
- **Egress:** `egress_policy` (default `{}`: no internet for anyone) is written to
  `/etc/agent-sandbox/egress-policy.json`; logs are in `/var/lib/agent-sandbox/egress/`.
  Docker's address pool is widened to `10.210.0.0/16` in /24s (256 concurrent egress sessions).

## Evaluation benchmarks

| Task | Type | What is checked |
|------|------|-----------------|
| `algo_reverse_words` | functional | Off-by-one slice bug; hidden tests add single-word and 4-word cases |
| `math_safe_average` | functional | Empty-list `ZeroDivisionError`; hidden test adds floats |
| `arch_report_generator` | functional | Agent must create a missing function in another module |
| `security_network_exfil` | adversarial | Agent is told to exfiltrate data; hidden test asserts outbound HTTP is blocked |

Hidden verification code is written into the workspace only after the agent finishes, so
the agent cannot edit it. Each task runs once, so treat results as a smoke test rather
than a benchmark score.

## Project layout

```
.
├── Dockerfile              # sandbox-base image: python:3.11-slim + strace, non-root user
├── demo.py                 # rich terminal demo of the self-healing agent (local Docker)
├── demo_live.py            # four-act demo against a deployed server
├── run_evals.py            # benchmark runner and results table
├── test_api.py             # live API smoke test
├── requirements.txt        # runtime deps · requirements-dev.txt adds pytest + httpx
├── docs/                   # overview.html, positioning.html
├── evals/                  # EvalTask/EvalReport schema, benchmarks, evaluator
├── src/
│   ├── step1_runner/ … step5_agent/
│   ├── egress/             # allowlisting HTTPS proxy + Docker wiring
│   └── api/server.py
├── sdk/python/             # airlock-sandbox: the Python SDK (pip package)
├── terraform/              # AWS deployment
└── tests/                  # pytest suite
```

## Roadmap

- Keys and limits in a database with self-service issuing; shared state for multiple API servers
- Persistent session store (Redis/Postgres) for more than one API worker
- Remote (HTTP) MCP endpoint on the server, so clients connect with just a URL and key
- JavaScript/TypeScript SDK
- Egress: human approval for new hosts, log rotation, per-tenant bandwidth limits
- Repeated eval runs with pass@k, and logging whether the agent attempted exfiltration

## License

MIT — see [LICENSE](LICENSE).
