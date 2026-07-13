"""Mind map stage: full-transcript outline generation, defensive parsing,
durable pipeline persistence, degradable failure, UI tab, and export."""

from __future__ import annotations

import pytest

from localplaud.asr.base import Segment, Transcript
from localplaud.worker.mindmap import _normalize_outline, generate_mind_map


def _transcript(*segs: Segment) -> Transcript:
    return Transcript(segments=list(segs))


# --------------------------------------------------------------------------- #
# _normalize_outline — defensive parsing
# --------------------------------------------------------------------------- #


def test_normalize_strips_code_fences():
    raw = "```markdown\n# Root\n- a\n  - b\n```"
    assert _normalize_outline(raw) == "# Root\n- a\n  - b"


def test_normalize_keeps_wellformed_outline():
    raw = "# Root\n- a\n  - b\n    - c"
    assert _normalize_outline(raw) == raw


def test_normalize_adds_root_when_missing():
    out = _normalize_outline("- a\n  - b")
    assert out.splitlines()[0] == "# Mind map"
    assert out.splitlines()[1] == "- a"


def test_normalize_wraps_prose_lines_as_bullets():
    out = _normalize_outline("Topic one\nTopic two")
    assert out == "# Mind map\n- Topic one\n- Topic two"


def test_normalize_converts_extra_headings_to_bullets():
    out = _normalize_outline("# Root\n## Section\n- a\n### Sub\n- b")
    lines = out.splitlines()
    assert lines[0] == "# Root"
    assert "- Section" in lines
    assert "  - Sub" in lines  # one level deeper than an H2


def test_normalize_rejects_empty_output():
    with pytest.raises(ValueError):
        _normalize_outline("")
    with pytest.raises(ValueError):
        _normalize_outline("```\n\n```")


# --------------------------------------------------------------------------- #
# generate_mind_map — coverage and metadata
# --------------------------------------------------------------------------- #


class _FakeLlm:
    def __init__(self, final: str = "# Root topic\n- a\n  - b\n- c"):
        self.calls: list[str] = []
        self.options: list[dict] = []
        self.final = final

    def complete(self, prompt, **kwargs):
        self.calls.append(prompt)
        self.options.append(kwargs)
        if prompt.startswith("Extract hierarchical outline notes"):
            return f"- outline note {len(self.calls)}"
        if prompt.startswith("Consolidate these ordered outline notes"):
            return "- consolidated"
        return self.final


def test_long_transcript_outline_covers_every_chunk(monkeypatch):
    from localplaud.config import Settings

    llm = _FakeLlm()
    monkeypatch.setattr("localplaud.worker.mindmap.build_llm", lambda cfg: llm)
    transcript = _transcript(
        *(
            Segment(text=f"segment-{idx}-" + chr(65 + idx) * 35, start=idx, end=idx + 1)
            for idx in range(4)
        )
    )
    settings = Settings(pipeline={"summary_chunk_chars": 50})
    result = generate_mind_map(transcript, settings, None)

    map_prompts = [p for p in llm.calls if p.startswith("Extract hierarchical outline notes")]
    assert result["detail"]["strategy"] == "hierarchical"
    assert result["detail"]["chunks"] > 1
    assert len(map_prompts) == result["detail"]["chunks"]
    assert "[truncated]" not in "".join(map_prompts)
    reduce_options = [
        options
        for prompt, options in zip(llm.calls, llm.options, strict=True)
        if prompt.startswith("Consolidate these ordered outline notes")
    ]
    assert reduce_options and all(call["max_tokens"] == 32 for call in reduce_options)
    # Every chunk of the transcript reached the LLM.
    joined = "".join(map_prompts)
    for idx in range(4):
        assert f"segment-{idx}-" in joined
    assert result["template"] == "mind_map"
    assert result["title"] is None
    assert result["content_md"].startswith("# Root topic")
    assert result["detail"]["outline_nodes"] == 4  # root + three bullets


def test_summary_context_is_passed_to_final_prompt(monkeypatch):
    from localplaud.config import Settings

    llm = _FakeLlm()
    monkeypatch.setattr("localplaud.worker.mindmap.build_llm", lambda cfg: llm)
    transcript = _transcript(Segment(text="short recording", start=0.0, end=1.0))
    result = generate_mind_map(transcript, Settings(), "# Existing summary\n- key point")

    assert result["detail"]["strategy"] == "direct"
    final = llm.calls[-1]
    assert "Existing summary" in final
    assert "short recording" in final


def test_fallback_wraps_malformed_llm_output(monkeypatch):
    from localplaud.config import Settings

    llm = _FakeLlm(final="```\nJust some prose.\nAnother line.\n```")
    monkeypatch.setattr("localplaud.worker.mindmap.build_llm", lambda cfg: llm)
    transcript = _transcript(Segment(text="hello", start=0.0, end=1.0))
    result = generate_mind_map(transcript, Settings(), None)

    assert result["content_md"] == "# Mind map\n- Just some prose.\n- Another line."
    assert result["detail"]["outline_nodes"] == 3


# --------------------------------------------------------------------------- #
# pipeline stage — persistence, resume, degradable failure
# --------------------------------------------------------------------------- #


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'mm.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")  # skip ffmpeg
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__POLISH", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def _install_fakes(monkeypatch, counters):
    def fake_asr(wav, settings):
        counters["asr"] += 1
        return Transcript(
            segments=[Segment(text="hello world", start=0.0, end=1.0, speaker="SPEAKER_00")],
            language="en",
            provider="fake",
            has_speakers=True,
        )

    def fake_summary(transcript, settings):
        counters["sum"] += 1
        return {
            "title": "T",
            "content_md": "# T\n\nbody",
            "provider": "fake",
            "model": "m",
            "template": settings.pipeline.summary_template,
        }

    def fake_mindmap(transcript, settings, summary_md=None):
        counters["mm"] += 1
        counters["mm_summary_md"] = summary_md
        return {
            "template": "mind_map",
            "title": None,
            "content_md": "# Root\n- a\n  - b",
            "provider": "fake",
            "model": "mm-model",
            "detail": {"strategy": "direct", "outline_nodes": 3},
        }

    def fake_embed(chunks, settings):
        counters["emb"] += 1
        return [b"\x00\x00\x80?" for _ in chunks], "fake", 1

    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr", fake_asr)
    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
    monkeypatch.setattr("localplaud.worker.pipeline.mindmap.generate_mind_map", fake_mindmap)
    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", fake_embed)


def _seed_file(tmp_path, file_id):
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    audio = tmp_path / f"{file_id}.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.add(PlaudFile(id=file_id, status=FileStatus.downloaded, audio_path=str(audio)))


def test_pipeline_persists_mind_map_and_resumes(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import process_file

    _seed_file(tmp_path, "mm1")
    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    process_file("mm1")
    assert counters["mm"] == 1
    # The local summary produced earlier in the run is offered as context.
    assert counters["mm_summary_md"] == "# T\n\nbody"
    with session_scope() as s:
        f = s.get(PlaudFile, "mm1")
        assert f.status == FileStatus.done
        mm = next(x for x in f.summaries if x.template == "mind_map")
        assert mm.source == "local"
        assert mm.title is None
        assert mm.content_md == "# Root\n- a\n  - b"
        assert mm.llm_provider == "fake" and mm.model == "mm-model"
        run = next(x for x in f.stage_runs if x.stage == StageName.mind_map)
        assert run.status == StageStatus.completed
        assert run.attempts == 1
        assert run.provider == "fake" and run.model == "mm-model"
        assert run.artifact_source == "local"
        assert run.detail["outline_nodes"] == 3

    # Resume: the existing local mind map is reused without regeneration.
    process_file("mm1")
    assert counters["mm"] == 1
    with session_scope() as s:
        run = next(x for x in s.get(PlaudFile, "mm1").stage_runs if x.stage == StageName.mind_map)
        assert run.detail == {"reused": True}

    # A different notes template changes the mind-map input even when the
    # mind-map provider selection itself is unchanged.
    with session_scope() as s:
        s.get(PlaudFile, "mm1").note_template_key = "meeting"
    process_file("mm1")
    assert counters["mm"] == 2
    with session_scope() as s:
        mind_map = next(
            item for item in s.get(PlaudFile, "mm1").summaries
            if item.template == "mind_map"
        )
        assert mind_map.template_snapshot["source_template_key"] == "meeting"

    from localplaud.db.models import NoteTemplate

    with session_scope() as s:
        meeting = s.query(NoteTemplate).filter_by(key="meeting", is_active=True).one()
        meeting.is_active = False
        s.add(
            NoteTemplate(
                key="meeting",
                version=meeting.version + 1,
                name=meeting.name,
                system_prompt=meeting.system_prompt,
                instructions=meeting.instructions + "\n\n## New section",
                is_active=True,
            )
        )
    process_file("mm1")
    assert counters["mm"] == 3
    with session_scope() as s:
        mind_map = next(
            item for item in s.get(PlaudFile, "mm1").summaries
            if item.template == "mind_map"
        )
        assert mind_map.template_snapshot["source_template_version"] == 2

    # Force: the mind map is rebuilt.
    process_file("mm1", force=True)
    assert counters["mm"] == 4


def test_mind_map_failure_leaves_partial_file_with_usable_artifacts(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import process_file

    _seed_file(tmp_path, "mm-fail")
    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    def fail_mindmap(transcript, settings, summary_md=None):
        raise RuntimeError("LLM unavailable for mind map")

    monkeypatch.setattr("localplaud.worker.pipeline.mindmap.generate_mind_map", fail_mindmap)
    process_file("mm-fail")

    with session_scope() as s:
        f = s.get(PlaudFile, "mm-fail")
        assert f.status == FileStatus.partial
        assert "mind_map: LLM unavailable for mind map" in f.error
        # Transcript, notes, and index all survive the mind map failure.
        assert f.local_transcript is not None
        assert [x.template for x in f.summaries] == ["default"]
        assert len(f.chunks) == 1
        run = next(x for x in f.stage_runs if x.stage == StageName.mind_map)
        assert run.status == StageStatus.failed
        assert "LLM unavailable for mind map" in run.error


def test_mind_map_stage_can_be_disabled(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", "false")
    from localplaud.config import get_settings

    get_settings(reload=True)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import process_file

    _seed_file(tmp_path, "mm-off")
    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    process_file("mm-off")
    assert counters["mm"] == 0
    with session_scope() as s:
        f = s.get(PlaudFile, "mm-off")
        assert f.status == FileStatus.done
        assert all(x.template != "mind_map" for x in f.summaries)
        run = next(x for x in f.stage_runs if x.stage == StageName.mind_map)
        assert run.status == StageStatus.skipped
        assert run.detail == {"reason": "disabled"}


# --------------------------------------------------------------------------- #
# Web UI + export
# --------------------------------------------------------------------------- #


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'ui.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed_ui():
    from localplaud.db.models import FileStatus, PlaudFile, Summary
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(
            PlaudFile(
                id="r1",
                filename="Weekly Sync",
                status=FileStatus.done,
                duration_ms=600000,
                start_time_ms=1783582737000,
            )
        )
        s.add(
            TranscriptRow(
                file_id="r1",
                provider="faster-whisper",
                language="en",
                has_speakers=True,
                text="hi",
                segments=[
                    {"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}
                ],
            )
        )
        s.add(
            Summary(file_id="r1", template="meeting", title="Sync", content_md="# Sync\n\n- point")
        )
        s.add(
            Summary(
                file_id="r1",
                template="mind_map",
                title=None,
                content_md="# Sync topics\n- agenda\n  - budget\n- decisions",
            )
        )


def test_detail_page_renders_mind_map_tab(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_ui()
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "Mind map" in r.text
    assert 'data-panel="mindmap"' in r.text
    assert 'id="mindmap-src"' in r.text and "Sync topics" in r.text
    assert "Expand all" in r.text and "Collapse all" in r.text
    # The mind map is excluded from the generic summary tabs.
    assert "Mind_map" not in r.text
    assert 'data-panel="sum-0"' in r.text  # the meeting note keeps its tab
    assert 'data-panel="sum-1"' not in r.text


def test_export_markdown_includes_mind_map_before_transcript(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)  # sets up the DB for the exporter too
    _seed_ui()
    from localplaud.exporter import render_markdown

    md = render_markdown("r1")
    assert "## Mind map" in md
    assert "### Sync topics" in md  # root demoted under the section heading
    assert "  - budget" in md
    assert md.index("## Mind map") < md.index("## Transcript")
    # The generic notes section does not duplicate the outline.
    assert "## mind_map" not in md


def test_mind_map_png_export_contains_complete_tree(monkeypatch, tmp_path):
    import io

    from PIL import Image

    c = _client(monkeypatch, tmp_path)
    _seed_ui()
    response = c.get("/file/r1/export/mind-map.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == 'attachment; filename="r1-mind-map.png"'
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    image = Image.open(io.BytesIO(response.content))
    assert image.width == 1400
    assert image.height > 200


def test_mind_map_png_export_requires_local_mind_map(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="cloud-map", filename="Cloud map"))
        session.add(
            Summary(
                file_id="cloud-map",
                template="mind_map",
                content_md="# Imported\n- not local",
                source="plaud",
            )
        )
    assert c.get("/file/cloud-map/export/mind-map.png").status_code == 409
