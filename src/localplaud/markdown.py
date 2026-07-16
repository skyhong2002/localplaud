"""Safe Markdown rendering shared by Web pages and JSON preview APIs."""

from markdown_it import MarkdownIt
from markupsafe import Markup

_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "linkify": False})
    .enable("table")
    .enable("strikethrough")
    .disable("image")
)


def render_markdown(value: str | None) -> Markup:
    """Render Markdown with raw HTML and unsafe link schemes disabled."""
    return Markup(_MARKDOWN.render(value or ""))
