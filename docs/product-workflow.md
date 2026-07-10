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

### 1. Recordings library

- Persistent navigation for Recordings, Ask, Templates, Automation, and Settings.
- Search by title and transcript content.
- Folders/tags, date/source/duration/status filters, and useful sorting.
- Every row communicates recorded time, duration, processing state, and whether
  attention is needed.
- Bulk organization, export, reprocess, and deletion of local derivatives.
- Responsive desktop split-pane and mobile list/detail navigation.

### 2. Recording workspace

- Header: editable title, date, duration, tags/folder, processing state, and actions.
- Persistent audio player with seek, speed, skip, waveform/progress, and keyboard
  controls.
- Transcript tab: click-to-seek, active-segment tracking, timestamps, speaker colors,
  speaker rename, inline correction, and search within the recording.
- Notes: multiple generated or user-authored note tabs, inline editing, generation
  provenance, template/model selection, and safe regeneration.
- Mind map: navigable hierarchy generated from the canonical corrected transcript
  and notes.
- Ask: single-file chat, quick actions, follow-up prompts, timestamp citations,
  source excerpts, and save-to-note.
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
- Multiple templates may run for the same recording without deleting prior notes.
- Long recordings use full-coverage hierarchical summarization; silent truncation is
  forbidden.

### 5. Automation

- Rules match source, duration, title/early-transcript keywords, folder/tag, or other
  explicit metadata.
- Actions choose transcription/diarization profile, templates, exports, notifications,
  email, or webhooks.
- Rules have ordering, enable/disable, dry-run visibility, and run history.
- A failed downstream action can retry without rerunning ASR.

### 6. Settings and system health

- Plaud OAuth and last successful sync.
- ASR model/device, diarization model/token, language defaults, and custom vocabulary.
- LLM and embedding providers with real model-level health checks.
- Storage use, backup, retention, privacy, authentication, and remote-access settings.
- Queue/stage status, current job, retry controls, and useful errors.

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
but this document describes the target rather than current parity. Major gaps include
strict raw-audio-only processing, stage-level state, reliable embeddings, default
diarization, word alignment, editable artifacts, long-form summarization, mind maps,
single-file Ask, richer organization/export, automation, and UI polish.
