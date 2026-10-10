# REST API reference

All `/v1` routes require the `X-API-Key` header. Each key belongs to a tenant, and a
session can only be used by the tenant that created it. Any `/v1` call can return `429`
when the tenant hits a limit (see [limits](#per-tenant-limits)). Most developers use the
[Python SDK](../sdk/python/README.md) instead of calling these directly.

| Method | Path | Body / query | Result |
|--------|------|--------------|--------|
| GET | `/healthz` | — | `{status, active_sessions}` |
| POST | `/v1/sessions` | `{template?, metadata?, egress?}` | `201 {session_id, tenant_id, egress, disk_quota_mb, created_at, status}` · `400` template not allowed · `403` egress outside policy · `429` session limit · `503` no free workspace |
| GET | `/v1/sessions` | — | `{tenant_id, sessions: [{session_id, egress, created_at, last_accessed_at, expires_at, disk_quota_mb}]}` — this tenant's open sessions |
| POST | `/v1/sessions/{id}/write` | `{path, content}` | `{status, message}` · `403` on traversal · `413` over disk quota |
| GET | `/v1/sessions/{id}/read` | `?path=` | `{path, content}` · `403` on traversal |
| POST | `/v1/sessions/{id}/exec` | `{command, timeout_seconds (1–60)}` | `{command, stdout, stderr, exit_code, timed_out, oom_killed, warnings, output}` · `429` too many commands running |
| POST | `/v1/sessions/{id}/import` | `{repo, ref?, path?}` | `{repo, ref, path, files, archive_bytes}` — GitHub repo unpacked into `/workspace/<path>`; public, or private through the tenant's connected GitHub App installation · `400` bad name · `404` not found/private · `409` folder exists · `413` too large · `422` unpack failed |
| DELETE | `/v1/sessions/{id}` | — | `{status: "terminated"}` |
| GET | `/v1/sessions/{id}/egress` | `?limit=` | `{session_id, events: [...]}` — the session's egress log |
| GET | `/v1/egress/policy` | — | `{tenant_id, allowed}` — hosts this tenant may request |
| GET | `/v1/keys` | — | this tenant's keys (no secrets) |
| POST | `/v1/keys` | `{name?}` | `201` new key incl. `api_key` (shown once) · `409` 10 active keys |
| DELETE | `/v1/keys/{key_id}` | — | revoked key · `409` last active key · `404` not this tenant's |
| GET/POST | `/v1/admin/keys` | `?tenant=` / `{tenant, name?}` | admin key only: list all / issue for any tenant |
| DELETE | `/v1/admin/keys/{key_id}` | — | admin key only: revoke any key |
| GET | `/v1/usage` | — | `{tenant_id, limits, sessions_open, commands_running, requests_available}` |
| GET/POST/DELETE | `/mcp` | MCP Streamable HTTP; key in `X-API-Key` or `Authorization: Bearer`; `?egress=pypi`, `?workspace=name` | Remote MCP endpoint: the same tools as the local MCP server plus `activity`; one sandbox per key, egress and workspace · `401` bad key · `429` rate limit |
| GET | `/v1/audit` | `?limit=` (≤500) `&before=` | `{tenant_id, entries: [{ts, session_id, actor, kind, command, exit_code, duration_s, timed_out, oom_killed, detail}], next, retention_days}` — this tenant's command history, newest first; pass `next` as `before` for older entries · `501` when disabled |

`exec` output is plain text with `[STDOUT]:`, `[STDERR]:`, `[WARNING]:` (OOM kill or over disk quota),
`[TIMEOUT]:` and a final `[EXIT CODE]: n` line. Allowed templates: `sandbox-base:latest`, `python:3.11-slim`.

## Self-service API keys

Keys can be issued and revoked at runtime — no redeploy. They look like
`asb_<key_id>_<secret>`, are shown **once** when created, and only their SHA-256 is stored
(DynamoDB on the server, `SANDBOX_KEYSTORE=dynamodb:<table>`; SQLite for local development,
`sqlite:<path>`). Revocation takes effect immediately. Static keys from configuration
(`SANDBOX_API_KEYS`) keep working alongside them. Developers usually manage keys in the
[console](console.md).

| Who | Can | With |
|-----|-----|------|
| **Admin** (`SANDBOX_ADMIN_KEY`) | issue a key for any tenant, list all keys, revoke any key | `airlock-sandbox-keys --admin …` or `/v1/admin/keys` |
| **Tenant** (its own key) | create more keys, list and revoke its own — i.e. rotate | `airlock-sandbox-keys …`, `/v1/keys` or the console |

```bash
# admin: onboard a developer (prints the key once)
airlock-sandbox-keys --admin create --tenant acme --name "acme onboarding"
airlock-sandbox-keys --admin list --tenant acme
airlock-sandbox-keys --admin revoke <key_id>

# developer: rotate their own key
airlock-sandbox-keys create --name laptop-2026     # with the old key in SANDBOX_API_KEY
airlock-sandbox-keys revoke <old_key_id>           # with the new key
```

Guards: at most 10 active keys per tenant; a tenant can't revoke its last active key through
the API (an admin can, and so can the tenant in the console); the admin key manages keys
only and can't run sandboxes. A new tenant comes into existence with its first key and gets
the `"*"` limits and egress until its own policy is set.

## Per-tenant limits

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

## Server configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `SANDBOX_API_KEYS` | — | Static per-tenant keys: `tenant:key,tenant:key` |
| `SANDBOX_API_KEY` | — | Single static key for a tenant named `default` (one of these or `SANDBOX_KEYSTORE` is required) |
| `SANDBOX_KEYSTORE` | unset | `dynamodb:<table>` or `sqlite:<path>`: enables self-service keys |
| `SANDBOX_ADMIN_KEY` | unset | Enables the admin API (≥ 32 chars) |
| `SANDBOX_SESSION_TTL` | `1800` | Idle seconds before a session is reaped |
| `SANDBOX_REQUIRE_GVISOR` | unset | `1` = fail session creation if `runsc` is missing |
| `SANDBOX_TENANT_LIMITS` | built-in | JSON `{"*": {...defaults}, "tenant": {...overrides}}`; keys `max_sessions`, `requests_per_minute`, `max_concurrent_exec` |
| `SANDBOX_TENANT_LIMITS_FILE` | — | Same, read from a file |
| `SANDBOX_WORKSPACE_QUOTA_MB` | `512` | Disk quota per session workspace |
| `SANDBOX_WORKSPACE_POOL` | unset | Directory of pre-made, kernel-quota'd `slot-NNN` workspaces (set by the Terraform deploy); unset = temp dirs with the soft check only |
| `SANDBOX_EGRESS_POLICY` | — | JSON `{"tenant": ["pypi", "host"], "*": [...]}`; empty = no tenant gets internet |
| `SANDBOX_EGRESS_POLICY_FILE` | — | Same, read from a file |
| `SANDBOX_EGRESS_LOG_DIR` | system temp dir | Where proxies write `<session_id>.jsonl` |
| `SANDBOX_EGRESS_DNS` | detected | Comma-separated DNS servers for the proxies (default: host's non-loopback nameservers) |
| `SANDBOX_IMPORT_MAX_MB` | `100` | Largest GitHub archive `/import` downloads |
| `SANDBOX_GITHUB_APP_ID`, `_SLUG`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_PRIVATE_KEY` | unset | All five enable private repository import through a GitHub App |
| `SANDBOX_AUDIT` | unset | `dynamodb:<table>` (key `tenant` + sort key `sk`, TTL on `expires_at`) or `sqlite:<path>`: enables the command audit log |
| `SANDBOX_AUDIT_RETENTION_DAYS` | `90` | Days audit entries are kept |
| `SANDBOX_NOTIFY_TOPIC_ARN` | unset | SNS topic for operator notices (new invite requests) |
| `SANDBOX_ACCOUNTS` | unset | `dynamodb:<table>` or `sqlite:<path>`: console accounts, invites and usage metering |
| `SANDBOX_CONSOLE_BASE_URL`, `SANDBOX_GITHUB_CLIENT_ID`, `SANDBOX_GITHUB_CLIENT_SECRET`, `SANDBOX_CONSOLE_SECRET` | unset | All four enable the [console](console.md) |
| `SANDBOX_CONSOLE_ADMINS`, `SANDBOX_CONSOLE_SIGNUP` | —, `invite` | Console admins (GitHub logins) and sign-up mode |
