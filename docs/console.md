# Developer console

`<server>/console` (e.g. `https://airlock.complyoo.com/console`): developers sign in with
GitHub, get a tenant (`gh-<login>`) automatically, create, rotate and revoke API keys, see
their usage this month (commands, sandbox time, sessions), limits and internet access, and
copy quickstart snippets (Python SDK, Claude Code MCP, curl) with the server URL filled in.
The code is in `src/console/` (server-rendered Jinja templates, no build step).

## Workspaces

Start a sandbox session from the browser, optionally importing a public GitHub repository
into it, and run commands in a browser terminal — `pip install`, `python -m pytest`,
anything bash can do within the sandbox limits. Each line runs as its own command on the
session's persistent workspace; the terminal carries the working directory between lines
(`cd` works), environment variables and background processes don't persist. Sessions are
shared with the API: ones started with an API key appear here and vice versa, and terminal
commands count toward usage like API calls.

- **Repo import:** the server downloads `github.com/<owner>/<repo>/archive/<ref>.tar.gz`
  (redirects only to github.com/codeload.github.com, capped at `SANDBOX_IMPORT_MAX_MB`, default
  100) and writes it into the workspace as one new file (`O_EXCL|O_NOFOLLOW`, so nothing the
  sandbox planted is followed). It is unpacked **inside the sandbox** by the unprivileged
  sandbox user, so hostile archives (`../` paths, symlinks, huge files) stay contained and the
  disk quota applies; an archive tar refuses leaves nothing behind. The session needs no
  egress for this. Public repositories only for now. Also available as
  `POST /v1/sessions/{id}/import`, `sbx.import_repo()` and the MCP `import_repo` tool.
- **Terminal safety:** commands go through the same exec path as the API (gVisor, limits,
  metering); output is inserted as text, never HTML; requests carry the CSRF token in a header.

## Sign-in and access

- **Sign-in:** GitHub OAuth (`read:user` only; the GitHub token is not kept). Sessions are
  HttpOnly, Secure, SameSite=Lax signed cookies; every form carries a CSRF token; pages send
  a strict Content-Security-Policy.
- **Access:** `console_signup = "invite"` (default): admins (`console_admins`) and invited
  GitHub users only — admins invite people from the console's **Admin** page. `"open"` lets
  anyone with GitHub sign in.
- **Invite requests:** someone who signs in with GitHub without an invite can request one
  (optional use case and contact). Requests are tied to the verified GitHub account (a signed,
  30-minute cookie carries it), one per account, at most 500 waiting. Admins see a count on the
  **Admin** tab and approve or dismiss each one; approving invites that username. Each new
  request also emails the operator (the alert email, via `SANDBOX_NOTIFY_TOPIC_ARN`); the
  console sends nothing to the requester, so tell people when they're in.
- **Removing access:** **Remove** on a user (Admin → Users) revokes all their API keys, closes
  their open sandboxes, ends their console sign-in immediately (every request checks the
  account still exists) and blocks the GitHub username from signing in or requesting again.
  Usage history and the audit log are kept. **Unblock** lets them request access again.
  Console admins can't be removed from the page.
- **Activity:** every command run in a tenant's sandboxes — API, SDK, MCP, browser terminal and
  repo imports — with time, session, who ran it (the API key's id or `console @login`), exit
  code, duration, timeout/out-of-memory, newest first. Commands only, never output; kept for
  `audit_retention_days` (90). Admins can open any tenant's activity from the Users table.
  Commands are stored as typed, so a secret pasted into a command line is kept too; write
  secrets to a file (`write_file`) instead of putting them in commands.
- **Usage metering:** every session and command is counted per tenant per month
  (`SANDBOX_ACCOUNTS`, the same table as accounts and invites) — the basis for free-plan
  quotas and paid plans later.
- **Default egress:** an `"*"` entry in `egress_policy` applies to every tenant not listed, so
  console sign-ups can `pip install` (`"*" = ["pypi"]`).

## Setup

Create a GitHub OAuth app (Settings → Developer settings → OAuth Apps → New) with
homepage `<server>/console` and callback `<server>/console/auth/callback`
(`terraform output github_oauth_callback_url`), then set `github_oauth_client_id` and
`github_oauth_client_secret` in `terraform.tfvars` and `terraform apply`. See [deploy.md](deploy.md).
