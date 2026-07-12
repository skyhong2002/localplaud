"""Small, dependency-free interface translation catalog."""

from __future__ import annotations

from collections.abc import Callable

SUPPORTED_LOCALES = {"en": "English", "zh-Hant-TW": "繁體中文（台灣）"}

_ZH_HANT_TW = {
    "Add audio": "新增音訊",
    "Import audio": "匯入音訊",
    "From this device": "從這部裝置",
    "Import from Plaud": "從 Plaud 匯入",
    "Metadata first · audio on demand": "先匯入中繼資料 · 需要時再下載音訊",
    "Home": "首頁",
    "All files": "所有檔案",
    "Ask & Search": "詢問與搜尋",
    "Saved notes": "已儲存筆記",
    "Templates": "範本",
    "Discover": "探索",
    "Notifications": "通知",
    "Settings": "設定",
    "Status": "系統狀態",
    "self-hosted": "自架服務",
    "Close": "關閉",
    "Click or drag an audio file to import": "點選或拖曳音訊檔案至此匯入",
    "Mirror your Plaud library": "同步你的 Plaud 資料庫",
    "Welcome back": "歡迎回來",
    "Your private recording library, mirrored and processed locally.": "你的私人錄音資料庫，在本機同步與處理。",
    "Recordings": "錄音",
    "Metadata only": "僅中繼資料",
    "Audio on this host": "本機音訊",
    "Processing now": "處理中",
    "hours mirrored": "小時已同步",
    "Audio stays in Plaud": "音訊仍保留在 Plaud",
    "Available for local processing": "可在本機處理",
    "Durable local stages": "可續跑的本機階段",
    "Recent recordings": "最近錄音",
    "View all files →": "查看所有檔案 →",
    "No recordings yet. Import from Plaud or add an audio file.": "目前沒有錄音。請從 Plaud 匯入或新增音訊檔案。",
    "Plaud mirror": "Plaud 同步",
    "AutoFlow activity": "AutoFlow 活動",
    "No Plaud import has run yet.": "尚未執行 Plaud 匯入。",
    "automation runs": "次自動化執行",
    "Needs attention": "需要處理",
    "Structured notes with immutable versions and reproducible prompts.": "使用不可變版本與可重現提示詞建立結構化筆記。",
    "New template": "新增範本",
    "My Space": "我的空間",
    "Explore": "探索",
    "Search templates, scenarios, or categories": "搜尋範本、情境或分類",
    "Search": "搜尋",
    "All": "全部",
    "View": "檢視",
    "Copy to My Space": "複製到我的空間",
    "No templates match this view.": "沒有符合目前條件的範本。",
    "Local automation that never sends Plaud credentials or recording data elsewhere.": "本機自動化，不會將 Plaud 憑證或錄音資料傳送到其他地方。",
    "Run now": "立即執行",
    "New AutoFlow": "新增 AutoFlow",
    "Applications & integrations": "應用程式與整合",
    "Every rule and destination shows who owns it and where it can be changed.": "每項規則與目的地都會顯示擁有者及可變更位置。",
    "Manage authorizations →": "管理授權 →",
    "AutoFlow": "AutoFlow",
    "rules": "項規則",
    "Dry run": "試跑",
    "Disable": "停用",
    "Enable": "啟用",
    "Edit": "編輯",
    "Delete": "刪除",
    "Read-only": "唯讀",
    "No AutoFlows yet. Create one to organize new recordings automatically.": "目前沒有 AutoFlow。建立一項規則即可自動整理新錄音。",
    "Run history": "執行紀錄",
    "Durable local updates from your AutoFlows.": "來自 AutoFlow、可持久保存的本機更新。",
    "Mark all read": "全部標示為已讀",
    "No notifications yet. AutoFlows with notifications enabled will appear here.": "目前沒有通知。啟用通知的 AutoFlow 更新會顯示在這裡。",
    "Workspace preferences": "工作區偏好設定",
    "Durable display preferences for every browser using this localplaud workspace.": "套用至所有使用此 localplaud 工作區瀏覽器的顯示偏好。",
    "Interface language": "介面語言",
    "Workspace name": "工作區名稱",
    "Timezone": "時區",
    "Theme": "主題",
    "Display density": "顯示密度",
    "Clock": "時鐘",
    "Follow system": "跟隨系統",
    "Light": "淺色",
    "Dark": "深色",
    "Comfortable": "舒適",
    "Compact": "緊湊",
    "Save preferences": "儲存偏好設定",
}

CATALOGS = {"zh-Hant-TW": _ZH_HANT_TW}


def translator(locale: str) -> Callable[[str], str]:
    catalog = CATALOGS.get(locale, {})

    def translate(message: str) -> str:
        return catalog.get(message, message)

    return translate
