# Architecture and security

## How a command runs

```
            ┌──────────────────────────────── host ───────────────────────────────┐
 client ─TLS─► Caddy :443 ─► FastAPI 127.0.0.1:8000 (key → tenant, image allowlist)   │
            │                   └─ session registry (in-memory, thread lock, TTL)  │
 LLM  ◄──►  │ AutonomousCodingAgent ── tools ──► SandboxedWorkspace               │
            │                                     │  workspace dir, realpath-checked│
            │                                     ▼                               │
            │   ┌────── container (one per command, removed afterwards) ──────┐   │
            │   │ runtime: runsc (gVisor) if present, else runc               │   │
            │   │ network none · mem 256m · swap 0 · cpu 1 · pids 64          │   │
            │   │ cap_drop ALL · no-new-privileges · non-root uid             │   │
            │   │ read-only rootfs · /tmp tmpfs 64m (noexec)                  │   │
            │   │ /workspace bind-mounted rw (disk quota)                     │   │
            │   └─────────────────────────────────────────────────────────────┘   │
            └─────────────────────────────────────────────────────────────────────┘
```

Files persist across commands through the bind-mounted workspace directory; processes do
not — every command starts a fresh container. The code is in `src/sandbox/`
(`workspace.py`, `workspace_pool.py`, `gvisor.py`); the agent loop is in `src/agent/`.

## Security model

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
| `../` traversal or symlinks planted by sandboxed code | Host-side paths are resolved with `realpath` and must stay inside the workspace (`commonpath`); repo-import archives are written with `O_EXCL\|O_NOFOLLOW` and unpacked inside the sandbox |
| Arbitrary images | `template` must be in `ALLOWED_TEMPLATES` |
| One tenant reaching another's sessions | Each API key maps to a tenant; sessions are bound to the tenant that created them (403 otherwise) |
| One tenant starving the others | Per-tenant limits (429): concurrent sessions (default 10), requests per minute (default 120, with `Retry-After`), concurrent running commands (default 4); the slot pool caps all sessions together |
| Key guessing / leakage | Constant-time comparison; self-service keys are stored only as SHA-256 hashes and revocable immediately; generated keys live in SSM |
| Agent derailed by a tool failure or huge output | Tool errors are returned to the model as text; output is cut to 8,000 characters, keeping head and tail |

Known limits: sessions and rate limits are tracked in memory by one API process (not
shared across servers); egress logs are pruned after 30 days but not rotated by size.

Found a vulnerability? See [SECURITY.md](../SECURITY.md).

## Egress gateway

Sandboxes have no network unless a session asks for it. A session can request HTTPS access
to specific hosts, limited to what its tenant's policy allows, and every connection
attempt is logged:

```
sandbox (private network, no route out) ──► egress proxy ──► pypi.org           allowed + logged
          HTTPS_PROXY set automatically        checks pass  ──► example.com        403 + logged
                                                            ──► 169.254.169.254   403 + logged
```

- **Policy per tenant:** `SANDBOX_EGRESS_POLICY='{"acme": ["pypi", "api.openai.com"]}'`
  (or a JSON file via `SANDBOX_EGRESS_POLICY_FILE`). Tenants not listed get the `"*"` entry,
  or no internet if there is none. Rules are exact hosts or `*.example.com` (subdomains
  only); presets: `pypi`, `npm`, `github`, `huggingface`.
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

Under gVisor a fork bomb is stopped by the 256 MB memory limit after roughly 10–20
processes (the sandbox exits with code 2); the host and other sessions keep running.
