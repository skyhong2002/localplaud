# Scoped processing recovery

For an operator-requested backlog repair, run:

```sh
.venv/bin/python scripts/maintenance/repair_processing.py /private/path/queue.json
```

Run from the repository directory so the normal configuration and API credentials
are loaded. The manifest contains `recordings`, each with an `id` and initial
`outcome: "pending"`. It is private operational data; never commit it. Back up
the database before an explicit model migration.

The runner serially submits the normal authenticated recording recovery action,
adopts existing processing claims, persists progress atomically, and uses a file
lock to prevent duplicate runners. It waits for the selected Codex provider's
quota reserve instead of changing providers or disabling reserve protection.
Speech recovery can run once during that wait. Three submissions per recording
are allowed; persistent failures are marked `needs_attention` in the manifest,
not falsely completed in the library.

For an explicitly authorized model migration, set each item's `target_asr_model`.
An existing local transcript from another model triggers `force=true`; subsequent
attempts resume once the new raw transcript exists. The normal pipeline preserves
human revisions. Old generated notes remain until successful replacements.
Optional manifest `required_stages` maps stage names to exact `connection` and
`model` selections. If those change before submission, recovery stops for that
recording instead of using another model. Select the intended execution profile
through the provider service before starting this runner.

`counts`, per-item `observed`, and `provider_status` report progress. A runner exit
does not imply all recordings succeeded: inspect `needs_attention` and the normal
stage errors. An OS service can keep the runner alive across terminal closure;
its logs and manifest must stay outside version control.

The speech image installs Qwen forced alignment's Korean (`soynlp`) and Japanese
(`nagisa`) tokenizers. Build-time smoke checks execute both tokenizers, because
importing the Transformers auto classes alone does not detect these missing
language dependencies. This does not force a language or change ASR models.

When all recorded speech stages have completed (or were explicitly skipped),
recovery uses the local transcript to regenerate notes, the mind map, and indexes
through `/generate-notes`. It does not fetch evicted audio or rerun ASR. The normal
automatic queue also recognizes this state without requiring a prior manual
notes request. Failed or degraded speech stages still require speech recovery;
Plaud-imported transcripts do not qualify. Backoff, retry caps, and active claims
continue to apply.

Evidence-note repair reviews receive the preceding review's outstanding issues.
They check those fixes and substantive regressions against the source, with the
same quality threshold as the initial review. They retain bounded attempts and
never accept an unresolved substantive issue just because retries ran out.
Accepted extraction and draft checkpoints remain reusable across retries.

New recordings take priority over maintenance. Download batches are newest-first;
the processing queue never reserves a retry slot ahead of an eligible new download.
Before starting each queued retry it checks for newly arrived downloads again.
The maintenance runner waits while eligible new audio or another active processing
claim exists, without consuming a repair attempt. Already running processing is
not preempted; new audio gets the next available dispatch opportunity.
