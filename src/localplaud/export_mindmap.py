"""Render a generated Markdown mind-map outline to a portable PNG."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MindMapLine:
    depth: int
    text: str
    heading: bool = False


def parse_outline(markdown: str) -> list[MindMapLine]:
    lines: list[MindMapLine] = []
    for raw in markdown.splitlines():
        if not raw.strip():
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", raw.strip())
        if heading:
            lines.append(MindMapLine(max(0, len(heading.group(1)) - 1),
                                     heading.group(2).strip(), True))
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.+)$", raw)
        if bullet:
            lines.append(MindMapLine(len(bullet.group(1).expandtabs(2)) // 2 + 1,
                                     bullet.group(2).strip()))
            continue
        lines.append(MindMapLine(1, raw.strip()))
    return lines or [MindMapLine(0, "Mind map", True)]


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size, index=0)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_mindmap_png(markdown: str, *, title: str = "Mind map") -> bytes:
    from PIL import Image, ImageDraw

    outline = parse_outline(markdown)
    width, padding, indent = 1600, 64, 48
    body_font = _font(24)
    heading_font = _font(28, bold=True)
    title_font = _font(38, bold=True)
    row_gap = 14

    # Pixel-aware wrapping keeps the export readable without dropping long nodes.
    wrapped: list[tuple[MindMapLine, list[str]]] = []
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for item in outline:
        font = heading_font if item.heading else body_font
        available = max(280, width - padding * 2 - item.depth * indent - 36)
        words = list(item.text) if " " not in item.text else item.text.split(" ")
        separator = "" if " " not in item.text else " "
        rows: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else current + separator + word
            if current and probe.textlength(candidate, font=font) > available:
                rows.append(current)
                current = word
            else:
                current = candidate
        rows.append(current or item.text)
        wrapped.append((item, rows))

    title_height = 74
    height = padding * 2 + title_height + sum(
        (34 if item.heading else 30) * len(rows) + row_gap for item, rows in wrapped
    )
    image = Image.new("RGB", (width, max(260, height)), "#f8fafc")
    draw = ImageDraw.Draw(image)
    draw.text((padding, padding), title, font=title_font, fill="#0f172a")
    draw.line((padding, padding + 56, width - padding, padding + 56), fill="#cbd5e1", width=2)

    y = padding + title_height
    ancestor_x: dict[int, int] = {}
    for item, rows in wrapped:
        x = padding + item.depth * indent
        font = heading_font if item.heading else body_font
        line_height = 34 if item.heading else 30
        block_height = line_height * len(rows) + 8
        node_y = y + block_height // 2
        if item.depth:
            parent_x = ancestor_x.get(item.depth - 1, padding)
            draw.line((parent_x + 12, node_y, x - 12, node_y), fill="#94a3b8", width=2)
        draw.ellipse((x - 7, node_y - 7, x + 7, node_y + 7), fill="#2563eb")
        for offset, row in enumerate(rows):
            draw.text((x + 22, y + offset * line_height), row, font=font,
                      fill="#0f172a" if item.heading else "#334155")
        ancestor_x[item.depth] = x
        y += block_height + row_gap

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
