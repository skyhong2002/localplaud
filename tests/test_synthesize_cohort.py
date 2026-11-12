"""Synthetic speech cohort builder: timeline, references and manifest, with fake TTS/ffmpeg."""

from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import pytest

from localplaud import benchmark_speech as speech

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "benchmark" / "synthesize_speech_cohort.py"

FAKE_TTS = """import math, struct, sys, wave
text = sys.stdin.read()
out = sys.argv[1]
rate, seconds = 16000, 0.1 * len(text)
with wave.open(out, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    silence = b"\\x00\\x00" * int(0.2 * rate)
    tone = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(int(seconds * rate)))
    w.writeframes(silence + tone + silence)
"""

FAKE_FFMPEG = """import sys, wave
source = sys.argv[sys.argv.index("-i") + 1]
with wave.open(source) as w:
    sys.stdout.buffer.write(w.readframes(w.getnframes()))
"""


def _load():
    spec = importlib.util.spec_from_file_location("synthesize_speech_cohort", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cohort_scripts_are_valid_and_fictional():
    scripts = sorted((SCRIPT.parent / "zh_tw_cohort").glob("*.json"))
    assert len(scripts) >= 4
    for path in scripts:
        data = json.loads(path.read_text())
        assert "Fictional" in data["description"]
        speakers = {t["speaker"] for t in data["turns"] if "speaker" in t}
        assert speakers <= set(data["voices"])
        assert all(voice.startswith("zh-TW-") for voice in data["voices"].values())


def test_builder_writes_a_manifest_the_benchmark_accepts(tmp_path):
    module = _load()
    tts = tmp_path / "tts.py"
    tts.write_text(FAKE_TTS)
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(f"#!{sys.executable}\n" + FAKE_FFMPEG)
    ffmpeg.chmod(0o755)
    script = tmp_path / "dialogue.json"
    script.write_text(json.dumps({
        "id": "fake-dialogue", "description": "Fictional.", "language": "zh",
        "voices": {"A": "zh-TW-A", "B": "zh-TW-B"},
        "turns": [{"speaker": "A", "text": "你好世界"},
                  {"silence": 2.0, "noise_dbfs": -60},
                  {"speaker": "B", "text": "OK 收到", "overlap": 0.3}],
    }))
    out = tmp_path / "cohort"
    status = module.main(["--out", str(out), "--scripts", str(script), "--backend", "command",
                          "--tts-command", f"{sys.executable} {tts} {{out}}", "--ffmpeg", str(ffmpeg)])
    assert status == 0
    manifest = json.loads((out / "manifest.json").read_text())
    (row,) = manifest["recordings"]
    assert row["source"] == "synthetic" and "owned" not in row
    reference = row["reference"]
    assert reference["text"] == "你好世界\nOK 收到"
    assert [t["speaker"] for t in reference["speaker_turns"]] == ["A", "B"]
    (pause,) = reference["non_speech"]
    assert pause["end"] - pause["start"] == pytest.approx(2.0)
    first, second = reference["segments"]
    # Leading clip silence is trimmed from the reference turn; speech stays after the pause.
    assert first["start"] == pytest.approx(0.5 + 0.2, abs=0.02)
    assert second["start"] > pause["end"] - 0.3 - 0.01
    with wave.open(str(out / "fake-dialogue.wav")) as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1
    provenance = json.loads((out / "PROVENANCE.json").read_text())
    assert provenance["backend"] == "command" and "never a production default" in provenance["evidence"]
    (loaded,) = speech.load_manifest(out / "manifest.json")
    assert loaded["source"] == "synthetic"


def test_network_tts_requires_explicit_acknowledgement(tmp_path):
    module = _load()
    with pytest.raises(SystemExit):
        module.main(["--out", str(tmp_path / "x"), "--backend", "edge-tts", "--ffmpeg", sys.executable])
