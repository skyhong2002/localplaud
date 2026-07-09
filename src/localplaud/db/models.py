"""SQLAlchemy models — the local mirror + knowledge base.

Design: one row per Plaud cloud file (``PlaudFile``), holding both the cloud
metadata we sync and the state of our own local processing. Derived artifacts
(transcript, summary, embedding chunks) hang off it. Audio bytes live on the
filesystem; everything else lives here.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class FileStatus(str, enum.Enum):
    """Local lifecycle of a file, independent of the cloud's own flags."""

    discovered = "discovered"  # seen in the cloud listing, not yet downloaded
    downloading = "downloading"
    downloaded = "downloaded"  # audio on disk, pipeline not finished
    processing = "processing"
    done = "done"  # pipeline complete
    error = "error"


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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="file", uselist=False, cascade="all, delete-orphan"
    )
    summaries: Mapped[list["Summary"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="transcript")


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("plaud_files.id", ondelete="CASCADE"))

    # A file can have several notes under different templates (like Plaud's
    # multi-dimensional summaries).
    template: Mapped[str] = mapped_column(String(64), default="default")
    title: Mapped[str | None] = mapped_column(String(512), default=None)
    content_md: Mapped[str] = mapped_column(Text, default="")  # markdown
    llm_provider: Mapped[str | None] = mapped_column(String(64), default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    source: Mapped[str] = mapped_column(String(16), default="local")  # local | cloud

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="summaries")

    __table_args__ = (UniqueConstraint("file_id", "template", name="uq_summary_file_template"),)


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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    file: Mapped[PlaudFile] = relationship(back_populates="chunks")


class KeyValue(Base):
    """Small persistent store for sync bookkeeping (cursors, last poll, etc.)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
