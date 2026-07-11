"""Re-index one recording without rerunning ASR.

Used after a transcript correction: the corrected canonical transcript (latest
revision) is re-chunked and re-embedded, and the durable ``index`` stage run
records the outcome. Summaries are deliberately not regenerated — that stays
an explicit user action.
"""

from __future__ import annotations

import logging
import threading

from ..config import Settings, get_settings
from ..db.models import PlaudFile, StageName
from ..db.session import session_scope
from ..store.speakers import display_names
from .pipeline import (
    _begin_stage,
    _fail_stage,
    _finish_stage,
    _load_transcript,
    _persist_chunks,
    _select_raw_transcript,
    _transcript_lineage,
)

log = logging.getLogger(__name__)

_locks_guard = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def _file_lock(file_id: str) -> threading.Lock:
    with _locks_guard:
        return _file_locks.setdefault(file_id, threading.Lock())


def reindex_file(
    file_id: str,
    settings: Settings | None = None,
    *,
    expected_revision: int | None = None,
    expected_speaker_names: dict[str, str] | None = None,
) -> bool:
    """Rebuild the embedding chunks for ``file_id`` from the canonical
    (corrected) transcript. Returns True on success; failures are recorded on
    the durable ``index`` stage run and never raise."""
    settings = settings or get_settings()
    with _file_lock(file_id):
        with session_scope() as session:
            row = session.get(PlaudFile, file_id)
            if row is None:
                return False
            raw = _select_raw_transcript(row, settings)
            source = raw.source if raw is not None else "local"
            revision = row.corrected_transcript_for_source(source)
            current_revision = revision.revision if revision is not None else 0
            current_names = display_names(session, file_id)
        # A newer edit/rename already superseded this queued background job.
        if expected_revision is not None and current_revision != expected_revision:
            return False
        if expected_speaker_names is not None and current_names != expected_speaker_names:
            return False

        _begin_stage(file_id, StageName.index)
        try:
            loaded = _load_transcript(file_id, settings)
            if loaded is None:
                raise ValueError(f"no canonical transcript to index for {file_id}")
            transcript, _source = loaded
            lineage = _transcript_lineage(file_id, settings)
            model_name = _persist_chunks(file_id, transcript, settings, lineage)
            _finish_stage(
                file_id,
                StageName.index,
                provider=settings.embeddings.provider,
                model=model_name,
                artifact_source="local",
                detail={"transcript": lineage},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - transcript/notes remain usable
            log.exception("Re-indexing failed for %s", file_id)
            _fail_stage(file_id, StageName.index, exc)
            return False
