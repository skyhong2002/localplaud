# Localplaud 接手備忘錄

使用者的 Goal：

> 請繼續完成系統

請先讀 `AGENTS.md`、`docs/product-workflow.md`、`TODO.md`，檢查目前程式、Git、測試、資料庫與 SkyLabMac 的實際服務狀態，直接延續現有工作，不要重做專案。

目前第一件事是完成 SkyLabMac production database 的 legacy schema migration，確認資料無損並恢復服務。Provider／execution profile migration 已完成；下一批已知舊表是：

- `note_templates`
- `vocabulary_terms`
- `stage_attempts`
- `ask_messages`

一定要先用 production DB 的副本完整演練 migration、檢查 row count 與 `PRAGMA foreign_key_check`，測試通過後才可遷移正式資料庫。

SkyLabMac 上的保留資料：

- 目前專案：`~/Projects/localplaud`
- 舊工作區：`~/Projects/localplaud-pre-migration-20260712`
- migration artifacts：`~/Projects/localplaud-migration-artifacts-20260712`
- DB 備份：`~/Projects/localplaud/data/backups/localplaud-pre-full-migration-20260712.db`

legacy migration 和服務恢復後，繼續依 `AGENTS.md` 與 `docs/product-workflow.md` 把 Localplaud 整套做完。每一段都要實作、測試、必要時用真實瀏覽器驗證，commit/push，並驗證 SkyLabMac 上的實際執行狀態。不要把 Plaud 付費 transcript/summary 當主要 pipeline 依賴，不要破壞錄音、使用者修改、production data、秘密或既有備份。

單一功能完成不代表 Goal 完成；只有整個產品的 definition of done 都以實際測試與部署結果驗證後，才能結束「請繼續完成系統」。
