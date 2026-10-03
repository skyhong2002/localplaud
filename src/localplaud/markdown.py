"""Safe Markdown rendering shared by Web pages and JSON preview APIs."""

import re

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markupsafe import Markup
from mdit_py_plugins.tasklists import tasklists_plugin


def _render_image(renderer, tokens, idx, options, env) -> str:
    token = tokens[idx]
    alt = renderer.renderInlineAsText(token.children or [], options, env)
    src = token.attrGet("src") or ""
    if not src.startswith("/") or src.startswith("//"):
        return escapeHtml(alt)
    token.attrSet("alt", alt)
    return renderer.renderToken(tokens, idx, options, env)

_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "linkify": False})
    .enable("table")
    .enable("strikethrough")
    .use(tasklists_plugin)
)
_MARKDOWN.add_render_rule("image", _render_image)


def render_markdown(value: str | None) -> Markup:
    """Render Markdown with raw HTML and unsafe link schemes disabled."""
    # Plaud embeds an app-only summary-card marker, not a retrievable image.
    # The cloud note panel explains the missing asset; never render a dead URI.
    value = re.sub(r"(?m)^\[\]\(plaud://image\?[^)\n]*\)[ \t]*$", "", value or "")
    return Markup(_MARKDOWN.render(value))


def render_markdown_blocks(value: str | None) -> Markup:
    """Render Markdown, tagging each top-level block with its source lines.

    ``data-md="start:end"`` (zero-based, end exclusive) lets the in-place note
    editor keep every block the user did not touch byte-for-byte, so only
    edited blocks are re-serialized from the page.
    """
    source = re.sub(r"(?m)^\[\]\(plaud://image\?[^)\n]*\)[ \t]*$", "", value or "")
    tokens = _MARKDOWN.parse(source)
    for token in tokens:
        if token.level == 0 and token.nesting in (0, 1) and token.map and token.block:
            token.attrSet("data-md", f"{token.map[0]}:{token.map[1]}")
    return Markup(_MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {}))
