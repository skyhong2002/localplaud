"""Below-threshold voice candidates surface as suggestions, never as names."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tests.test_speakers import _client, _mute_reindex, _seed


def _assignment(key, status, evidence, *, file_id="r1"):
    from localplaud.db.models import Speaker
    from localplaud.db.session import get_engine, session_scope
    from localplaud.voice_identity import VoiceAssignment, create_schema

    create_schema(get_engine())
    with session_scope() as s:
        speaker = s.scalar(select(Speaker).where(Speaker.file_id == file_id, Speaker.key == key))
        s.add(VoiceAssignment(speaker_id=speaker.id, file_id=file_id, speaker_key=key,
                              status=status, evidence=evidence))


def _suggestions(file_id="r1"):
    from localplaud.db.session import session_scope
    from localplaud.voice_suggestions import speaker_suggestions

    with session_scope() as s:
        return speaker_suggestions(s, file_id)


UNKNOWN = {"status": "unknown", "name": "Avery", "score": 0.6842, "runner_up": 0.5513,
           "votes": 1, "windows": 6, "threshold": 0.75}


def test_no_voice_tables_means_no_suggestions(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    assert _suggestions() == {}


def test_unknown_candidate_is_suggested_with_reason(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    _assignment("SPEAKER_00", "unknown", UNKNOWN)
    assert _suggestions() == {"SPEAKER_00": {
        "name": "Avery", "score": 0.6842, "runner_up": 0.5513, "margin": 0.1329,
        "threshold": 0.75, "status": "unknown", "reason": "below_threshold",
    }}


def test_ambiguous_candidate_is_suggested(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    _assignment("SPEAKER_01", "ambiguous", UNKNOWN | {"status": "ambiguous", "score": 0.81,
                                                      "runner_up": 0.79})
    result = _suggestions()["SPEAKER_01"]
    assert (result["status"], result["reason"]) == ("ambiguous", "ambiguous")


@pytest.mark.parametrize("evidence", [
    {}, {"name": "Avery"}, {"name": "Avery", "score": "0.7"}, {"name": "", "score": 0.7},
    {"name": "Speaker 2", "score": 0.7}, {"name": ["Avery"], "score": 0.7},
    {"name": "Avery", "score": float("nan")}, {"name": "Avery", "score": True}, [1, 2], None,
])
def test_malformed_evidence_is_no_suggestion(monkeypatch, tmp_path, evidence):
    _client(monkeypatch, tmp_path)
    _seed()
    _assignment("SPEAKER_00", "unknown", evidence)
    assert _suggestions() == {}


def test_applied_named_and_trashed_are_not_suggested(monkeypatch, tmp_path):
    from localplaud.db.models import PlaudFile, Speaker
    from localplaud.db.session import session_scope

    _client(monkeypatch, tmp_path)
    _seed()
    _assignment("SPEAKER_00", "applied", UNKNOWN | {"status": "matched"})
    _assignment("SPEAKER_01", "unknown", UNKNOWN)
    with session_scope() as s:
        s.scalar(select(Speaker).where(Speaker.key == "SPEAKER_01")).display_name = "Alex"
    assert _suggestions() == {}
    with session_scope() as s:
        s.scalar(select(Speaker).where(Speaker.key == "SPEAKER_01")).display_name = None
    assert set(_suggestions()) == {"SPEAKER_01"}
    with session_scope() as s:
        s.get(PlaudFile, "r1").is_trash = True
    assert _suggestions() == {}


def test_confirming_suggestion_is_recorded_as_user_confirmed(monkeypatch, tmp_path):
    from localplaud.db.models import Speaker
    from localplaud.db.session import session_scope
    from localplaud.voice_identity import VoiceAssignment, VoiceEvent, inventory

    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _seed()
    _assignment("SPEAKER_00", "unknown", UNKNOWN)
    _assignment("SPEAKER_01", "ambiguous", UNKNOWN | {"name": "Lin"})
    response = c.post(
        "/file/r1/speakers",
        data={"names": json.dumps({"SPEAKER_00": "Avery", "SPEAKER_01": "Lin Chen"}),
              "suggested": json.dumps(["SPEAKER_00", "SPEAKER_01"])},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200, response.text
    with session_scope() as s:
        rows = {r.speaker_key: r for r in s.scalars(select(VoiceAssignment))}
        # Edited away from the candidate: an ordinary manual name, not a confirmation.
        assert rows["SPEAKER_01"].status == "ambiguous"
        assert rows["SPEAKER_00"].status == "user_confirmed"
        assert rows["SPEAKER_00"].applied_name is None
        events = list(s.scalars(select(VoiceEvent)))
        assert [(e.action, e.detail["source"], e.detail["origin"]) for e in events] == [
            ("suggestion_confirmed", "user", "voice_suggestion")
        ]
        assert events[0].detail["suggestion"]["score"] == 0.6842
        speaker = s.scalar(select(Speaker).where(Speaker.key == "SPEAKER_00"))
        assert speaker.display_name == "Avery"
        # The identity service treats it as an explicit user name (not an inferred
        # "applied" match), so a refresh neither overrides nor un-names it.
        from localplaud.db.models import PlaudFile

        inventory(s, s.get(PlaudFile, "r1"))
        assert s.get(VoiceAssignment, speaker.id).status == "user_confirmed"
        assert [e.action for e in s.scalars(select(VoiceEvent))] == ["suggestion_confirmed"]
    assert _suggestions() == {}


def test_suggested_keys_must_be_part_of_the_save(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _seed()
    for suggested in ("not json", json.dumps({"a": 1}), json.dumps(["SPEAKER_01"])):
        response = c.post(
            "/file/r1/speakers",
            data={"names": json.dumps({"SPEAKER_00": "Avery"}), "suggested": suggested},
            headers={"accept": "application/json"},
        )
        assert response.status_code == 400


@pytest.mark.parametrize("locale, expected", [
    ("en", ["Possibly Avery · voice similarity 68%", "Below the auto-naming confidence threshold",
            "1 suggested name", 'aria-label="Use suggested name Avery"',
            "They are not confirmed identities"]),
    ("zh-Hant-TW", ["可能是 Avery · 聲音相似度 68%", "低於自動命名的信心門檻", "1 個建議名稱",
                    'aria-label="採用建議名稱 Avery"', "並非確認的身分"]),
])
def test_dialog_renders_suggestion_without_filling_the_field(monkeypatch, tmp_path, locale,
                                                             expected):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _assignment("SPEAKER_00", "unknown", UNKNOWN)
    with c:
        preferences = c.get("/api/preferences/workspace").json()
        assert c.put("/api/preferences/workspace",
                     json=preferences | {"locale": locale}).status_code == 200
        page = c.get("/file/r1").text
    for text in expected:
        assert text in page
    assert 'data-suggestion-for="SPEAKER_00" data-suggestion-name="Avery"' in page
    assert 'id="speaker-suggestion-hint"' in page
    assert 'name="SPEAKER_00" value=""' in page  # never silently auto-filled
    assert "/static/js/speaker-suggestions.js" in page


def test_no_hint_without_suggestions(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    page = c.get("/file/r1").text
    assert "speaker-suggestion-hint" not in page
    assert "data-suggestion-for" not in page
