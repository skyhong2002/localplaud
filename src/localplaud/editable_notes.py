"""Shared bounds for user-editable note bodies."""

import re

USER_NOTE_TITLE_MAX_LENGTH = 200
USER_NOTE_CONTENT_MAX_LENGTH = 200_000
USER_NOTE_PREVIEW_LENGTH = 240


class EditableNoteContentError(ValueError):
    """A source artifact cannot fit in the editable-note contract."""


def require_editable_note_content(content_md: str) -> None:
    if not content_md.strip():
        raise EditableNoteContentError("content must not be blank")
    if len(content_md) > USER_NOTE_CONTENT_MAX_LENGTH:
        raise EditableNoteContentError(
            "content is too large to create an editable note"
        )


def editable_note_preview(content_md: str) -> str:
    compact = re.sub(r"\s+", " ", content_md).strip()
    compact = re.sub(r"[*_`#>|~]+", "", compact).strip()
    return compact[:USER_NOTE_PREVIEW_LENGTH]
