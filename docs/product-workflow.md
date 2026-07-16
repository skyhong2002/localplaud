# Product workflow and Plaud Web parity

Status: Target product specification

## Outcome

localplaud replaces the day-to-day Plaud Intelligence subscription experience while
continuing to use a physical Plaud recorder and Plaud's raw-audio upload path. The
user records and syncs as usual, but does not ask Plaud to transcribe or summarize.
localplaud downloads the audio and owns every derived artifact and interaction.

This is workflow parity, not a promise to copy Plaud's code, trademarks, artwork, or
every commercial feature. The goal is that a Plaud user can move the same daily job
to localplaud without losing the important loop from capture to trusted action.

## End-to-end journey

```text
Capture              Ingest                 Extract
Plaud recorder  ->   Plaud raw upload  ->   download + verify + transcode
                                                  |
                                                  v
Utilize              Understand             Enrich
Ask/export/act  <-   notes + mind map  <-   ASR + align + diarize + correct
      |
      v
Review and improve -> edit transcript/speakers -> regenerate/re-index
```

The normal path must work when Plaud's transcript, summary, outline, and AI task
fields are absent. Imported Plaud artifacts are a separately labelled migration
feature, never an implicit fallback.

## Information architecture

### Observed Plaud Web baseline (read-only audit, 2026-07-10)

The authenticated Plaud Web surface was inspected without generating, editing,
exporting, sharing, or deleting anything. localplaud should preserve the useful
workflow concepts below while using local artifacts, original branding, and its own
interaction design:

- a persistent personal-workspace shell with Search, Home, library-wide Ask,
  Templates, Discover/Automation, folders, source filters, plan/usage state, and
  Settings;
- a recent-files home and an all-files table with name, duration, creation date,
  sortable columns, folders, uncategorized/trash views, and capture-source facets;
- an Add audio entry point with local file upload and Import from Plaud. The latter
  refreshes all recording metadata and existing Plaud transcript/summary artifacts
  without downloading raw audio; a metadata-only recording offers an explicit,
  per-file Import audio action when opened;
- a file workspace that keeps the library context available while switching between
  transcript and notes, with file-level Ask and compact share/export/more actions;
- a transcript reader with a synchronized duration/player, timestamps, speaker
  labels, an explicit polished-versus-raw transcript view, and find/replace;
- notes that identify the template used, support multiple structured sections and a
  mind map, and keep generation feedback separate from editing;
- Ask at both file and library scope, including suggested questions and versioned,
  inspectable quick actions for action-item extraction, task tables, and insight
  generation. Quick actions remain grounded and read-only until the user explicitly
  saves an answer as a note;
- a searchable template library with My Space and Explore, scenario/category
  browsing, first-party and community templates, descriptions, authorship, and
  popularity signals;
- one export menu for copy transcript/notes and export audio, transcript, notes, and
  mind map; transcript export exposes format plus timestamp and speaker-label
  switches (Plaud currently offered TXT, SRT, DOCX, and PDF in the audited dialog);
- Discover includes AutoFlow, applications, integrations, and feedback. AutoFlow
  exposes enablement, notification state, a human-readable trigger/action summary,
  and currently tells Web users when rules are view-only and must be edited in the
  mobile app;
- Settings separates account/security, workspace personalization, preferences,
  custom vocabulary, private-cloud sync, authorized applications, support, and
  product information.

This list is a workflow benchmark, not a requirement to reproduce Plaud's account,
subscription, community, or cloud-sharing implementation. localplaud must improve on
it where subscription independence, local provenance, privacy, recovery, or
accessibility require a different design.

### 1. Recordings library

- Persistent navigation for Home, Search, Recordings, Ask, Templates,
  Discover/Automation, and Settings.
- Search by title and transcript content.
- Recent-files home plus all-files, uncategorized, and trash/recovery views.
- Folders/tags, capture-source, date, duration, and processing-state filters, with
  sortable name, duration, and creation-date columns.
- Every row communicates recorded time, duration, processing state, and whether
  attention is needed.
- Bulk organization, export, reprocess, and deletion of local derivatives.
- Responsive desktop split-pane and mobile list/detail navigation.

### 2. Recording workspace

- Header: editable title, date, duration, tags/folder, processing state, and actions.
- Persistent audio player with seek, speed, skip, waveform/progress, and keyboard
  controls.
- Transcript tab: click-to-seek, active-segment tracking, timestamps, speaker colors,
  speaker rename, inline correction, search/find-replace within the recording, and a
  clearly labelled switch between raw ASR and the corrected canonical transcript.
- After word alignment and diarization, mixed-speaker ASR segments are split only
  when their timed words can reproduce the source text without loss. Consecutive
  runs from the same speaker are stored as one readable paragraph when separated by
  no more than three seconds; speaker changes, unsafe text reconstruction, and longer
  silence always start a new paragraph. Paragraphs are capped at 120 seconds or 1,200
  characters so correction, editing, and subtitle export remain bounded. Word
  timestamps and confidence remain intact.
- Notes: multiple generated or user-authored note tabs, inline editing, generation
  provenance, template/model selection, and safe regeneration.
- Mind map: navigable hierarchy generated from the canonical corrected transcript
  and notes.
- Ask: single-file chat, quick actions, follow-up prompts, timestamp citations,
  suggested questions, reusable skills/quick actions, follow-up prompts, timestamp
  citations, source excerpts, and save-to-note.
- History: visible revisions and stage failures where useful, without overwhelming
  the normal reading experience.

### 3. Ask across recordings

- Searches corrected transcripts and appropriate notes across the whole library.
- Answers must be grounded, name their recordings, and link to playable timestamps.
- Filters may scope by date, folder/tag, speaker, or selected recordings.
- Folder, tag, capture source, user-assigned speaker display name, inclusive date
  range, and a selected recording list are enforced before vector ranking. The
  speaker filter joins each chunk's recording-local stable key to its editable name;
  anonymous labels are deliberately excluded rather than conflated across files.
  The normalized boundary is
  stored on the Ask thread, shown with the answer, and cannot change during a
  follow-up; starting a different scope creates a new thread.
- A citation opens the recording at the cited moment.
- Users can continue a durable grounded thread and save any answer as an editable
  note. Saved notes retain source moments, link back to recordings, appear in the
  recording workspace, and can be copied or exported as Markdown, edited, or deleted
  independently without altering the original Ask thread.
- Suggested questions and versioned quick actions for action items, cross-recording
  task tables, and recurring insights run through this same library-scoped retrieval.
  Their scope-specific prompt snapshot is durable; execution creates only an Ask
  thread unless the user explicitly saves an answer as a note.

### 4. Templates and generation

- Auto mode chooses a sensible workflow from recording metadata/content.
- Custom mode selects language, ASR profile, speaker settings, note templates, and
  LLM per file.
- Users can create templates as structured prompts and edit them by creating a new
  immutable version. Generated notes retain the exact version and prompt snapshot;
  remote execution receives the same snapshot.
- Template management has separate personal and discovery surfaces, search,
  scenario/category browsing, first-party/community provenance, description,
  authorship, and optional popularity signals.
- Multiple templates may run for the same recording without deleting prior notes.
- Long recordings use full-coverage hierarchical summarization; silent truncation is
  forbidden.

### 5. Providers, models, and execution profiles

Provider choice is stage-scoped. ASR, alignment, diarization, transcript correction,
notes, mind maps, embeddings, and Ask may each use a different local runtime, cloud
API, or remote worker. The product must not reduce this to one global “AI model”
setting.

- Reusable execution profiles group the provider, model, device/worker, stage
  options, fallback policy, privacy boundary, and optional cost ceiling for all
  enabled stages. Starting profiles include Apple Local, NVIDIA Local, CPU/Other
  Local, OpenAI Cloud, OpenAI-compatible, and Remote GPU; users can copy and edit
  them.
- Profile resolution is deterministic: system default → folder → winning durable
  AutoFlow assignment → active template version → per-recording profile →
  per-recording stage/policy patch. Lower numeric AutoFlow priority wins, with the
  higher source rule ID breaking ties. The UI shows the structured resolved layer
  chain and can return a recording to inherited Automatic mode.
- Providers advertise capabilities and health rather than relying on their names.
  OpenAI-compatible text generation, audio transcription, and embeddings are
  independently testable capabilities. A service that implements only one remains
  valid for that stage without being presented as compatible with the others.
- Local hardware detection recommends appropriate runtimes and model sizes, but the
  user remains in control. Apple MLX, NVIDIA/CUDA, CPU, and other verified backends
  expose their real device, memory, installed-model, and degraded-state information.
- Remote GPU execution uses a versioned localplaud worker contract with capability
  discovery, authenticated and idempotent jobs, minimal input access, progress,
  cancellation, checksummed results, and retryable errors. A worker never receives
  Plaud account credentials.
- Profiles explicitly declare whether data may leave the host. Local-only/no-egress
  mode cannot fall back to a cloud or rented worker. Every other fallback is visible,
  ordered, and constrained by capability, quality, timeout, and cost policy.
- Each stage run and artifact stores the resolved profile snapshot plus actual
  provider, model, version, execution target, configuration/prompt version, timing,
  and usage/cost data where available. Later profile edits never alter old
  provenance.
- An experimental trusted-single-user Codex-backed text provider may use a supported
  local Codex CLI/app-server integration. It must not scrape or copy auth tokens,
  masquerade as an OpenAI-compatible API, become an unattended public-server
  default, or be treated as evidence that OpenAI API usage is included with a
  ChatGPT/Codex subscription.

### 6. Automation

- Rules match source, duration, title/early-transcript keywords, folder/tag, or other
  explicit metadata.
- Actions choose transcription/diarization profile, templates, exports, notifications,
  email, or webhooks.
- Rules have ordering, enable/disable, dry-run visibility, and run history.
- Each rule has a readable sentence describing trigger, scope, conditions, and
  actions; the UI shows enablement, notification policy, last/next run where
  applicable, and which editing surface owns the rule.
- The Web App must support creating and editing local rules. If another client or
  external integration owns a rule, show it as explicitly read-only rather than
  presenting controls that cannot save.
- A failed downstream action can retry without rerunning ASR.

The local AutoFlow foundation now executes source/title/duration/folder/tag rules
after metadata sync. Actions can select a note template or execution profile and
move/tag recordings. Rules are ordered, versioned, idempotent, dry-runnable, locally
editable, and retain per-recording history. Notification-enabled runs create durable,
deduplicated local inbox items only after core actions commit; unread state, dismissal,
preserved provenance, and delivery-only retry are available without rerunning ASR or
rolling back successful actions. Additional external integration action types remain
future work. AutoFlow transcript export is implemented for its automation-safe
TXT/SRT/VTT formats: each run/format records canonical transcript lineage, checksum,
size, status, error, and an independently retryable local file without rolling back
the matched rule's core actions.

A successful AutoFlow profile action is stored per recording and source rule. It is
below a manual recording override and uses the rule priority/version captured by the
successful run. A failed newer run leaves the prior assignment intact. Disabling or
deleting the rule stops future execution but does not silently undo past organization,
template, or profile actions; another successful action or a manual override changes
the recording's effective choice.

Authorized webhook destinations are durable Settings records with explicit
metadata/transcript/notes scopes and environment-only bearer secret references.
Public destinations require HTTPS and private/LAN targets require a separate explicit
allowance. AutoFlow stores a non-secret destination snapshot per run and sends a
bounded JSON payload with a stable idempotency key. Response status/excerpt, payload
hash, attempts, health, last use, and failures are retained; delivery-only retry does
not rerun processing or organization actions. See [`webhooks.md`](webhooks.md).

AutoFlow ownership is explicit. Rules created in localplaud are locally editable;
another application can mirror a rule through a stable owner/external identifier and
update it idempotently. Mirrored rules execute through the same validated local
actions and retain versioned history, but local update, toggle, and delete endpoints
reject them. Discover labels the owner and management hint, offers read-only dry-run,
and catalogs local rules, external owners, authorized webhooks, and authorized email
without pretending generic integrations are installed applications. Its edit dialog
uses the same focus trap, Escape, inert-background, focus-restoration, and unsaved-form
protections as the recording workspace; mobile rule/run history remains readable, and
failed rule actions or delivery retries stay visible without a misleading reload.

Authorized SMTP destinations use the same durable downstream boundary. Settings stores
only an environment password reference and explicit From/To addresses, TLS mode,
private/LAN allowance, subject prefix, and metadata/transcript/notes scopes. Test sends
no recording data. AutoFlow messages have stable Message-ID and delivery IDs; payload
hash, attempts, health, last use, and errors remain independently retryable without
rerunning processing or local rule actions. See [`email-integrations.md`](email-integrations.md).

Settings now groups its implemented account, processing, vocabulary, template,
provider/profile, remote-worker, and authorized-integration controls behind a
responsive section navigator with a direct system-health destination. Translated-
interface locale now has a durable English / Traditional Chinese (Taiwan) selector,
correct document language metadata, localized dates, and a centralized catalog used
by the global shell and Home, Library, recording workspace, Search, Saved notes,
Templates, Discover, Notifications, and Status pages. The recording workspace covers
transcript editing/search, playback, profiles/templates, local-data controls, Ask,
organization, and export. Settings now covers its section navigation and primary
account, security, backup, processing, vocabulary, template, provider/model/profile,
worker, webhook, email, and support controls. Template-owned dynamic helper,
health-state, error-fallback, confirmation, and action messages use the same catalog;
a template-contract test rejects new user-visible JavaScript literals that bypass the
translation helper. Provider/runtime diagnostic detail remains verbatim rather than
being mistranslated or hidden.

Workspace display preferences are durable local data rather than browser-only state:
the chosen workspace name, comfortable/compact density, IANA timezone, and
12/24-hour clock apply to every browser using the instance. The redesigned shell
uses one deliberate light theme; the stored theme preference is pinned to `light`
and Settings does not offer a theme selector. Invalid
timezones are rejected before persistence. Interface-language selection remains
unavailable until the corresponding translations exist.

Private workspace backup is available from Settings and the API for file-backed
SQLite deployments. It uses SQLite's online backup API so the snapshot is consistent
while the service remains available, and can optionally include regular files under
the configured media root. Every archive carries a versioned manifest and SHA-256;
environment/config secrets, Plaud tokens, reverse-proxy credentials, and symlinks are
excluded. Restore remains an explicit offline operation documented in
[`backups.md`](backups.md), so an active Web request can never replace the live database.
Completed archives can also be sent by HTTP PUT to an explicitly authorized HTTPS or
private/LAN destination. Credentials remain environment references; URL validation,
no-redirect delivery, a stable delivery ID, archive checksum, durable attempt state,
idempotent completion, independent retry, and authorization revocation keep this
transport separate from backup creation and restore.

Access & Security reports whether the built-in Web login and API token are configured
and that reverse-proxy authentication is external. The Web login creates durable,
expiring browser sessions using opaque cookies and peppered token hashes. Settings
enumerates them, marks the current browser, and supports immediate remote revocation.
The pre-authentication login surface follows the workspace's durable interface locale
and the light visual theme, while keeping Plaud OAuth visibly separate.
Support & About exposes package/build/runtime identity and a downloadable, no-store
diagnostics document containing only aggregate counts and non-secret switches. Tests
prove that recording identity/content, paths, URLs/addresses, errors, environment
variables, tokens, and credentials are absent. See [`support.md`](support.md).

The recording workspace now has a sticky custom player backed by locally generated
and cached ffmpeg waveform envelopes. Playback state survives tab switches and stays
synchronized with transcript segments; seek, speed, skip, deep links, and keyboard
controls share one audio element.

Recording titles have a separate local override: editing never mutates Plaud and is
not overwritten by later metadata sync. The latest cloud title remains visible and
can be restored with one action; every local user-facing/search/export surface uses
the override consistently.

Folder and tag metadata is editable directly from the recording header through the
same atomic organization contract used by Library bulk actions. Counts and filters
update immediately, and clearing organization remains entirely local.

Home is now a distinct operational landing page rather than an alias for All files.
It presents recent recordings, mirror/import progress, metadata-only versus local
audio counts, current processing, AutoFlow activity, attention items, and direct
import actions; the Library remains the dense filtering and bulk-management surface.

Local-data cleanup is explicit and scoped: Plaud-backed audio and waveform caches can
be removed and re-imported later, while local ASR/notes/map/index/history can be reset
separately. Plaud metadata/cloud artifacts, Saved notes, Ask history, title/folder/
tags, and the remote source are preserved by both operations.

### 7. Settings and system health

- Plaud OAuth and last successful sync.
- Provider connections, separately declared capabilities, model catalog, reusable
  execution profiles, profile defaults, and resolution preview.
- ASR model/device, diarization model/token, language defaults, and custom vocabulary.
- LLM and embedding providers with real model-level health checks, masked secret
  references, test actions, and explicit data-egress/cost policy.
- Remote workers with capability, device/memory, queue, version, last health check,
  and revocation state.
- Storage use, backup, retention, privacy, authentication, and remote-access settings.
- Queue/stage status, current job, retry controls, and useful errors.
- Separate, navigable sections for account/security and active sessions, workspace
  personalization, locale/preferences, custom vocabulary, private sync/backup,
  authorized applications/integrations, support, and version/about information.
- Authorized integrations show scope, provenance, last use, health, and revocation;
  destructive account/session actions are isolated from ordinary preferences.

## Processing contract

Each recording progresses through durable stage runs:

```text
discovered -> downloading -> downloaded -> converting -> transcribing
           -> aligning -> diarizing -> enriching -> summarizing
           -> mapping -> indexing -> ready
```

`ready` represents the configured minimum usable product result, not the success of
every optional integration. Stages retain independent status and may be retried from
their last valid input. The UI should expose friendly aggregate states while keeping
detailed diagnostics available.

Failed and usable-partial processing cycles are retried with durable exponential
backoff. Newly downloaded recordings remain ahead of retries in each bounded daemon
batch; the recording UI shows the next retry or exhausted state, and an explicit
Resume bypasses the delay and resets the consecutive-failure budget.

The first successful Plaud listing establishes a durable metadata-only catalog
baseline, so connecting an established account never downloads its entire history by
surprise. With automatic download enabled (the default), recordings first observed
after that baseline are downloaded and enter the worker without a Web or CLI action.
Explicit Import audio remains available for any metadata-only historical recording.

Before work starts, localplaud resolves and persists the recording's execution
profile. Each stage dispatches independently to its selected local runtime, cloud
provider, or remote worker. Retries are idempotent and preserve the resolved profile
unless the user explicitly chooses another profile; fallback never silently crosses
the profile's privacy or cost boundary.

The baseline speech stack is Whisper large-v3-turbo plus word alignment and
production-quality speaker diarization. Speaker labels are derived by the
diarization/alignment stages, not by Whisper itself.

## Editing and provenance

- Original audio is immutable.
- Raw ASR output, corrected canonical transcript, notes, maps, and indexes have
  explicit provenance and revision relationships.
- Speaker IDs remain stable inside a recording; display names are editable.
- Transcript edits invalidate only dependent summaries/maps/indexes, not the audio or
  ASR artifact.
- Regeneration never silently destroys user edits.
- Custom vocabulary rules are local, optionally language/case scoped, and apply as
  immutable transcript revisions after ASR and diarization. They never rewrite raw
  provider output; explicit library-wide application marks affected notes, maps, and
  indexes stale before those artifacts can be reused.

## Required export scope

| Content | Formats |
| --- | --- |
| Transcript | TXT, SRT, VTT, DOCX, PDF |
| Generated and Saved notes | Markdown, TXT, DOCX, PDF |

The recording workspace provides transcript TXT/SRT/VTT/DOCX/PDF exports with
timestamp and speaker-label controls. DOCX uses a compact long-form transcript
layout; PDF embeds Noto Sans TC so mixed Taiwan Mandarin/English remains portable.
Generated and Saved notes use Markdown-aware DOCX/PDF rendering rather than exposing
raw Markdown syntax. Audio, archive, and mind-map exports remain complementary formats.

The recording export dialog can also copy the canonical transcript with the same
timestamp/speaker controls or copy all current generated and Saved notes as Markdown.
The Library exports up to 50 selected recordings as one bounded ZIP. It reuses these
same canonical renderers, preserves requested order, excludes stale or disallowed
cloud-derived artifacts, and includes a checksummed `manifest.json` that records
transcript lineage plus every emitted, unavailable, or failed output. A partially
available selection still downloads its valid content; an entirely unavailable
selection fails without creating a misleading empty archive.

Transcript export must allow timestamps and speaker names to be toggled.

## Acceptance scenarios

1. A recording uploaded on a Plaud free account with no generated transcript is
   downloaded and becomes ready automatically.
2. Mandarin/English code-switching is transcribed with Whisper large-v3-turbo and
   distinct speakers are consistently labelled. A separate contextual AI correction
   then removes ASR stutters/repetition and fixes recognition errors without changing
   timestamps, speaker ownership, or facts; raw ASR remains directly inspectable.
3. The user corrects a name once, renames a speaker, regenerates notes, and sees the
   corrected values in notes, search, and Ask.
4. A long recording is summarized using its complete transcript.
5. Embedding failure leaves transcript and notes usable; indexing resumes later.
6. A cited Ask answer opens the right recording and seeks to the relevant moment.
7. The complete daily workflow after OAuth is usable from the Web App on desktop and
   mobile without CLI commands.
8. The same clean raw recording can complete with an Apple-local profile and with an
   NVIDIA-local or remote-GPU profile while retaining comparable timestamped,
   diarized artifact contracts.
9. OpenAI cloud text/audio/embedding capabilities and an OpenAI-compatible service's
   partial capabilities are tested independently; unsupported stages are rejected or
   routed according to the visible profile rather than guessed from the API shape.
10. A local-only profile never sends audio, transcript, prompts, or embeddings to an
    external provider, including during retries or health degradation.
11. Editing a profile does not change the provider/model/configuration provenance of
    prior stage runs or artifacts.
12. Retrying or reconnecting a remote worker neither duplicates a completed artifact
    nor discards a valid result from another stage.

## Current gaps

As of 2026-07-10, the repository has the core poll/download/process/store/UI skeleton,
but this document describes the target rather than current parity. Independent mode
now enforces local transcript provenance and safely preserves/requeues legacy Plaud
imports. Implemented pipeline stages now persist attempts, provider/model provenance,
timestamps, and failures; optional-stage errors retain usable transcript/notes and
can resume from existing artifacts. Ollama embeddings have model-aware health checks
and modern batch API support. The recordings library now supports sortable
name/duration/recorded columns, processing-state and capture-source filters, per-row
processing state with error/partial attention indicators, and a read-only trash
mirror view. Local folders/tags, uncategorized organization, counts/filters, and
atomic bulk organization are implemented without modifying Plaud cloud state.
Recorded-date and duration ranges now compose with those filters across the HTML and
JSON surfaces, use the workspace timezone for inclusive calendar days, and remain
intact when opening a recording or paging its side list. Unknown capture sources and
legacy Plaud-origin rows are filterable rather than silently omitted.
Selected recordings can also be queued for durable Resume or have only their local
processing artifacts removed in one validated operation. Active claims are rejected;
original audio, Plaud data, organization, Ask history, and editable notes remain.
Search now works without an embedding provider across local titles, the
provenance-correct canonical transcript, generated notes, and saved notes. Folder,
tag, source, and recording-date filters apply consistently; date boundaries use the
workspace timezone before lexical and semantic ranking. Timestamped transcript
matches open the player at the matching moment; note and mind-map matches open the
exact artifact, and note selection survives refresh and browser navigation. Search
and new Ask citations resolve stable speaker keys to the user's recording-local
display names without changing stored transcript/chunk identity. Semantic hits are
blended in when an embedding index is available.
MLX large-v3-turbo is smoke-tested on SkyLabMac, and
the code targets pyannote Community-1. Optional VAD groundwork now exists behind a
default-off `asr.vad.enabled` flag (silero-vad on the mlx path with global-timestamp
region offsetting; faster-whisper's native bundled VAD filter), and degrades honestly
to whole-file transcription with a visible health note when the optional `vad` extra
is absent — but it still needs real Taiwan Mandarin / code-switch validation before
being enabled by default. The durable align stage validates Whisper word timing
without calling it forced alignment. An explicit local `align:whisperx` provider now
dispatches language-specific wav2vec2 forced alignment on CUDA or CPU, persists
provider/model/version/coverage evidence, updates timing without replacing the raw
transcript identity or user revisions, and resumes completed work. It remains
default-off until owned Taiwan Mandarin and Mandarin/English recordings validate the
language models, accuracy, timestamps, speed, and memory. The subscription-
independence gate requires this `forced_alignment=true` evidence. Authenticated
real-audio diarization is verified on SkyLabMac, including
durable speaker output and resume behavior. Single-file
Ask now answers grounded only in one recording and renders each citation as a
playable timestamp that seeks the player, and whole-library Ask citations deep-link
to the cited moment (`/file/{id}?t=`). Long transcripts are summarized with full
coverage through bounded hierarchical map/reduce. Mind maps are generated from the
canonical transcript as full-coverage Markdown outlines, rendered as a collapsible
tree in the recording workspace, included in Markdown export, and downloadable as
a complete locally rendered PNG tree. Speaker identities are now persisted
per recording with stable local keys and user-editable display names
(renamed from the Web detail page and applied in transcript view, regenerated
artifacts, Ask, and export). Run-local provider labels are reconciled using clear,
one-to-one timestamp overlap; ambiguous or new voices get fresh unnamed identities
so a saved name is never silently moved to uncertain speech. One canonical segment
can also be reassigned to another stable speaker without altering raw ASR;
speaker-only revisions preserve its timed words and flow through regenerated notes,
search, Ask, and export. Transcript corrections are
stored as provenance-preserving revisions: per-segment
inline edits create a corrected canonical transcript on top of the immutable raw
ASR row, survive re-ASR, re-index in the background without rerunning ASR, and hide
stale notes/maps until explicit regeneration, with a labelled raw-versus-corrected
view switch. Plaud-derived edits remain excluded from independent mode. Transcript
find/replace, historical previews, and non-destructive restore-as-new-revision are
implemented. Summaries, mind maps, embedding chunks, and stage runs persist their
exact input transcript lineage; notes and processing details expose it in the Web UI.
Transcript correction now dispatches through the durable stage-scoped profile rather
than being tied to one provider. Ollama, OpenAI, Anthropic, and the no-tools OpenCode
Go integration can be selected when their catalog model advertises `correct`;
provider/model/configuration provenance and no-egress policy are resolved before
execution, and unavailable providers never trigger an implicit fallback. A production
recording has completed the OpenCode Go path end to end: the immutable `ai_polish`
revision became the canonical input for notes, mind map, and embedding chunks while
raw ASR remained directly inspectable.
Saved Ask answers are editable note bodies with durable follow-up threads. Library
and recording Ask surfaces expose exact-scope conversation history through a
searchable, paginated desktop/mobile drawer. Threads can be renamed or deleted;
deleting one preserves Saved notes and their citations as independent local data.
New date-scoped library threads freeze the workspace timezone and exact UTC
millisecond boundaries in a versioned retrieval scope; changing display preferences
cannot move later follow-ups. Existing date scopes retain their legacy UTC meaning,
and whole-library retrieval excludes Trash before vector ranking.
Generated notes can be promoted idempotently to a provenance-linked editable copy
inside the same recording workspace. User edits affect only that copy; the original
AI artifact and its model/template/transcript lineage remain immutable.
Manual notes, Saved Ask answers, and editable generated-note copies use optimistic
versions rather than last-write-wins updates. Every real edit archives the displaced
title and exact Markdown body; history is bounded and lazy-loaded, and restoring an
older version creates a new live version while keeping citations, Ask/source links,
and generated-copy provenance unchanged. The recording workspace and Saved Notes hub
share the same keyboard-accessible desktop/mobile history drawer.
User-authored Markdown notes are independent local data and can be created before
audio import, transcription, or generation. They are selectable in the recording
Notes workspace, searchable and exportable through the existing note paths, and never
claim provider/model provenance or alter transcript, generation, Ask, or processing
state. The Saved Notes hub can find a recording and create the same note without
opening its workspace first.
The Templates workspace now separates My Space and Explore, with server-side search,
scenario/category browsing, provenance, descriptions, authorship/popularity signals,
prompt preview, immutable version editing, and copy-to-workspace. Other major gaps
include automation and UI polish. Auto template selection is local and deterministic,
uses title/transcript/duration signals, previews its reasoning before processing, and
persists the actual selected template plus recommendation engine provenance. Because
the recommendation depends on the transcript, speech/correction stages retain the
pre-template profile snapshot while notes, mind maps, and indexing re-resolve against
the selected template. Reuse accepts an explicit current fallback candidate without
repeating provider calls, and a changed template key/version invalidates the mind-map
input lineage.
Provider/model/profile management and the versioned remote-worker protocol
are implemented. Local hardware/runtime detection now provides evidence-backed,
ranked Apple MLX, NVIDIA CUDA, and CPU ASR recommendations with guarded one-click
profile creation that preserves the current non-ASR stages and policy. Explicit
cross-provider fallback is stage-scoped, capability/policy validated, limited to
retryable failures, and recorded as separate attempts. Provider connection and model
health checks for remote workers use the authenticated protocol-v1 capability
handshake; a healthy worker does not imply that an unadvertised model is available.
The remaining real-hardware acceptance matrix is an engineering/deployment task,
not a user-facing benchmark feature. localplaud intentionally exposes only the
deterministic subscription-independence gate in the daily Web App and CLI.

The deterministic subscription-independence gate is available as
`localplaud acceptance-check RECORDING_ID` (or `--json` for automation). It audits
the raw-audio boundary, local provenance, timestamped speakers, notes, mind map, Ask
index, durable stage/profile state, and required TXT/SRT/VTT/DOCX/PDF transcript
exports. The automated
harness additionally exercises grounded single-file Ask with a playable citation;
see [`acceptance.md`](acceptance.md).
Each recording workspace renders this gate as an expandable checklist and exposes
the identical versioned JSON report through the API, so readiness does not require
CLI access.
Each concrete pipeline attempt is now retained in an append-only usage ledger with
profile snapshot, provider/model, outcome, latency, normalized audio/text/token usage,
and catalog-driven estimated cost. Recording and Status surfaces expose both attempts
and aggregate totals; estimates remain zero when no explicit model pricing is stored.
When a Profile sets a cost ceiling, cloud and remote stages must have explicit model
pricing (or an explicit free declaration). A conservative pre-egress reservation is
checked against all prior attempt cost; an over-budget or unknown-cost stage fails
before provider invocation and can Resume after the user changes Profile policy.
