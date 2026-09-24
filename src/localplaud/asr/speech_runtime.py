"""Pinned speech models; only this child process imports their GPU runtimes."""

from __future__ import annotations

import gc
import json
import math
import sys
import tempfile
from pathlib import Path

from .base import AsrError
from .vad import audio_duration_seconds, merge_speech_regions, slice_region


def speech_regions(audio, cfg, duration):
    # faster-whisper ships a CPU ONNX Silero model: no network or optional VAD
    # package, and the same detector used by the previous production baseline.
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    vad = cfg["vad"]
    samples = decode_audio(str(audio), sampling_rate=16000)
    spans = get_speech_timestamps(
        samples,
        VadOptions(
            threshold=vad["threshold"],
            min_speech_duration_ms=vad["min_speech_ms"],
            min_silence_duration_ms=vad["min_silence_ms"],
            speech_pad_ms=vad["speech_pad_ms"],
        ),
    )
    regions = merge_speech_regions(
        [(s["start"] / 16000, s["end"] / 16000) for s in spans],
        vad["merge_gap_s"],
        vad["region_pad_s"],
        cfg["qwen"]["max_chunk_seconds"],
    )
    return [(s, min(e, duration)) for s, e in regions if s < duration]


def aligned_words(aligned, offset, duration):
    result = []
    for word in aligned:
        start, end = float(word["start_time"]), float(word["end_time"])
        if not math.isfinite(start + end) or start < 0 or end < start:
            raise AsrError("Qwen alignment returned invalid timestamps")
        # Retain legitimate point timestamps and clamp padded predictions.
        result.append(
            {
                "text": word["text"],
                "start": offset + min(start, duration),
                "end": offset + min(end, duration),
            }
        )
    return result


def qwen(audio, cfg):
    import torch
    from transformers import (
        AutoModelForMultimodalLM,
        AutoModelForTokenClassification,
        AutoProcessor,
    )

    if not torch.cuda.is_available():
        raise AsrError("Qwen requires an available CUDA device")
    duration = audio_duration_seconds(audio)
    if duration is None:
        raise AsrError("Qwen requires canonical PCM WAV input")
    options = cfg["qwen"]
    regions = speech_regions(audio, cfg, duration)
    metadata = {
        "vad": {
            "provider": "faster-whisper/silero-onnx",
            "enabled": True,
            "regions": len(regions),
            "speech_seconds": sum(e - s for s, e in regions),
            "skipped_seconds": duration - sum(e - s for s, e in regions),
            "timeline": "original-audio",
            "configuration": {
                **cfg["vad"],
                "enabled": True,
                "max_region_s": options["max_chunk_seconds"],
            },
        },
        "asr_revision": options["revision"],
        "aligner": options["aligner"],
        "aligner_revision": options["aligner_revision"],
    }
    result = {
        "segments": [],
        "language": None,
        "duration": duration,
        "provider": "qwen",
        "model": options["model"],
        "processing_metadata": metadata,
    }
    if not regions:
        return result
    with tempfile.TemporaryDirectory(prefix="qwen-chunks-") as directory:
        processor = AutoProcessor.from_pretrained(options["model"], revision=options["revision"])
        model = (
            AutoModelForMultimodalLM.from_pretrained(
                options["model"],
                revision=options["revision"],
                dtype=torch.bfloat16,
                attn_implementation="sdpa",
            )
            .to("cuda")
            .eval()
        )
        chunks = []
        for index, (start, end) in enumerate(regions):
            path = slice_region(audio, start, end, Path(directory) / f"{index}.wav")
            inputs = processor.apply_transcription_request(
                audio=str(path), language=None if cfg["language"] == "auto" else cfg["language"]
            ).to("cuda", torch.bfloat16)
            with torch.inference_mode():
                output = model.generate(**inputs, max_new_tokens=4096, do_sample=False)
            generated = output[:, inputs["input_ids"].shape[1] :]
            if generated.shape[1] >= 4096:
                raise AsrError("Qwen reached its token limit; transcription is incomplete")
            parsed = processor.decode(generated, return_format="parsed")[0]
            chunks.append((path, start, end, parsed))
            del inputs, output, generated
        del model, processor
        gc.collect()
        torch.cuda.empty_cache()
        processor = AutoProcessor.from_pretrained(
            options["aligner"], revision=options["aligner_revision"]
        )
        model = (
            AutoModelForTokenClassification.from_pretrained(
                options["aligner"],
                revision=options["aligner_revision"],
                dtype=torch.bfloat16,
                attn_implementation="sdpa",
            )
            .to("cuda")
            .eval()
        )
        languages = []
        for path, start, end, parsed in chunks:
            text = parsed["transcription"].strip()
            if not text:
                continue
            language = parsed["language"]
            languages.append(language)
            inputs, word_lists = processor.prepare_forced_aligner_inputs(
                audio=str(path), transcript=text, language=language
            )
            inputs = inputs.to("cuda", torch.bfloat16)
            with torch.inference_mode():
                output = model(**inputs)
            aligned = processor.decode_forced_alignment(
                logits=output.logits,
                input_ids=inputs["input_ids"],
                word_lists=word_lists,
                timestamp_token_id=model.config.timestamp_token_id,
            )[0]
            words = aligned_words(aligned, start, end - start)

            def lexical(value):
                return "".join(c.casefold() for c in value if c.isalnum())

            if not words or lexical("".join(w["text"] for w in words)) != lexical(text):
                raise AsrError("Qwen alignment did not preserve the complete transcription")
            result["segments"].append({"text": text, "start": start, "end": end, "words": words})
            del inputs, output
        if languages:
            language = max(dict.fromkeys(languages), key=languages.count)
            result["language"] = {"Chinese": "zh", "English": "en", "Cantonese": "yue"}.get(
                language, language
            )
    return result


def nemotron(audio, cfg):
    import torch
    from huggingface_hub import hf_hub_download
    from nemo.collections.asr.models import SortformerEncLabelModel

    if cfg.get("num_speakers") and cfg["num_speakers"] > 8:
        raise AsrError("Nemotron supports at most eight speakers")
    checkpoint = hf_hub_download(
        cfg["model"], "Nemotron-3-Diarization.nemo", revision=cfg["revision"]
    )
    model = SortformerEncLabelModel.restore_from(checkpoint, map_location="cuda").eval()
    for key, value in {
        "spkcache_len": 264,
        "fifo_len": 40,
        "chunk_len": 340,
        "chunk_right_context": 40,
        "spkcache_update_period": 300,
    }.items():
        setattr(model.sortformer_modules, key, value)
    model._check_streaming_parameters()
    # Whole recording maintains one speaker cache and stable identities.
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        spans = model.diarize(audio=[str(audio)], batch_size=1)[0]
    duration = audio_duration_seconds(audio)
    turns = []
    for span in spans:
        start, end, speaker = span.split()
        start, end = float(start), float(end)
        if not math.isfinite(start + end) or end < start:
            raise AsrError("Nemotron returned invalid timestamps")
        start, end = max(0, start), min(duration, end)
        if end > start:
            turns.append((start, end, speaker))
    return {"turns": turns}


if __name__ == "__main__":
    kind, request_path, output_path = sys.argv[1:]
    request = json.loads(Path(request_path).read_text())
    try:
        result = {"qwen": qwen, "nemotron": nemotron}[kind](
            Path(request["audio"]), request["config"]
        )
    except Exception as exc:
        # Do not expose decoded text, paths, or provider credentials in UI errors.
        message = (
            str(exc)
            if isinstance(exc, AsrError)
            else f"{kind} runtime failed: {type(exc).__name__}"
        )
        Path(output_path).write_text(json.dumps({"error": message}))
        raise
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False))
