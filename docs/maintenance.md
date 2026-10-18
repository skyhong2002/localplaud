# Operator maintenance and planned restart

Maintenance is an explicit local operator action. Normal Web use requires no CLI,
and the Web App exposes no maintenance control endpoint. Without a request file,
admission and provider/profile policies keep their existing defaults.

## Begin, drain, and explicitly resume

Run from the deployment working directory with its configured environment. The
control directory must equal `poller.download_dir.parent / ".maintenance"`; for
the standard `data/audio` layout it is `data/.maintenance`.

```sh
.venv/bin/python -m localplaud.maintenance --control-dir data/.maintenance begin
```

Keep the printed `request_id`. Begin creates private durable local state and does
not signal/restart a service, query recordings, or invoke a provider. Existing
accepted work finishes. New HTTP work returns 503 with Retry-After; `/healthz`
continues returning its ordinary public health response. Authenticated remote-job
status, artifact download, and cancellation remain available. Their authentication
and workspace rules are unchanged. There are no remote heartbeat, completion, or
upload-result callbacks in protocol v1: the controller polls and downloads results.
The daemon keeps its owner heartbeat alive while new queue items are fenced.

New daemon recordings, downloads, independent transcript/note index items, and
voice passes pause before execution. An admitted recording finishes its enabled
stages, and accepted HTTP work can hand off its detached workers without losing
its activity count. ASGI streaming/background lifetime, waveform/readiness futures,
imports, eager reindex, Ask workers, and voice child lifetime are counted. The
main command waits for detached work before releasing its daemon owner.

Read both **current loaded** PIDs from `launchctl print gui/UID/LABEL` (or your
supervisor). Do not copy PIDs from an earlier run. For the standard two processes:

```sh
.venv/bin/python -m localplaud.maintenance --control-dir data/.maintenance status \
  --main-pid CURRENT_MAIN_PID --voice-pid CURRENT_VOICE_PID
```

Exit 0 plus `quiescent: true` means both private receipts match this request,
current PID/birth, role, serving state, active count zero, and a timestamp no older
than two seconds. Exit 2 means work remains or a receipt is stale/mismatched;
missing, corrupt, oversized, unsafe, or unreadable state never authorizes a stop.
This CLI performs no service action. Keep the request present throughout planned
stop, source update, and restart. Check identities and receipts again immediately
before acting; an explicit concurrent resume invalidates this precondition.

The receipts cover the main process and voice watcher using this code. Separate
CLI/backfill processes, another worker host, and unmanaged processes are outside
these counts and must be independently excluded or drained before changing shared
source/runtime files. Existing host-wide concurrency, cost reserves, model scope,
and workspace isolation still apply.

Resume is always explicit, including after an aborted maintenance or crash:

```sh
.venv/bin/python -m localplaud.maintenance --control-dir data/.maintenance resume \
  --request-id THE_REQUEST_ID_FROM_BEGIN
```

A mismatched nonce preserves the request. Resume is also a safe abort while work
is still draining: admitted work continues and new admission reopens. A durable
resume receipt prevents a short pause/resume from being missed between reporter
polls. Main and standalone API runtimes redispatch only remote jobs explicitly
marked as deferred by maintenance, including after restart. With automatic
processing disabled, unrelated historical remote jobs stay queued. Queued inputs
are retained; cancellation and terminal-job input cleanup still work during pause.

## Failure and durability limits

The request survives process death. A restarted service remains paused until
explicit resume; it does not silently reopen admission. Existing owner-epoch and
expired-lease recovery retain completed artifacts and recover interrupted stages.
Interrupted imports keep their existing explicit retry behavior. Maintenance does
not create a new durable payload queue for manual HTTP requests: a rejected request
must be retried, and a crash before accepted force/profile choices reach stage
setup can still require resubmission. It does not guarantee exactly-once remote
provider billing or cancellation.

A configured supervisor timeout does not prove the loaded timeout. On the audited
Mac on 2026-10-18, both plist files specified 300 seconds while `launchctl print`
still reported 60 seconds. Beginning maintenance and waiting while the service is
running avoids spending that stop grace on long model work. Sending bootout/stop
while work is active is still unsafe. `ExitTimeOut=0` is infinity and can stall
system shutdown; it is not an admission/drain mechanism. `launchctl disable` alone
was not a validated KeepAlive respawn fence and must not be used as one.

## First deployment from code without this gate

Old running interpreters do not observe the new request. Do not begin maintenance
and infer that an old service has drained, and do not update all modules underneath
an interpreter that can still lazily import them. A first rollout needs separate
proof that old detached API work has completed plus an ingress fence, a verified
new-process interlock for **both** KeepAlive jobs, and fresh process identities.
A database count of zero or a C-level process sample alone is insufficient proof
that no unclaimed detached Python thread remains.

A direct cooperative SIGINT can drain an old process without starting launchd's
stop escalation, but it requires a separately verified respawn interlock and old
API-worker completion evidence. No generic first-rollout automation is shipped
here. If those preconditions cannot be established, leave the old service running
and defer the switch. The ordinary commands above apply after the maintenance
implementation is actually running.

## Verification

Use `tests/run_runtime_offline.sh` with the deployment dependency interpreter.
The maintenance tests include a real pipeline driven only by fake providers for
longer than 60 seconds, durable pending recording retention, crash during summary
with completed ASR reused on resume, ASGI/detached handoff races, watcher children,
and authenticated remote completion/poll/cancel plus natural explicit-resume
recovery with automatic processing disabled. These checks do not invoke real
models/providers or establish that an old production interpreter is idle.
