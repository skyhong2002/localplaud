"""segments_to_json: dataclass Transcript -> plain JSON-able dicts."""

from __future__ import annotations

import json

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.worker.transcribe import segments_to_json


def _transcript() -> Transcript:
    return Transcript(
        segments=[
            Segment(
                text="hello world",
                start=0.0,
                end=1.5,
                speaker="SPEAKER_00",
                words=[
                    Word(text="hello", start=0.0, end=0.6, speaker="SPEAKER_00", confidence=0.98),
                    Word(text="world", start=0.7, end=1.5, speaker="SPEAKER_00"),
                ],
            ),
            Segment(text="bye", start=2.0, end=2.5),
        ],
        language="en",
        provider="dummy",
    )


def test_segments_to_json_shape_and_keys():
    out = segments_to_json(_transcript())

    assert isinstance(out, list) and len(out) == 2
    for seg in out:
        assert isinstance(seg, dict)
        assert set(seg) == {"text", "start", "end", "speaker", "words"}

    first = out[0]
    assert first["text"] == "hello world"
    assert first["start"] == 0.0
    assert first["end"] == 1.5
    assert first["speaker"] == "SPEAKER_00"

    # Words are recursively converted to plain dicts (asdict), not Word objects.
    assert all(isinstance(w, dict) for w in first["words"])
    assert set(first["words"][0]) == {"text", "start", "end", "speaker", "confidence"}
    assert first["words"][0]["confidence"] == 0.98
    assert first["words"][1]["confidence"] is None  # default survives


def test_segments_to_json_defaults_for_bare_segment():
    out = segments_to_json(_transcript())
    bare = out[1]
    assert bare["speaker"] is None
    assert bare["words"] == []


def test_segments_to_json_round_trips():
    original = _transcript()
    out = segments_to_json(original)

    # Fully JSON-serialisable (this is what gets persisted to the DB).
    restored_raw = json.loads(json.dumps(out))

    rebuilt = [
        Segment(**{**seg, "words": [Word(**w) for w in seg["words"]]})
        for seg in restored_raw
    ]
    assert rebuilt == original.segments
