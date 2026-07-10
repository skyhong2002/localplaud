"""Safe transition from legacy Plaud-derived rows to independent processing."""

from __future__ import annotations


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def test_prepare_independent_mode_preserves_cloud_and_requeues(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.migrations import prepare_independent_mode
    from localplaud.db.models import Chunk, FileStatus, PlaudFile, Summary, Transcript
    from localplaud.db.session import get_engine, init_db, session_scope

    init_db()
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3")
    with session_scope() as s:
        file = PlaudFile(
            id="legacy",
            status=FileStatus.done,
            audio_path=str(audio),
            transcripts=[
                Transcript(
                    provider="plaud",
                    source="cloud",
                    text="cloud transcript",
                    segments=[{"text": "cloud transcript", "start": 0.0, "end": 1.0}],
                )
            ],
        )
        file.summaries.extend(
            [
                Summary(template="default", source="local", content_md="legacy derived note"),
                Summary(template="plaud", source="cloud", content_md="Plaud note"),
            ]
        )
        file.chunks.append(Chunk(idx=0, text="cloud-derived index"))
        s.add(file)

    counts = prepare_independent_mode(get_engine(), force=True)
    assert counts == {"files": 1, "summaries": 1, "chunks": 1, "requeued": 1}

    with session_scope() as s:
        file = s.get(PlaudFile, "legacy")
        assert file.status == FileStatus.downloaded
        assert [(t.source, t.text) for t in file.transcripts] == [
            ("cloud", "cloud transcript")
        ]
        assert file.transcript is not None and file.transcript.source == "cloud"
        assert len(file.chunks) == 0
        by_source = {summary.source: summary for summary in file.summaries}
        assert by_source["cloud"].template == "plaud"
        assert by_source["legacy"].template.startswith("legacy-cloud-default")
        assert by_source["legacy"].content_md == "legacy derived note"

    # The normal startup path is marker-gated and does not keep resetting errors.
    assert prepare_independent_mode(get_engine()) == {
        "files": 0,
        "summaries": 0,
        "chunks": 0,
        "requeued": 0,
    }
