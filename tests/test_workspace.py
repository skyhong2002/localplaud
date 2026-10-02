"""Recording workspace: speaker merge/rename, word sync markup, provenance and
degraded/failure states rendered without any Plaud Intelligence artifact."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select

STATIC = Path(__file__).parents[1] / "src/localplaud/api/static"


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'ws.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _mute_reindex(monkeypatch):
    import localplaud.worker.reindex as reindex_mod

    calls = []
    monkeypatch.setattr(
        reindex_mod,
        "reindex_file",
        lambda file_id, settings=None, **kwargs: calls.append((file_id, kwargs)),
    )
    return calls


def _words(text, start, end, speaker):
    tokens = text.split(" ")
    step = (end - start) / len(tokens)
    return [
        {
            "text": (" " if i else "") + token,
            "start": round(start + i * step, 2),
            "end": round(start + (i + 1) * step, 2),
            "speaker": speaker,
        }
        for i, token in enumerate(tokens)
    ]


SEGMENTS = [
    {"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00",
     "words": _words("hello team", 1.0, 2.0, "SPEAKER_00")},
    {"text": "hi there", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01",
     "words": _words("hi there", 2.0, 3.0, "SPEAKER_01")},
    {"text": "sounds good", "start": 3.0, "end": 4.0, "speaker": "SPEAKER_02",
     "words": _words("sounds good", 3.0, 4.0, "SPEAKER_02")},
    {"text": "see you", "start": 4.0, "end": 6.5, "speaker": "SPEAKER_01",
     "words": [{"text": "see", "start": 4.0, "end": 4.5, "speaker": "SPEAKER_01"},
               {"text": "you", "start": 4.5, "end": 5.0, "speaker": "SPEAKER_01"}]},
]


def _seed(file_id="r1", segments=SEGMENTS, *, diarize_status=None, summarize=None, audio=None):
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Summary,
        Transcript,
    )
    from localplaud.db.session import session_scope
    from localplaud.store.speakers import speaker_keys_from_segments, sync_speakers

    with session_scope() as s:
        s.add(PlaudFile(id=file_id, filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000, scene=1,
                        audio_path=audio))
        s.flush()
        s.add(Transcript(file_id=file_id, provider="faster-whisper", model="large-v3-turbo",
                         language="en", has_speakers=any(x.get("speaker") for x in segments),
                         source="local", text="\n".join(x["text"] for x in segments),
                         segments=segments))
        sync_speakers(s, file_id, speaker_keys_from_segments(segments))
        if diarize_status is not None:
            s.add(StageRun(file_id=file_id, stage=StageName.diarize,
                           status=StageStatus(diarize_status),
                           error="pyannote token missing" if diarize_status == "failed" else None))
        if summarize == "failed":
            s.add(StageRun(file_id=file_id, stage=StageName.summarize, status=StageStatus.failed,
                           provider="ollama", model="qwen3:14b",
                           error="Provider timeout after 3 attempts"))
        elif summarize == "done":
            s.add(Summary(file_id=file_id, template="default", template_version=4,
                          template_snapshot={"name": "Meeting notes"}, title="Weekly",
                          content_md="## Action items\n- [ ] Ship it\n",
                          llm_provider="ollama", model="qwen3:14b", source="local",
                          input_transcript_revision=0, input_transcript_source="local"))


def test_workspace_assets_are_served_and_referenced(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    page = c.get("/file/r1").text
    version = re.search(r'/static/js/workspace\.js\?v=([0-9a-f]{12})"', page)
    assert version and f"/static/css/recording.css?v={version.group(1)}" in page
    assert c.get("/static/js/workspace.js").status_code == 200
    assert c.get("/static/css/recording.css").status_code == 200
    config = re.search(r'<script type="application/json" id="ws-config">(.*?)</script>', page, re.S)
    assert config and '"fileId": "r1"' in config.group(1) and '"canEdit": true' in config.group(1)


def test_word_spans_render_only_when_they_rebuild_the_text(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _seed()
    page = c.get("/file/r1/transcript-page?view=raw").text
    assert '<span class="w" data-s="1.0" data-e="1.5">hello</span><span class="w" ' in page
    # Whitespace-free word tokens are joined with single spaces when that is lossless.
    assert '<span class="w" data-s="4.5" data-e="5.0"> you</span>' in page
    # A corrected paragraph has no timed words any more and renders plain text.
    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
           headers={"accept": "application/json"})
    corrected = c.get("/file/r1/transcript-page?view=corrected").text
    first = corrected.split('data-idx="1"', 1)[0]
    assert '<p class="seg-text">hello, team!</p>' in first
    assert 'class="w"' not in first
    # Every paragraph exposes its index and a seekable timestamp button.
    assert 'data-idx="3"' in corrected and 'data-seek="4.0"' in corrected


def test_word_spans_reject_mismatched_tokens():
    from localplaud.api.app import _segment_word_spans

    assert _segment_word_spans({"text": "abc", "words": [{"text": "xyz", "start": 0, "end": 1}]}) is None
    assert _segment_word_spans({"text": "abc", "words": []}) is None
    assert _segment_word_spans(
        {"text": "你好", "words": [{"text": "你", "start": 0, "end": 0.5}, {"text": "好", "start": 0.5, "end": 1}]}
    ) == [{"text": "你", "start": 0.0, "end": 0.5}, {"text": "好", "start": 0.5, "end": 1.0}]
    assert _segment_word_spans(
        {"text": "a", "words": [{"text": "a", "start": None, "end": 1}]}
    ) is None


def test_speaker_rename_returns_json_for_in_place_updates(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _seed()
    response = c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "  Alice  "},
                      headers={"accept": "application/json"})
    assert response.status_code == 200
    assert {k: response.json()[k] for k in ("key", "name", "display")} == {
        "key": "SPEAKER_00", "name": "Alice", "display": "Alice",
    }
    assert response.json()["note_projection"]["file_id"] == "r1"
    cleared = c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": ""},
                     headers={"accept": "application/json"})
    assert {k: cleared.json()[k] for k in ("key", "name", "display")} == {
        "key": "SPEAKER_00", "name": None, "display": "Speaker 1",
    }
    # Browsers without JS still get the redirect contract.
    legacy = c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "Bo"},
                    follow_redirects=False)
    assert legacy.status_code == 303


def test_merge_speakers_creates_reversible_revision_and_keeps_raw(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    calls = _mute_reindex(monkeypatch)
    _seed()
    from localplaud.db.models import Speaker, Transcript, TranscriptRevision
    from localplaud.db.session import session_scope

    c.post("/file/r1/speakers", data={"key": "SPEAKER_02", "name": "Carol"},
           headers={"accept": "application/json"})
    merged = c.post("/file/r1/speakers/merge",
                    data={"source": "SPEAKER_01", "target": "SPEAKER_00", "base_revision": 0})
    assert merged.status_code == 200
    assert merged.json() == {"changed": True, "revision": 1, "segments": 2, "target": "SPEAKER_00"}
    with session_scope() as s:
        revision = s.scalar(select(TranscriptRevision).where(TranscriptRevision.file_id == "r1"))
        assert revision.kind == "speaker_edit" and revision.source == "local"
        assert [seg["speaker"] for seg in revision.segments] == [
            "SPEAKER_00", "SPEAKER_00", "SPEAKER_02", "SPEAKER_00"]
        assert {w["speaker"] for w in revision.segments[1]["words"]} == {"SPEAKER_00"}
        assert revision.segments[1]["text"] == "hi there"  # text and timing untouched
        assert revision.segments[1]["words"][1]["start"] == 2.5
        raw = s.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        assert raw.segments[1]["speaker"] == "SPEAKER_01"  # raw ASR immutable
        # Display names are kept: nothing user-authored is destroyed.
        names = {sp.key: sp.display_name for sp in s.scalars(select(Speaker))}
        assert names["SPEAKER_02"] == "Carol"
    assert calls  # re-indexed without rerunning ASR
    history = c.get("/file/r1?tab=transcript").text
    assert "Merged speakers across segments 2: SPEAKER_01 → SPEAKER_00" in history
    # Stale base, identical speakers, and unknown keys are rejected.
    stale = c.post("/file/r1/speakers/merge",
                   data={"source": "SPEAKER_02", "target": "SPEAKER_00", "base_revision": 0})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    same = c.post("/file/r1/speakers/merge",
                  data={"source": "SPEAKER_00", "target": "SPEAKER_00", "base_revision": 1})
    assert same.status_code == 400
    unknown = c.post("/file/r1/speakers/merge",
                     data={"source": "SPEAKER_09", "target": "SPEAKER_00", "base_revision": 1})
    assert unknown.status_code == 400 and unknown.json()["code"] == "unknown_speaker"


def test_generated_note_shows_template_model_and_lineage(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(summarize="done")
    page = c.get("/file/r1?tab=notes").text
    provenance = page.split('data-note-provenance>', 1)[1].split("</div>", 1)[0]
    assert "Meeting notes · v4" in provenance
    assert "ollama:qwen3:14b" in provenance
    assert "Generated from raw ASR" in provenance and "local" in provenance
    assert "Template used: <a href=\"/templates\">Meeting notes</a> · v4" in page
    # Rich rendering: task list items render as checkboxes, not raw Markdown.
    assert 'class="task-list-item' in page
    # Notes are regenerated from a template chooser dialog with visible status.
    assert 'id="note-generate-backdrop"' in page and 'id="generate-notes"' in page
    assert "Select a template" in page


def test_failed_notes_surface_error_and_recovery(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(summarize="failed")
    page = c.get("/file/r1?tab=notes").text
    alert = page.split("data-notes-failed>", 1)[1].split("</div>\n      </div>", 1)[0]
    assert "Notes could not be generated" in alert
    assert "ollama:qwen3:14b" in alert
    assert "Provider timeout after 3 attempts" in alert
    assert "data-open-note-dialog" in alert  # Try again without leaving the page
    assert "Speaker labels are unavailable" not in page  # transcript stays usable


def test_degraded_diarization_banner(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    undiarized = [{k: v for k, v in seg.items() if k != "speaker"} | {"words": []} for seg in SEGMENTS]
    _seed("nodiar", undiarized, diarize_status="failed")
    page = c.get("/file/nodiar?tab=transcript").text
    assert "data-diarization-degraded" in page
    assert "Speaker diarization failed" in page and "pyannote token missing" in page
    _seed("ok", diarize_status="completed")
    assert "data-diarization-degraded" not in c.get("/file/ok?tab=transcript").text
    _seed("degraded", undiarized, diarize_status="degraded")
    assert "Speaker diarization failed or was unavailable" in c.get(
        "/file/degraded?tab=transcript"
    ).text
    _seed("never", undiarized)
    never = c.get("/file/never?tab=transcript").text
    assert "Speaker diarization did not run for this transcript." in never


def test_name_speakers_dialog_lists_talk_time_and_clips(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    page = c.get("/file/r1").text
    dialog = page.split('id="name-speakers-backdrop"', 1)[1].split("</section>", 1)[0]
    row = dialog.split('data-speaker-row="SPEAKER_01"', 1)[1].split('data-speaker-row=', 1)[0]
    assert "Total duration 0:03" in row  # 1.0 s + 2.5 s
    assert 'data-seek="4.0"' in row and "see you" in row
    assert 'id="menu-name-speakers"' in page


def test_workspace_strings_are_translated(monkeypatch, tmp_path):
    from localplaud.i18n import catalog

    zh = catalog("zh-Hant-TW")
    for key in ("Name this speaker", "Back to current", "Speaker labels are unavailable",
                "Notes could not be generated", "Select a template", "Export transcript"):
        assert zh.get(key) and zh[key] != key


def test_notes_boundary_classifies_local_remote_and_cloud():
    from types import SimpleNamespace

    from localplaud.api.app import _notes_boundary

    connections = {
        "ollama": SimpleNamespace(execution_target="local", data_egress=False),
        "worker": SimpleNamespace(execution_target="remote", data_egress=True),
        "openai": SimpleNamespace(execution_target="cloud", data_egress=True),
    }

    def privacy(connection):
        return _notes_boundary(
            {"summarize": {"connection": connection, "model": "m"}}, connections, False
        )["privacy"]

    assert privacy("ollama") == "local"
    assert privacy("worker") == "remote"
    assert privacy("openai") == "cloud"
    assert _notes_boundary({}, connections, True) == {
        "privacy": "unknown", "connection": None, "model": None, "no_egress": True,
    }


def test_generate_sheet_shows_auto_custom_and_privacy_boundary(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(summarize="done")
    page = c.get("/file/r1?tab=notes").text
    sheet = page.split('id="note-generate-backdrop"', 1)[1].split("</section>", 1)[0]
    assert 'name="note-generate-mode" value="auto"' in sheet
    assert 'name="note-generate-mode" value="custom"' in sheet
    assert "data-gen-boundary" in sheet and "ws-privacy" in sheet
    assert 'id="note-profile-select"' in sheet and "data-privacy=" in sheet
    assert "Language" in sheet


def test_phone_sources_notes_layout_and_outline_contract(monkeypatch, tmp_path):
    """Plaud app phone layout: Sources | Notes switch, note picker, outline slot.

    The chapter outline comes from GET /api/files/{id}/outline; the section is
    offers explicit generation and retries; a missing outline keeps the
    transcript available."""
    c = _client(monkeypatch, tmp_path)
    _seed(summarize="done", audio=str(tmp_path / "a.opus"))
    (tmp_path / "a.opus").write_bytes(b"x")
    page = c.get("/file/r1?tab=transcript").text
    assert 'data-m-switch="transcript" aria-pressed="true"' in page
    assert 'data-m-switch="notes" aria-pressed="false"' in page
    assert '<section class="ws-outline" data-outline aria-labelledby' in page
    assert "data-outline-generate" in page and "data-outline-status" in page
    assert "Time sections · no AI" in page
    assert "data-transcript-expand" in page and "data-chapter-card hidden" in page
    assert 'data-note-picker aria-haspopup="menu"' in page
    workspace_js = (STATIC / "js/workspace.js").read_text()
    assert "/outline`" in workspace_js
    assert "Number(item.start_ms) / 1000" in workspace_js
    assert "data.stale" in workspace_js
