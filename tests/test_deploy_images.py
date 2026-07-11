"""Deployment image invariants that prevent incompatible CUDA dependency drift."""

from pathlib import Path


def test_cuda_image_pins_pyannote_compatible_torch_stack():
    dockerfile = Path("Dockerfile.cuda").read_text()
    assert "nvidia/cuda:12.8.1-cudnn-runtime" in dockerfile
    assert "torch==2.8.0 torchaudio==2.8.0" in dockerfile
    assert "torchcodec==0.7" in dockerfile
    assert "download.pytorch.org/whl/cu128" in dockerfile
    assert "cu124" not in dockerfile
