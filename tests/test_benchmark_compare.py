"""Candidate-vs-baseline and host-vs-host speech comparisons; synthetic inputs only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from localplaud import benchmark_compare as compare
from localplaud import benchmark_speech as speech
from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import Settings


def _manifest(tmp_path: Path, source: str = "owned") -> Path:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(exist_ok=True)
    rows = []
    for identity, text in (("meeting-a", "我們下週三 release 新版本"), ("meeting-b", "預算上限三百萬")):
        (audio_dir / f"{identity}.wav").write_bytes(b"synthetic")
        row = {"id": identity, "audio": f"audio/{identity}.wav", "source": source,
               "reference": {"text": text, "speaker_turns": [{"start": 0, "end": 4, "speaker": "A"}]}}
        if source == "owned":
            row["owned"] = True
        if source == "public":
            row["license"] = "CC-BY-4.0"
        rows.append(row)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "recordings": rows}))
    return path


def _executor(texts: dict[str, str], *, elapsed=1.0, speaker="S1", shift=0.0):
    def run(audio, settings, alignment):
        text = texts[audio.stem]
        tokens = speech.mixed_tokens(text)
        step = 4 / max(1, len(tokens))
        words = [Word(text=t, start=i * step + shift, end=(i + 1) * step + shift, speaker=speaker)
                 for i, t in enumerate(tokens)]
        transcript = Transcript(segments=[Segment(text=text, start=0, end=4, speaker=speaker, words=words)],
                                duration=4.0, provider="fake", model="turbo", has_speakers=True)
        return transcript, 4.0, {"elapsed_seconds": elapsed, "peak_memory_bytes": 1024, "stages": {}}
    return run


def _run(tmp_path, name, texts, source="owned", **kwargs):
    manifest = _manifest(tmp_path, source)
    settings, align, provenance = speech.configuration(Settings(_env_file=None))
    hypotheses = tmp_path / f"{name}-hyp"
    report = speech.benchmark(manifest, settings, align, provenance, executor=_executor(texts, **kwargs),
                              hypotheses=hypotheses)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False))
    return path, hypotheses, report


GOOD = {"meeting-a": "我們下週三 release 新版本", "meeting-b": "預算上限三百萬"}
WORSE = {"meeting-a": "我們下週 release 新版", "meeting-b": "預算上限三百萬"}


def test_report_records_source_and_saves_hypotheses(tmp_path):
    _, hypotheses, report = _run(tmp_path, "base", GOOD, source="synthetic")
    assert report["sources"] == ["synthetic"]
    assert {r["source"] for r in report["recordings"]} == {"synthetic"}
    saved = json.loads((hypotheses / "meeting-a.json").read_text())
    assert saved["schema"] == speech.HYPOTHESIS_SCHEMA
    assert saved["segments"][0]["words"][0]["speaker"] == "S1"


def test_manifest_source_rules(tmp_path):
    path = _manifest(tmp_path, "public")
    data = json.loads(path.read_text())
    del data["recordings"][0]["license"]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="license"):
        speech.load_manifest(path)
    data["recordings"][0]["source"] = "scraped"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="source"):
        speech.load_manifest(path)


def test_identical_runs_are_not_worse_and_comparable(tmp_path):
    a, ha, _ = _run(tmp_path, "a", GOOD)
    b, hb, _ = _run(tmp_path, "b", GOOD)
    result = compare.compare(baseline=a, candidate=b, hypotheses_a=ha, hypotheses_b=hb)
    assert result["passed"]
    assert result["reports"]["verdict"] == "not-worse"
    assert result["reports"]["evidence"] == "owned"
    host = result["hosts"]["recordings"][0]
    assert host["cross_host_cer"] == 0 and host["timestamp_mae_seconds"] == 0
    assert host["speaker_disagreement"] == 0
    assert host["contract"]["a"]["word_timing_coverage"] == 1.0


def test_candidate_regression_and_slowdown_fail(tmp_path):
    a, _, _ = _run(tmp_path, "a", GOOD)
    b, _, _ = _run(tmp_path, "b", WORSE, elapsed=3.0)
    part = compare.compare(baseline=a, candidate=b)["reports"]
    assert part["verdict"] == "regression"
    assert part["deltas"]["cer"] > 0
    assert part["deltas"]["rtf_ratio"] == pytest.approx(3.0)
    assert any(f.startswith("meeting-a CER increase") for f in part["failures"])
    assert any(f.startswith("RTF ratio") for f in part["failures"])


def test_synthetic_cohort_is_tooling_evidence_only(tmp_path):
    a, _, _ = _run(tmp_path, "a", GOOD, source="synthetic")
    b, _, _ = _run(tmp_path, "b", GOOD, source="synthetic")
    part = compare.compare(baseline=a, candidate=b)["reports"]
    assert part["verdict"] == "not-worse"
    assert part["evidence"] == "tooling-only"


def test_host_divergence_in_timing_text_and_speakers(tmp_path):
    _, ha, _ = _run(tmp_path, "mac", GOOD)
    _, hb, _ = _run(tmp_path, "wsl", WORSE, shift=0.6)
    hosts = compare.compare(hypotheses_a=ha, hypotheses_b=hb)["hosts"]
    assert hosts["verdict"] == "diverged"
    row = next(r for r in hosts["recordings"] if r["id"] == "meeting-a")
    assert row["cross_host_cer"] > 0.05
    assert any("cross-host CER" in p for p in row["problems"])
    same_text = next(r for r in hosts["recordings"] if r["id"] == "meeting-b")
    assert same_text["cross_host_cer"] == 0
    assert same_text["timestamp_mae_seconds"] == pytest.approx(0.6)
    assert any("timestamp MAE" in p for p in same_text["problems"])


def test_host_without_speakers_breaks_the_contract(tmp_path):
    _, ha, _ = _run(tmp_path, "mac", GOOD)
    _, hb, _ = _run(tmp_path, "wsl", GOOD, speaker=None)
    row = compare.compare(hypotheses_a=ha, hypotheses_b=hb)["hosts"]["recordings"][0]
    assert row["contract"]["b"]["speaker_coverage"] == 0
    assert any("host B speaker coverage" in p for p in row["problems"])


def test_argument_pairs_are_required(tmp_path):
    with pytest.raises(ValueError, match="go together"):
        compare.compare(baseline=tmp_path / "a.json")
    with pytest.raises(ValueError, match="give two reports"):
        compare.compare()


def test_cli_exit_status_and_output(tmp_path):
    from localplaud import cli

    a, ha, _ = _run(tmp_path, "a", GOOD)
    b, hb, _ = _run(tmp_path, "b", WORSE)
    out = tmp_path / "comparison.json"
    ok = CliRunner().invoke(cli.app, ["benchmark-speech-compare", "--baseline", str(a), "--candidate", str(a),
                                      "--hypotheses-a", str(ha), "--hypotheses-b", str(ha), "--output", str(out)])
    assert ok.exit_code == 0, ok.output
    assert json.loads(out.read_text())["schema"] == compare.COMPARISON_SCHEMA
    assert "NOT-WORSE" in ok.stdout and "COMPARABLE" in ok.stdout
    bad = CliRunner().invoke(cli.app, ["benchmark-speech-compare", "--baseline", str(a), "--candidate", str(b)])
    assert bad.exit_code == 1
    clobber = CliRunner().invoke(cli.app, ["benchmark-speech-compare", "--baseline", str(a), "--candidate", str(b),
                                           "--output", str(a)])
    assert clobber.exit_code != 0 and "overwrite" in clobber.output
