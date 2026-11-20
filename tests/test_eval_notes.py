"""Note-fidelity scoring: answer-key coverage, reversals, attribution and tail loss."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from localplaud.eval_notes import evaluate, numbers, parse_transcript, score_note

FIXTURE = Path(__file__).parent / "fixtures" / "note_fidelity"


def _case(label: str) -> dict:
    report = evaluate(FIXTURE / "manifest.json")
    (case,) = report["cases"]
    assert case["source"] == "synthetic"
    return case["candidates"][label]


def test_faithful_note_passes_every_gate():
    result = _case("faithful")
    assert result["verdict"] == "pass", result
    metrics = result["metrics"]
    assert metrics["weighted_coverage"] == 1.0
    assert metrics["critical_coverage"] == 1.0
    assert metrics["third_coverage"] == {"early": 1.0, "middle": 1.0, "late": 1.0}
    assert metrics["contradictions"] == metrics["leaks"] == metrics["action_attribution_errors"] == 0
    assert result["unsupported_numbers"] == []
    assert all(row["third"] for row in result["items"])


def test_flawed_note_reports_each_planted_defect():
    result = _case("flawed")
    assert result["verdict"] == "fail"
    metrics = result["metrics"]
    # The tail of the meeting (crash target, FAQ, beta) is missing entirely.
    assert metrics["third_coverage"]["late"] == 0.0
    assert {"crash-target", "faq-owner", "beta-open", "no-dark-mode", "budget-cap"} <= set(result["missed"])
    assert metrics["critical_coverage"] < 1.0
    assert [c["id"] for c in result["contradictions"]] == ["dark-mode-reversed"]
    assert "請依照範本" in result["leaks"]
    # The design hand-off is credited to the wrong person and has no deadline.
    design = next(row for row in result["items"] if row["id"] == "design-handoff")
    assert design["covered"] and design["attribution"] == {"owner": False, "deadline": False}
    assert metrics["action_attribution_errors"] == 2
    assert result["unsupported_numbers"] == ["5000000"]
    assert metrics["repeated_lines"] == 1
    assert any(failure.startswith("late_coverage") for failure in result["failures"])


def test_numbers_fold_chinese_numerals_units_and_decimals():
    reference = numbers("上限是三百萬，百分之一點二，降到百分之零點五，十二月一日，三個工作天", loose=True)
    assert {"3000000", "1.2", "0.5", "12", "1", "3"} <= reference
    note = numbers("- 預算 300 萬\n1. 12 月 8 日\n- 0.5% 與 1.2%，第一季，兩週，一起")
    assert note == {"3000000", "12", "8", "0.5", "1.2"}


def test_web_app_txt_export_is_accepted():
    text = "週會\n\n[00:05] Speaker 1: 開始\n[01:02:03] Speaker 2: 結束\nnot a segment\n"
    segments = parse_transcript(text, ".txt")
    assert [s["start"] for s in segments] == [5.0, 3723.0]
    assert segments[0]["end"] == 3723.0
    with pytest.raises(ValueError, match="timestamps"):
        parse_transcript("no stamps here", ".txt")


def test_paraphrase_misses_are_listed_for_review_not_hidden():
    segments = [{"start": 0, "end": 60, "text": "我們決定延後上線"}]
    key = {"items": [{"id": "delay", "importance": "critical", "match": [["延後", "上線"]]}]}
    result = score_note("- 發布時間往後挪。", segments, key)
    assert result["verdict"] == "fail"
    assert result["missed"] == ["delay"]
    widened = {"items": [{"id": "delay", "importance": "critical", "match": [["延後", "上線"], ["往後挪"]]}]}
    assert score_note("- 發布時間往後挪。", segments, widened)["verdict"] == "pass"


def test_gate_overrides_and_unlocated_items(tmp_path):
    segments = [{"start": 0, "end": 30, "text": "預算三百萬"}]
    key = {"items": [{"id": "budget", "match": [["300萬"]]},
                     {"id": "ghost", "importance": "minor", "match": [["不存在的詞"]]}]}
    strict = score_note("- 預算 300 萬", segments, key)
    assert strict["verdict"] == "fail"
    relaxed = score_note("- 預算 300 萬", segments, key, {"min_weighted_coverage": 0.5, "min_third_coverage": 0.5})
    assert relaxed["verdict"] == "review"
    assert any("could not be located" in warning for warning in relaxed["warnings"])


def test_manifest_requires_declared_source_and_notes(tmp_path):
    for name in ("transcript.json", "key.json", "note-faithful.md"):
        shutil.copy(FIXTURE / name, tmp_path / name)
    manifest = tmp_path / "manifest.json"
    base = {"id": "a", "transcript_file": "transcript.json", "key": "key.json",
            "notes": {"x": "note-faithful.md"}}
    manifest.write_text(json.dumps({"version": 1, "cases": [base]}))
    with pytest.raises(ValueError, match="source"):
        evaluate(manifest)
    manifest.write_text(json.dumps({"version": 1, "cases": [base | {"source": "owned", "notes": {}}]}))
    with pytest.raises(ValueError, match="notes"):
        evaluate(manifest)


def test_cli_scores_and_protects_inputs(tmp_path):
    from typer.testing import CliRunner

    from localplaud.cli import app

    runner = CliRunner()
    output = tmp_path / "report.json"
    result = runner.invoke(app, ["eval-notes", str(FIXTURE / "manifest.json"), "--output", str(output)])
    assert result.exit_code == 1  # the flawed candidate fails
    report = json.loads(output.read_text())
    assert report["schema"] == "localplaud-note-fidelity/v1"
    assert report["summary"] == {"pass": 1, "review": 0, "fail": 1, "error": 0}
    assert runner.invoke(app, ["eval-notes", str(FIXTURE / "manifest.json"), "--report-only"]).exit_code == 0
    clobber = runner.invoke(
        app, ["eval-notes", str(FIXTURE / "manifest.json"), "--output", str(FIXTURE / "note-flawed.md")]
    )
    assert clobber.exit_code != 0
    assert "must not overwrite" in clobber.output
    assert (FIXTURE / "note-flawed.md").read_text().startswith("# 會議記錄")


def test_full_width_commas_do_not_merge_numbers():
    assert numbers("三百萬，百分之五", loose=True) == {"3000000", "5"}
    assert numbers("共 1,200 人，3 組") == {"1200", "3"}
