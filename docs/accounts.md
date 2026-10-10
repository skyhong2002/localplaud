# Web App accounts

localplaud supports Google OpenID Connect and local username/email + password
login. Every account owns one private workspace: its own Plaud connection,
recordings, transcripts, notes, templates, folders and tags, vocabulary, Ask
history and index, AutoFlow rules, integrations, notifications and preferences.
No account can see another account's workspace, including the owner. Speech, AI
provider, remote-worker and backup configuration is shared and managed by the
owner. See [ADR 0008](adr/0008-workspace-isolation.md) for how isolation is enforced.

## Enable accounts

Back up the database and private environment before upgrading. Set in private `.env`:

```dotenv
LOCALPLAUD_API__ACCOUNTS_ENABLED=true
LOCALPLAUD_API__OWNER_USERNAME=sky
LOCALPLAUD_API__OWNER_EMAIL=owner@example.com
LOCALPLAUD_API__PUBLIC_URL=https://plaud.example.com
LOCALPLAUD_API__GOOGLE_REDIRECT_URI=https://plaud.example.com/auth/google/callback
LOCALPLAUD_API__GOOGLE_CLIENT_ID=your-client-id
LOCALPLAUD_API__GOOGLE_CLIENT_SECRET=your-client-secret
LOCALPLAUD_API__SESSION_SECRET=generate-a-long-random-secret
LOCALPLAUD_API__SESSION_COOKIE_SECURE=true
```

Register the exact HTTPS redirect URI on the Google Web application OAuth client.
Request only `openid email profile`; Gmail, Drive and Plaud permissions are not
needed. Configure the Google consent audience for the people intended to sign in.
Keep secrets out of source control and logs. A configured public URL is the trusted
origin for browser form submissions behind the reverse proxy.

The first verified Google login matching `owner_email` creates the protected owner
with `owner_username`. Public registration cannot claim the reserved username or
email. Existing owners are not replaced by later changes to the configuration.
Google identities are bound by provider subject, not a mutable email address.
Bootstrap and account linking require a Gmail address or a verified Workspace
identity whose signed `hd` claim matches the email domain. A third-party Google
email alone is not accepted as proof of current mailbox ownership.

After Google login, use **My account / 我的帳號** to set a local password. This
allows future login without Google. Existing local accounts remain usable when
Google is unavailable; first-time owner bootstrap requires Google. Disabling Google
configuration before the owner has a local password removes their ordinary login
method. Local operator recovery below works once the owner exists.

Active accounts use the recording workspace's sidebar, appearance, and shared
controls on account and user-management pages. Pending accounts use the same design
tokens without loading private workspace metadata or navigation. Account pages use
full document navigation to initialize their forms; older tabs requesting a partial
receive `HX-Redirect`. If an older tab is already blank, reload it once.

Account mode rejects the old shared password and all shared API-token access,
including Bearer, `X-Auth-Token`, and `?token=`. Old sessions without a user are
invalid. Remote workers retain their separate authenticated worker protocol;
Plaud OAuth and provider API keys are unchanged. Check external API clients before
cutover: general machine API credentials are not supported. The completion
notification watcher has a separate, metadata-only credential described in
`docs/completion-watcher.md`; it cannot access other API or browser routes.

## Account lifecycle and permissions

- Anyone may create a local account or sign in with Google. New accounts are pending
  and can only view their own account, change their password, manage their own
  sessions and sign out. They have no workspace yet.
- The owner approves (activates), disables, or re-enables accounts from
  **帳號管理**. Activation creates the account's empty workspace, named after the
  username, with the owner's locale, timezone and the built-in note templates.
  Disabling revokes sessions immediately and keeps the workspace intact.
- The owner can also create an account directly (**帳號管理 → 新增帳號**) with a
  username, email and initial password. It is active immediately with its own
  empty workspace; share the password privately and ask the user to change it.
  Each creation records a `user_created` audit event.
- Roles are `owner` (system administration plus workspace 1, which holds the
  library that predates accounts) and `member`. Members have full control of their
  own workspace. System administration is owner-only: account management,
  provider connections and models, execution-profile edits, remote workers,
  backups, storage retention and diagnostics. Members may read the shared
  execution-profile catalog to choose a profile for their recordings.
- The owner sees account names, status and each workspace's recording count, never
  its contents.
- Local-registration email addresses are not verified by an email service in this
  release. The owner must confirm the applicant before approval; the displayed
  address alone does not prove identity. Google email claims are verified.
- Existing public share links remain deliberately public to their holders. A link
  is served from the workspace that issued it. Pending or disabled status is not
  intended to invalidate independently issued public links.

## Plaud connection per workspace

Each workspace connects its own Plaud account under **設定 → Plaud 帳號 → 連結 Plaud
帳號**. The official Open API client only accepts a loopback redirect, so the Web
flow is: Plaud opens in a new tab, the member authorizes, the tab lands on an
unreachable `http://localhost:8199/auth/callback?...` address, and the member
pastes that full address back. localplaud checks the single-use, 15-minute state
and exchanges the code with its stored PKCE verifier. One Plaud account can back
only one workspace.

Workspace 1 keeps the configured token cache (`plaud.official.tokens_path`, shared
with `localplaud auth login`). Other workspaces store tokens in
`<download_dir>/../plaud-connections/workspace-N.json` with mode `0600`; include
that directory in host backups and keep it private. The first successful listing of
a newly connected account is a metadata-only baseline, exactly like a new
deployment: earlier recordings appear without downloading their audio, and
recordings uploaded afterwards are downloaded and processed automatically. A
member can import older audio on demand. Disconnecting stops synchronization and
keeps every recording already in the workspace. Workspace 1 keeps the host's
configured transport (Open API or MCP); other workspaces always use the official
read-only Open API, so they can connect even when the host uses MCP.

Recording ids in workspace 1 are Plaud's own ids, so audio paths, share links and
citations from before workspaces are unchanged. Other workspaces use `wN-<plaud id>`
locally; the Plaud client translates ids at its boundary.

Account mode uses opaque revocable sessions with peppered token hashes, HttpOnly /
Secure / SameSite=Lax cookies, Argon2id password hashes, same-origin checks on unsafe
browser requests, atomic login-rate counters, and single-use expiring OIDC state
bound to the initiating browser. Google tokens are validated for signature, issuer,
audience, expiry and nonce. OAuth authorization uses PKCE. No Google access/refresh
tokens are retained as account credentials.

Authenticated responses are not cacheable; account-mode HTMX history persistence
is disabled and authentication pages clear the former workspace history cache.
OAuth callback access logs are filtered by the bundled server; reverse proxies must
also avoid recording authorization codes and state in callback query strings.

To connect an existing local account to Google, sign in locally, open My account,
and choose **Connect Google / 連結 Google 帳號** using the same email. The app does
not automatically merge accounts with matching emails. A recent Google login can
be used to set/reset a local password. Password changes revoke other sessions.

## Recovery

Run on the trusted deployment host, with its normal private configuration:

```bash
localplaud recover-owner-password
localplaud recover-account-password USERNAME
```

Both prompt for a new password without echoing it or placing it in command history.
Recovery revokes that user's sessions and records an audit event. It does not approve
pending users, grant roles, or change recording content. There is no emailed reset
link or MFA enrollment in this release. Normal approval and session management are
available in the Web App without CLI access.

## Deployment and validation

Pause new automatic work using the durable workspace preference and wait for active
processing to finish before restarting. Use SQLite's online backup API. The schema
migration adds account tables and nullable session ownership/authentication fields,
then the `workspaces` table, a `workspace_id` column (default 1) on every
workspace-owned table, and per-workspace uniqueness for vocabulary, note templates
and external AutoFlow rules. Former `admin` and `viewer` accounts become `member`
accounts with their own new workspace on next sign-in. Original recordings,
artifacts, revisions and pipeline provenance are preserved in workspace 1.
Restore the previous automatic-processing preference after the cutover.

Verify local registration/login, Google redirect and callback, pending access denial,
cross-workspace isolation, owner-only administration, owner protection, revocation,
local password recovery and desktop/mobile account screens. `tests/test_workspace_*`
covers ORM isolation, every recording route against another workspace's id, and
per-workspace Plaud connection and polling. Tests use isolated databases and mocked
Google replies with real signed-token verification. A successful redirect alone does
not prove a live Google login completed; verify the latter with the account holder.

For rollback, restore the previous application/environment and restart without
accounts enabled. Additive database columns can remain; do not restore a stale full
database over recordings or edits made after the backup. New account-bound sessions
must be invalidated before reverting to a legacy shared-login deployment.
