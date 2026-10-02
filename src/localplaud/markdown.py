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
