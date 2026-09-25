"""Private stdin/stdout voice embedding worker; runs on the explicitly selected host.

CPU inference intentionally runs independently of the ASR GPU queue. One persistent
model, bounded mono PCM clips, no shell commands or paths from incoming requests.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys

from .voice_matching import MODEL, REVISION


def main():
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        import soundfile as sf
        import torch
        from huggingface_hub import hf_hub_download
        from nemo.collections.asr.models import EncDecSpeakerLabelModel

        torch.set_num_threads(2)
        path = hf_hub_download(
            MODEL, "speakerverification_en_titanet_large.nemo", revision=REVISION
        )
        model = EncDecSpeakerLabelModel.restore_from(path, map_location="cpu").eval()
    print(
        json.dumps({"ready": True, "model": MODEL, "revision": REVISION}), file=output, flush=True
    )
    for line in sys.stdin:
        request_id = None
        try:
            if len(line) > 80_000_000:
                raise ValueError("voice request too large")
            request = json.loads(line)
            request_id = request.get("request_id")
            samples, rate = sf.read(
                io.BytesIO(base64.b64decode(request["wav"], validate=True)), dtype="float32"
            )
            if rate != 16000 or samples.ndim != 1 or len(samples) > 16000 * 1800:
                raise ValueError("voice request must contain bounded 16k mono audio")
            vectors = []
            windows = request["windows"]
            if not 1 <= len(windows) <= 256:
                raise ValueError("invalid number of voice windows")
            with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
                for start, end in windows:
                    if (
                        not 0 <= start < end <= len(samples) / rate + 0.001
                        or not 2.9 <= end - start <= 10.1
                    ):
                        raise ValueError("invalid voice window")
                    clip = samples[round(start * rate) : round(end * rate)]
                    if not np.isfinite(clip).all() or np.sqrt(np.mean(clip * clip)) < 0.001:
                        vectors.append(None)
                        continue
                    signal = torch.from_numpy(clip.copy()).unsqueeze(0)
                    _, embedding = model(
                        input_signal=signal, input_signal_length=torch.tensor([len(clip)])
                    )
                    vector = embedding[0].cpu().numpy()
                    vector /= max(float(np.linalg.norm(vector)), 1e-12)
                    if vector.shape != (192,) or not np.isfinite(vector).all():
                        raise ValueError("invalid model embedding")
                    vectors.append(vector.tolist())
            result = {"vectors": vectors, "model": MODEL, "revision": REVISION}
        except Exception as exc:
            result = {"error": type(exc).__name__}
        result["request_id"] = request_id
        print(json.dumps(result), file=output, flush=True)


if __name__ == "__main__":
    main()
