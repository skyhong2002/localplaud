"""Stable speaker bindings for locally generated Markdown notes.

Bindings are checked character spans, not name search-and-replace. Materializing
names keeps HTML, exports, copies, public shares and Ask evidence on the same
stored content. Prior generations/renames remain immutable in SummaryRevision.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy

from sqlalchemy import select

from .db.models import PlaudFile, Summary
from .store.speakers import speaker_labels

SPEAKER_ATTRIBUTION_PROMPT_VERSION = "speaker-attribution/v1"
_BINDING = "speaker_bindings"
_FIELDS = ("content_md", "title")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _name(value: str, *, markdown: bool) -> str:
    value = " ".join(value.split())
    return re.sub(r"([\\`*_[\]<>#|~(){}!+.\-])", r"\\\1", value) if markdown else value


def _protected(text: str) -> list[tuple[int, int]]:
    # Speaker-like words in code, URLs, link destinations and HTML are content,
    # never attribution. Leave them byte-for-byte intact.
    return [
        (m.start(), m.end())
        for m in re.finditer(
            r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\n]*`|https?://[^\s<>]+|\]\([^\n)]*\)|<[^>\n]+>",
            text,
        )
    ]


def _valid(field: dict | None, text: str) -> bool:
    if not isinstance(field, dict) or field.get("sha256") != _digest(text):
        return False
    end = 0
    for span in field.get("spans", []):
        if not isinstance(span, dict):
            return False
        start, stop = span.get("start"), span.get("end")
        if type(start) is not int or type(stop) is not int or not end <= start < stop <= len(text):
            return False
        if text[start:stop] != span.get("text") or not isinstance(span.get("key"), str):
            return False
        end = stop
    return True


def _attribution(text: str, start: int, end: int) -> bool:
    """Only explicit name-as-speaker syntax is safe for legacy personal names."""
    prefix = text[text.rfind("\n", 0, start) + 1 : start]
    suffix = text[end : text.find("\n", end) if "\n" in text[end:] else len(text)]
    # Bold labels followed by colon, list/header labels, or first table column.
    if re.fullmatch(r"\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?", prefix):
        return bool(re.match(r"(?:\*\*|__)?\s*[:：]", suffix))
    if re.fullmatch(r"\s*#{1,6}\s+(?:\*\*|__)?", prefix):
        return bool(re.fullmatch(r"(?:\*\*|__)?\s*", suffix))
    return bool(
        re.fullmatch(r"\s*\|\s*(?:\*\*|__)?", prefix) and re.match(r"(?:\*\*|__)?\s*\|", suffix)
    )


def _bind(
    text: str,
    anonymous: dict[str, str],
    previous_names: dict[str, str | None],
    *,
    title: bool = False,
) -> tuple[dict, bool]:
    aliases: dict[str, set[str]] = {}
    for key, label in anonymous.items():
        for alias in (key, label):
            aliases.setdefault(alias, set()).add(key)
    personal: dict[str, set[str]] = {}
    for key, value in previous_names.items():
        if value and key in anonymous:
            personal.setdefault(value, set()).add(key)
    protected = [] if title else _protected(text)
    spans = []
    unresolved = False
    # Longest alias first; overlap checks prevent partial duplicate bindings.
    for alias in sorted(set(aliases) | set(personal), key=len, reverse=True):
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])"
        candidates = aliases.get(alias, set()) | personal.get(alias, set())
        for match in re.finditer(pattern, text):
            start, end = match.span()
            if any(start < b and end > a for a, b in protected) or any(
                start < s["end"] and end > s["start"] for s in spans
            ):
                continue
            if len(candidates) != 1 or (
                alias not in aliases and (title or not _attribution(text, start, end))
            ):
                unresolved = True
                continue
            spans.append(
                {"key": next(iter(candidates)), "start": start, "end": end, "text": match.group()}
            )
    return {
        "sha256": _digest(text),
        "spans": sorted(spans, key=lambda span: span["start"]),
    }, unresolved


def _project(text: str, field: dict, labels: dict[str, str], *, markdown: bool) -> tuple[str, dict]:
    pieces, spans = [], []
    cursor = length = 0
    for span in field["spans"]:
        before = text[cursor : span["start"]]
        pieces.append(before)
        length += len(before)
        replacement = _name(labels.get(span["key"], span["text"]), markdown=markdown)
        pieces.append(replacement)
        spans.append(
            {
                "key": span["key"],
                "start": length,
                "end": length + len(replacement),
                "text": replacement,
            }
        )
        length += len(replacement)
        cursor = span["end"]
    pieces.append(text[cursor:])
    result = "".join(pieces)
    return result, {"sha256": _digest(result), "spans": spans}


def bind_generated_summary(session, summary: Summary) -> None:
    """Bind model-produced anonymous labels and materialize current names in-place.

    Called before first persistence. No personal-name inference is performed.
    """
    if summary.source != "local" or summary.input_transcript_source != "local":
        return
    anonymous = speaker_labels(session, summary.file_id, anonymous=True)
    labels = speaker_labels(session, summary.file_id)
    binding = {"version": 1, "anonymous_labels": anonymous, "unresolved": False, "fields": {}}
    binding["prompt_version"] = SPEAKER_ATTRIBUTION_PROMPT_VERSION
    for field in _FIELDS:
        original = getattr(summary, field) or ""
        bound, _ = _bind(original, anonymous, {}, title=field == "title")
        projected, bound = _project(original, bound, labels, markdown=field == "content_md")
        setattr(summary, field, projected if getattr(summary, field) is not None else None)
        binding["fields"][field] = bound
    if any(field["spans"] for field in binding["fields"].values()):
        summary.template_snapshot = dict(summary.template_snapshot or {}) | {_BINDING: binding}


def anonymous_summary_content(summary: Summary) -> str:
    """Feed a dependent generator the bound labels, keeping personal prose intact."""
    binding = (summary.template_snapshot or {}).get(_BINDING) or {}
    field = (binding.get("fields") or {}).get("content_md")
    text = summary.content_md or ""
    if binding.get("version") != 1 or not _valid(field, text):
        return text
    from .store.speakers import anonymous_speaker_labels

    labels = binding.get("anonymous_labels") or anonymous_speaker_labels(
        [span["key"] for span in field["spans"]]
    )
    return _project(text, field, labels, markdown=True)[0]


def refresh_generated_note_speaker_names(
    session, file_id: str, *, previous_names: dict[str, str | None] | None = None
) -> dict:
    """Archive and rename only bound local generated note/map spans atomically.

    Caller writes/flushed Speaker names first and owns transcript-index scheduling.
    Existing stage stale flags are deliberately untouched. User notes, historical
    versions, cloud artifacts, pronouns and unbound personal prose are immutable.
    """
    from .note_history import archive_summary, fingerprint_digest, source_summary_provenance
    from .worker.knowledge_index import lock_summary_for_mutation, sync_summary_document

    anonymous = speaker_labels(session, file_id, anonymous=True)
    labels = speaker_labels(session, file_id)
    from .store.speakers import display_names

    previous_names = display_names(session, file_id) if previous_names is None else previous_names
    rows = list(
        session.scalars(
            select(Summary)
            .where(Summary.file_id == file_id, Summary.source == "local")
            .order_by(Summary.id)
        )
    )
    rows = [row for row in rows if row.input_transcript_source not in {"cloud", "plaud"}]
    changed_ids, unresolved_ids = [], []
    recording = session.get(PlaudFile, file_id)
    previous_recording_title = recording.generated_title if recording is not None else None
    projected_recording_titles: set[str] = set()
    title_source_ids = {
        row.id
        for row in rows
        if recording is not None
        and row.template != "mind_map"
        and recording.generated_title_provider
        and recording.generated_title_model
        and row.llm_provider == recording.generated_title_provider
        and row.model == recording.generated_title_model
        and row.title == previous_recording_title
    }
    old_fingerprints = {row.id: fingerprint_digest(row) for row in rows}
    for original in rows:
        row = lock_summary_for_mutation(session, original.id, file_id)
        snapshot = deepcopy(row.template_snapshot or {})
        old_binding = snapshot.get(_BINDING) or {}
        binding = {"version": 1, "anonymous_labels": anonymous, "unresolved": False, "fields": {}}
        if old_binding.get("prompt_version"):
            binding["prompt_version"] = old_binding["prompt_version"]
        unresolved = bool(old_binding.get("unresolved"))
        if old_binding and old_binding.get("version") != 1:
            if not old_binding.get("unresolved"):
                archive_summary(session, row, reason="speaker_binding")
                row.template_snapshot = snapshot | {_BINDING: old_binding | {"unresolved": True}}
                changed_ids.append(row.id)
                session.flush()
            unresolved_ids.append(row.id)
            continue
        changes = {}
        for field in _FIELDS:
            text = getattr(row, field) or ""
            bound = (old_binding.get("fields") or {}).get(field)
            if old_binding and not _valid(bound, text):
                # A generated body changed without matching binding metadata.
                # Never reinterpret current personal prose as an old stable alias.
                binding["fields"][field] = {"sha256": _digest(text), "spans": []}
                changes[field] = getattr(row, field)
                unresolved = True
                continue
            if not _valid(bound, text):
                bound, ambiguous = _bind(text, anonymous, previous_names, title=field == "title")
                unresolved |= ambiguous
            projected, bound = _project(text, bound, labels, markdown=field == "content_md")
            binding["fields"][field] = bound
            changes[field] = projected if getattr(row, field) is not None else None
        binding["unresolved"] = unresolved
        if unresolved:
            unresolved_ids.append(row.id)
        if any(getattr(row, field) != value for field, value in changes.items()):
            # A recording title is a copy of one generated note title, not a
            # free-text alias field. Only a matching generated source and exact
            # old title can authorize projecting its already-bound spans.
            if (
                recording is not None
                and row.template != "mind_map"
                and len(title_source_ids) == 1
                and row.id in title_source_ids
                and recording.generated_title_provider
                and recording.generated_title_model
                and row.llm_provider == recording.generated_title_provider
                and row.model == recording.generated_title_model
                and row.title == previous_recording_title
                and changes["title"]
                and changes["title"] != row.title
                and binding["fields"]["title"]["spans"]
            ):
                projected_recording_titles.add(changes["title"])
            archive_summary(session, row, reason="speaker_rename")
            for field, value in changes.items():
                setattr(row, field, value)
            row.template_snapshot = snapshot | {_BINDING: binding}
            changed_ids.append(row.id)
        elif old_binding != binding and (
            unresolved or any(field["spans"] for field in binding["fields"].values())
        ):
            # Metadata-only adoption still archives the exact old generated artifact.
            archive_summary(session, row, reason="speaker_binding")
            row.template_snapshot = snapshot | {_BINDING: binding}
            changed_ids.append(row.id)
        session.flush()
    # A name projection changes a source-note fingerprint but not its meaning.
    # Update only a map whose prior source identity was provably this very note.
    by_template = {row.template: row for row in rows if row.template != "mind_map"}
    for row in rows:
        if row.template != "mind_map":
            continue
        snapshot = deepcopy(row.template_snapshot or {})
        source = by_template.get(snapshot.get("source_template_key"))
        if source is None or source.id not in changed_ids:
            continue
        recorded = (snapshot.get("source_note") or {}).get("content_fingerprint")
        if recorded != old_fingerprints[source.id]:
            continue
        if row.id not in changed_ids:
            archive_summary(session, row, reason="speaker_rename")
            changed_ids.append(row.id)
        snapshot["source_note"] = dict(
            snapshot.get("source_note") or {}
        ) | source_summary_provenance(source)
        row.template_snapshot = snapshot
    # Competing same-name notes must not select a different speaker by accident.
    # A manual local_title always remains the preferred user-owned display title.
    if recording is not None and len(projected_recording_titles) == 1:
        recording.generated_title = next(iter(projected_recording_titles))
    session.flush()
    for row in rows:
        if row.id in changed_ids:
            sync_summary_document(session, row)
    return {
        "recording_title": recording.generated_title if recording is not None else None,
        "display_title": recording.display_title if recording is not None else None,
        "recording_title_changed": bool(
            recording is not None and recording.generated_title != previous_recording_title
        ),
        "changed_ids": changed_ids,
        "unresolved_ids": unresolved_ids,
        "changed": len(changed_ids),
        "unresolved": len(unresolved_ids),
    }
