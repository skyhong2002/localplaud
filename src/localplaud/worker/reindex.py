"""Re-index one recording without rerunning ASR.

Used after a transcript correction: the corrected canonical transcript (latest
revision) is re-chunked and re-embedded, and the durable ``index`` stage run
records the outcome. Summaries are deliberately not regenerated — that stays
an explicit user action.
"""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from ..db.models import StageName
from .pipeline import _begin_stage, _fail_stage, _finish_stage, _load_transcript, _persist_chunks

log = logging.getLogger(__name__)


def reindex_file(file_id: str, settings: Settings | None = None) -> bool:
    """Rebuild the embedding chunks for ``file_id`` from the canonical
    (corrected) transcript. Returns True on success; failures are recorded on
    the durable ``index`` stage run and never raise."""
    settings = settings or get_settings()
    _begin_stage(file_id, StageName.index)
    try:
        loaded = _load_transcript(file_id, settings)
        if loaded is None:
            raise ValueError(f"no canonical transcript to index for {file_id}")
        transcript, _source = loaded
        model_name = _persist_chunks(file_id, transcript, settings)
        _finish_stage(
            file_id,
            StageName.index,
            provider=settings.embeddings.provider,
            model=model_name,
            artifact_source="local",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - transcript/notes remain usable
        log.exception("Re-indexing failed for %s", file_id)
        _fail_stage(file_id, StageName.index, exc)
        return False
