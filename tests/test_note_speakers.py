"""Stable note speaker bindings without model calls or destructive user rewrites."""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import select

from localplaud.db.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    PlaudFile,
    Speaker,
    StageName,
    StageRun,
    StageStatus,
    Summary,
    SummaryRevision,
    Transcript,
    UserNote,
)
from localplaud.db.session import init_db, session_scope
from localplaud.note_history import source_summary_provenance
from localplaud.note_speakers import (
    anonymous_summary_content,
    bind_generated_summary,
    refresh_generated_note_speaker_names,
)
from localplaud.store.speakers import display_names, speaker_labels


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    import localplaud.config as config
    import localplaud.db.session as db

    settings = config.Settings(
        _env_file=None, store={"database_url": f"sqlite:///{tmp_path / 'speakers.db'}"}
    )
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)
    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="recording", filename="Original title"))
        session.flush()
        session.add(
            Transcript(
                file_id="recording",
                provider="test",
                source="local",
                text="Speech",
                segments=[
                    {"start": 0, "end": 1, "text": "Speech", "speaker": "SPEAKER_00"},
                    {"start": 1, "end": 2, "text": "Reply", "speaker": "SPEAKER_01"},
                ],
            )
        )
        session.add_all(
            [
                Speaker(file_id="recording", key="SPEAKER_00"),
                Speaker(file_id="recording", key="SPEAKER_01"),
            ]
        )
    yield settings
    db.get_engine().dispose()


def seed(content, *, template="test", bind=False, title=None, source="local"):
    with session_scope() as session:
        raw = session.get(PlaudFile, "recording").local_transcript
        row = Summary(
            file_id="recording",
            template=template,
            source=source,
            title=title,
            content_md=content,
            llm_provider="fake",
            model="model",
            input_transcript_id=raw.id,
            input_transcript_revision=0,
            input_transcript_source=source,
            template_snapshot={"original_prompt": "preserve"},
            resolved_profile_snapshot={"original": "snapshot"},
        )
        if bind:
            bind_generated_summary(session, row)
        session.add(row)
        session.flush()
        return row.id


def rename(name, *, previous=None, key="SPEAKER_00"):
    with session_scope() as session:
        old = {
            row.key: row.display_name
            for row in session.scalars(select(Speaker).where(Speaker.file_id == "recording"))
        }
        row = session.scalar(
            select(Speaker).where(Speaker.file_id == "recording", Speaker.key == key)
        )
        row.display_name = name
        session.flush()
        return refresh_generated_note_speaker_names(
            session, "recording", previous_names=old if previous is None else previous
        )


def test_friendly_labels_do_not_change_explicit_name_contract(isolated):
    with session_scope() as session:
        assert speaker_labels(session, "recording") == {
            "SPEAKER_00": "Speaker 1",
            "SPEAKER_01": "Speaker 2",
        }
        assert display_names(session, "recording") == {}
    rename("Sky")
    with session_scope() as session:
        assert speaker_labels(session, "recording")["SPEAKER_00"] == "Sky"
        assert speaker_labels(session, "recording", anonymous=True)["SPEAKER_00"] == "Speaker 1"
        assert display_names(session, "recording") == {"SPEAKER_00": "Sky"}


def test_legacy_raw_alias_rename_repeat_clear_preserves_history_and_people(isolated):
    original = "SPEAKER_00：討論計畫。Speaker 2 回答。天空的 Sky 不是說話者。SPEAKER_001 不變。"
    sid = seed(original, title="Speaker 1 的提案")
    assert rename("Sky")["changed_ids"] == [sid]
    with session_scope() as session:
        row = session.get(Summary, sid)
        assert (
            row.content_md
            == "Sky：討論計畫。Speaker 2 回答。天空的 Sky 不是說話者。SPEAKER_001 不變。"
        )
        assert row.title == "Sky 的提案"
        assert row.llm_provider == "fake" and row.model == "model"
        assert row.resolved_profile_snapshot == {"original": "snapshot"}
        assert row.template_snapshot["original_prompt"] == "preserve"
    rename("洪同學")
    with session_scope() as session:
        row = session.get(Summary, sid)
        assert "洪同學：" in row.content_md
        assert "天空的 Sky 不是說話者" in row.content_md
    rename(None)
    with session_scope() as session:
        row = session.get(Summary, sid)
        assert row.content_md.startswith("Speaker 1：")
        assert row.title == "Speaker 1 的提案"
        versions = list(session.scalars(select(SummaryRevision).order_by(SummaryRevision.revision)))
        assert versions[0].content_md == original
        assert [version.archive_reason for version in versions] == ["speaker_rename"] * 3
        assert (
            session.get(PlaudFile, "recording").local_transcript.segments[0]["speaker"]
            == "SPEAKER_00"
        )


def test_new_generation_materializes_known_names_then_tracks_key(isolated):
    rename("Sky")
    sid = seed("Speaker 1: chose A. Speaker 2: chose B. Person Sky is mentioned.", bind=True)
    with session_scope() as session:
        row = session.get(Summary, sid)
        assert row.content_md.startswith("Sky: chose A.")
        assert anonymous_summary_content(row).startswith("Speaker 1: chose A.")
    rename("River")
    with session_scope() as session:
        text = session.get(Summary, sid).content_md
        assert text.startswith("River: chose A.")
        assert text.endswith("Person Sky is mentioned.")


def test_legacy_person_name_only_binds_explicit_unique_attribution(isolated):
    rename("Ann")
    sid = seed("**Ann**: agreed.\nAnna spoke to Ann.\n### Ann\n| Ann | owner |")
    result = rename("Sky")
    assert result["unresolved_ids"] == [sid]
    with session_scope() as session:
        row = session.get(Summary, sid)
        assert row.content_md == "**Sky**: agreed.\nAnna spoke to Ann.\n### Sky\n| Sky | owner |"
        assert row.template_snapshot["speaker_bindings"]["unresolved"] is True
    assert rename("New")["unresolved_ids"] == [sid]


def test_duplicate_person_names_never_guess_speaker_identity(isolated):
    rename("Ann")
    rename("Ann", key="SPEAKER_01")
    sid = seed("Ann: said hello.\nSpeaker 1 agrees.\nSpeaker 2 disagrees.")
    result = rename("Sky")
    assert result["unresolved"] == 1
    with session_scope() as session:
        assert (
            session.get(Summary, sid).content_md == "Ann: said hello.\nSky agrees.\nAnn disagrees."
        )


def test_new_name_cannot_capture_existing_person_attribution(isolated):
    sid = seed("Sky: a third-party quote.\nSpeaker 1: my decision.")
    rename("Sky")
    rename("River")
    with session_scope() as session:
        assert (
            session.get(Summary, sid).content_md == "Sky: a third-party quote.\nRiver: my decision."
        )


def test_code_urls_and_substrings_not_rewritten(isolated):
    text = "Speaker 1 agreed. `SPEAKER_00` https://example.test/SPEAKER_00\n```\nSpeaker 1\n```\n[docs](SPEAKER_00) SPEAKER_000 Speaker 10"
    sid = seed(text)
    rename("Sky")
    with session_scope() as session:
        assert session.get(Summary, sid).content_md == text.replace(
            "Speaker 1 agreed", "Sky agreed"
        )


def test_user_cloud_and_history_are_not_rewritten(isolated):
    cloud = seed("SPEAKER_00", template="cloud", source="cloud")
    sid = seed("SPEAKER_00", bind=True)
    with session_scope() as session:
        session.add(UserNote(file_id="recording", title="Mine", content_md="SPEAKER_00 notes"))
    rename("Sky")
    with session_scope() as session:
        assert session.get(Summary, cloud).content_md == "SPEAKER_00"
        assert session.scalar(select(UserNote)).content_md == "SPEAKER_00 notes"
        assert (
            session.scalar(
                select(SummaryRevision).where(SummaryRevision.template == "test")
            ).content_md
            == "Speaker 1"
        )
        assert session.get(Summary, sid).content_md == "Sky"


def test_note_index_invalidates_old_evidence_and_preserves_stale_state(isolated):
    from localplaud.worker.knowledge_index import sync_summary_document

    sid = seed("Speaker 1 chose A.", bind=True)
    with session_scope() as session:
        doc = sync_summary_document(session, session.get(Summary, sid))
        session.flush()
        doc.status = "completed"
        session.add(
            KnowledgeChunk(
                document_id=doc.id, idx=0, text="Speaker 1 chose A.", dim=1, embedding=b"old"
            )
        )
        generation = doc.generation
        session.add(
            StageRun(
                file_id="recording",
                stage=StageName.mind_map,
                status=StageStatus.pending,
                detail={"stale": True, "reason": "unrelated"},
            )
        )
    rename("Sky")
    with session_scope() as session:
        doc = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.summary_id == sid))
        assert doc.status == "pending" and doc.generation != generation
        assert list(session.scalars(select(KnowledgeChunk))) == []
        run = session.scalar(select(StageRun))
        assert run.detail == {"stale": True, "reason": "unrelated"}


def test_mindmap_source_fingerprint_tracks_only_proven_same_note(isolated):
    sid = seed("Speaker 1 chose A.", bind=True)
    mid = seed("# Plan\n- Speaker 1 chose A", template="mind_map", bind=True)
    with session_scope() as session:
        note = session.get(Summary, sid)
        mindmap = session.get(Summary, mid)
        mindmap.template_snapshot = dict(mindmap.template_snapshot) | {
            "source_template_key": "test",
            "source_note": source_summary_provenance(note),
        }
    rename("Sky")
    with session_scope() as session:
        note = session.get(Summary, sid)
        mindmap = session.get(Summary, mid)
        assert (
            mindmap.template_snapshot["source_note"]["content_fingerprint"]
            == source_summary_provenance(note)["content_fingerprint"]
        )
        assert "Sky" in mindmap.content_md
        assert list(session.scalars(select(StageRun))) == []


def test_unknown_binding_version_and_corrupt_spans_fail_closed(isolated):
    sid = seed("Speaker 1 is a person", bind=True)
    with session_scope() as session:
        row = session.get(Summary, sid)
        snapshot = deepcopy(row.template_snapshot)
        snapshot["speaker_bindings"]["version"] = 99
        row.template_snapshot = snapshot
    result = rename("New")
    assert result["unresolved_ids"] == [sid]
    with session_scope() as session:
        assert session.get(Summary, sid).content_md == "Speaker 1 is a person"


def test_name_markdown_is_escaped_without_changing_other_prose(isolated):
    sid = seed("**Speaker 1**: action", bind=True)
    rename("[Sky](https://example.test)")
    with session_scope() as session:
        row = session.get(Summary, sid)
        assert row.content_md.startswith("**\\[Sky\\]\\(https://example\\.test\\)**:")
    rename(None)
    with session_scope() as session:
        assert session.get(Summary, sid).content_md == "**Speaker 1**: action"


def test_markdown_table_and_strikethrough_survive_name_projection(isolated):
    from localplaud.markdown import render_markdown

    sid = seed("| Person | Decision |\n| --- | --- |\n| Speaker 1 | Detail |", bind=True)
    rename("A|~~B~~")
    with session_scope() as session:
        body = session.get(Summary, sid).content_md
        html = str(render_markdown(body))
        assert html.count("<td>") == 2
        assert "Detail" in html and "A|~~B~~" in html
        assert "<del>" not in html


def test_generation_uses_stable_friendly_labels_and_persists_names(isolated, monkeypatch):
    from localplaud.worker import pipeline

    rename("Sky")
    isolated.pipeline.polish = isolated.pipeline.index = False
    isolated.pipeline.summary_template = "plaud-autopilot"
    seen = {}

    def summary(transcript, settings, **kwargs):
        seen["summary"] = [segment.speaker for segment in transcript.segments]
        return {
            "title": "Speaker 1 project decision",
            "content_md": "Speaker 1 chose A; Speaker 2 agreed.",
            "template": "plaud-autopilot",
            "provider": "fake",
            "model": "test",
            "template_snapshot": {
                "execution": {"version": "evidence-notes/v2", "note_quality": "evidence"}
            },
        }

    def mindmap(transcript, settings, summary_md):
        seen["map"] = [segment.speaker for segment in transcript.segments]
        seen["source"] = summary_md
        return {
            "template": "mind_map",
            "content_md": "# Project\n- Speaker 1 chose A",
            "provider": "fake",
            "model": "test",
        }

    monkeypatch.setattr(pipeline.summarize, "summarize", summary)
    monkeypatch.setattr(pipeline.mindmap, "generate_mind_map", mindmap)
    pipeline.process_derived_artifacts("recording", isolated)
    assert seen["summary"] == seen["map"] == ["Speaker 1", "Speaker 2"]
    assert seen["source"] == "Speaker 1 chose A; Speaker 2 agreed."
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        assert all("Sky" in summary.content_md for summary in row.summaries)
        assert row.generated_title == "Sky project decision"
        assert row.local_transcript.segments[0]["speaker"] == "SPEAKER_00"


def test_corrupt_binding_does_not_reinterpret_unbound_person_name(isolated):
    rename("Sky")
    sid = seed("Speaker 1 agreed", bind=True)
    with session_scope() as session:
        session.get(Summary, sid).content_md = "Sky: unrelated imported quotation."
    result = rename("River")
    assert result["unresolved_ids"] == [sid]
    with session_scope() as session:
        assert session.get(Summary, sid).content_md == "Sky: unrelated imported quotation."


def test_bound_generated_recording_title_follows_rename_and_keeps_manual_title(isolated):
    sid = seed("Speaker 1 decided", bind=True, title="Speaker 1 planning discussion")
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        row.generated_title = "Speaker 1 planning discussion"
        row.generated_title_provider = "fake"
        row.generated_title_model = "model"
        row.local_title = "My personal meeting label"
    result = rename("Sky")
    assert result["recording_title_changed"] is True
    assert result["recording_title"] == "Sky planning discussion"
    assert result["display_title"] == "My personal meeting label"
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        assert row.local_title == "My personal meeting label"
        assert row.generated_title == session.get(Summary, sid).title
        assert session.scalar(select(SummaryRevision)).title == "Speaker 1 planning discussion"
        row.local_title = None
    assert rename("River")["display_title"] == "River planning discussion"
    assert rename(None)["display_title"] == "Speaker 1 planning discussion"


@pytest.mark.parametrize(
    "title,provider,model",
    [
        ("An unrelated recording title", "fake", "model"),
        ("Speaker 1 planning discussion", "another-provider", "model"),
        ("Speaker 1 planning discussion", "fake", "another-model"),
        ("Speaker 1 planning discussion", None, None),
    ],
)
def test_unproven_recording_title_is_never_guessed(isolated, title, provider, model):
    seed("Speaker 1 decided", bind=True, title="Speaker 1 planning discussion")
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        row.generated_title = title
        row.generated_title_provider = provider
        row.generated_title_model = model
    result = rename("Sky")
    assert result["recording_title_changed"] is False
    assert result["recording_title"] == title


def test_mindmap_title_cannot_rename_recording(isolated):
    seed(
        "- Speaker 1 decided", bind=True, title="Speaker 1 planning discussion", template="mind_map"
    )
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        row.generated_title = "Speaker 1 planning discussion"
        row.generated_title_provider = "fake"
        row.generated_title_model = "model"
    assert rename("Sky")["recording_title_changed"] is False


def test_duplicate_title_sources_do_not_guess_recording_title_owner(isolated):
    seed("Speaker 1 decided", bind=True, title="Speaker 1 planning discussion")
    seed(
        "Speaker 1 decided", bind=True, title="Speaker 1 planning discussion", template="secondary"
    )
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        row.generated_title = "Speaker 1 planning discussion"
        row.generated_title_provider = "fake"
        row.generated_title_model = "model"
    assert rename("Sky")["recording_title_changed"] is False


def test_migration_derived_note_does_not_acquire_local_speaker_bindings(isolated):
    sid = seed("SPEAKER_00: cloud-derived note")
    with session_scope() as session:
        summary = session.get(Summary, sid)
        summary.input_transcript_source = "cloud"
        bind_generated_summary(session, summary)
        assert "speaker_bindings" not in summary.template_snapshot
    assert rename("Sky")["changed_ids"] == []
    with session_scope() as session:
        assert session.get(Summary, sid).content_md == "SPEAKER_00: cloud-derived note"
