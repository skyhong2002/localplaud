"""SQLAlchemy models — the local mirror + knowledge base.

Design: one row per Plaud cloud file (``PlaudFile``), holding both the cloud
metadata we sync and the state of our own local processing. Derived artifacts
(transcript, summary, embedding chunks) hang off it. Audio bytes live on the
filesystem; everything else lives here.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class FileStatus(enum.StrEnum):
    """Local lifecycle of a file, independent of the cloud's own flags."""

    discovered = "discovered"  # seen in the cloud listing, queued for download
    metadata_only = "metadata_only"  # visible locally; audio stays remote until requested
    downloading = "downloading"
    downloaded = "downloaded"  # audio on disk, pipeline not finished
    processing = "processing"
    partial = "partial"  # core transcript usable; one or more downstream stages degraded
    done = "done"  # pipeline complete
    error = "error"


recording_tags = Table(
    "recording_tags",
    Base.metadata,
    Column(
        "file_id", String(64), ForeignKey("plaud_files.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    recordings: Mapped[list[PlaudFile]] = relationship(back_populates="folder")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    recordings: Mapped[list[PlaudFile]] = relationship(
        secondary=recording_tags, back_populates="tags"
    )


class StageName(enum.StrEnum):
    convert = "convert"
    transcribe = "transcribe"
    align = "align"
    diarize = "diarize"
    summarize = "summarize"
    mind_map = "mind_map"
    index = "index"


class StageStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    degraded = "degraded"
    failed = "failed"
    skipped = "skipped"


class PlaudFile(Base):
    __tablename__ = "plaud_files"

    # Plaud's file id is the primary key — stable across syncs.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ---- Cloud metadata (from GET /file/simple/web) ----
    filename: Mapped[str] = mapped_column(String(512), default="")
    fullname: Mapped[str | None] = mapped_column(String(512), default=None)
    filesize: Mapped[int | None] = mapped_column(BigInteger, default=None)
    file_md5: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    start_time_ms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    end_time_ms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    scene: Mapped[int | None] = mapped_column(Integer, default=None)
    is_trash: Mapped[bool] = mapped_column(default=False)

    # Change-detection: bump of version/version_ms means re-fetch.
    version: Mapped[int | None] = mapped_column(BigInteger, default=None)
    version_ms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    edit_time: Mapped[int | None] = mapped_column(BigInteger, default=None)

    # Cloud's own processing flags — tells us whether Plaud already made a
    # transcript/summary we could reuse instead of recomputing.
    cloud_is_trans: Mapped[bool] = mapped_column(default=False)
    cloud_is_summary: Mapped[bool] = mapped_column(default=False)

    # Full raw object as returned by the API, for anything we didn't model.
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    # ---- Local state ----
    status: Mapped[FileStatus] = mapped_column(
        Enum(FileStatus, native_enum=False, length=20), default=FileStatus.discovered
    )
    audio_path: Mapped[str | None] = mapped_column(String(1024), default=None)  # .opus
    wav_path: Mapped[str | None] = mapped_column(String(1024), default=None)  # converted
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    origin: Mapped[str] = mapped_column(String(32), default="plaud", index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), default=None, index=True
    )
    note_template_key: Mapped[str | None] = mapped_column(String(64), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    transcripts: Mapped[list[Transcript]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="Transcript.id",
    )
    summaries: Mapped[list[Summary]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    stage_runs: Mapped[list[StageRun]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="StageRun.id",
    )
    speakers: Mapped[list[Speaker]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="Speaker.id",
    )
    transcript_revisions: Mapped[list[TranscriptRevision]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="TranscriptRevision.revision",
    )
    user_notes: Mapped[list[UserNote]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="UserNote.id"
    )
    folder: Mapped[Folder | None] = relationship(back_populates="recordings")
    tags: Mapped[list[Tag]] = relationship(
        secondary=recording_tags, back_populates="recordings", order_by="Tag.id"
    )

    @property
    def local_transcript(self) -> Transcript | None:
        local = [row for row in self.transcripts if row.source == "local"]
        return local[-1] if local else None

    @property
    def plaud_transcript(self) -> Transcript | None:
        imported = [row for row in self.transcripts if row.source in {"cloud", "plaud"}]
        return imported[-1] if imported else None

    @property
    def transcript(self) -> Transcript | None:
        """Return the canonical transcript without hiding imported provenance.

        A locally generated transcript always wins. Plaud/cloud transcripts remain
        attached for migration or comparison and are returned only when no local
        result exists. Pipeline code applies the stricter configured artifact mode
        before deciding whether an existing transcript may be reused.
        """
        return self.local_transcript or self.plaud_transcript

    @transcript.setter
    def transcript(self, value: Transcript | None) -> None:
        """Compatibility setter for callers that assign one transcript."""
        self.transcripts = [] if value is None else [value]

    @property
    def corrected_transcript(self) -> TranscriptRevision | None:
        """Latest user-corrected revision, or ``None`` when no edits exist."""
        return self.transcript_revisions[-1] if self.transcript_revisions else None

    def corrected_transcript_for_source(self, source: str) -> TranscriptRevision | None:
        """Latest correction derived from the requested artifact source."""
        matches = [row for row in self.transcript_revisions if row.source == source]
        return matches[-1] if matches else None


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))

    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    language: Mapped[str | None] = mapped_column(String(16), default=None)
    has_speakers: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(16), default="local")  # local | cloud

    text: Mapped[str] = mapped_column(Text, default="")
    # Full segment list (with words/speakers/timestamps) as JSON — see
    # asr.base.Segment for the shape.
    segments: Mapped[list] = mapped_column(JSON, default=list)
    resolved_profile_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="transcripts")


class Speaker(Base):
    """A stable per-recording speaker identity with an editable display name.

    ``key`` is the diarization label stored inside the transcript segment JSON
    (e.g. ``SPEAKER_00``) and never changes; ``display_name`` is what the user
    renames it to. ``None`` means "no custom name yet" — show the key.
    """

    __tablename__ = "speakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))

    key: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    file: Mapped[PlaudFile] = relationship(back_populates="speakers")

    __table_args__ = (UniqueConstraint("file_id", "key", name="uq_speaker_file_key"),)


class TranscriptRevision(Base):
    """A user correction of the transcript — never destroys the raw ASR row.

    Each edit produces the next ``revision`` for the file; the latest revision
    is the corrected canonical transcript. ``base_transcript_id`` points at the
    raw ASR transcript the revision chain was built on and is nullable so user
    edits survive a re-run of ASR (the raw row may be replaced, edits stay).
    """

    __tablename__ = "transcript_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))
    base_transcript_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transcripts.id", ondelete="SET NULL"), default=None
    )

    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Editing an imported Plaud transcript must retain cloud provenance.
    source: Mapped[str] = mapped_column(String(16), default="local")
    # Same shape as Transcript.segments (see asr.base.Segment).
    segments: Mapped[list] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text, default="")
    has_speakers: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str | None] = mapped_column(String(256), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="transcript_revisions")

    __table_args__ = (
        UniqueConstraint("file_id", "revision", name="uq_transcript_revision_file_revision"),
    )


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))

    # A file can have several notes under different templates (like Plaud's
    # multi-dimensional summaries).
    template: Mapped[str] = mapped_column(String(64), default="default")
    template_version: Mapped[int | None] = mapped_column(Integer, default=None)
    template_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    title: Mapped[str | None] = mapped_column(String(512), default=None)
    content_md: Mapped[str] = mapped_column(Text, default="")  # markdown
    llm_provider: Mapped[str | None] = mapped_column(String(64), default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    source: Mapped[str] = mapped_column(String(16), default="local")  # local | cloud
    input_transcript_id: Mapped[int | None] = mapped_column(Integer, default=None)
    input_transcript_revision: Mapped[int | None] = mapped_column(Integer, default=None)
    input_transcript_source: Mapped[str | None] = mapped_column(String(16), default=None)
    resolved_profile_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="summaries")

    __table_args__ = (UniqueConstraint("file_id", "template", name="uq_summary_file_template"),)


class NoteTemplate(Base):
    """Versioned, locally editable prompt used to generate structured notes."""

    __tablename__ = "note_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80))
    system_prompt: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    is_builtin: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_note_template_key_version"),
    )


class AskThread(Base):
    __tablename__ = "ask_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_id: Mapped[str | None] = mapped_column(
        ForeignKey("plaud_files.id", ondelete="CASCADE"), default=None, index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    messages: Mapped[list[AskMessage]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="AskMessage.id",
    )


class AskMessage(Base):
    __tablename__ = "ask_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("ask_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    thread: Mapped[AskThread] = relationship(back_populates="messages")


class UserNote(Base):
    __tablename__ = "user_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str | None] = mapped_column(
        ForeignKey("plaud_files.id", ondelete="CASCADE"), default=None, index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    content_md: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32), default="manual")
    ask_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("ask_messages.id", ondelete="SET NULL"), unique=True, default=None
    )
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    file: Mapped[PlaudFile | None] = relationship(back_populates="user_notes")


class ImportRun(Base):
    """Durable progress for a user-triggered metadata-only import."""

    __tablename__ = "import_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="plaud")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    changed_count: Mapped[int] = mapped_column(Integer, default=0)
    transcript_count: Mapped[int] = mapped_column(Integer, default=0)
    summary_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Chunk(Base):
    """A retrievable text chunk with its embedding, for Q&A / semantic search."""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))

    idx: Mapped[int] = mapped_column(Integer, default=0)  # order within the file
    text: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[float | None] = mapped_column(Float, default=None)  # seconds
    end: Mapped[float | None] = mapped_column(Float, default=None)
    speaker: Mapped[str | None] = mapped_column(String(64), default=None)

    embedding_model: Mapped[str | None] = mapped_column(String(128), default=None)
    dim: Mapped[int | None] = mapped_column(Integer, default=None)
    # float32 vector packed as bytes; decode with numpy.frombuffer.
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    input_transcript_id: Mapped[int | None] = mapped_column(Integer, default=None)
    input_transcript_revision: Mapped[int | None] = mapped_column(Integer, default=None)
    input_transcript_source: Mapped[str | None] = mapped_column(String(16), default=None)
    resolved_profile_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="chunks")


class StageRun(Base):
    """Durable state for one processing stage of one recording."""

    __tablename__ = "stage_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))
    stage: Mapped[StageName] = mapped_column(
        Enum(StageName, native_enum=False, length=32)
    )
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, native_enum=False, length=20), default=StageStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(64), default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    artifact_source: Mapped[str | None] = mapped_column(String(32), default=None)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    resolved_profile_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    file: Mapped[PlaudFile] = relationship(back_populates="stage_runs")

    __table_args__ = (UniqueConstraint("file_id", "stage", name="uq_stage_run_file_stage"),)


class ProviderConnection(Base):
    """Configured provider endpoint. Credentials live behind ``secret_ref`` only."""

    __tablename__ = "provider_connections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    provider_type: Mapped[str] = mapped_column(String(64))
    execution_target: Mapped[str] = mapped_column(String(32), default="local")
    data_egress: Mapped[bool] = mapped_column(default=False)
    secret_ref: Mapped[str | None] = mapped_column(String(256), default=None)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ModelCatalogEntry(Base):
    __tablename__ = "model_catalog_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="CASCADE")
    )
    model_key: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(256))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (
        UniqueConstraint("connection_id", "model_key", name="uq_model_connection_key"),
    )


class ExecutionProfile(Base):
    __tablename__ = "execution_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_system_default: Mapped[bool] = mapped_column(default=False)
    privacy_policy: Mapped[str] = mapped_column(String(32), default="allow-egress")
    no_egress: Mapped[bool] = mapped_column(default=False)
    cost_ceiling: Mapped[float | None] = mapped_column(Float, default=None)
    fallback_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    stage_selections: Mapped[list[ProfileStageSelection]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="ProfileStageSelection.id"
    )
    __table_args__ = (UniqueConstraint("key", "version", name="uq_profile_key_version"),)


class ProfileStageSelection(Base):
    __tablename__ = "profile_stage_selections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("execution_profiles.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="RESTRICT")
    )
    model_id: Mapped[int] = mapped_column(ForeignKey("model_catalog_entries.id", ondelete="RESTRICT"))
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    profile: Mapped[ExecutionProfile] = relationship(back_populates="stage_selections")
    connection: Mapped[ProviderConnection] = relationship()
    model_entry: Mapped[ModelCatalogEntry] = relationship()
    __table_args__ = (UniqueConstraint("profile_id", "stage", name="uq_profile_stage"),)


class RecordingProfileOverride(Base):
    __tablename__ = "recording_profile_overrides"
    file_id: Mapped[str] = mapped_column(
        ForeignKey("plaud_files.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(ForeignKey("execution_profiles.id"))
    stage_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RemoteWorker(Base):
    """Controller-side registration; authentication remains an env reference."""

    __tablename__ = "remote_workers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(1024))
    token_env: Mapped[str] = mapped_column(String(128), default="LOCALPLAUD_WORKER_TOKEN")
    protocol_version: Mapped[str | None] = mapped_column(String(16), default=None)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    health: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RemoteJob(Base):
    """Durable worker job ledger, including checksummed inline artifacts."""

    __tablename__ = "remote_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True)
    protocol_version: Mapped[str] = mapped_column(String(16), default="1")
    stage: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(256), default=None)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    input_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[dict | None] = mapped_column(JSON, default=None)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class KeyValue(Base):
    """Small persistent store for sync bookkeeping (cursors, last poll, etc.)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
