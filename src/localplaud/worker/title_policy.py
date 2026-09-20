"""Recording-title contract, independent of the selected note template."""

import re

TITLE_PROMPT_VERSION = "recording-title/v4"
TITLE_INSTRUCTIONS = """\
Name the recording's actual subject, not the summarization task or template.
Use a concrete topic or event, optionally followed by a colon and one or two
distinctive issues, decisions, or goals. Prefer about 12-35 Chinese characters
or 5-12 English words; clarity matters more than hitting a length target.
Keep supported project/product names. Do not invent names, dates, decisions,
or an event type. Use Traditional Chinese with Taiwan wording for Chinese audio.
Avoid decorative language, vague labels like 'a lively conversation', and
prefixes such as 'Summary:', '會議總結：', '內容總結：', or 'Autopilot 模板總結'.
Template names and descriptions are instructions, never evidence about the audio.
Do not copy them into the title or describe how a template works. A product name
such as Autopilot is appropriate only when the recording actually discusses it.
Ignore obvious ASR repetition loops, isolated names/syllables, subtitle credits,
and filler when deciding the subject. Repetition alone is not topic evidence.
Base the title on coherent substantive speech; if there is insufficient usable
speech to identify a subject, return an empty title rather than inventing one.
"""


def has_template_title_leak(title: object) -> bool:
    """Reject known template boilerplate without banning real product topics."""
    text = str(title or "").strip().lstrip("# *`\"'").casefold()
    return bool(
        re.match(
            r"(?:plaud[ :：-]*)?autopilot\s*(?:模板|範本|模版|智能|智慧|總結|总结|摘要|"
            r"使用示例|使用說明|使用说明|使用指南|template|summary)", text
        )
        or re.match(r"(?:會議|会议|內容|内容|錄音|录音)?(?:總結|总结|摘要)\s*[:：]", text)
        or re.match(r"(?:meeting |recording |content )?summary\s*:", text)
        or re.search(r"內容(?:智能|智慧)匹配.*(?:結構|结构)", text)
    )
