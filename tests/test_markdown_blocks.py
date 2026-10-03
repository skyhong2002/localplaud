"""Note blocks carry source line ranges for the in-place editor."""

import re

from localplaud.markdown import render_markdown, render_markdown_blocks

SOURCE = """## Heading

Paragraph **bold**
continued.

- item
  - nested

| a | b |
| --- | --- |
| 1 | 2 |

```
code
```"""


def test_top_level_blocks_map_to_their_source_lines():
    html = str(render_markdown_blocks(SOURCE))
    lines = SOURCE.split("\n")
    ranges = [tuple(map(int, match.split(":"))) for match in re.findall(r'data-md="([^"]+)"', html)]

    assert ranges[0] == (0, 1) and lines[0] == "## Heading"
    assert "\n".join(lines[2:4]) == "Paragraph **bold**\ncontinued."
    assert (2, 4) in ranges
    # Nested blocks are owned by their top-level list, not mapped separately.
    assert html.count("data-md=") == 5
    assert '<ul data-md="5:' in html


def test_source_map_does_not_change_rendered_content():
    plain = str(render_markdown(SOURCE))
    mapped = re.sub(r' data-md="[^"]+"', "", str(render_markdown_blocks(SOURCE)))
    assert mapped == plain


def test_unsafe_content_stays_escaped():
    html = str(render_markdown_blocks("<script>alert(1)</script>\n\n[x](javascript:alert(1))"))
    assert "<script>" not in html
    assert "href=\"javascript:" not in html
