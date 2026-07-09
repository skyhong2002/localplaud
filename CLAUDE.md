# localplaud

一個自架的 Plaud 復刻品，跑在使用者的 Mac mini 或 Linux Docker machine 上。目標：**復刻 Plaud 雲端會對錄音做的所有事情**（下載、轉檔、逐字稿、講者分離、摘要、模板化筆記、問答／搜尋），但在本地／自架環境完成，資料不外流。

## Workflow（使用者的實際使用情境）

1. 使用者照常用實體 Plaud 裝置錄音。
2. 錄音照常同步到官方 Plaud App，並上傳到 Plaud 雲端（**我們不取代這一段**）。
3. **localplaud 定期輪詢 Plaud 雲端 API**，偵測到有新的檔案（或既有檔案有更新）就把它抓下來。
4. localplaud 在本地跑自己的 pipeline：下載音檔 → 轉檔 → 轉逐字稿 → 講者分離 → 產生摘要／筆記 → 存進本地資料庫。
5. 使用者透過 localplaud 自己的介面（TBD：CLI／web UI）查看、搜尋、問答。

換句話說：**官方 Plaud 是資料來源（source of truth for raw audio），localplaud 是本地的處理與知識庫層。** 我們是官方雲端的一個「鏡像 + 加工廠」，不碰裝置端的藍牙同步。

## 已知的 Plaud 雲端架構（2026-07-10 從 web.plaud.ai 逆向觀察）

> 這些都是從使用者自己登入的帳號、在瀏覽器 devtools 觀察到的，用途是備份／處理使用者自己的資料。**尚未完整驗證，實作前要再確認。**

### Hosts
- **Web app**: `https://web.plaud.ai`（Vue 3 SPA，Sentry 用 `guardian-web.plaud.ai`）
- **API**: `https://api-apse1.plaud.ai`（`apse1` = AWS ap-southeast-1；domain 存在 localStorage `pld_plaud_user_api_domain`，**不同帳號／區域可能不同，要動態讀取，不要 hardcode**）
- **靜態資源**: `https://web-static.plaud.ai`

### 認證
- Web app 靠 **cookie**（httpOnly，JS 讀不到 `document.cookie`）帶 session；API 用 `credentials: include` 就能通。
- localStorage 有一堆 `pld_*` key（`pld_userId`、`pld_sessionMeta`、`pld_pubKey`、`pld_passAlgorithm`、`pld_loginMethod`…），推測登入流程牽涉到 client 端的密鑰／簽章（`pld_pubKey` + `pld_passAlgorithm`）。**登入機制還沒逆完**，這是 localplaud 最需要先解決的一塊：如何在無瀏覽器的 server 上取得並維持有效 session token。
  - 可能路線：(a) 逆向官方登入 API（帳密／OTP → token）；(b) 讓使用者手動從瀏覽器貼上 cookie／token，localplaud 定期用它；(c) 走官方 App 的 API（如果和 web 不同）。實作前要先探明。

### 已確認的 API endpoints（都是 GET，`api-apse1.plaud.ai` 底下）
- `GET /file/simple/web?skip=0&limit=99999&is_trash=2&sort_by=start_time&is_desc=true`
  → **檔案清單**。回傳格式：
    ```json
    { "status": 0, "msg": "success", "data_file_total": N,
      "data_file_list": [ { ...file... } ] }
    ```
  參數：`is_trash`（0=正常、2=全部含垃圾桶推測）、`sort_by`（`start_time`/`edit_time`）、`is_desc`、`skip`/`limit` 分頁。
- `GET /ai/file-task-status` → AI 處理任務狀態（轉檔／摘要進度，輪詢用）。
- `GET /device/list` → 綁定的實體裝置清單。
- `GET /user-app/profile/workspace/me?fields=setting`、`GET /user-app/profile/account/me?fields=setting` → 使用者／workspace 設定。
- `GET /team-app/workspaces/detail` → workspace 詳情。
- `GET /user/feature-access` → 訂閱／功能權限。
- `GET /filetag/` → 標籤清單（檔案用 `filetag_id_list` 關聯）。

### File 物件 schema（實測一筆）
```json
{
  "id": "dab5c6ca728964152f32d93ed76c1950",     // 檔案主鍵
  "filename": "2026-07-09 15:38:57",            // 顯示名稱（預設是錄音時間）
  "keywords": [],
  "filesize": 9958560,                           // bytes
  "fullname": "dab5c6ca...c1950.opus",           // 實體檔名，音檔是 opus
  "file_md5": "0d1a2f87...",                     // 完整性校驗
  "ori_ready": false,                            // 原始檔是否就緒
  "version": 1783594217, "version_ms": ...,      // 版本戳（用來偵測更新 → 觸發重抓）
  "edit_time": 1783594217,
  "edit_from": "android",                        // 來源（android/ios/web）
  "is_trash": false,
  "start_time": 1783582737000,                   // 錄音起訖（epoch ms）
  "end_time":   1783585226000,
  "duration": 2489000,                           // ms
  "timezone": 8, "zonemins": 0,
  "scene": 1,                                     // 場景類型（會議／通話…）
  "filetag_id_list": [],
  "serial_number": "888215141902622886",         // 裝置端序號
  "is_trans": false,                              // 是否已有逐字稿
  "is_summary": false,                            // 是否已有摘要
  "is_markmemo": false,
  "wait_pull": 0
}
```
**同步關鍵欄位**：`id`（主鍵）、`version`/`version_ms`（偵測變更）、`file_md5`（校驗）、`is_trans`/`is_summary`（判斷雲端已加工到哪、決定 localplaud 要不要自己補做）。

### 逆向進度更新（2026-07-10，codex 唯讀觀察，只碰使用者本人帳號）

> 瀏覽器觀察結果，補進原本 TODO。證據：`scratchpad/plaud-recon/FINDINGS.md` + DevTools 截圖。所有請求皆 GET/OPTIONS，未改動雲端。

- **認證：header-token，不是單一 cookie。** `document.cookie` 只有 analytics/ALB cookie（`_ga`、`AWSALBTG`…），無可重用 session token；`localStorage` 的 `pld_*` 都是 UI/workspace 狀態。已登入時 `GET /user/me` 回 200，CORS 允許這組自訂 header：`Authorization, Content-Type, X-Request-ID, x-device-id, timezone, app-language, app-platform, app-version, app-versionNumber, edit-from, x-pld-user, X-Encrypt-Response`。**結論：無瀏覽器 client 要重放這組 header（至少 `Authorization` + Plaud client/device header），用 `GET /user/me` 驗證。** localplaud auth 走「使用者從 DevTools 複製已認證 XHR 的 header 集，localplaud 存起來重放」。確切 `Authorization` scheme/值、哪些 header 強制，**待確認**。
- **登入/取 token：未逆出**（未登出、未送表單）。`pld_pubKey`/`pld_passAlgorithm` 簽章推導未知。→ 先走貼 header 路線，程式化登入列 TODO。
- **逐字稿 + 摘要：同一支 `GET /file/detail/{file_id}`。** 開一筆錄音只打這支就渲染出「含講者+時間軸的逐字稿」+「模板摘要（模板名 `Adaptive Summary`，含 section 標題、action items）」。無獨立 `/trans/{id}`、`/ai/summary/{id}`（試打 404）。→ client 用 `/file/detail/{id}` 一次抓兩者。確切 JSON key/segment schema 待補。
- **音檔下載 URL：仍未確定（最高優先，最低門檻靠它）。** detail 已 cache、未點下載，未捕捉到 media 請求。`/file/url/{id}`、`/file/content/{id}` 皆 404，勿照猜實作。→ 下輪必須實際點下載/播放，捕捉真正產出音檔 URL 的請求。

### 仍待探明（open questions）
1. `Authorization` 確切 scheme/值 + 哪些自訂 header 強制。
2. httpOnly cookie 名稱/domain/expiry。
3. 登入 endpoint、POST body/response、`pld_pubKey`/`pld_passAlgorithm` 推導。
4. `/file/detail/{id}` 確切 JSON key 與 segment schema。
5. 簽章音檔 URL endpoint、回傳 body、CDN host/pattern。

> 逆向時的做法：在 web.plaud.ai 開一個已有逐字稿+摘要的檔案，先呼叫 `read_network_requests(clear:true)` 再觸發載入，比對打了哪些 `api-apse1` endpoint；或用 `javascript_tool` 在頁面 context 內 `fetch(..., {credentials:'include'})` 試打候選 endpoint。**只操作使用者自己的帳號與資料。**

## 本地 pipeline 需要復刻的處理（當雲端沒做、或我們想在本地重做時）
- **音檔轉檔**：opus → wav/mp3（ffmpeg）。
- **逐字稿（ASR）**：多語言（Plaud 宣稱 112 種語言）。本地候選：`whisper.cpp` / `faster-whisper` / WhisperX（後者含對齊與講者分離）。
- **講者分離（diarization）**：`pyannote.audio` 或 WhisperX 內建。
- **摘要／結構化筆記**：丟給 LLM（本地 Ollama 或雲端 API，讓使用者可設定）。可復刻 Plaud 的「多維摘要 / 模板」概念。
- **問答／搜尋（"Ask Plaud" 對應物）**：逐字稿做 embedding → 向量檢索 + LLM 回答。

## 技術方向（初步，未定案，開發時再決定）
- 要能跑在 **Mac mini 與 Linux Docker** 兩種環境 → 用 Docker Compose 打包，ASR/diarization 這種吃 GPU 的部分要能 fallback 到 CPU。
- 建議拆成：`poller`（輪詢雲端、下載）、`worker`（pipeline 處理）、`store`（DB + 檔案）、`api/ui`（查詢介面）。
- 資料存哪：音檔存本地檔案系統，metadata + 逐字稿 + 摘要存 DB（SQLite 起步，或 Postgres）。
- 語言／框架未定 — 開發時依 pipeline 生態（Python 對 ASR/diarization 生態最順）決定。

## 給 Claude 的工作守則
- 這是**乾淨的新專案**，還沒有程式碼。第一步是把上面的架構驗證清楚、定案技術選型，再動手。
- 逆向 Plaud API 時只碰使用者本人的帳號資料，別做出任何會改動雲端資料的呼叫（先只做 GET／唯讀）。
- 需要跑重活（逆向大量 API、寫 pipeline 骨架、資料分析）可以 spawn subagent；model routing 規則見 @.claude/model-routing.md（隨 repo 攜帶，其他機器也適用）——機械性 bulk work 丟 GPT-5.6（codex），面向使用者的 API/UI/文案設計留給 Opus/Fable。codex 的呼叫方式在 `.claude/skills/codex-*`。
- 面向使用者的東西（CLI/UI、設定檔格式、輸出格式）要有 taste。
