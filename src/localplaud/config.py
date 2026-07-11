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


class PlaudOfficialConfig(BaseModel):
    """The official Plaud Open API (platform.plaud.ai) with sanctioned OAuth.

    The one-time browser sign-in happens through the official Plaud CLI
    (``localplaud auth login`` wraps it); after that, localplaud reads and
    auto-refreshes the token set in ``tokens_path``. No secrets needed for the
    default public client.
    """

    api_base: str = "https://platform.plaud.ai/developer/api"
    refresh_url: str = (
        "https://platform.plaud.ai/developer/api/oauth/third-party/access-token/refresh"
    )
    # Token cache written by the official CLI (`plaud login`).
    tokens_path: Path = Path("~/.plaud/tokens.json")
    request_timeout_seconds: float = 30.0


class PlaudConfig(BaseModel):
    """How to reach and authenticate against the Plaud cloud.

    ``provider`` picks the client:

    - ``official`` (default) — the sanctioned Open API with auto-refreshing
      OAuth (see ``official``). No more pasting browser sessions.
    - ``apse1`` — the reverse-engineered web API, driven by a pasted browser
      session (``cookie``/``token``/``extra_headers``).

    When the provider is ``official`` and apse1 credentials are ALSO present,
    the poller uses apse1 as an optional enrichment source for change-detection
    fields the Open API lacks (``version``/``file_md5``/``edit_time``/
    ``is_trash``) — disable with ``apse1_enrichment = false``.

    ``api_base`` (apse1) is region-specific (the browser stores it in
    localStorage as ``pld_plaud_user_api_domain``). Read it from your own
    browser — do not assume the default matches your account.
    """

    provider: Literal["official", "apse1"] = "official"
    official: PlaudOfficialConfig = Field(default_factory=PlaudOfficialConfig)
    apse1_enrichment: bool = False

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

    # Network politeness. The Plaud edge rejects non-browser User-Agents with
    # 403, so default to a browser UA (override via plaud.user_agent if needed).
    request_timeout_seconds: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
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
    align: bool = True
    diarize: bool = True
    correct: bool = True  # durable vocabulary/name correction after speaker assignment
    summarize: bool = True
    mind_map: bool = True  # nested Markdown outline rendered as a tree
    index: bool = True  # embeddings for Q&A / semantic search
    # Number of files processed concurrently by the worker.
    concurrency: int = 1
    # The daemon yields after a small newest-first batch so fresh recordings
    # are discovered promptly instead of sitting behind a months-long backlog.
    files_per_cycle: int = 1
    retry_max_attempts: int = Field(default=3, ge=1, le=100)
    retry_base_seconds: int = Field(default=60, ge=1)
    retry_max_seconds: int = Field(default=3600, ge=1)
    # Durable cross-process claim. A crashed worker becomes reclaimable after
    # this interval; normal stage work refreshes by completing within one day.
    processing_lease_seconds: int = Field(default=86_400, ge=300)
    # Character budget per LLM call. Longer transcripts are covered through
    # hierarchical map/reduce notes instead of being truncated.
    summary_chunk_chars: int = 12_000
    # Which summary template to use (default | meeting | call | lecture |
    # personal — see worker/summary_templates.py).
    summary_template: str = "default"
    # independent: Plaud supplies metadata + raw audio only; only locally
    # generated transcripts may satisfy the processing pipeline.
    # migration: allow explicitly imported Plaud Intelligence artifacts for
    # comparison/backfill. This mode is never the subscription-free default.
    artifact_mode: Literal["independent", "migration"] = "independent"
    # Migration/debug selection only. This compatibility flag is effective only
    # when artifact_mode = "migration" and controls canonical selection.
    prefer_cloud_artifacts: bool = False
    # Mirror already-generated Plaud transcripts and notes for reference without
    # allowing them to satisfy or replace the independent local pipeline.
    mirror_cloud_artifacts: bool = False

    @property
    def cloud_import_enabled(self) -> bool:
        return self.mirror_cloud_artifacts or (
            self.artifact_mode == "migration" and self.prefer_cloud_artifacts
        )


# ---- ASR providers ------------------------------------------------------- #


class FasterWhisperConfig(BaseModel):
    model: str = "large-v3-turbo"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"


class WhisperCppConfig(BaseModel):
    binary: str = "whisper-cli"  # from whisper.cpp; uses Metal on Apple Silicon
    model_path: Path = Path("./models/ggml-large-v3-turbo.bin")
    extra_args: list[str] = Field(default_factory=list)


class MlxWhisperConfig(BaseModel):
    model: str = "mlx-community/whisper-large-v3-turbo"


class OpenAIAsrConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str = "whisper-1"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 120
    max_input_bytes: int | None = None


class DeepgramConfig(BaseModel):
    api_key: str | None = None
    model: str = "nova-2"
    diarize: bool = True  # Deepgram returns speaker labels server-side


class AssemblyAIConfig(BaseModel):
    api_key: str | None = None
    speaker_labels: bool = True


class RemoteWorkerAsrConfig(BaseModel):
    base_url: str = ""
    token: str | None = None
    model: str = "large-v3-turbo"
    input_base_url: str = ""
    input_signing_secret: str | None = None
    input_url_ttl_seconds: int = Field(default=600, gt=0, le=3600)
    timeout_seconds: float = Field(default=3600, gt=0)
    poll_interval_seconds: float = Field(default=2, gt=0, le=60)


class RemoteInputConfig(BaseModel):
    base_url: str = ""
    signing_secret: str | None = None
    url_ttl_seconds: int = Field(default=600, gt=0, le=3600)


class VadConfig(BaseModel):
    """Voice Activity Detection (the first stage of the speech pipeline).

    When enabled, speech regions are detected before ASR so long silences are
    trimmed and long recordings are transcribed in bounded chunks with globally
    correct timestamps. Local silero-vad requires the optional ``vad`` extra
    (``pip install 'localplaud[vad]'``); faster-whisper's native ``vad_filter``
    bundles its own silero and needs no extra. Defaults to disabled until it is
    benchmarked on real Taiwan Mandarin / code-switch recordings.
    """

    enabled: bool = False
    # silero-vad thresholds (defaults match silero's own recommendations).
    threshold: float = 0.5  # speech probability cutoff (0-1)
    min_speech_ms: int = 250  # drop speech blips shorter than this
    min_silence_ms: int = 100  # silence needed to close a speech region
    speech_pad_ms: int = 30  # padding silero adds around each detected region
    # Chunk planning for the local (mlx) path — see asr.vad.merge_speech_regions.
    merge_gap_s: float = 0.5  # merge regions separated by a shorter gap
    region_pad_s: float = 0.2  # extra padding per chunk so words aren't clipped
    max_region_s: float = 30.0  # split longer regions to bound each ASR call


AsrProviderName = Literal[
    "faster-whisper", "whispercpp", "mlx-whisper", "openai", "deepgram", "assemblyai",
    "remote-worker"
]


class AsrConfig(BaseModel):
    """ASR is pluggable, with local Whisper large-v3-turbo as the default
    subscription-independent quality baseline. ``fallback`` names providers tried
    in order when the primary is unavailable; paid cloud fallback requires explicit
    operator configuration."""

    provider: AsrProviderName = "faster-whisper"
    language: str = "auto"  # ISO code (e.g. "en", "zh") or "auto"
    fallback: list[AsrProviderName] = Field(default_factory=list)
    fallback_routes: list[dict[str, Any]] = Field(default_factory=list, exclude=True)

    vad: VadConfig = Field(default_factory=VadConfig)
    faster_whisper: FasterWhisperConfig = Field(default_factory=FasterWhisperConfig)
    whispercpp: WhisperCppConfig = Field(default_factory=WhisperCppConfig)
    mlx_whisper: MlxWhisperConfig = Field(default_factory=MlxWhisperConfig)
    openai: OpenAIAsrConfig = Field(default_factory=OpenAIAsrConfig)
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)
    assemblyai: AssemblyAIConfig = Field(default_factory=AssemblyAIConfig)
    remote_worker: RemoteWorkerAsrConfig = Field(default_factory=RemoteWorkerAsrConfig)


class DiarizeConfig(BaseModel):
    provider: Literal["pyannote", "remote-worker", "none"] = "pyannote"
    model: str = "pyannote/speaker-diarization-community-1"
    hf_token: str | None = None
    # Optional hints; leave 0/None to auto-detect.
    num_speakers: int | None = None
    base_url: str = ""
    token: str | None = None
    timeout_seconds: float = 3600
    poll_interval_seconds: float = 2


class AlignConfig(BaseModel):
    provider: Literal["whisper-timestamps", "whisperx", "remote-worker", "none"] = "whisper-timestamps"
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    model: str | None = None
    # Optional explicit WhisperX aligner overrides by detected language
    # (`zh`, `en`, `zh-TW`, or `default`). Unlisted languages use WhisperX's
    # own language-aware model selection.
    models_by_language: dict[str, str] = Field(default_factory=dict)
    base_url: str = ""
    token: str | None = None
    timeout_seconds: float = 3600
    poll_interval_seconds: float = 2


# ---- LLM (summaries + Q&A) ----------------------------------------------- #


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    model: str = "llama3.1:8b"


class OpenAILlmConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str = "gpt-4o-mini"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 120
    max_input_chars: int | None = None


class AnthropicLlmConfig(BaseModel):
    api_key: str | None = None
    model: str = "claude-haiku-4-5"


class CodexLocalConfig(BaseModel):
    enabled: bool = False
    binary: str = "codex"
    model: str | None = None
    timeout_seconds: float = 300


class RemoteTextConfig(BaseModel):
    base_url: str = ""
    token: str | None = None
    model: str = ""
    capability: Literal["notes", "mind_map", "ask", "correction"] = "notes"
    timeout_seconds: float = 600
    poll_interval_seconds: float = 2


class LlmConfig(BaseModel):
    provider: Literal["ollama", "openai", "anthropic", "codex-local", "remote-worker"] = "ollama"
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    openai: OpenAILlmConfig = Field(default_factory=OpenAILlmConfig)
    anthropic: AnthropicLlmConfig = Field(default_factory=AnthropicLlmConfig)
    codex_local: CodexLocalConfig = Field(default_factory=CodexLocalConfig)
    remote_worker: RemoteTextConfig = Field(default_factory=RemoteTextConfig)
    correction_provider: str | None = Field(default=None, exclude=True)


# ---- Embeddings (Q&A / search) ------------------------------------------- #


class LocalEmbeddingsConfig(BaseModel):
    model: str = "sentence-transformers/all-MiniLM-L6-v2"


class OpenAIEmbeddingsConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str = "text-embedding-3-small"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 120
    max_input_chars: int | None = None


class OllamaEmbeddingsConfig(BaseModel):
    """Local embeddings via Ollama — no torch/sentence-transformers needed."""

    host: str = "http://localhost:11434"
    model: str = "bge-m3"


class RemoteEmbeddingsConfig(BaseModel):
    base_url: str = ""
    token: str | None = None
    model: str = ""
    dim: int = Field(default=1024, gt=0)
    timeout_seconds: float = 600
    poll_interval_seconds: float = 2


class EmbeddingsConfig(BaseModel):
    provider: Literal["local", "openai", "ollama", "remote-worker"] = "local"
    local: LocalEmbeddingsConfig = Field(default_factory=LocalEmbeddingsConfig)
    openai: OpenAIEmbeddingsConfig = Field(default_factory=OpenAIEmbeddingsConfig)
    ollama: OllamaEmbeddingsConfig = Field(default_factory=OllamaEmbeddingsConfig)
    remote_worker: RemoteEmbeddingsConfig = Field(default_factory=RemoteEmbeddingsConfig)


class ApiConfig(BaseModel):
    # Loopback by default so an accidental `localplaud run` isn't exposed to the
    # LAN. In Docker this is overridden to 0.0.0.0 (the container sits behind
    # Caddy and its port isn't published).
    host: str = "127.0.0.1"
    port: int = 8080
    # Optional shared secret. When set, every request must present it as an
    # ``X-Auth-Token`` header or ``?token=`` query param. Prefer putting real
    # auth (e.g. Caddy basic_auth) in front; this is a lightweight backstop.
    auth_token: str | None = None
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
    align: AlignConfig = Field(default_factory=AlignConfig)
    remote_input: RemoteInputConfig = Field(default_factory=RemoteInputConfig)
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
