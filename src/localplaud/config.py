"""Configuration for localplaud.

Config is layered, later layers win:

1. Built-in defaults (this file).
2. A TOML file — ``config.toml`` in the working directory by default, or the
   path in ``LOCALPLAUD_CONFIG``.
3. Environment variables and an optional ``.env`` file, prefixed
   ``LOCALPLAUD_`` with ``__`` as the nesting separator
   (e.g. ``LOCALPLAUD_ASR__OPENAI__API_KEY``).

Secrets (Plaud cookie, API keys, HF token) should come from the environment or
``.env`` and never be committed. The example config marks which fields those
are.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# --------------------------------------------------------------------------- #
# Sub-models
# --------------------------------------------------------------------------- #


class PlaudConfig(BaseModel):
    """How to reach and authenticate against the Plaud cloud API.

    ``api_base`` is region-specific (the browser stores it in localStorage as
    ``pld_plaud_user_api_domain``). Read it from your own browser — do not
    assume the default matches your account.
    """

    api_base: str = "https://api-apse1.plaud.ai"
    # "cookie": paste a session cookie/token from the browser (most reliable).
    # "login": programmatic email/password login (only if supported).
    auth_mode: Literal["cookie", "login"] = "cookie"

    # auth_mode = "cookie": full Cookie header value, or a bare bearer token.
    # Prefer the env var LOCALPLAUD_PLAUD__COOKIE over writing it to disk.
    cookie: str | None = None
    # Some deployments use a bearer token instead of / in addition to a cookie.
    token: str | None = None

    # auth_mode = "login":
    email: str | None = None
    password: str | None = None

    # Extra request headers if a particular account/region needs them.
    extra_headers: dict[str, str] = Field(default_factory=dict)

    # Network politeness.
    request_timeout_seconds: float = 30.0
    user_agent: str = (
        "localplaud/0.1 (+https://github.com/skyhong2002/localplaud)"
    )


class PollerConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = 300
    include_trash: bool = False
    download_dir: Path = Path("./data/audio")
    # Cap concurrent downloads to be gentle on the cloud + local disk.
    max_concurrent_downloads: int = 2


class StoreConfig(BaseModel):
    database_url: str = "sqlite:///./data/localplaud.db"


class PipelineConfig(BaseModel):
    """Which local processing stages to run after a file is downloaded."""

    convert: bool = True  # opus -> 16kHz mono wav for ASR
    transcribe: bool = True
    diarize: bool = True
    summarize: bool = True
    index: bool = True  # embeddings for Q&A / semantic search
    # Number of files processed concurrently by the worker.
    concurrency: int = 1
    # Re-use Plaud's own transcript/summary when the cloud already made one,
    # instead of recomputing locally. Set false to always redo locally.
    prefer_cloud_artifacts: bool = False


# ---- ASR providers ------------------------------------------------------- #


class FasterWhisperConfig(BaseModel):
    model: str = "large-v3"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"


class WhisperCppConfig(BaseModel):
    binary: str = "whisper-cli"  # from whisper.cpp; uses Metal on Apple Silicon
    model_path: Path = Path("./models/ggml-large-v3.bin")
    extra_args: list[str] = Field(default_factory=list)


class MlxWhisperConfig(BaseModel):
    model: str = "mlx-community/whisper-large-v3-mlx"


class OpenAIAsrConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str = "whisper-1"


class DeepgramConfig(BaseModel):
    api_key: str | None = None
    model: str = "nova-2"
    diarize: bool = True  # Deepgram returns speaker labels server-side


class AssemblyAIConfig(BaseModel):
    api_key: str | None = None
    speaker_labels: bool = True


AsrProviderName = Literal[
    "faster-whisper", "whispercpp", "mlx-whisper", "openai", "deepgram", "assemblyai"
]


class AsrConfig(BaseModel):
    """ASR is fully pluggable. Local and cloud providers are equal first-class
    choices — pick whichever gives the best accuracy / speaker separation for
    your machine, not just as a fallback. ``fallback`` providers are tried in
    order if the primary can't run (e.g. no GPU, model missing)."""

    provider: AsrProviderName = "faster-whisper"
    language: str = "auto"  # ISO code (e.g. "en", "zh") or "auto"
    fallback: list[AsrProviderName] = Field(default_factory=list)

    faster_whisper: FasterWhisperConfig = Field(default_factory=FasterWhisperConfig)
    whispercpp: WhisperCppConfig = Field(default_factory=WhisperCppConfig)
    mlx_whisper: MlxWhisperConfig = Field(default_factory=MlxWhisperConfig)
    openai: OpenAIAsrConfig = Field(default_factory=OpenAIAsrConfig)
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)
    assemblyai: AssemblyAIConfig = Field(default_factory=AssemblyAIConfig)


class DiarizeConfig(BaseModel):
    provider: Literal["pyannote", "none"] = "pyannote"
    hf_token: str | None = None
    # Optional hints; leave 0/None to auto-detect.
    num_speakers: int | None = None


# ---- LLM (summaries + Q&A) ----------------------------------------------- #


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    model: str = "llama3.1:8b"


class OpenAILlmConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-4o-mini"


class AnthropicLlmConfig(BaseModel):
    api_key: str | None = None
    model: str = "claude-haiku-4-5"


class LlmConfig(BaseModel):
    provider: Literal["ollama", "openai", "anthropic"] = "ollama"
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    openai: OpenAILlmConfig = Field(default_factory=OpenAILlmConfig)
    anthropic: AnthropicLlmConfig = Field(default_factory=AnthropicLlmConfig)


# ---- Embeddings (Q&A / search) ------------------------------------------- #


class LocalEmbeddingsConfig(BaseModel):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"


class OpenAIEmbeddingsConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str = "text-embedding-3-small"


class EmbeddingsConfig(BaseModel):
    provider: Literal["local", "openai"] = "local"
    local: LocalEmbeddingsConfig = Field(default_factory=LocalEmbeddingsConfig)
    openai: OpenAIEmbeddingsConfig = Field(default_factory=OpenAIEmbeddingsConfig)


class ApiConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # Used to build absolute links behind a reverse proxy; set per machine.
    public_url: str | None = None


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #


def _toml_config_path() -> Path:
    return Path(os.environ.get("LOCALPLAUD_CONFIG", "config.toml"))


class _TomlSource(PydanticBaseSettingsSource):
    """Load the TOML file (if present) as a settings source."""

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = _toml_config_path()
        if not path.exists():
            return {}
        with path.open("rb") as fh:
            return tomllib.load(fh)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCALPLAUD_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    plaud: PlaudConfig = Field(default_factory=PlaudConfig)
    poller: PollerConfig = Field(default_factory=PollerConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    diarize: DiarizeConfig = Field(default_factory=DiarizeConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Precedence (first wins): init kwargs, env, .env, TOML file, defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _TomlSource(settings_cls),
        )


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
