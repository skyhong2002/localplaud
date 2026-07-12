"""Portable recording exports for the Web App."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from .config import get_settings
from .db.models import PlaudFile, StageName
from .db.session import session_scope
from .store.speakers import display_names

_PDF_FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "NotoSansTC.ttf"


def _stamp(seconds: float, *, milliseconds: bool = False) -> str:
    milliseconds_total = max(0, round(float(seconds or 0) * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def recording_data(file_id: str) -> dict:
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            raise ValueError(f"no such file: {file_id}")
        settings = get_settings()
        if settings.pipeline.artifact_mode == "independent":
            raw = file.local_transcript
        elif settings.pipeline.prefer_cloud_artifacts:
            raw = file.plaud_transcript or file.local_transcript
        else:
            raw = file.local_transcript
        corrected = file.corrected_transcript_for_source(raw.source) if raw else None
        segments = list((corrected.segments if corrected else raw.segments) or []) if raw else []
        names = display_names(session, file.id)
        stale = {
            run.stage for run in file.stage_runs if (run.detail or {}).get("stale")
        }
        summaries = [
            summary
            for summary in file.summaries
            if (settings.pipeline.artifact_mode != "independent" or summary.source == "local")
            and summary.template != "mind_map"
            and not (
                summary.source == "local"
                and StageName.summarize in stale
            )
        ]
        notes = [
            {
                "title": (summary.template_snapshot or {}).get("name")
                or summary.title
                or summary.template.replace("-", " ").title(),
                "content": summary.content_md,
            }
            for summary in summaries
        ] + [
            {"title": note.title, "content": note.content_md} for note in file.user_notes
        ]
        return {
            "title": file.display_title,
            "segments": segments,
            "speaker_names": names,
            "notes": notes,
            "audio_path": file.audio_path,
            "transcript_provenance": (
                {
                    "transcript_id": raw.id,
                    "transcript_source": raw.source,
                    "transcript_revision_id": corrected.id if corrected else None,
                    "transcript_revision": corrected.revision if corrected else None,
                }
                if raw
                else {}
            ),
        }


def transcript_provenance(file_id: str) -> dict:
    """Return the canonical transcript lineage used by transcript exports."""
    return recording_data(file_id)["transcript_provenance"]


def render_transcript(
    file_id: str,
    fmt: str,
    *,
    timestamps: bool = True,
    speakers: bool = True,
) -> tuple[bytes, str]:
    data = recording_data(file_id)
    segments = data["segments"]
    if not segments:
        raise LookupError("recording has no exportable transcript")

    def text_for(segment: dict) -> str:
        text = str(segment.get("text") or "").strip()
        speaker = segment.get("speaker")
        if speakers and speaker:
            text = f"{data['speaker_names'].get(speaker, speaker)}: {text}"
        return text

    def parts_for(segment: dict) -> tuple[str, str]:
        labels: list[str] = []
        if timestamps:
            labels.append(f"[{_stamp(segment.get('start') or 0)}]")
        speaker = segment.get("speaker")
        if speakers and speaker:
            labels.append(data["speaker_names"].get(speaker, speaker))
        return " · ".join(labels), str(segment.get("text") or "").strip()

    if fmt in {"srt", "vtt"}:
        blocks = []
        for index, segment in enumerate(segments, 1):
            start = _stamp(segment.get("start") or 0, milliseconds=True)
            end = _stamp(segment.get("end") or segment.get("start") or 0, milliseconds=True)
            if fmt == "vtt":
                start, end = start.replace(",", "."), end.replace(",", ".")
            blocks.append(f"{index}\n{start} --> {end}\n{text_for(segment)}")
        prefix = "WEBVTT\n\n" if fmt == "vtt" else ""
        return (prefix + "\n\n".join(blocks) + "\n").encode(), f"text/{fmt}"

    lines = []
    for segment in segments:
        prefix = f"[{_stamp(segment.get('start') or 0)}] " if timestamps else ""
        lines.append(prefix + text_for(segment))
    title = data["title"]
    if fmt == "txt":
        return (f"{title}\n\n" + "\n".join(lines) + "\n").encode(), "text/plain"
    if fmt == "docx":
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Inches, Pt, RGBColor

        document = Document()
        section = document.sections[0]
        section.page_width, section.page_height = Inches(8.5), Inches(11)
        section.top_margin = section.right_margin = Inches(1)
        section.bottom_margin = section.left_margin = Inches(1)
        section.header_distance = section.footer_distance = Inches(0.492)

        normal = document.styles["Normal"]
        normal.font.name = "Arial"
        normal.font.size = Pt(11)
        normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft JhengHei")
        normal.paragraph_format.space_before = Pt(0)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.25

        header = section.header.paragraphs[0]
        header.text = "localplaud · Transcript"
        header.runs[0].font.name = "Arial"
        header.runs[0].font.size = Pt(9)
        header.runs[0].font.color.rgb = RGBColor(102, 112, 133)

        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer_run = footer.add_run("Page ")
        footer_run.font.size = Pt(9)
        field = OxmlElement("w:fldSimple")
        field.set(qn("w:instr"), "PAGE")
        footer._p.append(field)

        title_paragraph = document.add_paragraph()
        title_paragraph.paragraph_format.space_after = Pt(5)
        title_run = title_paragraph.add_run(title)
        title_run.bold = True
        title_run.font.name = "Arial"
        title_run.font.size = Pt(22)
        title_run.font.color.rgb = RGBColor(11, 37, 69)
        subtitle = document.add_paragraph("Canonical transcript · exported by localplaud")
        subtitle.paragraph_format.space_after = Pt(18)
        subtitle.runs[0].font.size = Pt(10)
        subtitle.runs[0].font.color.rgb = RGBColor(102, 112, 133)

        for segment in segments:
            label, text = parts_for(segment)
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.keep_together = True
            if label:
                label_run = paragraph.add_run(label + "  ")
                label_run.bold = True
                label_run.font.color.rgb = RGBColor(31, 77, 120)
            paragraph.add_run(text)

        output = BytesIO()
        document.save(output)
        return output.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if fmt == "pdf":
        from reportlab.lib.colors import HexColor
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.pdfbase.pdfmetrics import registerFont, registerFontFamily
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        registerFont(TTFont("NotoSansTC", str(_PDF_FONT_PATH)))
        registerFontFamily(
            "NotoSansTC",
            normal="NotoSansTC",
            bold="NotoSansTC",
            italic="NotoSansTC",
            boldItalic="NotoSansTC",
        )
        output = BytesIO()
        document = SimpleDocTemplate(
            output,
            pagesize=letter,
            leftMargin=inch,
            rightMargin=inch,
            topMargin=0.82 * inch,
            bottomMargin=0.82 * inch,
            title=title,
            author="localplaud",
        )
        body = ParagraphStyle(
            "TranscriptBody",
            fontName="NotoSansTC",
            fontSize=11,
            leading=15,
            textColor=HexColor("#1F2937"),
            spaceAfter=8,
            alignment=TA_LEFT,
            splitLongWords=True,
        )
        title_style = ParagraphStyle(
            "TranscriptTitle",
            parent=body,
            fontSize=22,
            leading=27,
            textColor=HexColor("#0B2545"),
            spaceAfter=5,
        )
        subtitle_style = ParagraphStyle(
            "TranscriptSubtitle",
            parent=body,
            fontSize=9,
            leading=12,
            textColor=HexColor("#667085"),
            spaceAfter=18,
        )
        story = [
            Paragraph(escape(title), title_style),
            Paragraph("Canonical transcript · exported by localplaud", subtitle_style),
        ]
        for segment in segments:
            label, text = parts_for(segment)
            content = f'<font color="#1F4D78"><b>{escape(label)}</b></font>  ' if label else ""
            story.append(Paragraph(content + escape(text).replace("\n", "<br/>"), body))
            story.append(Spacer(1, 1))

        def decorate_page(canvas, doc):
            canvas.saveState()
            canvas.setFont("NotoSansTC", 8)
            canvas.setFillColor(HexColor("#667085"))
            canvas.drawString(inch, 10.45 * inch, "localplaud · Transcript")
            canvas.drawRightString(7.5 * inch, 0.48 * inch, f"Page {doc.page}")
            canvas.restoreState()

        document.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
        return output.getvalue(), "application/pdf"
    raise ValueError("unsupported transcript format")


def render_notes(file_id: str, fmt: str) -> tuple[bytes, str]:
    data = recording_data(file_id)
    if not data["notes"]:
        raise LookupError("recording has no exportable notes")
    lines: list[str] = []
    markdown: list[str] = [f"# {data['title']}", ""]
    for note in data["notes"]:
        lines += [note["title"], note["content"], ""]
        markdown += [f"## {note['title']}", "", note["content"].strip(), ""]
    if fmt == "md":
        return "\n".join(markdown).encode(), "text/markdown"
    if fmt == "txt":
        return (data["title"] + "\n\n" + "\n".join(lines)).encode(), "text/plain"
    raise ValueError("unsupported notes format")
