# Security policy

Airlock exists to run untrusted code safely, so security reports are very welcome.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/farvez/agent-sandbox/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You can expect an
acknowledgement within 3 days and a fix or mitigation plan within 14 days for confirmed
issues. Please give us a chance to fix it before disclosing publicly; we're happy to credit
you in the advisory.

## In scope

- Escaping the sandbox: reaching the host, another tenant's session, or files outside the workspace
- Bypassing the egress allowlist, or reaching private/metadata addresses from a sandbox
- Authentication or tenant-isolation flaws in the API or the developer console (keys, sessions, CSRF, OAuth)
- Getting around per-tenant limits or disk quotas in ways that affect other tenants
- Vulnerabilities in the `airlock-sandbox` SDK or MCP server

Please test only against your own deployment (see [docs/deploy.md](docs/deploy.md)), not the
hosted preview at airlock.complyoo.com, and don't run denial-of-service tests against it.

## Supported versions

Fixes go into the `main` branch and the latest `airlock-sandbox` release on PyPI.

## Design

The threat model and the controls for each threat are documented in
[docs/architecture.md](docs/architecture.md#security-model).
