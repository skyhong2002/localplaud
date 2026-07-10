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
- Users can continue a thread, edit a result, copy/export it, or save it as a note.

### 4. Templates and generation

- Auto mode chooses a sensible workflow from recording metadata/content.
- Custom mode selects language, ASR profile, speaker settings, note templates, and
  LLM per file.
- Users can create and edit templates as structured prompts.
- Template management has separate personal and discovery surfaces, search,
  scenario/category browsing, first-party/community provenance, description,
  authorship, and optional popularity signals.
- Multiple templates may run for the same recording without deleting prior notes.
- Long recordings use full-coverage hierarchical summarization; silent truncation is
  forbidden.

### 5. Automation

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

### 6. Settings and system health

- Plaud OAuth and last successful sync.
- ASR model/device, diarization model/token, language defaults, and custom vocabulary.
- LLM and embedding providers with real model-level health checks.
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
| Transcript | TXT, Markdown, SRT, VTT, DOCX, PDF |
| Notes | Markdown, TXT, DOCX, PDF |
| Mind map | Markdown outline, PNG |
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

## Current gaps

As of 2026-07-10, the repository has the core poll/download/process/store/UI skeleton,
but this document describes the target rather than current parity. Independent mode
now enforces local transcript provenance and safely preserves/requeues legacy Plaud
imports. Implemented pipeline stages now persist attempts, provider/model provenance,
timestamps, and failures; optional-stage errors retain usable transcript/notes and
can resume from existing artifacts. Ollama embeddings have model-aware health checks
and modern batch API support. MLX large-v3-turbo is smoke-tested on SkyLabMac, and
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
to the cited moment (`/file/{id}?t=`). Other major gaps include editable artifacts,
mind maps, Ask save-to-note and follow-up threads, richer organization/export,
automation, and UI polish.
