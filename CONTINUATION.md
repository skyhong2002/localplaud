# Localplaud 接手備忘錄

使用者的 Goal：

> 請繼續完成系統

請先讀 `AGENTS.md`、`docs/product-workflow.md`、`TODO.md`，檢查目前程式、Git、測試、資料庫與 SkyLabMac 的實際服務狀態，直接延續現有工作，不要重做專案。

目前第一件事仍是完成 SkyLabMac production database 的 legacy schema migration，確認資料無損並恢復服務。Provider／execution profile migration 已完成；下一批已知舊表是：

- `note_templates`
- `vocabulary_terms`
- `stage_attempts`
- `ask_messages`

2026-07-13 已完成 migration 程式、production-backup 演練與正式資料庫遷移：

- 修正 legacy Ask migration：舊 `ask_threads.id INTEGER` 與 `ask_messages` 的
  `citations/profile_snapshot/estimated_cost/actual_cost` schema 會原子重建，保留
  thread/message ID、引用、provider/model、profile、usage、cost 與時間；新 ORM
  UUID thread 與 message 寫入已驗證。
- note-template migration 遇到現行 schema 無法表達的非空 `language` 或
  `execution_profile_id` 時會中止，不再靜默丟資料。production backup 的這兩欄皆為空。
- 對 SHA-256
  `ee08c38eba328db462bee0d3f1b8e41c77827da52c32538db7d66a26fcec16f6`
  的 242 MB 備份完整執行 `init_db()`：`PRAGMA integrity_check=ok`、
  `PRAGMA foreign_key_check` 無結果，`note_templates 5 -> 5`、
  `vocabulary_terms 0 -> 0`、`stage_attempts 438 -> 438`、
  `ask_threads 0 -> 0`、`ask_messages 0 -> 0`。template prompt 與 stage-attempt
  payload 比對一致，四張表與 Ask thread 的現行寫入均成功後 rollback。
- `transcripts local 176 -> 67` 是既有 canonical-local migration 的預期去重；
  cloud 280 筆完全保留，使用者 transcript revisions 在刪除舊 raw row 前會脫鉤保存。
- migration 關聯測試 36 passed；全套在 `whispercpp` health override 下為
  353 passed、1 deselected，另 2 個 OAuth loopback 測試只因本 sandbox 禁止綁定
  `127.0.0.1` 而失敗。直接載入本機 MLX native extension 會 abort runner。
- 實際 production host 已確認是目前的 `skyhong-CCLabMacmini`；舊 `SkyLabMac`
  hostname 不再存在。正式遷移前以 SQLite online backup 建立
  `data/backups/localplaud-pre-legacy-final-20260713-041143.db`，SHA-256 為
  `6374e3dc0225e5d5fd74f2ef1ad280893e7072ae0e93ffae50ab0d1d789a31d3`。
- `com.localplaud.agent` 已停機後用 commit `572177a` 的程式執行 `init_db()`；
  正式 DB 的核心筆數在 migration 前後完全一致，完整性與外鍵檢查通過。
  LaunchAgent 已恢復，local/public `/healthz` 均回傳 200，worker 也重設停機前的
  1 筆 in-flight lease 並繼續處理。
- commit 已建立但尚未 push：GitHub CLI 的既有 `skyhong2002` token 已失效，需在
  重新登入 GitHub 後執行 `git push origin main`。不要把未 push 誤判成未部署；
  production 直接執行這個已 commit 的本機 worktree。

一定要先用 production DB 的副本完整演練 migration、檢查 row count 與 `PRAGMA foreign_key_check`，測試通過後才可遷移正式資料庫。

SkyLabMac 上的保留資料：

- 目前專案：`~/Projects/localplaud`
- 舊工作區：`~/Projects/localplaud-pre-migration-20260712`
- migration artifacts：`~/Projects/localplaud-migration-artifacts-20260712`
- DB 備份：`~/Projects/localplaud/data/backups/localplaud-pre-full-migration-20260712.db`

legacy migration 和服務恢復後，繼續依 `AGENTS.md` 與 `docs/product-workflow.md` 把 Localplaud 整套做完。每一段都要實作、測試、必要時用真實瀏覽器驗證，commit/push，並驗證 SkyLabMac 上的實際執行狀態。不要把 Plaud 付費 transcript/summary 當主要 pipeline 依賴，不要破壞錄音、使用者修改、production data、秘密或既有備份。

單一功能完成不代表 Goal 完成；只有整個產品的 definition of done 都以實際測試與部署結果驗證後，才能結束「請繼續完成系統」。
