"""Auto-tagging: JSON parsing, extraction, and provenance-safe application."""

from __future__ import annotations


def _init_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'tags.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()


def test_parse_json_handles_fences_and_noise():
    from localplaud.worker.tagging import _parse_json

    assert _parse_json('{"topics":["a"]}') == {"topics": ["a"]}
    assert _parse_json('```json\n{"topics":["a"]}\n```') == {"topics": ["a"]}
    assert _parse_json('Here you go: {"topics":["a"]} thanks') == {"topics": ["a"]}
    assert _parse_json("not json") == {}
    assert _parse_json("") == {}


def test_clean_strips_markup_and_caps():
    from localplaud.worker.tagging import _clean

    assert _clean("  # 「口琴社」 ") == "口琴社"
    assert _clean("  ") is None
    assert _clean(None) is None
    assert len(_clean("x" * 200)) == 80
    # Anonymous diarization labels are rejected (LLM mistakes them for people).
    assert _clean("SPEAKER_00") is None
    assert _clean("Speaker 1") is None
    assert _clean("語者2") is None
    assert _clean("冠子老師") == "冠子老師"


def test_extract_tags_dedups_caps_and_types(monkeypatch):
    from localplaud.worker import tagging
    from localplaud.config import get_settings

    class _FakeLLM:
        def complete(self, *a, **k):
            return (
                '{"topics":["Planning","planning","T2","T3","T4","T5","T6"],'
                '"people":["Alice","Bob"],"orgs":["Acme Inc"]}'
            )

    monkeypatch.setattr(tagging, "build_llm", lambda cfg: _FakeLLM())
    out = tagging.extract_tags("some summary", get_settings())
    assert out["topic"][:1] == ["Planning"]
    assert "planning" not in out["topic"]  # case-insensitive dedup
    assert len(out["topic"]) == 5  # capped
    assert out["person"] == ["Alice", "Bob"]
    assert out["org"] == ["Acme Inc"]


def test_extract_tags_survives_llm_failure(monkeypatch):
    from localplaud.worker import tagging
    from localplaud.config import get_settings

    class _Boom:
        def complete(self, *a, **k):
            raise RuntimeError("llm down")

    monkeypatch.setattr(tagging, "build_llm", lambda cfg: _Boom())
    assert tagging.extract_tags("x", get_settings()) == {}


def test_apply_tags_creates_typed_tags_and_gates(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, Tag
    from localplaud.db.session import session_scope
    from localplaud.worker.tagging import apply_tags

    with session_scope() as session:
        session.add(PlaudFile(id="rec", filename="a"))

    with session_scope() as session:
        r = apply_tags(
            session, "rec",
            {"topic": ["Planning"], "person": ["Alice"], "org": ["Acme"]},
        )
        assert r["applied"] == 3

    with session_scope() as session:
        row = session.get(PlaudFile, "rec")
        kinds = {t.kind for t in row.tags}
        assert kinds == {"topic", "person", "org"}
        assert row.auto_tagged_at is not None
        # Colours assigned per kind
        planning = next(t for t in row.tags if t.name == "Planning")
        assert planning.color

    # Second run is gated (run-once), does not duplicate.
    with session_scope() as session:
        r2 = apply_tags(session, "rec", {"topic": ["Another"]})
        assert r2["applied"] == 0
        assert session.get(PlaudFile, "rec").tags  # unchanged count below
    with session_scope() as session:
        assert len(session.get(PlaudFile, "rec").tags) == 3


def test_apply_tags_reuses_existing_and_preserves_manual(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, Tag
    from localplaud.db.session import session_scope
    from localplaud.worker.tagging import apply_tags

    with session_scope() as session:
        rec = PlaudFile(id="rec", filename="a")
        manual = Tag(name="Keep Me", kind="custom")
        shared = Tag(name="planning", kind="topic")  # pre-existing, lowercased
        rec.tags.append(manual)
        session.add_all([rec, shared])

    with session_scope() as session:
        apply_tags(session, "rec", {"topic": ["Planning"]})  # should reuse "planning"

    with session_scope() as session:
        from sqlalchemy import select

        # No duplicate "planning" tag was created (case-insensitive reuse).
        planning_tags = session.scalars(
            select(Tag).where(Tag.name.in_(["planning", "Planning"]))
        ).all()
        assert len(planning_tags) == 1
        names = {t.name for t in session.get(PlaudFile, "rec").tags}
        assert "Keep Me" in names  # manual tag preserved
        assert "planning" in names
