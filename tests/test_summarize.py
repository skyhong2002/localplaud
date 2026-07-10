"""Summarize-stage helpers: transcript rendering and title extraction."""

from __future__ import annotations

from localplaud.asr.base import Segment, Transcript
from localplaud.worker.summarize import _extract_title, _render_transcript


def _transcript(*segs: Segment) -> Transcript:
    return Transcript(segments=list(segs))


# --------------------------------------------------------------------------- #
# _render_transcript
# --------------------------------------------------------------------------- #


def test_render_prefixes_speaker_labels():
    t = _transcript(
        Segment(text="hello there", start=0.0, end=1.0, speaker="SPEAKER_00"),
        Segment(text="hi back", start=1.0, end=2.0, speaker="SPEAKER_01"),
    )
    assert _render_transcript(t) == "SPEAKER_00: hello there\nSPEAKER_01: hi back"


def test_render_omits_prefix_when_no_speaker():
    t = _transcript(
        Segment(text="unlabelled", start=0.0, end=1.0),
        Segment(text="labelled", start=1.0, end=2.0, speaker="SPEAKER_00"),
    )
    lines = _render_transcript(t).splitlines()
    assert lines[0] == "unlabelled"  # no ": " prefix, no stray colon
    assert lines[1] == "SPEAKER_00: labelled"


def test_render_strips_segment_whitespace():
    t = _transcript(Segment(text="  padded text \n", start=0.0, end=1.0, speaker="A"))
    assert _render_transcript(t) == "A: padded text"


def test_render_truncates_at_max_chars_with_marker():
    t = _transcript(Segment(text="x" * 500, start=0.0, end=1.0))
    out = _render_transcript(t, max_chars=100)
    assert out.startswith("x" * 100)
    assert out.endswith("\n...[truncated]")
    # Only the marker follows the cut — nothing from the tail leaks through.
    assert out == "x" * 100 + "\n...[truncated]"


def test_render_no_marker_when_under_limit():
    t = _transcript(Segment(text="short", start=0.0, end=1.0))
    out = _render_transcript(t, max_chars=100)
    assert out == "short"
    assert "[truncated]" not in out


def test_render_empty_transcript():
    assert _render_transcript(_transcript()) == ""


# --------------------------------------------------------------------------- #
# _extract_title
# --------------------------------------------------------------------------- #


def test_extract_title_first_h1():
    md = "# Weekly Sync\n\n## Summary\nStuff happened.\n# Second Heading\n"
    assert _extract_title(md) == "Weekly Sync"


def test_extract_title_skips_leading_noise_and_strips():
    md = "\nsome preamble\n   #  Spaced Title   \nbody\n"
    assert _extract_title(md) == "Spaced Title"


def test_extract_title_ignores_deeper_headings():
    assert _extract_title("## Summary\n- point\n### Sub\n") is None


def test_extract_title_none_when_absent():
    assert _extract_title("plain text without headings") is None
    assert _extract_title("") is None
