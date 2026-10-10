# 8. One private workspace per account

Status: Accepted

## Context

Accounts were first added as roles (owner/admin/viewer) in one shared library.
The intended use is different: several people on one server, each recording with
their own Plaud device and account, each seeing only their own recordings. The
expensive parts — GPU speech models, LLM providers, remote workers — should stay
shared and owner-managed.

Every content query in the codebase (about 250 ORM statements across the API,
pipeline, poller, Ask index and AutoFlow) was written for one library. Scoping each
by hand would leave a leak behind every query someone forgets, now or later.

## Decision

- A `workspaces` table; each account owns exactly one. The library that predates
  workspaces is workspace 1, owned by the owner account. Roles become `owner`
  (system administration) and `member`.
- Every content model mixes in `WorkspaceOwned` (`workspace_id`, default 1 for
  migrated rows). Children carry the column too, so a query on a child table is
  scoped without trusting its parent lookup. Shared configuration (provider
  connections, models, execution profiles, remote workers, backups) and account
  tables stay global. A test fails if a new model is neither workspace-owned nor
  deliberately listed as system data.
- `db/tenancy.py` enforces isolation once, in the ORM:
  - a `do_orm_execute` hook adds `with_loader_criteria(workspace_id = active)` to
    every ORM SELECT, UPDATE and DELETE, including relationship loads and
    `Session.get`. The workspace id is a bound parameter, so cached compiled
    statements never carry another workspace's value;
  - a `before_flush` hook stamps new rows with the active workspace and refuses
    rows placed in, or moved to, a different one.
- The active workspace is a context variable. The account gate sets it for each
  request; public share links use the issuing workspace. Code that never sets one
  reads and writes workspace 1, so a forgotten path can miss another account's
  rows but cannot read them. Cross-workspace loops (daemon scheduling, restart
  recovery, administration counts) use `system_scope()` explicitly and enter each
  workspace before doing work.
- Pipeline entry points take a recording id and run in that recording's workspace
  (`scoped_to_file`), whether called by the daemon or a request thread. Threads a
  request starts for non-recording work are bound to the caller's scope.
- Per-workspace `kv` rows use a `wsN:` key prefix; workspace 1 keeps bare keys.
- Recording ids stay globally unique: workspace 1 keeps Plaud ids, others use
  `wN-<plaud id>`, translated by a workspace Plaud client. Existing audio paths,
  share links and citations do not move.
- Each workspace connects its own Plaud account through a pasted-redirect PKCE
  flow (the official client only allows a loopback redirect). Tokens for
  workspaces other than 1 live in private files beside the library, mirroring how
  workspace 1's token cache already works.
- The daemon polls every connected workspace and rotates which workspace starts
  each processing batch, so one large library cannot starve another. Concurrency
  limits remain global because the hardware is shared.

## Consequences

- New routes and queries are isolated by default; reviewers still check Core/raw
  SQL, file paths and new threads, which the ORM hook cannot see.
- The owner cannot browse a member's recordings in the Web App. The owner still
  operates the host and its database, so this is product isolation, not protection
  from the server operator.
- The cross-recording voice identity service runs only for workspace 1, because
  its voice profile belongs to the owner.
- CLI commands act on workspace 1 unless a future option selects another.
