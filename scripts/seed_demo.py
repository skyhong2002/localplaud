#!/usr/bin/env python3
"""Build a synthetic localplaud demo library for UI development and screenshots.

Everything this script writes is invented: titles, people, transcripts, notes,
Ask threads, templates, and automations. It never contacts Plaud or any model
provider. The library deliberately covers the states the Web App must render:
complete recordings, active processing, failed ASR, degraded diarization,
failed embeddings with a usable transcript, cloud-only metadata, queued audio,
trash, unfiled rows, folders, tags, capture sources, raw and corrected transcript
revisions, several note templates per recording, mind maps, and Ask history.

Usage::

    python scripts/seed_demo.py --out /tmp/lp-tools/demo [--force] [--short-audio]

The output directory receives ``localplaud.db`` plus ``audio/<id>/audio.opus``.
Audio is synthetic tone/noise encoded as low-bitrate Ogg Opus so the player,
waveform, and seek work. By default the audio length matches the recording's
metadata (up to two hours); ``--short-audio`` caps audio at 45 seconds for fast
test runs, in which case long transcripts extend past the end of the audio.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# Invented content
# --------------------------------------------------------------------------- #

ZH_LINES = {
    "product": [
        "我們先看一下上週的進度，註冊流程的轉換率有提升一點。",
        "設計稿這週五之前會更新，主要是調整首頁的卡片排列。",
        "後端那邊的 API 延遲已經降到兩百毫秒以內了。",
        "我覺得通知設定應該放在個人設定裡面，比較好找。",
        "這個需求的優先順序要再跟業務確認一次。",
        "測試環境下週一會重新部署，請大家先把分支合併。",
        "使用者回饋說匯出的檔名太長，我們可以改成日期加標題。",
        "行動版的底部選單要保留四個項目就好。",
        "下一個里程碑是十一月中，時間有點緊。",
        "我會把會議紀錄整理好，再寄給大家確認。",
    ],
    "customer": [
        "請問您們目前是怎麼處理每天的訂單資料？",
        "我們現在大部分還是用紙本，月底再手動輸入電腦。",
        "最麻煩的是分店之間的庫存沒辦法即時同步。",
        "如果系統可以自動提醒補貨，會省下很多時間。",
        "價格方面我們比較在意每個月的固定費用。",
        "員工的流動率很高，所以操作一定要很簡單。",
        "我們上次試用的系統，報表都看不太懂。",
        "可以先從兩家分店開始試用嗎？",
        "資料的安全性也是老闆很在意的部分。",
        "好，那我們下週再約時間做產品展示。",
    ],
    "call": [
        "喂，您好，我想詢問一下理賠的進度。",
        "好的，請提供您的保單號碼後四碼。",
        "資料我們已經收到了，目前在審核中。",
        "大概還需要七到十個工作天。",
        "那如果需要補件的話，會怎麼通知我？",
        "我們會用簡訊跟電子郵件同時通知您。",
        "了解，謝謝你的說明。",
        "不客氣，祝您有美好的一天。",
    ],
    "memo": [
        "提醒自己，週末要去買咖啡豆跟牛奶。",
        "下週二下午三點要跟房東確認續約的事情。",
        "記得把上個月的發票整理好，報帳截止日是二十五號。",
        "健身房的會員下個月到期，要比較一下其他方案。",
        "幫媽媽預約眼科門診，最好是星期六早上。",
        "讀書會的書還有兩章沒看完。",
    ],
    "family": [
        "今年過年要不要回南部？",
        "我覺得可以初二出發，避開塞車的時間。",
        "那禮物要準備什麼比較好？",
        "阿嬤上次說想要一台新的收音機。",
        "好啊，那我負責訂高鐵票。",
        "記得也要問一下表哥他們幾點到。",
    ],
}

EN_LINES = {
    "product": [
        "Let's start with the onboarding funnel numbers from last week.",
        "Activation is up about four points since we shipped the new checklist.",
        "The biggest drop-off is still on the permissions screen.",
        "I think we should test a version that explains why we need access.",
        "Design can have two variants ready by Thursday.",
        "Engineering estimate is roughly three days including QA.",
        "Can we make sure the analytics events are named consistently this time?",
        "Let's not block the release on the dashboard redesign.",
        "I'll write up the experiment plan and share it in the channel.",
        "Any objections to shipping behind a feature flag first?",
    ],
    "lecture": [
        "Today we're going to talk about consensus in distributed systems.",
        "Remember that a network partition can make two nodes disagree about the leader.",
        "Raft splits the problem into leader election, log replication, and safety.",
        "A follower that hears nothing for an election timeout becomes a candidate.",
        "The key property is that a committed entry is never lost.",
        "Let's look at what happens when the old leader comes back.",
        "This is exactly why terms are monotonically increasing.",
        "For the homework, implement the election part and write tests for split votes.",
        "Good question, the log matching property is what makes this safe.",
        "We'll cover snapshots and membership changes next week.",
    ],
    "interview": [
        "Thanks for joining. Could you walk me through a recent project?",
        "Sure. I led the migration of our billing service to an event-driven design.",
        "What was the hardest trade-off you had to make?",
        "Probably choosing at-least-once delivery and making every handler idempotent.",
        "How did you measure whether the migration was successful?",
        "We tracked reconciliation errors, p95 latency, and on-call pages per week.",
        "Tell me about a time you disagreed with your manager.",
        "We disagreed on rewriting a module; I proposed a smaller, staged refactor instead.",
        "Do you have any questions for us?",
        "Yes, how does the team decide what goes into each quarter?",
    ],
    "oneonone": [
        "How's the week been going so far?",
        "Pretty good, a bit busy with the launch checklist.",
        "Anything blocking you that I can help with?",
        "I could use a second reviewer for the data migration script.",
        "Let's pair on that tomorrow morning.",
        "Also, I'd like to start mentoring one of the new hires.",
        "That's a great idea, I'll set it up with the team lead.",
        "Let's revisit your growth goals at the end of the month.",
    ],
}

MIXED_LINES = [
    "這個 sprint 的 scope 我覺得有點太大了。",
    "我們可以先把 MVP 做出來，然後再 iterate。",
    "Dashboard 的 loading time 要再 optimize 一下。",
    "Stakeholder 那邊希望 Q4 之前可以 launch。",
    "這個 bug 是 edge case，但 customer 一直遇到。",
    "我下午會開一個 ticket，assign 給 backend。",
    "OKR 的部分我們下次 review 再討論。",
    "Offsite 的地點可以考慮宜蘭或是花蓮。",
    "Budget 的上限大概是每人五千塊。",
    "活動流程我會先 draft 一版放在 Notion。",
    "Feedback 的部分大家可以直接在文件上 comment。",
    "那我們就這樣 decide，下週一 kick-off。",
]

PEOPLE_ZH = ["王小明", "陳怡君", "林志豪", "張雅婷", "黃柏翰", "李佳穎", "吳承恩"]
PEOPLE_EN = ["Alex Chen", "Priya Raman", "Jordan Lee", "Sam Ortiz", "Mei Tanaka", "Dana Kim"]

FOLDERS = [
    ("Product team", "#3B82F6"),
    ("客戶訪談", "#10B981"),
    ("Lectures", "#F59E0B"),
    ("個人備忘", "#8B5CF6"),
    ("Hiring", "#EF4444"),
]
TAGS = [
    ("action-items", "#2563EB"),
    ("重要", "#DC2626"),
    ("follow-up", "#D97706"),
    ("Q4", "#059669"),
]


@dataclass
class Scenario:
    key: str
    lang: str  # zh | en | mixed
    lines: list[str]
    speakers: tuple[int, int]
    folder: str | None
    titles: list[str]
    templates: list[str]
    people: list[str] = field(default_factory=list)


SCENARIOS = [
    Scenario(
        "product_zh", "zh", ZH_LINES["product"], (3, 5), "Product team",
        ["Q4 產品路線圖週會", "註冊流程改版討論", "行動版導覽列設計評審", "上線前檢查會議"],
        ["plaud-meeting-minutes", "plaud-meeting-highlights"], PEOPLE_ZH,
    ),
    Scenario(
        "product_en", "en", EN_LINES["product"], (3, 4), "Product team",
        ["Weekly product sync — onboarding funnel", "Pricing page experiment review",
         "Release readiness check", "Roadmap planning (H1)"],
        ["plaud-meeting-minutes", "plaud-key-metrics"], PEOPLE_EN,
    ),
    Scenario(
        "customer", "zh", ZH_LINES["customer"], (2, 3), "客戶訪談",
        ["客戶訪談：連鎖咖啡店 POS 需求", "客戶訪談：社區診所預約系統", "客戶訪談：手搖飲分店庫存",
         "客戶回訪：試用兩週後的意見"],
        ["plaud-research-interview", "plaud-intent-analysis"], PEOPLE_ZH,
    ),
    Scenario(
        "lecture", "en", EN_LINES["lecture"], (2, 3), "Lectures",
        ["Distributed Systems 6 — Consensus and Raft", "Distributed Systems 7 — Snapshots",
         "Guest lecture: Designing for failure"],
        ["plaud-lecture-deep-dive"], PEOPLE_EN,
    ),
    Scenario(
        "interview", "en", EN_LINES["interview"], (2, 3), "Hiring",
        ["Interview — backend engineer (round 2)", "Interview — product designer portfolio review",
         "Hiring debrief"],
        ["plaud-interview"], PEOPLE_EN,
    ),
    Scenario(
        "oneonone", "en", EN_LINES["oneonone"], (2, 2), None,
        ["1:1 with design lead", "1:1 — growth goals", "Weekly 1:1"],
        ["plaud-autopilot"], PEOPLE_EN,
    ),
    Scenario(
        "call", "zh", ZH_LINES["call"], (2, 2), None,
        ["電話：保險理賠進度", "電話：搬家公司報價", "電話：牙醫預約改期"],
        ["plaud-autopilot"], PEOPLE_ZH,
    ),
    Scenario(
        "memo", "zh", ZH_LINES["memo"], (1, 1), "個人備忘",
        ["語音備忘：週末採買", "語音備忘：報帳提醒", "Idea: 讀書會題目"],
        ["plaud-autopilot"], PEOPLE_ZH,
    ),
    Scenario(
        "family", "zh", ZH_LINES["family"], (3, 4), None,
        ["過年返鄉討論", "家庭聚餐規劃"],
        ["plaud-autopilot"], PEOPLE_ZH,
    ),
    Scenario(
        "brainstorm", "mixed", MIXED_LINES, (4, 5), "Product team",
        ["Brainstorm: 年終 offsite ideas", "Sprint retro 回顧 + next steps", "OKR draft review 討論"],
        ["plaud-meeting-highlights", "plaud-meeting-minutes"], PEOPLE_ZH + PEOPLE_EN,
    ),
]

# (scenario key, duration minutes, days ago, state, extras)
# States: done, processing_asr, processing_notes, downloading, failed_asr,
# degraded_diarize, failed_index, metadata_only, queued, trash
PLAN: list[tuple[str, float, float, str, dict]] = [
    ("product_zh", 47, 0.08, "processing_notes", {}),
    ("memo", 1.2, 0.2, "done", {}),
    ("product_en", 52, 0.9, "done", {"tags": ["action-items", "Q4"], "revisions": 2}),
    ("customer", 38, 1.3, "done", {"tags": ["follow-up"], "revisions": 1}),
    ("call", 6, 1.8, "done", {}),
    ("brainstorm", 64, 2.4, "done", {"tags": ["Q4"], "revisions": 1}),
    ("lecture", 95, 3.1, "done", {"revisions": 1}),
    ("interview", 58, 4.0, "degraded_diarize", {}),
    ("oneonone", 28, 5.2, "done", {}),
    ("product_zh", 41, 6.0, "done", {"tags": ["action-items", "重要"], "revisions": 2}),
    ("family", 22, 7.5, "done", {}),
    ("customer", 33, 8.1, "failed_index", {"tags": ["follow-up"]}),
    ("product_en", 36, 9.0, "failed_asr", {}),
    ("memo", 2.5, 10.2, "done", {"untitled": True}),
    ("lecture", 118, 12.0, "done", {"long_title": True}),
    ("interview", 45, 14.4, "done", {"tags": ["重要"]}),
    ("brainstorm", 71, 16.0, "done", {"revisions": 1}),
    ("call", 4, 18.3, "done", {}),
    ("product_zh", 55, 21.0, "done", {"tags": ["Q4"]}),
    ("oneonone", 31, 23.0, "processing_asr", {}),
    ("customer", 49, 26.0, "done", {"revisions": 1}),
    ("lecture", 87, 29.0, "failed_index", {}),
    ("product_en", 44, 33.0, "done", {}),
    ("memo", 1.0, 36.0, "metadata_only", {}),
    ("family", 15, 40.0, "done", {}),
    ("interview", 62, 44.0, "done", {"tags": ["follow-up"]}),
    ("brainstorm", 39, 49.0, "degraded_diarize", {}),
    ("product_zh", 120, 55.0, "done", {"tags": ["action-items"], "revisions": 1}),
    ("call", 9, 61.0, "queued", {}),
    ("customer", 27, 68.0, "done", {}),
    ("oneonone", 25, 75.0, "done", {}),
    ("lecture", 92, 83.0, "done", {}),
    ("product_en", 39, 92.0, "failed_asr", {"retry_exhausted": True}),
    ("memo", 3.2, 101.0, "done", {"source": "local"}),
    ("product_zh", 46, 110.0, "done", {"source": "local"}),
    ("family", 18, 121.0, "done", {}),
    ("interview", 50, 130.0, "downloading", {}),
    ("call", 7, 20.0, "trash", {}),
    ("memo", 1.5, 45.0, "trash", {}),
    ("product_en", 33, 98.0, "trash", {}),
]

LONG_TITLE = (
    "Distributed Systems 8 — A very long lecture title about Byzantine fault tolerance, "
    "practical BFT, and why your quorum math must survive malicious replicas"
)

TEMPLATE_HEADINGS = {
    "zh": {
        "plaud-meeting-minutes": ["會議摘要", "討論重點", "決議事項", "待辦事項"],
        "plaud-meeting-highlights": ["重點摘要", "關鍵時刻", "後續追蹤"],
        "plaud-research-interview": ["受訪者背景", "主要痛點", "需求與期待", "洞察"],
        "plaud-intent-analysis": ["客戶意圖", "購買訊號", "疑慮", "建議下一步"],
        "plaud-autopilot": ["摘要", "重點", "待辦"],
    },
    "en": {
        "plaud-meeting-minutes": ["Summary", "Discussion", "Decisions", "Action items"],
        "plaud-meeting-highlights": ["Highlights", "Key moments", "Follow-ups"],
        "plaud-key-metrics": ["Metrics mentioned", "Trends", "Risks", "Next steps"],
        "plaud-lecture-deep-dive": ["Overview", "Core concepts", "Worked example", "Review questions"],
        "plaud-interview": ["Candidate background", "Strengths", "Concerns", "Recommendation"],
        "plaud-autopilot": ["Summary", "Key points", "To-dos"],
    },
}

PERSONAL_TEMPLATES = [
    {
        "key": "standup-digest",
        "name": "Stand-up digest",
        "category": "Meetings",
        "scenario": "Team",
        "description": "Yesterday / today / blockers per speaker, with owners.",
        "author": "Local workspace",
        "provenance": "personal",
        "instructions": "## Per person\n- Yesterday\n- Today\n- Blockers\n\n## Owners and due dates",
    },
    {
        "key": "customer-voice",
        "name": "客戶聲音整理",
        "category": "Research",
        "scenario": "Sales",
        "description": "整理客戶原話、痛點與可行的產品機會。",
        "author": "Local workspace",
        "provenance": "personal",
        "instructions": "## 客戶原話\n## 痛點\n## 機會\n## 下一步",
    },
    {
        "key": "lecture-flashcards",
        "name": "Lecture flashcards",
        "category": "Education",
        "scenario": "Study",
        "description": "Turns a lecture into question/answer flashcards with timestamps.",
        "author": "Community · Study Circle",
        "provenance": "community",
        "popularity": 1840,
        "instructions": "Produce 10-20 Q/A flashcards. Cite the timestamp for each answer.",
    },
    {
        "key": "decision-log",
        "name": "Decision log",
        "category": "Meetings",
        "scenario": "Leadership",
        "description": "Only decisions, rationale, and who agreed — nothing else.",
        "author": "Community · Ops Guild",
        "provenance": "community",
        "popularity": 962,
        "instructions": "## Decisions\nFor each: decision, rationale, participants, timestamp.",
    },
]


# --------------------------------------------------------------------------- #
# Generation helpers
# --------------------------------------------------------------------------- #


def _words(text: str, start: float, end: float, speaker: str | None, rng: random.Random) -> list:
    from localplaud.asr.base import Word

    if any("一" <= ch <= "鿿" for ch in text):
        tokens: list[str] = []
        buffer = ""
        for ch in text:
            if ch.isascii() and (ch.isalnum() or ch in "-'"):
                buffer += ch
                continue
            if buffer:
                tokens.append(buffer)
                buffer = ""
            if ch.strip():
                tokens.append(ch)
        if buffer:
            tokens.append(buffer)
        # Group CJK characters into two-character "words" like ASR output.
        grouped: list[str] = []
        for token in tokens:
            if grouped and len(grouped[-1]) == 1 and not token.isascii() and not grouped[-1].isascii():
                grouped[-1] += token
            else:
                grouped.append(token)
        tokens = grouped
    else:
        tokens = text.split()
    if not tokens:
        return []
    span = (end - start) / len(tokens)
    out = []
    for index, token in enumerate(tokens):
        w_start = round(start + index * span, 3)
        w_end = round(min(end, w_start + span * 0.92), 3)
        out.append(
            Word(
                text=token,
                start=w_start,
                end=w_end,
                speaker=speaker,
                confidence=round(rng.uniform(0.72, 0.995), 3),
            )
        )
    return out


def _segments(scn: Scenario, duration_s: float, speaker_count: int, rng: random.Random,
              *, with_speakers: bool = True) -> list:
    from localplaud.asr.base import Segment

    keys = [f"SPEAKER_{i:02d}" for i in range(speaker_count)]
    segments = []
    t = rng.uniform(0.4, 2.0)
    current = 0
    line_index = rng.randrange(len(scn.lines))
    while t < duration_s - 1.5:
        text = scn.lines[line_index % len(scn.lines)]
        line_index += 1
        length = min(duration_s - t - 0.2, max(2.2, len(text) * (0.28 if scn.lang != "en" else 0.075)))
        length *= rng.uniform(0.85, 1.25)
        start, end = round(t, 2), round(min(duration_s - 0.1, t + length), 2)
        if speaker_count > 1 and rng.random() < 0.6:
            current = (current + rng.randrange(1, speaker_count)) % speaker_count
        speaker = keys[current] if with_speakers else None
        segments.append(
            Segment(
                text=text,
                start=start,
                end=end,
                speaker=speaker,
                words=_words(text, start, end, speaker, rng),
            )
        )
        t = end + rng.uniform(0.25, 3.5 if duration_s > 600 else 1.4)
    return segments


def _polish(text: str, lang: str) -> str:
    replacements = {
        "zh": [("一點", "約 3%"), ("兩百毫秒", "200 ms"), ("十一月中", "11 月中旬")],
        "en": [("about four points", "about 4 points"), ("roughly three days", "~3 days")],
        "mixed": [("Notion", "Notion 文件"), ("五千塊", "NT$5,000")],
    }
    for old, new in replacements.get(lang, []):
        text = text.replace(old, new)
    return text


def _summary_md(scn: Scenario, template: str, title: str, names: list[str],
                rng: random.Random) -> str:
    lang = "en" if scn.lang == "en" else "zh"
    headings = TEMPLATE_HEADINGS[lang].get(template) or TEMPLATE_HEADINGS[lang]["plaud-autopilot"]
    pool = list(scn.lines)
    rng.shuffle(pool)
    out = [f"# {title}", ""]
    for i, heading in enumerate(headings):
        out.append(f"## {heading}")
        picks = pool[i * 2 : i * 2 + 3] or pool[:2]
        for line in picks:
            owner = rng.choice(names) if names else ""
            if "待辦" in heading or "Action" in heading or "To-do" in heading or "Next" in heading:
                out.append(f"- [ ] {line.rstrip('。.?？')} — **{owner}**")
            else:
                out.append(f"- {line}")
        out.append("")
    if lang == "en":
        out.append("> Generated locally from the corrected transcript (full coverage).")
    else:
        out.append("> 依據校正後逐字稿於本機產生（完整涵蓋全文）。")
    return "\n".join(out)


def _mind_map_md(scn: Scenario, title: str, rng: random.Random) -> str:
    lang = "en" if scn.lang == "en" else "zh"
    branches = TEMPLATE_HEADINGS[lang]["plaud-meeting-minutes"]
    lines = [f"# {title}"]
    pool = list(scn.lines)
    rng.shuffle(pool)
    for i, branch in enumerate(branches):
        lines.append(f"- {branch}")
        for line in pool[i * 2 : i * 2 + 2]:
            lines.append(f"  - {line.rstrip('。.?？')[:40]}")
            if rng.random() < 0.4:
                lines.append(f"    - {rng.choice(pool).rstrip('。.?？')[:28]}")
    return "\n".join(lines)


def _base_tone(cache: Path, variant: int) -> Path:
    """Render a 3-minute speech-like tone pattern once; recordings loop it."""
    base = cache / f"base-{variant}.wav"
    if base.exists():
        return base
    cache.mkdir(parents=True, exist_ok=True)
    f1 = 140 + variant * 23
    f2 = 0.11 + variant / 40
    expr = (
        f"0.35*sin(2*PI*{f1}*t)*(0.55+0.45*sin(2*PI*{f2}*t))"
        f"*gt(sin(2*PI*0.37*t+{variant})+0.35*sin(2*PI*1.3*t),-0.25)"
        f"+0.04*(random(0)-0.5)"
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"aevalsrc=exprs='{expr}':s=8000:d=180", "-ac", "1", str(base)],
        check=True,
    )
    return base


def _audio(path: Path, seconds: float, seed: int, cache: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base = _base_tone(cache, seed % 4)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-stream_loop", "-1", "-i", str(base),
            "-t", f"{seconds:.2f}", "-ac", "1", "-c:a", "libopus", "-b:a", "6k",
            "-application", "voip", str(path),
        ],
        check=True,
    )


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


def build(out: Path, *, force: bool = False, short_audio: bool = False,
          now: datetime | None = None, audio: bool = True) -> dict:
    out = out.resolve()
    if out.exists():
        if not force and any(out.iterdir()):
            raise SystemExit(f"{out} is not empty; pass --force to rebuild it")
        for name in ("localplaud.db", "localplaud.db-wal", "localplaud.db-shm",
                     "localplaud.db.schema.lock"):
            (out / name).unlink(missing_ok=True)
        shutil.rmtree(out / "audio", ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    os.environ["LOCALPLAUD_STORE__DATABASE_URL"] = f"sqlite:///{out / 'localplaud.db'}"
    os.environ["LOCALPLAUD_POLLER__DOWNLOAD_DIR"] = str(out / "audio")

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    db_session._engine = None
    db_session._Session = None
    get_settings(reload=True)

    from localplaud.db.models import (
        AskMessage,
        AskThread,
        AutomationRule,
        AutomationRun,
        FileStatus,
        Folder,
        NoteTemplate,
        Notification,
        PlaudFile,
        Speaker,
        StageAttempt,
        StageName,
        StageRun,
        Summary,
        Tag,
        Transcript,
        TranscriptRevision,
        UserNote,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.preferences import save_workspace_preferences
    from localplaud.store.speakers import speaker_keys_from_segments, sync_speakers
    from localplaud.worker.summary_templates import get_template, template_snapshot

    rng = random.Random(20261002)
    now = now or datetime.now(UTC)
    init_db()

    S = StageName
    asr_provider = ("mlx-whisper", "mlx-community/whisper-large-v3-turbo")
    stage_meta = {
        S.convert: ("ffmpeg", "opus->wav16k"),
        S.transcribe: asr_provider,
        S.align: ("whisperx", "wav2vec2-auto"),
        S.diarize: ("pyannote", "pyannote/speaker-diarization-community-1"),
        S.summarize: ("ollama", "qwen3:14b"),
        S.mind_map: ("ollama", "qwen3:14b"),
        S.index: ("ollama", "bge-m3"),
    }
    ordered = [S.convert, S.transcribe, S.align, S.diarize, S.summarize, S.mind_map, S.index]

    summary: dict = {"recordings": 0, "audio_seconds": 0.0, "states": {}}
    file_ids: list[str] = []
    done_ids: list[tuple[str, Scenario, list]] = []

    with session_scope() as session:
        save_workspace_preferences(
            session,
            {
                "workspace_name": "Demo workspace",
                "timezone": "Asia/Taipei",
                "locale": "en",
                "auto_process_new_recordings": False,
            },
        )
        folders = {}
        for index, (name, color) in enumerate(FOLDERS):
            folder = Folder(name=name, color=color,
                            created_at=now - timedelta(days=150 - index))
            session.add(folder)
            folders[name] = folder
        tags = {}
        for name, color in TAGS:
            tag = Tag(name=name, color=color)
            session.add(tag)
            tags[name] = tag
        session.flush()

        for t in PERSONAL_TEMPLATES:
            session.add(
                NoteTemplate(
                    key=t["key"], version=1, name=t["name"],
                    system_prompt="You write faithful notes grounded in the transcript.",
                    instructions=t["instructions"], prompt_mode="structured",
                    category=t["category"], scenario=t["scenario"],
                    description=t["description"], author=t["author"],
                    provenance=t["provenance"], popularity=t.get("popularity"),
                    is_builtin=False, is_active=True,
                    created_at=now - timedelta(days=rng.randint(5, 90)),
                )
            )
        # A second immutable version of one personal template.
        session.add(
            NoteTemplate(
                key="standup-digest", version=2, name="Stand-up digest",
                system_prompt="You write faithful notes grounded in the transcript.",
                instructions=PERSONAL_TEMPLATES[0]["instructions"] + "\n\n## Risks",
                prompt_mode="structured", category="Meetings", scenario="Team",
                description="Yesterday / today / blockers / risks per speaker.",
                author="Local workspace", provenance="personal",
                is_builtin=False, is_active=True,
            )
        )
        session.execute(
            NoteTemplate.__table__.update()
            .where(NoteTemplate.key == "standup-digest", NoteTemplate.version == 1)
            .values(is_active=False)
        )

        scenario_by_key = {scn.key: scn for scn in SCENARIOS}
        title_cursor: dict[str, int] = {}
        for index, (skey, minutes, days_ago, state, extras) in enumerate(PLAN):
            scn = scenario_by_key[skey]
            cursor = title_cursor.get(skey, 0)
            title_cursor[skey] = cursor + 1
            title = scn.titles[cursor % len(scn.titles)]
            if extras.get("long_title"):
                title = LONG_TITLE
            start = now - timedelta(days=days_ago, minutes=rng.randint(0, 600))
            start = start.replace(second=rng.randint(0, 59), microsecond=0)
            duration_s = round(minutes * 60 + rng.uniform(-8, 8), 2) if minutes > 2 else minutes * 60
            duration_ms = int(duration_s * 1000)
            fid = hashlib.sha1(f"demo-{index}".encode()).hexdigest()[:24]
            file_ids.append(fid)
            plaud_name = start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
            source = extras.get("source", "plaud")
            scene = 102 if source == "local" else (101 if skey == "call" else 1)
            row = PlaudFile(
                id=fid,
                filename=plaud_name if extras.get("untitled") else (
                    title if source == "local" else plaud_name
                ),
                generated_title=None if extras.get("untitled") or source == "local" else title,
                generated_title_provider="ollama" if not extras.get("untitled") else None,
                generated_title_model="qwen3:14b" if not extras.get("untitled") else None,
                duration_ms=duration_ms,
                start_time_ms=int(start.timestamp() * 1000),
                end_time_ms=int(start.timestamp() * 1000) + duration_ms,
                filesize=int(duration_s * 1000),
                scene=scene,
                is_trash=state == "trash",
                origin=source,
                status=FileStatus.done,
                note_template_key=scn.templates[0],
                created_at=start + timedelta(minutes=minutes + 4),
                raw={"demo": True},
            )
            if scn.folder and (index % 4 != 3 or skey == "lecture"):
                row.folder = folders[scn.folder]
            for tag_name in extras.get("tags", []):
                row.tags.append(tags[tag_name])
            session.add(row)

            needs_audio = state not in {"metadata_only", "downloading"}
            if needs_audio and audio:
                audio_seconds = min(duration_s, 45.0) if short_audio else duration_s
                path = out / "audio" / fid / "audio.opus"
                _audio(path, audio_seconds, index, out / ".tone-cache")
                row.audio_path = str(path)
                row.downloaded_at = start + timedelta(minutes=minutes + 6)
                summary["audio_seconds"] += audio_seconds

            speaker_count = rng.randint(*scn.speakers)
            processed = state in {"done", "degraded_diarize", "failed_index", "trash",
                                  "processing_notes"}
            segments = []
            if processed:
                segments = _segments(scn, duration_s, speaker_count, rng,
                                     with_speakers=state != "degraded_diarize")
            if state == "processing_asr":
                # A partial transcript is not shown; the worker is mid-ASR.
                pass

            def stage(name, status, *, error=None, detail=None, offset=0,
                      fid=fid, start=start, minutes=minutes, duration_s=duration_s):
                provider, model = stage_meta[name]
                started = start + timedelta(minutes=minutes + 6 + offset)
                run = StageRun(
                    file_id=fid, stage=name, status=status,
                    attempts=0 if status == "pending" else 1,
                    provider=provider, model=model, artifact_source="local",
                    detail=detail or {}, error=error, started_at=started,
                    completed_at=None if status == "running" else started + timedelta(
                        seconds=max(4, duration_s * 0.04)
                    ),
                )
                session.add(run)
                if status == "pending":
                    run.started_at = run.completed_at = None
                    return
                session.add(
                    StageAttempt(
                        file_id=fid, stage=name, attempt=1, status=status,
                        provider=provider, model=model, error=error,
                        latency_ms=int(max(4, duration_s * 0.04) * 1000),
                        started_at=started,
                        completed_at=None if status == "running" else started + timedelta(
                            seconds=max(4, duration_s * 0.04)
                        ),
                    )
                )

            if state in {"done", "trash"}:
                for i, name in enumerate(ordered):
                    stage(name, "completed", offset=i)
                row.status = FileStatus.done
            elif state == "processing_notes":
                for i, name in enumerate(ordered[:4]):
                    stage(name, "completed", offset=i)
                stage(S.summarize, "running", offset=4,
                      detail={"progress": {"phase": "map", "done": 3, "total": 7}})
                stage(S.mind_map, "pending", offset=5)
                stage(S.index, "pending", offset=5)
                row.status = FileStatus.processing
            elif state == "processing_asr":
                stage(S.convert, "completed")
                stage(S.transcribe, "running", offset=1,
                      detail={"progress": {"phase": "asr", "seconds_done": duration_s * 0.42,
                                           "seconds_total": duration_s}})
                row.status = FileStatus.processing
            elif state == "downloading":
                row.status = FileStatus.downloading
            elif state == "queued":
                row.status = FileStatus.downloaded
            elif state == "metadata_only":
                row.status = FileStatus.metadata_only
            elif state == "failed_asr":
                stage(S.convert, "completed")
                stage(S.transcribe, "failed", offset=1,
                      error="ASR provider mlx-whisper failed: model weights could not be loaded "
                            "(out of memory while allocating 1.6 GB). Retry or choose a smaller "
                            "model in Settings → Speech.")
                row.status = FileStatus.error
                row.error = "transcribe: ASR provider failed (out of memory)"
                row.pipeline_retry_count = 3 if extras.get("retry_exhausted") else 1
                if not extras.get("retry_exhausted"):
                    row.pipeline_next_retry_at = now + timedelta(minutes=25)
                row.pipeline_last_failure_at = now - timedelta(days=days_ago - 0.01)
            elif state == "degraded_diarize":
                for i, name in enumerate(ordered):
                    if name == S.diarize:
                        stage(name, "degraded", offset=i,
                              error="Speaker diarization unavailable: pyannote pipeline could "
                                    "not be loaded (missing Hugging Face token). Transcript is "
                                    "usable without speaker labels.")
                    else:
                        stage(name, "completed", offset=i)
                row.status = FileStatus.partial
            elif state == "failed_index":
                for i, name in enumerate(ordered):
                    if name == S.index:
                        stage(name, "failed", offset=i,
                              error="Embedding provider ollama/bge-m3 unreachable at "
                                    "http://127.0.0.1:11434 (connection refused).")
                    else:
                        stage(name, "completed", offset=i)
                row.status = FileStatus.partial
            summary["states"][state] = summary["states"].get(state, 0) + 1
            summary["recordings"] += 1
            session.flush()

            if not segments:
                continue
            from dataclasses import asdict

            seg_dicts = [asdict(s) for s in segments]
            transcript = Transcript(
                file_id=fid, provider=asr_provider[0], model=asr_provider[1],
                language="en" if scn.lang == "en" else "zh",
                has_speakers=state != "degraded_diarize", source="local",
                text="\n".join(s.text for s in segments), segments=seg_dicts,
                created_at=start + timedelta(minutes=minutes + 8),
            )
            session.add(transcript)
            session.flush()
            keys = speaker_keys_from_segments(seg_dicts)
            sync_speakers(session, fid, keys)
            session.flush()
            people = list(scn.people)
            rng.shuffle(people)
            names: list[str] = []
            for i, speaker in enumerate(
                session.query(Speaker).filter(Speaker.file_id == fid).order_by(Speaker.id)
            ):
                # Leave the last speaker unnamed in larger meetings so the UI shows
                # both named and anonymous speakers.
                if i < len(people) and not (len(keys) >= 4 and i == len(keys) - 1):
                    speaker.display_name = people[i]
                    names.append(people[i])

            revisions = extras.get("revisions", 0)
            for rev in range(1, revisions + 1):
                polished = [dict(s) for s in seg_dicts]
                for item in polished:
                    item["text"] = _polish(item["text"], scn.lang)
                if rev == 2 and polished:
                    polished[0]["text"] = polished[0]["text"] + "（已人工校正）" if scn.lang != "en" \
                        else polished[0]["text"] + " (edited)"
                session.add(
                    TranscriptRevision(
                        file_id=fid, base_transcript_id=transcript.id, revision=rev,
                        source="local", segments=polished,
                        text="\n".join(item["text"] for item in polished),
                        has_speakers=True,
                        kind="ai_polish" if rev == 1 else "user_edit",
                        note="AI polished with ollama/qwen3:14b" if rev == 1
                        else "edited segment 1",
                        provider="ollama" if rev == 1 else None,
                        model="qwen3:14b" if rev == 1 else None,
                        prompt_version="transcript-polish/v4" if rev == 1 else None,
                        created_at=start + timedelta(minutes=minutes + 10 + rev * 30),
                    )
                )

            if state == "processing_notes":
                continue
            display_title = row.display_title
            for t_index, template_key in enumerate(scn.templates):
                tpl = get_template(template_key)
                session.add(
                    Summary(
                        file_id=fid, template=template_key, template_version=1,
                        template_snapshot=template_snapshot(tpl),
                        title=display_title,
                        content_md=_summary_md(scn, template_key, display_title, names, rng),
                        llm_provider="ollama", model="qwen3:14b", source="local",
                        input_transcript_id=transcript.id,
                        input_transcript_revision=revisions or None,
                        input_transcript_source="local",
                        created_at=start + timedelta(minutes=minutes + 12 + t_index),
                    )
                )
            session.add(
                Summary(
                    file_id=fid, template="mind_map", template_version=1,
                    title=None, content_md=_mind_map_md(scn, display_title, rng),
                    llm_provider="ollama", model="qwen3:14b", source="local",
                    input_transcript_id=transcript.id,
                    input_transcript_revision=revisions or None,
                    input_transcript_source="local",
                    created_at=start + timedelta(minutes=minutes + 14),
                )
            )
            if state in {"done"}:
                done_ids.append((fid, scn, segments))

        session.flush()

        # Saved notes (one manual, one from Ask) and Ask history.
        def source_for(fid, scn_, segs, k=0):
            seg = segs[min(k, len(segs) - 1)]
            return {
                "file_id": fid,
                "filename": session.get(PlaudFile, fid).display_title,
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": None,
                "score": round(0.62 + 0.3 * rng.random(), 2),
                "target": "transcript",
                "url": f"/file/{fid}?tab=transcript&t={int(seg.start)}",
            }

        library_threads = [
            ("What did we decide about the onboarding funnel?",
             "The team agreed to test an explanation screen before the permissions "
             "prompt and ship it behind a feature flag first [1]. Design owns two variants "
             "for Thursday [2]."),
            ("哪些客戶提到庫存同步的問題？",
             "兩場客戶訪談都提到分店之間的庫存無法即時同步 [1]，其中一位希望系統能自動提醒補貨 [2]。"),
            ("List all open action items from this month",
             "- Write up the experiment plan (Product sync) [1]\n- 整理會議紀錄並寄出 [2]\n"
             "- Pair on the data migration script [3]"),
        ]
        for t_index, (question, answer) in enumerate(library_threads):
            thread_id = str(uuid.UUID(int=rng.getrandbits(128)))
            created = now - timedelta(days=t_index * 3 + 0.5)
            thread = AskThread(id=thread_id, file_id=None, title=question[:80],
                               retrieval_scope={"scope_version": 2},
                               created_at=created, updated_at=created)
            session.add(thread)
            picks = done_ids[t_index * 2 : t_index * 2 + 3]
            session.add(AskMessage(thread_id=thread_id, role="user", content=question,
                                   created_at=created))
            session.add(
                AskMessage(
                    thread_id=thread_id, role="assistant", content=answer,
                    sources=[source_for(f, s, g, k) for k, (f, s, g) in enumerate(picks)],
                    provider="ollama", model="qwen3:14b", created_at=created,
                )
            )
        for t_index, (fid, scn, segs) in enumerate(done_ids[:3]):
            thread_id = str(uuid.UUID(int=rng.getrandbits(128)))
            created = now - timedelta(days=t_index + 0.2)
            question = "What are the next steps?" if scn.lang == "en" else "下一步是什麼？"
            session.add(AskThread(id=thread_id, file_id=fid, title=question,
                                  retrieval_scope={}, created_at=created, updated_at=created))
            session.add(AskMessage(thread_id=thread_id, role="user", content=question,
                                   created_at=created))
            answer_msg = AskMessage(
                thread_id=thread_id, role="assistant",
                content=("The group will follow up on the open items mentioned near the end "
                         "of the recording [1] [2].") if scn.lang == "en"
                else "會後需要確認優先順序並整理會議紀錄 [1] [2]。",
                sources=[source_for(fid, scn, segs, 2), source_for(fid, scn, segs, 5)],
                provider="ollama", model="qwen3:14b", created_at=created,
            )
            session.add(answer_msg)
            if t_index == 0:
                session.flush()
                session.add(
                    UserNote(
                        file_id=fid, title="Next steps (saved from Ask)",
                        content_md=answer_msg.content, source_type="ask",
                        ask_message_id=answer_msg.id,
                        citations=answer_msg.sources,
                    )
                )
        if done_ids:
            session.add(
                UserNote(
                    file_id=done_ids[1][0], title="My follow-up checklist",
                    content_md="- Email the summary to the team\n- Book the next session\n"
                               "- 確認預算上限",
                    source_type="manual",
                )
            )

        # Automations: local (enabled/disabled) and one externally owned read-only rule.
        rule_a = AutomationRule(
            name="Customer interviews → research notes", enabled=True, priority=10,
            trigger={"title_contains": "客戶"},
            actions={"note_template_key": "plaud-research-interview",
                     "folder_id": folders["客戶訪談"].id,
                     "add_tag_ids": [tags["follow-up"].id]},
            notify=True,
        )
        rule_b = AutomationRule(
            name="Long lectures → deep dive", enabled=True, priority=20,
            trigger={"min_duration_minutes": 75},
            actions={"note_template_key": "plaud-lecture-deep-dive",
                     "export_formats": ["md", "srt"]},
            notify=False,
        )
        rule_c = AutomationRule(
            name="Stand-ups (paused)", enabled=False, priority=30,
            trigger={"title_contains": "stand-up"},
            actions={"note_template_key": "standup-digest"}, notify=False,
        )
        rule_d = AutomationRule(
            name="Plaud app: auto-summarize calls", enabled=True, priority=40,
            trigger={"origin": "plaud"}, actions={"note_template_key": "plaud-autopilot"},
            notify=False, owner_type="external", owner_key="plaud-app",
            owner_label="Plaud mobile app", external_id="demo-external-1",
        )
        session.add_all([rule_a, rule_b, rule_c, rule_d])
        session.flush()
        customer_ids = [fid for fid, scn, _ in done_ids if scn.key == "customer"]
        for k, fid in enumerate(customer_ids[:3]):
            run = AutomationRun(
                rule_id=rule_a.id, rule_version=1, file_id=fid,
                status="completed" if k else "failed", matched=True,
                detail={"applied": ["note_template_key", "folder_id"]},
                error=None if k else "export failed: destination folder is not writable",
                created_at=now - timedelta(days=k * 9 + 1),
            )
            session.add(run)
            session.flush()
            session.add(
                Notification(
                    automation_run_id=run.id, file_id=fid,
                    title="AutoFlow matched: Customer interviews" if k
                    else "AutoFlow failed: Customer interviews",
                    body="Notes generated with Research interview template." if k
                    else "Export failed: destination folder is not writable.",
                    created_at=now - timedelta(days=k * 9 + 1),
                )
            )

    if audio:
        from localplaud.waveform import waveform_peaks

        for fid in file_ids:
            path = out / "audio" / fid / "audio.opus"
            if path.exists():
                for buckets in (180,):
                    waveform_peaks(path, buckets=buckets)

    summary["out"] = str(out)
    summary["database"] = str(out / "localplaud.db")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("/tmp/lp-tools/demo"))
    parser.add_argument("--force", action="store_true", help="Replace an existing demo library.")
    parser.add_argument("--short-audio", action="store_true",
                        help="Cap synthetic audio at 45 s (fast; long transcripts outlast audio).")
    parser.add_argument("--no-audio", action="store_true", help="Skip audio generation.")
    args = parser.parse_args(argv)
    if not args.no_audio and shutil.which("ffmpeg") is None:
        print("ffmpeg is required for synthetic audio (or pass --no-audio)", file=sys.stderr)
        return 2
    result = build(args.out, force=args.force, short_audio=args.short_audio,
                   audio=not args.no_audio)
    print(f"demo library: {result['recordings']} recordings at {result['out']}")
    for state, count in sorted(result["states"].items()):
        print(f"  {state:18} {count}")
    print(f"  synthetic audio  {result['audio_seconds'] / 60:.0f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
