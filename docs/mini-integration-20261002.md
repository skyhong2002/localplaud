# Mini integration and rollout, 2026-10-02

## Scope and source ownership

The integration branch starts with a reviewed checkpoint of the production Mini's
dirty source. Steam commits and saved worktree patches were restored separately
before merging. Private conversations, reference screenshots, configuration,
credentials, database and audio are outside Git. Production main was not reset.

The integrated UI includes the shared shell, mobile Sources/Notes workspace,
persistent player, Ask streaming and cancellation, Templates, Settings and
AutoFlow editing. Follow-up fixes address HTMX handler lifetime, hours in playback
timestamps, repeated titles, readable rule names, actionable unindexed Ask,
duplicate player IDs, and cancellable speaker-merge confirmation.

Chapter outlines use corrected local transcript input, timestamped full coverage,
append-only revisions, explicit provider selection and narrow durable retries.
Transcript text or speaker-assignment edits refresh their stale state; name-only
edits preserve freshness and project bound names. Out-of-order responses
cannot replace newer status. See [chapter-outline.md](chapter-outline.md).
Automatic chapters remain disabled by default. Topic quality with a real selected
model has not been benchmarked. Model-free time sections were generated and played
in the synthetic library; they are not labelled as AI topic analysis.

The restored service-worker experiment is intentionally excluded: it cached
private HTML/API responses without adequate account/locale/update isolation.
An installable shell does not imply offline access to recordings.

## Verification and limits

All tests use the Mini virtualenv with `PYTHONPATH` set to the integration
worktree's `src`, isolated temporary databases and a dedicated pytest basetemp.
The final full suite passed 1,377 tests (six existing dependency deprecation
warnings) in 215.81 seconds. Ruff and JavaScript syntax checks passed.
Executable Node regressions cover HTMX lifecycle, parked-player
identity, outline response races and speaker-merge cancellation.

Synthetic-library browser checks covered desktop and mobile navigation, persistent
playback, share-sheet content/focus, Templates create/edit, persisted AutoFlow
ordering, locale, and chapter generation/seek. Ask unavailable/index-empty paths
were real backend responses; partial-stream/Stop/error tests used a browser-local
mock stream and did not call a model. Built-in imported template authorship remains
truthful rather than relabelled as original localplaud work.

After the operator authorized Mini Chrome, actual 1440×900, 820×1180 and 390×844
viewports were verified. Library captures cover both languages and both themes at
every size; additional pages cover long content, empty, loading, degraded and error
states. Sixty-three fresh Chrome CDP captures supersede transient frames returned
by a screenshot wrapper, and twelve follow-up captures verify final fixes. No
page-level horizontal overflow or captured console errors were found. Reference
images, synthetic content and browser evidence stay private.

Chrome caught a real playback regression: returning to a still-playing recording
restored an older sessionStorage position. Commit `50e1138c` preserves the live
element's position unless an explicit timestamp is requested. A fresh repeat kept
the same node playing from 65.38 through 80.26 seconds over A → library → B → A;
an explicit 3789-second link still sought to 1:03:09 and showed hour-formatted
transcript timestamps. Commit `a2951183` fixes readable AutoFlow history names and
Chinese Ask shortcuts; historical rule-name precedence also has regression coverage.

### Completed speaker naming workflow

Anonymous speakers use stable Speaker 1/2 aliases in generation and friendly
localized transcript labels. Name speakers submits a batch atomically. Existing
bound local note paragraphs, tables, titles and mind maps update without a model
call or ASR rerun; exports read the same stored text. Validated spans associate
mentions with stable speaker keys, so repeated rename and clearing names work.
Each change archives the original note and provenance. User notes, manual titles,
raw transcripts, assignments, cloud migration artifacts and history are preserved.
Automatic voice-name application and undo use the same transactional projection.

Name-only changes queue indexing while preserving derivative freshness. Notes
already stale remain readable with their stale badge; they are not silently made
current. Ambiguous old personal-name prose remains unchanged with a persistent
review notice. It is not safe to infer attribution or globally replace a person's
name in arbitrary historical text. Unknown binding versions fail closed.

Actual Mini Chrome verified 1440×900 English/light batch naming with uninterrupted
playback, 820×1180 Chinese/dark rename, clear and history inspection, and 390×844
Chinese/light notes, tables, stale-state persistence and mind maps. A browser-tool
timeout in the delegated run was recovered in a fresh root-owned Chrome session;
the tablet/mobile checks were completed, not left blocked. Final captures show no
horizontal page overflow or captured console errors. Exports contain current names.
Browser-discovered map heading omissions and escaped-name display were fixed and
reverified; renaming preserves map zoom and collapsed branches. The final full
suite ran against immutable code commit `dae3ca05`, followed only by documentation.
Synthetic fixtures validate UI behavior without calling a model; production model
attribution quality is not established by these checks. Private evidence remains
outside Git. This integrated UI is committed but **not deployed**.

Scoped production pipeline repairs are separate from this UI release. Orphan
stage recovery and recent reindex processing were deployed and health checked;
historical quota failures were not bulk regenerated. Alignment provenance must
survive validation of reused ASR, and completion counts alone do not establish
independence. Two recordings passed the 13-check artifact gate after guarded
alignment-provenance recovery; this does not test live Ask generation, retrieval
ranking or answer quality. One further derived-only repair completed without
changing speech artifacts or user edits; its artifact gate is 12/13 solely because
the completed recording's raw-audio cache was evicted. The remaining recording is
summarizing (audit 9/36 at 15:52 Taipei, one active claim/attempt); the installed
alignment source patch needs an idle process reload. Local health returned 200.
Voice identity remains anonymous below the configured confidence
threshold or without adequate samples. Launchd enablement was inspected; an actual
machine reboot and the earlier claimed five-hour monitoring period were not proven.

## Finish sprint additions

`integration/finish-20261002` continues from the speaker-naming release with five
separately developed branches: voice-similarity name suggestions, Settings
(resolution preview, storage and backup retention, remote-worker runtime detail,
opt-in cloud/remote starting profiles), shell (sidebar tags, AutoFlow next-run
wording, template attribution), per-recording speech settings with workspace QA
follow-ups, and backend policy (quality floor, remote outline dispatch, speech
benchmark harness). An independent review of the merged diff found that
quality-floor metadata would have made fallback-produced artifacts look stale
after upgrade; profiles without a floor now resolve exactly as before and the
verdict is excluded from reuse comparisons. Retention never removes the newest
media archive and re-validates stored limits.

Schema changes are two additive nullable/defaulted columns
(`plaud_files.speech_overrides`, `execution_profiles.quality_floor`); the previous
release can read the database after rollback. Upgrade the controller before the
remote worker: a previous-release controller cannot parse the new `outline`
capability, while a new controller works with the current worker (runtime detail
shows "not reported", speech overrides are marked unconfirmed, outline dispatch
fails explicitly). Deployed 2026-10-02 18:24 Taipei from regenerated patches (all 131 live preimages
and postimages matched). Dry run on a production-data copy: no startup tracebacks,
core pages 200, 0 of 8,666 artifact-reuse predicates changed, identical summary
profile digest, and the rollback release loaded every stage row and inserted after
migration. Production restart took 93 seconds; user-owned table hashes were identical
before and after. Note checkpoints written before the release are not reused,
because the evidence-note system prompt changed.

## Cutover gates

1. Obtain the operator's explicit approval of the final tested UI diff. A commit or a green test suite does not
   authorize deployment.
2. Wait for active recording claims, running attempts and designated recovery
   processes to finish. Do not interrupt a long correction or summary. Pause
   new work for the short cutover window, preserving launchd enablement/settings.
3. Capture a fresh private source/config/launchd manifest and an **online SQLite
   backup**. Preserve current audio, user edits, enrollment and all artifacts.
   The initial preservation snapshot is not a substitute for this fresh backup.
4. Verify each touched source file against the reviewed production baseline and
   run `git apply --check` on the prepared source-only patch. Stop on drift and
   reconcile it; do not checkout/reset production or copy an old database.
5. Apply the reviewed patch, initialize the additive schema through normal startup,
   and restart only at the verified idle boundary. Production currently needs
   approximately seven minutes to initialize its large database; allow for that
   observed downtime rather than treating a slow start as an immediate failure.
6. Verify local and public health, authenticated library/workspace, current stage
   state, provider/profile selection and artifact/user-edit hashes. Re-enable
   normal processing only after those checks. Observe new events and record the
   actual observation period; do not claim unattended historical monitoring.

## Rollback without losing edits

The prepared `rollback/mini-20261002` branch keeps the prior UI and scoped pipeline
repairs. Its compatibility commit `f9b2c087` retains the `outline` stage enum so the
old ORM can read stage/attempt ledgers produced by the new release. A read-only
check against the populated synthetic database loaded 245 stage rows and 243
attempts, including the new outline row. Plain reversal to an old enum would fail.

At an idle boundary, compare live source against the deployed manifest, preserve
any subsequent source edits, and apply the reviewed source rollback diff. Keep the
additive outline table, all stage/attempt history, the current database and audio.
Do not restore an older database over newer user edits. Restart and repeat health
and artifact checks. Chapter generation is unavailable in the old UI but its data
is preserved for a later forward deployment. Fresh pipeline fixes added after the
rollback branch was prepared must also be retained before using that branch.
