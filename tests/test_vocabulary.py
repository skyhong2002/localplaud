from __future__ import annotations

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.db.models import VocabularyTerm
from localplaud.worker.correct import apply_vocabulary


def test_vocabulary_corrects_mandarin_and_english_without_mutating_raw():
    raw = Transcript(language="zh", segments=[Segment(
        text="今天和 sky hong 討論台灣口琴",
        start=0, end=2,
        words=[Word(text="sky hong", start=0.5, end=1.0)],
    )])
    terms = [VocabularyTerm(term="sky hong", replacement="Sky Hong"),
             VocabularyTerm(term="台灣", replacement="臺灣", case_sensitive=True)]
    corrected = apply_vocabulary(raw, terms)
    assert corrected.text == "今天和 Sky Hong 討論臺灣口琴"
    assert corrected.segments[0].words[0].text == "Sky Hong"
    assert raw.text == "今天和 sky hong 討論台灣口琴"


def test_vocabulary_pipeline_creates_revision_and_preserves_user_edit(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile, TranscriptRevision
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.correct import run

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'vocab.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="r1"))
        session.add(TranscriptRow(file_id="r1", provider="fake", source="local",
            text="hello sky", segments=[{"text": "hello sky", "start": 0, "end": 1,
                                          "speaker": None, "words": []}]))
        session.add(VocabularyTerm(term="sky", replacement="Sky Hong"))
    transcript = Transcript(segments=[Segment(text="hello sky", start=0, end=1)], provider="fake")
    corrected, detail = run("r1", transcript)
    assert detail["changed"] is True
    assert corrected.text == "hello Sky Hong"
    with session_scope() as session:
        revision = session.get(PlaudFile, "r1").corrected_transcript
        assert revision.kind == "vocabulary"
        assert revision.text == "hello Sky Hong"
        session.add(TranscriptRevision(file_id="r1", revision=2, source="local", kind="user",
            text="my manual text", segments=[{"text": "my manual text", "start": 0, "end": 1}],
            has_speakers=False))
    _, detail = run("r1", transcript)
    assert detail["reason"] == "user revision preserved"
