"""Render a complete Markdown mind-map outline to a portable PNG."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_MAX_LINES = 2_000
_MAX_PIXELS = 40_000_000


@dataclass(frozen=True)
class MindMapLine:
    depth: int
    text: str
    root: bool = False


def parse_outline(markdown: str) -> list[MindMapLine]:
    """Parse the normalized heading/bullet form without dropping prose."""
    rows: list[MindMapLine] = []
    for raw in markdown.splitlines():
        if not raw.strip():
            continue
        heading = re.match(r"^\s*#+\s+(.+?)\s*$", raw)
        if heading:
            rows.append(MindMapLine(0, heading.group(1), root=not rows))
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.+?)\s*$", raw)
        if bullet:
            rows.append(MindMapLine(len(bullet.group(1).expandtabs(2)) // 2 + 1, bullet.group(2)))
            continue
        rows.append(MindMapLine(1, raw.strip()))
    if not rows:
        raise ValueError("mind map is empty")
    if len(rows) > _MAX_LINES:
        raise ValueError(f"mind map has more than {_MAX_LINES} nodes; export a smaller revision")
    return rows


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    """Pixel-aware wrapping that also handles CJK text without spaces."""
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current.rstrip())
            current = char.lstrip()
        else:
            current = candidate
    if current or not lines:
        lines.append(current.rstrip())
    return lines


def render_mind_map_png(markdown: str, *, title: str | None = None) -> bytes:
    """Return a lossless PNG containing every parsed node in outline order."""
    rows = parse_outline(markdown)
    width, padding, indent = 1400, 54, 42
    body_font, root_font, title_font = _font(20), _font(27), _font(30)
    measuring = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    laid_out: list[tuple[MindMapLine, list[str], int]] = []
    height = padding + (48 if title else 0)
    for row in rows:
        font = root_font if row.root else body_font
        available = width - padding * 2 - row.depth * indent - 34
        wrapped = _wrap(measuring, row.text, font, max(180, available))
        row_height = len(wrapped) * (34 if row.root else 28) + 14
        laid_out.append((row, wrapped, row_height))
        height += row_height
    height += padding
    if width * height > _MAX_PIXELS:
        raise ValueError("mind map PNG would exceed the safe image-size limit")

    image = Image.new("RGB", (width, height), "#f7f8fb")
    draw = ImageDraw.Draw(image)
    y = padding
    if title:
        draw.text((padding, y), title, fill="#111827", font=title_font)
        y += 48
    parent_x: dict[int, int] = {}
    for row, wrapped, row_height in laid_out:
        x = padding + row.depth * indent
        font = root_font if row.root else body_font
        color = "#111827" if row.root else "#263244"
        center_y = y + 13
        if row.depth:
            dot_x = x - 16
            draw.ellipse((dot_x - 4, center_y - 4, dot_x + 4, center_y + 4), fill="#1677ff")
            parent = parent_x.get(row.depth - 1, padding)
            draw.line((parent, center_y, dot_x - 6, center_y), fill="#a8b5c7", width=2)
            parent_x[row.depth] = dot_x
        for index, line in enumerate(wrapped):
            draw.text((x, y + index * (34 if row.root else 28)), line, fill=color, font=font)
        y += row_height

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
