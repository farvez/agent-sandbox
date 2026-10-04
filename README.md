# Airlock

[![CI](https://github.com/farvez/agent-sandbox/actions/workflows/ci.yml/badge.svg)](https://github.com/farvez/agent-sandbox/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/airlock-sandbox)](https://pypi.org/project/airlock-sandbox/)
[![Python](https://img.shields.io/pypi/pyversions/airlock-sandbox)](https://pypi.org/project/airlock-sandbox/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Isolated, network-controlled sandboxes for AI coding agents.** Every command an agent runs
executes in a throwaway gVisor container with no network by default, hard memory/CPU/process
limits, a read-only root filesystem and a disk quota — while files persist across commands in
the session's workspace. Use it from Python, from Claude Code (MCP), over REST, or in the
browser.

- **Safe by default:** gVisor user-space kernel, no Linux capabilities, non-root, no network
- **Internet only where you allow it:** per-tenant HTTPS allowlists (e.g. PyPI), every connection logged
- **Bring your code:** import a public GitHub repository into a sandbox and run its tests
- **Multi-tenant:** API keys per tenant, rate and session limits, usage metering
- **Self-hostable:** one `terraform apply` deploys it to AWS with TLS, monitoring and alarms

## Try it

Sign in at **[airlock.complyoo.com/console](https://airlock.complyoo.com/console)** (invite-only
preview) to create an API key, or open a workspace and run commands in the browser.

```bash
pip install airlock-sandbox
```

```python
from airlock_sandbox import Sandbox

with Sandbox(base_url="https://airlock.complyoo.com", api_key="<key>", egress=["pypi"]) as sbx:
    sbx.import_repo("pallets/itsdangerous")
    sbx.run("pip install pytest freezegun", timeout=60).check()
    print(sbx.run("cd itsdangerous && PYTHONPATH=src python -m pytest -q", timeout=60).stdout)
```

**Claude Code** — give Claude a sandbox to run code in:

```bash
claude mcp add airlock --scope user -e SANDBOX_API_URL=https://airlock.complyoo.com -e SANDBOX_API_KEY=<key> -e AIRLOCK_EGRESS=pypi -- uvx --from "airlock-sandbox[mcp]" airlock-sandbox-mcp
```

More in the [SDK guide](sdk/python/README.md): typed errors and retries, ready-made agent tools
for OpenAI and Anthropic, the MCP server, and key management.

## How it works

```
 your agent ──► SDK / MCP / REST ──► API (key → tenant, limits) ──► one container per command
                                                                      gVisor · no network · 256 MB · 1 CPU
                                                                      read-only root · /workspace (quota)
                                                                           │ optional, per tenant
                                                                           ▼
                                                                egress proxy ──► pypi.org (allowed, logged)
```

Each command starts a fresh container on the session's persistent `/workspace`; processes
don't survive between commands, files do. Sessions expire after 30 idle minutes and their
workspace is wiped. Details: [architecture and security model](docs/architecture.md).

## Documentation

| Guide | |
|-------|---|
| [SDK, CLI and MCP server](sdk/python/README.md) | Using Airlock from Python, Claude Code and other MCP clients |
| [Architecture and security](docs/architecture.md) | Container hardening, threat model, egress gateway |
| [REST API](docs/api.md) | Endpoints, API keys, per-tenant limits, server configuration |
| [Developer console](docs/console.md) | GitHub sign-in, keys, workspaces, repo import, browser terminal |
| [Deploy to AWS](docs/deploy.md) | Terraform setup, TLS, operations, monitoring and alarms |
| [Development](docs/development.md) | Local setup, project layout, demos, evals, tests, releases |

## Roadmap

- Console: private repositories (GitHub App), a full PTY terminal, free-plan quotas from metering
- Shared state and a persistent session store for more than one API server
- Remote (HTTP) MCP endpoint, so clients connect with just a URL and key
- JavaScript/TypeScript SDK
- Egress: human approval for new hosts, per-tenant bandwidth limits

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
