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
  egress for this. Also available as `POST /v1/sessions/{id}/import`, `sbx.import_repo()` /
  `sbx.importRepo()` and the MCP `import_repo` tool.
- **Private repositories:** with the GitHub App configured, **Workspaces → Connect GitHub**
  installs the app on the user's account or organisation for the repositories they pick
  (Contents: read-only). On the way back, the one-time code is exchanged for a user token only
  to check that the GitHub user who installed it is the signed-in console user; the token is
  then dropped and the installation IDs are stored for the tenant. Importing tries the public
  download first; on 404 it asks GitHub for an installation token **limited to that one
  repository, read-only, valid for an hour**, downloads through the API, and drops the token.
  Only installations the tenant connected, on the repository's owner, are ever used.
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

## Privacy and terms

`/console/privacy` and `/console/terms` are public pages, linked from every page footer and the
sign-in page. The privacy page lists exactly what the service stores and for how long (activity
90 days, egress log 30 days, server logs 14 days, workspace files until the session closes), so
update it if those change. Set `console_contact` (an email address) in `terraform.tfvars` to show
a contact; without one the pages point to GitHub issues.

## Setup: GitHub App (private repositories, optional)

1. GitHub → Settings → Developer settings → **GitHub Apps → New GitHub App**:
   - **GitHub App name:** e.g. `Airlock Sandbox` (must be unique on GitHub); its URL name is the slug.
   - **Homepage URL:** `<server>/console`
   - **Callback URL:** `<server>/console/github/callback` (`terraform output github_app_callback_url`)
   - ☑ **Expire user authorization tokens** and ☑ **Request user authorization (OAuth) during installation**
   - **Webhook:** untick **Active** (not used).
   - **Repository permissions → Contents: Read-only** (Metadata: read-only is added automatically). Nothing else.
   - **Where can this GitHub App be installed?** Any account.
2. After creating it: note the **App ID**, the **Client ID** and the slug (from
   `github.com/apps/<slug>`), **Generate a new client secret**, and **Generate a private key**
   (a `.pem` file download). Save the `.pem` in `terraform/` (git-ignores `*.pem`).
3. In `terraform.tfvars`: `github_app_id`, `github_app_slug`, `github_app_client_id`,
   `github_app_client_secret`, `github_app_private_key_file = "<file>.pem"`, then
   `scripts/deploy.sh` (it replaces the server once, since the boot script changes).

## Setup

Create a GitHub OAuth app (Settings → Developer settings → OAuth Apps → New) with
homepage `<server>/console` and callback `<server>/console/auth/callback`
(`terraform output github_oauth_callback_url`), then set `github_oauth_client_id` and
`github_oauth_client_secret` in `terraform.tfvars` and `terraform apply`. See [deploy.md](deploy.md).
