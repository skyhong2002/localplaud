"""Named note templates, including read-only Plaud Web prompt snapshots.

Structured templates use the local markdown frame. Direct templates preserve a
prompt exposed by Plaud Web and append the transcript without adding a local
section schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from textwrap import dedent

log = logging.getLogger(__name__)


@dataclass
class SummaryTemplate:
    name: str
    system: str | None
    instructions: str  # the markdown-section instructions block
    version: int = 1
    display_name: str | None = None
    prompt_mode: str = "structured"
    provenance: str | None = None


# Only prompts captured from the user's signed-in Plaud Web "最近使用" surface
# belong in the built-in catalog. Do not add locally authored substitute prompts.
TEMPLATES: dict[str, SummaryTemplate] = {}


def _plaud_template(
    key: str,
    display_name: str,
    prompt: str,
) -> SummaryTemplate:
    return SummaryTemplate(
        name=key,
        system=None,
        instructions=dedent(prompt).strip(),
        display_name=display_name,
        prompt_mode="direct",
        provenance="plaud-web-readonly",
    )


# These are read-only prompt snapshots from the signed-in Plaud Web account's
# visible "最近使用" surface. Where Plaud exposed "查看原文", the original
# prompt is retained; otherwise the translated UI text is retained verbatim.
TEMPLATES.update(
    {
        "plaud-autopilot": _plaud_template(
            "plaud-autopilot",
            "智能總結",
            """
            Autopilot

            Autopilot 模板提供了壹種根據內容智能匹配最這合結構的總結方式：

            自這應結構：根據內容選擇最合這的總結結構。
            全場景這應：這用於各種場景，包括會議、采訪、講座等。
            Tips：如果您需要某些特定場景的特定格式，如醫療（例如，SOAP模板），建議選擇專用模板。
            """,
        ),
        "plaud-key-metrics": _plaud_template(
            "plaud-key-metrics",
            "關鍵數據",
            """
            關鍵量化數據可將人工智慧轉變為細緻的數據分析師，旨在解析非結構化對話並提取有價值的量化洞察。其處理過程有明確的規則集和標準化的輸出格式。

            角色與任務

            此模板首先將人工智慧設定為專業數據分析師的角色。其主要任務是識別文本中的每一項數值數據，理解其含義，並清晰地構建數據結構。

            提取規則

            這是關鍵量化數據的核心機制，指導人工智慧如何高精度地填充最終表格的每一列：

            實際數據：該規則用於直接提取數字本身。即便數字是間接提及（例如“那個數字”），也能通過追溯對話源頭來獲取具體數值。
            數據項：這是一條獨特且強大的規則，用於定義每個數字的正式名稱。人工智慧會分析周邊所有描述，並依據“頻率優先、時間優先”原則，通過算法確定該指標最準確、最相關的名稱。
            單位：基於數據項的上下文，此規則指導人工智慧推斷合理的度量單位，如“天”、“%”、“美元”或“件”等，增添專業的格式規範。
            關鍵點：為確保不遺漏任何上下文信息，該規則要求人工智慧總結數字周邊的討論內容。它會捕捉與該指標相關的關鍵背景、假設、決策或爭論點，並以簡潔的要點形式呈現。
            輸出格式

            最後，關鍵量化數據要求將所有提取的信息整理成清晰易讀的 Markdown 表格。這樣一來，雜亂無章的非結構化對話數據就會轉化為結構化報告，可用於分析、審核或納入其他文檔。
            """,
        ),
        "plaud-intent-analysis": _plaud_template(
            "plaud-intent-analysis",
            "意圖分析",
            """
            逐節詳細描述

            此提示分為三個不同部分——任務要求、分析視角和情報簡報，旨在引導分析從宏觀理念落實到精準、可操作的結果。

            【分析師任務要求】
            這部分設定了人工智能的核心角色和目標。它並非只是一台機器，而是像經驗豐富的外交官一樣的「溝通智能」專家。這部分確立了基本原則：透過字面意思探尋潛在目標（即「待完成的工作」），確保最終見解切實可行，並保持客觀、中立的視角。

            【診斷視角】
            這是提示的分析核心。它為人工智能提供了一套特定的文本分析工具。人工智能不僅要閱讀文字，還需從四個不同的「視角」進行審視：

            情感節奏：找出對話中情感能量最高的地方。
            沉默的內涵：將停頓和猶豫解讀為有意義的數據。
            用詞的分量：分析過度解釋或輕描淡寫，將其視為不自信或迴避的跡象。
            語境意識：這是一個關鍵的約束條件，考慮到在文化或正式場合中委婉表達是正常現象。

            【情報簡報：輸出格式】
            這部分定義了最終成果的結構，確保其成為一份簡潔且具戰略性的工具。輸出形式為專業的「簡報」，包含三個關鍵部分：

            主要意圖假設：這是核心結論——最有可能的單一意圖。它包括置信水平（高、中、低），以明確處理不確定性。
            診斷依據：這是證據。它要求人工智能將假設建立在文本中具體、可觀察的行為和引述之上，使分析具有可驗證性。
            戰略應對方向：這是可操作的要點。它並非簡單的建議回覆，而是根據推斷出的意圖，推薦一種最佳的對話應對策略。
            一句話精髓

            此提示解讀微妙的對話線索，生成一份簡潔的情報簡報，明確說話者的核心意圖、支持該意圖的證據以及應對的戰略建議。
            """,
        ),
        "plaud-meeting-narrative": _plaud_template(
            "plaud-meeting-narrative",
            "會議記錄轉為完整敘述紀錄",
            """
            Transform a raw meeting transcript into a comprehensive, detailed narrative record, capturing key points, discussions, decisions, action items, participant contributions, and conversational flow to reflect the meeting's context, tone, and dynamics.
            Reconstruct the meeting chronologically as a narrative, detailing who raised discussion points, arguments, supporting data, responses, decisions, rationales, dissenting opinions, and unresolved issues.
            Document all action items, responsible parties, and deadlines, while preserving the meeting's tone, mood, and atmosphere.
            Conclude with a summary of next steps, follow-up plans, and closing remarks, ensuring the narrative is comprehensive, detailed, and faithful to the original transcript, using "[fill in the blank]" for missing details.
            Format the record as a clear, chronological narrative with paragraphs and direct quotes, using subheadings for topic shifts and bolding action items and decisions, avoiding bullet points for a story-like account.
            Target audience is ChatGPT 4o or ChatGPT o1, acting as a professional assistant for business leaders, to create a full, accurate, and engaging meeting record suitable for absent stakeholders.
            Employ professional, clear language accessible to a business audience for the narrative.
            """,
        ),
        "plaud-lecture-deep-dive": _plaud_template(
            "plaud-lecture-deep-dive",
            "深度詳盡的演講細節、引言與概念",
            """
            The primary purpose is to generate high-fidelity summaries of lectures and keynotes, capturing the speaker's tone, concepts, and actionable insights.
            The summary should clearly state the main theme, include the presentation title, key terminology, metaphors, and catchphrases.
            A summary of the main message should be provided in one or two powerful paragraphs, mirroring the speaker's tone and word choice.
            The summary must identify the relevance of the message, the problem it solves, or its timeless/unique insight.
            The summary must be structured following the speaker's flow, broken down into clear parts or chapters.
            Each part of the summary needs a name (either the original title or a descriptive one).
            For each part, the core idea, purpose, and importance must be summarized.
            Main arguments, concepts, and theories presented by the speaker should be highlighted.
            Any frameworks, models, steps, methodologies, or tools presented by the speaker must be included and explained in detail.
            Examples, analogies, metaphors, or short stories used by the speaker to illustrate points should be included.
            Audience engagement moments, such as rhetorical questions, jokes, surprising statistics, or emotional stories, should be highlighted.
            A collection of 8 to 15 direct quotes should be extracted, focusing on catchphrases, inspirational statements, memorable metaphors, definitions, challenges, and shareable insights.
            All quotes must be faithful to the speaker's tone and clearly attributed.
            Quotes should be usable out of context but more impactful within the summary.
            Key concepts introduced in the lecture should be listed with brief, rich descriptions.
            This includes theoretical foundations, models, mental frameworks, scientific insights, data references, research findings, counterintuitive ideas, and challenging assumptions.
            The section on core concepts should be structured as a toolbox for teaching.
            3 to 7 clear, practical, and strategic action points, directly inspired by the keynote, should be listed.
            Each action point should be written as a command or guideline.
            Action points should include brief justification from the keynote or a quote.
            Action points should be relevant for teams, leaders, or individuals and applicable in real-world settings.
            A bonus is to highlight one first step recommended by the speaker and use the speaker's phrasing for memorability.
            The summary must mimic the speaker's tone, style, and terminology, reusing jargon, recurrent expressions, and unique idea structuring.
            Consistency with the speaker's framing of insights is crucial for the summary to sound like it originated from them.
            This ensures the document can be shared as a faithful reflection, allows mental and emotional reliving of the event, and preserves the energy and clarity of the original experience.
            An optional bonus section titled "What you missed if you weren’t there" can recap the speaker's personality, presence, room energy, emotional turning points, unexpected moments, and impactful one-liners.
            """,
        ),
        "plaud-research-interview": _plaud_template(
            "plaud-research-interview",
            "研究訪談",
            """
            采訪記錄

            時間：2024-07-03 17:00:23
            地點：會議室12
            受訪者：Smith

            介紹

            概述訪談的上下文及受訪者的背景。

            采訪精華
            采訪的要點1
            采訪的要點2
            采訪的要點3
            采訪過程
            問題 1
            受訪者的回答1
            受訪者的回答2
            問題 2
            受訪者的回答1
            受訪者的回答2
            """,
        ),
        "plaud-interview": _plaud_template(
            "plaud-interview",
            "採訪",
            """
            采访稿件需整理并结构化输出，包含背景信息、问答实录和价值点提炼。
            背景信息部分需记录被访者姓名、采访主题，并用★标注核心观点。
            问答实录部分需记录每个问题，并包含回应摘要、直接引语（标注情感倾向）和回答时长。
            问答实录中，需记录数据披露（如年产量）和回避内容（标示◆）。
            价值点提炼需包含行业洞见（用★标注）和争议观点（需法律审核标示◆）。
            被访者的动作描述需用括号补充，如[笑]或[停顿]。
            """,
        ),
        "plaud-full-transcript": _plaud_template(
            "plaud-full-transcript",
            "完整轉錄（供外部使用）",
            """
            Trascrivere fedelmente e integralmente tutto ciò che viene detto nella registrazione, senza riassumere, interpretare o modificare il contenuto.
            Mantenere l’ordine cronologico esatto degli interventi e specificare il cambio di voce quando possibile (es. “Dottore:”, “Paziente:”).
            Non aggiungere commenti, titoli, spiegazioni, riassunti o conclusioni.
            Il testo deve essere una trascrizione testuale completa, utilizzabile per analisi o archiviazione esterna.
            Formattare in modo chiaro e leggibile, utilizzando eventualmente gli a capo per separare i diversi interventi.
            """,
        ),
        "plaud-meeting-minutes": _plaud_template(
            "plaud-meeting-minutes",
            "會議紀要",
            """
            會議紀要系統是一個分兩階段處理的系統，旨在從原始對話資料中產生一份符合法律和專業標準的紀錄。該系統優先考慮全面捕捉而非總結，確保在重新整理內容以達到最大清晰度和實用性的同時，不遺漏任何實質性細節。

            指導理念：重構而非簡化

            其核心概念是充當一個嚴謹的編輯，而非分析者。它遵循一系列原則，在保證原始對話完整性的同時，使其便於理解和執行。

            保真優先：該系統的首要目標是保留所有實質內容。它僅去除對話中的填充詞（如「嗯」「啊」），並將對話改寫成專業陳述，保留原始含義和細節。
            片段感知處理：此模板專為分塊處理文字紀錄而設計。它使用「上下文標記」系統來識別和標記片段開頭或結尾不完整的表述，確保在最終階段能完美拼接。
            雙受眾導向：輸出內容的結構可同時服務於兩個目的：提供結果的「一目了然」式高層摘要，同時也提供詳細的按時間順序排列的紀錄，便於深入研究和參考。
            輸出結構：高層紀錄

            最終文件的組織方式便於立即使用和詳細審查，為會議建立單一的事實來源。

            高層儀表板：輸出開頭有兩個彙總列表：行動事項和關鍵決策。這兩部分將整個會議中指定的每項任務和決策彙總到頂部的一個位置，使相關人員無需閱讀完整紀錄就能掌握關鍵結果。
            時間順序敘述：儀表板之後是詳細紀要部分。它呈現了整個討論的清晰、按時間順序排列的紀錄，每個不同的主題塊包含：
            便於參考的時間戳。
            一個加粗的標題句，總結該討論塊的核心要點。
            嵌套的項目符號，提供改寫後的高保真細節，保留對話的實質內容。
            """,
        ),
        "plaud-meeting-highlights": _plaud_template(
            "plaud-meeting-highlights",
            "會議要點",
            """
            會議亮點是一種高級分析工具，其設計目的並非總結對話內容，而是提煉出對話中最深刻、永恆的智慧。它秉持「質量勝於數量」的嚴格理念，捨棄平凡細節，專注於具有變革性的見解。

            指導原則：見解過濾器

            這是會議亮點的核心。它運用一套精密的原則對每一條信息進行篩選。其目標是識別出在一年後仍具價值的觀點。為此，它會優先考慮以下方面：

            普遍見解 而非特定情境視角。
            反直覺的發現 而非常識性的確認。
            思維方式（心智模型） 而非具體、孤立的結論。
            可遷移的智慧 而非獨特的個人經歷。 其質量標準很高：輸出內容應改變讀者的思維方式，而非僅僅告知讀者發生了什麼。
            輸出結構：問題樹

            會議亮點將篩選出的見解整理成結構嚴謹的多級問題樹格式。這種結構遵循一條關鍵規則：

            分層總結：每個父級要點必須是一個不超過 20 字的單句，準確概括其下所有子要點。
            逐步細化：這形成了一個邏輯清晰的自上而下的流程，讓讀者能夠先把握最高層級的主要主題，然後深入了解更細緻的見解。問題樹的最底層包含了通過指導原則篩選的深刻觀點。這種格式要求表述極度簡潔、邏輯清晰。
            """,
        ),
    }
)

_PROMPT_FRAME = """\
Summarize the following transcript as Markdown with exactly these sections:

{instructions}

Transcript:
---
{transcript}
---
"""


def get_template(name: str) -> SummaryTemplate:
    """Look up a template; unknown/legacy names fall back to Plaud Autopilot."""
    template = TEMPLATES.get(name.strip().lower())
    if template is None:
        log.warning("unknown summary template %r, falling back to Plaud Autopilot", name)
        return TEMPLATES["plaud-autopilot"]
    return template


def bootstrap_note_templates(session) -> None:
    """Version built-ins to the exact captured catalog without touching personal templates."""
    from sqlalchemy import func, select

    from ..db.models import NoteTemplate

    removed_keys: list[str] = []
    for row in session.scalars(
        select(NoteTemplate).where(
            NoteTemplate.is_builtin.is_(True),
            NoteTemplate.is_active.is_(True),
        )
    ):
        if row.key not in TEMPLATES:
            row.is_active = False
            removed_keys.append(row.key)

    if removed_keys:
        from sqlalchemy import update

        from ..db.models import PlaudFile

        session.execute(
            update(PlaudFile)
            .where(PlaudFile.note_template_key.in_(removed_keys))
            .values(note_template_key="plaud-autopilot")
        )

    for key, template in TEMPLATES.items():
        current = session.scalar(
            select(NoteTemplate)
            .where(NoteTemplate.key == key, NoteTemplate.is_active.is_(True))
            .order_by(NoteTemplate.version.desc())
        )
        expected = {
            "name": template.display_name or key.replace("-", " ").title(),
            "system_prompt": template.system or "",
            "instructions": template.instructions,
            "prompt_mode": template.prompt_mode,
            "provenance": template.provenance,
        }
        if (
            current is not None
            and current.is_builtin
            and all(getattr(current, field) == value for field, value in expected.items())
        ):
            continue
        if current is not None:
            current.is_active = False
        version = (
            session.scalar(select(func.max(NoteTemplate.version)).where(NoteTemplate.key == key))
            or 0
        ) + 1
        session.add(
            NoteTemplate(
                key=key,
                version=version,
                **expected,
                is_builtin=True,
                is_active=True,
            )
        )


def get_effective_template(name: str) -> SummaryTemplate:
    """Resolve the active database version, with built-ins as a safe fallback."""
    key = name.strip().lower()
    try:
        from sqlalchemy import select

        from ..db.models import NoteTemplate
        from ..db.session import session_scope

        with session_scope() as session:
            row = session.scalar(
                select(NoteTemplate)
                .where(NoteTemplate.key == key, NoteTemplate.is_active.is_(True))
                .order_by(NoteTemplate.version.desc())
            )
            if row is not None:
                return SummaryTemplate(
                    name=row.key,
                    system=row.system_prompt or None,
                    instructions=row.instructions,
                    version=row.version,
                    display_name=row.name,
                    prompt_mode=row.prompt_mode,
                    provenance=row.provenance,
                )
    except Exception as exc:  # noqa: BLE001 - startup/standalone fallback
        log.debug("could not resolve database note template: %s", exc)
    return get_template(key)


def template_snapshot(template: SummaryTemplate) -> dict:
    return {
        "key": template.name,
        "version": template.version,
        "name": template.display_name or template.name.replace("-", " ").title(),
        "system_prompt": template.system,
        "instructions": template.instructions,
        "prompt_mode": template.prompt_mode,
        "provenance": template.provenance,
    }


def render_resolved_prompt(
    template: SummaryTemplate, transcript_text: str
) -> tuple[str | None, str]:
    if template.prompt_mode == "direct":
        return (
            template.system or None,
            f"{template.instructions}\n\nTranscript:\n---\n{transcript_text}\n---\n",
        )
    prompt = _PROMPT_FRAME.format(instructions=template.instructions, transcript=transcript_text)
    return template.system, prompt


def render_prompt(template_name: str, transcript_text: str) -> tuple[str | None, str]:
    """Return ``(system, full_user_prompt)`` for the named template."""
    return render_resolved_prompt(get_effective_template(template_name), transcript_text)
