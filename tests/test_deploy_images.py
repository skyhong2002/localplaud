"""Deployment image invariants that prevent incompatible CUDA dependency drift."""

from pathlib import Path


def test_cuda_image_pins_pyannote_compatible_torch_stack():
    dockerfile = Path("Dockerfile.cuda").read_text()
    assert "nvidia/cuda:12.8.1-cudnn-runtime" in dockerfile
    assert "torch==2.8.0 torchaudio==2.8.0" in dockerfile
    assert "torchcodec==0.7" in dockerfile
    assert "download.pytorch.org/whl/cu128" in dockerfile
    assert "cu124" not in dockerfile


def test_cuda_image_caches_dependencies_before_copying_application_source():
    dockerfile = Path("Dockerfile.cuda").read_text()
    dependency_install = dockerfile.index(
        'pip install ".[faster-whisper,forced-align,diarize,cloud,local-llm]"'
    )
    source_copy = dockerfile.index("COPY src ./src")
    application_install = dockerfile.index("pip install --no-deps --force-reinstall .")
    assert dependency_install < source_copy < application_install


def test_optional_ollama_runtime_is_private_and_persistent():
    compose = Path("docker-compose.yml").read_text()
    deploy = Path("docs/deploy.md").read_text()
    assert "ollama/ollama:0.31.2" in compose
    assert 'profiles: ["ollama"]' in compose
    assert "ollama_data:/root/.ollama" in compose
    assert 'OLLAMA_CONTEXT_LENGTH: "8192"' in compose
    assert 'expose:\n      - "11434"' in compose
    assert '11434:11434' not in compose
    assert "qwen3:4b-instruct-2507-q4_K_M" in deploy


def test_cuda_image_installs_selectable_forced_alignment_runtime():
    dockerfile = Path("Dockerfile.cuda").read_text()
    assert "forced-align" in dockerfile
