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
- Ask at both file and library scope, including suggested questions and reusable
  skills such as action-item extraction, task tables, and insight generation;
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
- A citation opens the recording at the cited moment.
- Users can continue a durable grounded thread and save any answer as an editable
  note. Saved notes retain source moments, link back to recordings, appear in the
  recording workspace, and can be copied or exported as Markdown, edited, or deleted
  independently without altering the original Ask thread.

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
- Profile resolution is deterministic: system default → folder/AutoFlow rule →
  template default → per-recording override. The UI previews the resolved choices
  before processing or reprocessing.
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
editable, and retain per-recording history; notification delivery and external
integration actions remain future work.

The recording workspace now has a sticky custom player backed by locally generated
and cached ffmpeg waveform envelopes. Playback state survives tab switches and stays
synchronized with transcript segments; seek, speed, skip, deep links, and keyboard
controls share one audio element.

Recording titles have a separate local override: editing never mutates Plaud and is
not overwritten by later metadata sync. The latest cloud title remains visible and
can be restored with one action; every local user-facing/search/export surface uses
the override consistently.

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

## Export parity target

| Content | Formats |
| --- | --- |
| Audio | original, MP3, WAV |
| Transcript | TXT, SRT, VTT |
| Notes | Markdown, TXT |
| Mind map | Markdown outline, PNG |

The recording workspace now provides transcript TXT/SRT/VTT exports with timestamp
and speaker-label controls, notes Markdown/TXT, original audio,
and a combined Markdown archive. Converted MP3/WAV audio and PNG mind-map rendering
remain to complete this matrix.
| Ask result | Markdown, clipboard, saved local note |

Transcript export must allow timestamps and speaker names to be toggled.

## Acceptance scenarios

1. A recording uploaded on a Plaud free account with no generated transcript is
   downloaded and becomes ready automatically.
2. Mandarin/English code-switching is transcribed with Whisper large-v3-turbo and
   distinct speakers are consistently labelled.
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
MLX large-v3-turbo is smoke-tested on SkyLabMac, and
the code targets pyannote Community-1. Optional VAD groundwork now exists behind a
default-off `asr.vad.enabled` flag (silero-vad on the mlx path with global-timestamp
region offsetting; faster-whisper's native bundled VAD filter), and degrades honestly
to whole-file transcription with a visible health note when the optional `vad` extra
is absent — but it still needs a real Taiwan Mandarin / code-switch benchmark before
being enabled by default. Word-level forced alignment is still not implemented:
Whisper's own word timestamps remain the alignment source, and a whisperX-style
wav2vec2 aligner needs per-language models and a real-recording accuracy evaluation
first. Authenticated real-audio diarization verification also remains. Single-file
Ask now answers grounded only in one recording and renders each citation as a
playable timestamp that seeks the player, and whole-library Ask citations deep-link
to the cited moment (`/file/{id}?t=`). Long transcripts are summarized with full
coverage through bounded hierarchical map/reduce. Mind maps are generated from the
canonical transcript as full-coverage Markdown outlines, rendered as a collapsible
tree in the recording workspace, and included in Markdown export; PNG mind-map
export remains. Speaker identities are now persisted
per recording with stable diarization keys and user-editable display names
(renamed from the Web detail page and applied in transcript view, regenerated
artifacts, Ask, and export); safe identity reconciliation across diarization reruns
remains because provider speaker labels are run-local. Transcript corrections are
stored as provenance-preserving revisions: per-segment
inline edits create a corrected canonical transcript on top of the immutable raw
ASR row, survive re-ASR, re-index in the background without rerunning ASR, and hide
stale notes/maps until explicit regeneration, with a labelled raw-versus-corrected
view switch. Plaud-derived edits remain excluded from independent mode. Transcript
find/replace, historical previews, and non-destructive restore-as-new-revision are
implemented. Summaries, mind maps, embedding chunks, and stage runs persist their
exact input transcript lineage; notes and processing details expose it in the Web UI.
Saved Ask answers are editable note bodies with durable follow-up threads.
The Templates workspace now separates My Space and Explore, with server-side search,
scenario/category browsing, provenance, descriptions, authorship/popularity signals,
prompt preview, immutable version editing, and copy-to-workspace. Other major gaps
include automation and UI polish. Auto template selection is local and deterministic,
uses title/transcript/duration signals, previews its reasoning before processing, and
persists the actual selected template plus recommendation engine provenance.
Provider/model/profile management and the versioned remote-worker protocol
are implemented; cross-provider fallback/cost accounting and the remaining hardware
acceptance matrix are still open.
