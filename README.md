# 消防圖審系統

目前進度：第 0 期法規庫已上線測試；**第 1 期自審**進行中——圖面管線、審核工作台、平面理解（認房間）與第一批逐項檢核（撒水頭、室內消防栓、揚聲器的水平距離，滅火器步行距離與數量，探測器數量）已完成，尚待真實消防設備圖校正。

## 目錄

| 路徑 | 內容 |
|---|---|
| `src/litian/lawdb/` | 法規庫：消防署行政規則與附件（nfa）、下載（fetch）、解析（parse）、場所代碼（occupancy）、交叉引用（xref）、建置（build）、載入（store）、檢索（search）、評測（evaluate） |
| `src/litian/api.py` | FastAPI 服務 |
| `src/litian/drawing/` | 圖面管線：DWG 轉檔佇列、圖面中介資料抽取（文字、圖塊、圖紙）、背景處理程序 |
| `src/litian/plan/` | 平面理解：展開圖塊幾何、由牆柱門窗圍出房間、判斷房間種類（廁所、樓梯、機電室、挑空…）、樓地板範圍、可走區域 |
| `src/litian/review/` | 逐項檢核：設備辨識（附件三圖例＋圖塊字典）、水平／步行距離涵蓋、規則（每條綁法規節點）、缺失與標示圖 |
| `data/lawdb/` | 建置產物（納入版控，部署直接用）：laws.json、nodes.jsonl、occupancy.json、xrefs.jsonl、legend.json、tables.json、build_report.json |
| `data/lawdb/legend/` | 附件三「消防圖說圖示範例」284 個圖例的符號圖（PNG） |
| `data/tables/` | 法定表格結構化檔（YAML，人工審閱的唯一來源）：第 18 條選設表、第 157 條避難器具表；`status: draft` 表示尚未經消防設備師校對 |
| `data/certs/` | 消防署網站缺的 TWCA 中繼憑證（來源與指紋見該目錄 README） |
| `data/raw/` | 官方原始資料（不納入版控，`build` 時自動下載） |
| `eval/` | 檢索測試集與評測報告 |
| `tests/` | 單元測試 |
| `deploy/oracle/` | Oracle 主機架設腳本（見該目錄 README） |
| `tools/dwg2dxf/` | DWG 轉檔與看圖工具 |
| `tools/synth_fire/` | 合成測試用消防設備圖（在建築平面上自動配置設備並埋入已知缺失；輸出只放私人資料夾） |
| `tools/tables/` | 2026-10-01 產生表格 YAML 的轉錄腳本（紀錄用；之後校對直接改 YAML） |

## 本機開發

```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m litian.lawdb.build            # 加 --refresh 重新下載官方資料
.venv\Scripts\python -m pytest -q
```

## 法規庫做了什麼

1. **來源**：全國法規資料庫官方 Open API（法律包＋命令包 ZIP）。Python 3.13 對法務部憑證過嚴，`fetch.py` 只關掉 X509 嚴格旗標，其餘照常驗證。
2. **收錄**：消防法、設置標準、消防法施行細則、檢修及申報辦法、設計監造測試及檢修作業辦法（全國法規資料庫）；消防署《消防機關辦理建築物消防安全設備審查及查驗作業基準》12 點＋附件三「消防圖說圖示範例」284 個圖例、17 類（消防署網站，`nfa.py`）。消防署網站伺服器附錯中繼憑證，以 `data/certs/` 補上正確憑證，不關閉驗證。
3. **節點**：每條拆成項／款／目／細目，`node_id` 例：`D0120029/12/1/1/3`（設置標準第 12 條第 1 項第 1 款第 3 目）；引用寫法單一項條文省略「第1項」。公式、表格行掛在上一個節點。20 條「表格只在 PDF」的條文標 `pdf_table_url`。
4. **場所代碼**：第 12 條展開成甲-1～戊-3 與「其他」，共 29 個。
5. **交叉引用**：「第十二條第一款第一目」「同款」「前條第一項」「本法第六條」等解析成節點，830 處中 809 處解析成功、4 處指向未收錄法規（未解析多在方框表格內）。
6. **檢索**：條號直取＋場所代碼直取＋Meilisearch 關鍵詞（詞頻比對與句尾丟詞兩種方式並行，各半權重）＋場所展開（KTV → 甲-1 → 引用甲-1 的條文）＋圖例（只在問到圖例、圖示、符號時才搜附件三，平常排除以免擠掉法條），RRF 合併。查詢會先把口語改成法條用語（「的、裝、放、要設」等，原因見 `search.py` 註解）。另有向量路線（OpenAI `text-embedding-3-large` 1024 維，存在 PostgreSQL pgvector；權重 0.25，`python -m litian.lawdb.vectors` 在主機建索引，只補算有變動的節點）。

## 法定表格（data/tables/）

| 表格 | 來源 | 結構 | 狀態 |
|---|---|---|---|
| 設置標準第 18 條附表（水霧、泡沫、二氧化碳或惰性氣體、鹵化烴、乾粉選設表） | 只在法務部「完整條文」PDF；檔案 sha256 記在 YAML | 7 列場所 × 5 欄設備，○ 為可選設；註 5 則 | draft |
| 設置標準第 157 條附表（避難器具選設表） | 條文內方框字元 | 5 列設置場所 × 4 樓層欄，每格列出器具；「同上」已展開並保留原文 | draft |

- 第 18 條由開發者目視 PDF 轉錄，另由獨立轉錄者從原件重抄一次，逐格比對（結果見 `eval/verification_2026-10-01.md`）；列名與註以程式比對 PDF 文字層逐字確認。
- 第 157 條由程式解析；建置時若現行條文的表格與 YAML 不一致會出警告，表示法規修正了、需重新校對。第 5 列「第二層」的「同上」正上方是空白格，指涉有疑義，已在 YAML 標 `review`。
- 只有消防設備師能把 `status` 改成 `verified`，並須填 `verified_by`、`verified_at`（程式會檢查）。草稿在 API 一律附警告，規則引擎不得當作確認的法源。
- 原文瑕疵照原樣保留並記在 `source_quirks`（例：第 18 條「三百平公尺」「二百平方公尺上者」）。

## 評測

```
ssh -N -L 18100:127.0.0.1:8100 <帳號>@<主機>      # 另開一個視窗
.venv\Scripts\python -m litian.lawdb.evaluate --out eval/report.md
```

2026-10-01 結果（含向量路線，權重 0.25）：81 題前 3 名命中 **80/81（99%）**、第 1 名 71/81、MRR 0.933，驗收門檻 95%；權重比較見 `eval/vector_weight_2026-10-01.md`。最早基準見 `eval/report_baseline.md`（60 題 47/60）。
注意：60 題由開發者出題並據以調整，有過度貼合的風險；獨立驗收要加上消防設備師提供的 40 題。

## 部署

主機上以 GitHub clone 部署（2026-10-01 起）：

```
# 第一次
git clone https://github.com/fsc0638/litian_fire_protection.git /opt/litian/repo
# 手動更新（備份資料庫 → git pull → 同步部署檔 → 重建 api → 重載法規庫 → 健康檢查）
bash /opt/litian/repo/deploy/oracle/06_update_from_git.sh
# 啟用自動部署（只需一次）
bash /opt/litian/repo/deploy/oracle/11_install_autodeploy.sh
```

**自動部署**（2026-10-05 起）：推上 GitHub `main` 後約 2～3 分鐘內，主機自己發現新提交 → 在拋棄式容器裡用新版程式跑全部測試（含資料庫整合測試；測試用專屬的資料庫帳號，碰不到正式資料）→ 通過才部署那一個提交（部署前自動備份資料庫）。主機上的指令一律不要加 `sudo`。

- 測試沒過：不部署，網站維持原版本；修好後再推一次就會重試。測試環境本身出錯（不是測試沒過）會自動重試，連續 3 次才暫停。
- 部署後健康檢查不過（含背景服務一直重啟），或部署途中被中斷（逾時、重開機）：程式與映像自動換回前一版，並**暫停自動部署**；處理後執行 `bash /opt/litian/repo/deploy/oracle/10_autodeploy.sh resume`，會重新測試並部署。注意：換回的是程式與映像，資料庫結構變更不會自動還原（需要時用 `/opt/litian/backup/` 裡部署前的備份）。
- 健康檢查通過、只有後續的法規庫重載失敗：保留新版，紀錄裡會有警告，手動重跑即可。
- 看狀態與最近紀錄：`bash /opt/litian/repo/deploy/oracle/10_autodeploy.sh status`；要手動維護時先 `pause`。紀錄在主機 `/opt/litian/autodeploy/`。目前沒有推播通知，暫停時只看得到狀態。
- `main` 的歷史被改寫（強制推送）時會暫停。處理方式：`git -C /opt/litian/repo fetch origin && git -C /opt/litian/repo reset --hard "$(git -C /opt/litian/repo merge-base HEAD origin/main)"` 再 `resume`（新的 main 會先測試再部署；不要直接 reset 到 origin/main 再手動部署，那樣會跳過測試）。
- 磁碟剩不到 6 GB 時整輪略過，清出空間後自動繼續；舊映像用 ID 清除，不使用 `docker system/builder prune`（同主機有其他服務）。
- 做法是主機定時讀取公開 repo，不需要金鑰或 webhook，GitHub 端不用設定。**能推上 `main` 就等於能在主機上以可 sudo 的身分執行程式**（新版的部署腳本會在主機執行；同主機的其他服務與 `.env` 金鑰都在範圍內），所以 `main` 的分支保護必須維持：只能經負責人核准的 PR 合併、禁止強制推送。

對外：`https://<SITE_ADDRESS>/api/...`（sslip.io 測試網址常被企業網路攔截，正式期要換自己的網域）。

## API

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康檢查 |
| GET | `/api/law/laws` | 收錄法規與版本日期 |
| GET | `/api/law/search?q=…&limit=5` | 檢索；回傳節點、引用寫法、整條原文、走了哪幾路、PDF 表格警告 |
| GET | `/api/law/nodes/{node_id}` | 取單一節點＋子節點＋它引用誰、誰引用它 |
| GET | `/api/law/occupancy` | 第 12 條場所代碼表 |
| GET | `/api/law/legend?category=…` | 附件三圖例清單（名稱、類別、備註、符號圖網址） |
| GET | `/api/law/legend/image/{檔名}` | 圖例符號圖（PNG；只接受建置產生的檔名） |
| GET | `/api/law/tables` | 已結構化的法定表格清單與校對狀態 |
| GET | `/api/law/tables/{node_id}` | 單張表格（草稿會附警告） |
| GET | `/` | 法規問答網頁 |
| GET | `/api/law/ask/status` | AI 回答是否啟用、每日上限 |
| POST | `/api/law/ask` | 法規問答（SSE 串流）：先回檢索到的條文，再串流 AI 回答與引用；需 `X-Access-Code` |

## 法規問答網頁

- AI 使用 OpenAI `gpt-5.6-sol`（Responses API，`reasoning.effort` medium，`store=false` 不在 OpenAI 端保存對話；2026-10-01 使用者決定）。
- 每題先檢索 8 條相關條文，以 [1]～[8] 編號附在問題裡，要求每個論點後標註編號；前端把編號連到條文卡片。
- OpenAI 沒有 API 層級的引用保證，由程式查核：超出範圍的編號列為「無效引用」，回答提到但這次沒檢索到的條號列為「未經檢索」，頁面都會警告。
- 主機 `.env` 填了 `OPENAI_API_KEY` 就啟用 AI 回答；沒填時只列檢索結果，不花費用。
- `ASK_ACCESS_CODE` 選填（至少 12 個字元）：設定後才要求存取碼；目前不用（2026-10-01 決定），任何人都能用 AI 回答，只受下列上限控管。設定後，同一來源一小時內錯 10 次就暫停一小時。
- 費用防護：每日總數 `ASK_DAILY_LIMIT`（預設 500，不分來源；台北時間每天 23:59 重新計算；計數存在資料庫 `ask_usage` 表，重新部署不會歸零）。今日用量見 `/api/law/ask/status` 的 `used_today`，每題的 token 用量記在 API 容器日誌。
- 問題提到具體場所（例：KTV、旅館）時，自動把該場所的第 12 條分類條文排在最前面，AI 才能先判斷場所類別再套門檻。
- 測試期提問紀錄：每次按「查詢」寫一筆到 `ask_log` 表（問題、檢索結果、AI 回答與查核結果、用量、耗時；來源只存 IP 的雜湊，無法還原）。頁尾已告知使用者。匯出 CSV：`bash /opt/litian/repo/deploy/oracle/08_export_ask_log.sh [起始日 YYYY-MM-DD]`。
- LINE 版法規問答機器人目前不做（2026-10-01 決定；與審核工作台的 LINE 登入無關）。

## 圖面處理（第 1 期 M1）

- 上傳的 DWG 交給 `converter` 容器轉 DXF（LibreDWG＋修補），該容器**無網路、唯讀、限資源、不碰資料庫**，只透過 `data/convert` 交接資料夾收件交件（協定見 `src/litian/drawing/convert_client.py`）。
- `worker` 從 PostgreSQL 佇列取檔，在子行程（限時、限記憶體）用 ezdxf 抽出圖面中介資料：依圖框與「圖號」屬性把模型空間切成各張圖，收文字、圖塊（含屬性）、線段、封閉多邊形（`src/litian/drawing/ir.py`）。
- 命令列（主機上）：`sudo docker compose exec worker python -m litian.drawing.cli ingest --name 名稱 /data/samples/*.dwg`，再用 `status 案件ID`、`sheets 案件ID` 看結果。

## 審核工作台（第 1 期 M1b）

- 網址 `/workbench`：登入、建立案件、上傳 DWG／DXF（拖放、單檔上限 200 MB）、看處理狀態（每個檔一句白話說明：已檢核幾層幾條、檢核失敗原因、被哪個消防設備圖當外部參考併入、為什麼沒檢核）；各張圖的圖號圖名與抽出的文字收在頁面最下方（認不出樓層、房間時核對用）。缺失的「依據」滑過或點按就顯示條文全文（表格照原表格顯示）。
- **登入用 LINE**（LINE Login v2.1，`src/litian/line_login.py`），不使用密碼。開通靠管理者發的**一次性邀請連結**：工作台右上角「帳號管理」輸入帳號名稱與角色 → 產生連結（預設 24 小時內有效、只能用一次）→ 本人點連結用 LINE 登入，該 LINE 帳號即綁定此帳號。同仁換手機、換 LINE 帳號時，在帳號列表按「重新綁定 LINE」另發換綁連結（角色不變，原有登入全部登出）。開新帳號遇到同名會拒絕，不會悄悄變成換綁。沒有綁定的 LINE 帳號一律進不來。
- 安全做法：state（綁定發起登入的瀏覽器 Cookie，只能用一次、10 分鐘內有效）、nonce、PKCE（S256）；ID token 交給 LINE 驗證端點驗簽，再核對 iss、aud、nonce、到期時間；LINE 的 access token 用完即丟。邀請連結的權杖放在網址 `#` 片段（不送到伺服器、不進存取紀錄），開通一律關閉 LINE 自動登入。邀請權杖與登入權杖在資料庫只存 SHA-256。Cookie 用 `__Host-` 前綴（同網域其他子網域塞不進來）、HttpOnly、Secure、SameSite=Lax，登入 12 小時過期。不以來源 IP 封鎖登入（權杖都是 256 位元亂數，無從猜測；IP 封鎖反而會讓整間辦公室被一個網頁鎖住），進行中的登入暫存另有全站上限。至少保留一位啟用中的管理者（同時互相停用也擋得住）；管理者不能停用自己、不能在網頁上替自己換綁；管理者被停用或降級時，他發出、還沒用掉的邀請一併作廢。
- 設定（由專案主本人做，**要在部署這一版之前**）：在 LINE Developers 建立 **LINE Login 頻道**（App type 選 Web app；和問答機器人的 Messaging API 頻道不同），LINE Login 分頁的 Callback URL 填 `https://<對外網址>/api/auth/line/callback`；把 `LINE_LOGIN_CHANNEL_ID`、`LINE_LOGIN_CHANNEL_SECRET`、`LINE_LOGIN_CALLBACK_URL` 填進主機 `/opt/litian/.env`。部署腳本 `06_update_from_git.sh` 會檢查這三項沒填就停止，並在部署前備份資料庫到 `/opt/litian/backup/`（這一版會移除舊的密碼欄位，回退需先還原備份）。頻道剛建好是「Developing」，只有頻道的 Admin／Tester 能登入（Tester 的開發者帳號要先連結 LINE 帳號）；開放給同仁要改成「Published」（改了不能改回）。
- 換正式網域時一起改：Caddy 網站位址（`05_route_api.sh`）、`.env` 的 `LINE_LOGIN_CALLBACK_URL`（改完 `cd /opt/litian && sudo docker compose up -d api`）、LINE 頻道的 Callback URL（可先同時登記新舊兩個，切換時不中斷）；已發出的邀請連結指向舊網址，要重發。從舊網址按登入時，系統會先轉到 `LINE_LOGIN_CALLBACK_URL` 的網址。
- LINE 開發準則要求使用者不再使用時解除授權：本系統不保存 LINE 的 access token，無法代為解除；停用帳號時請當事人到 LINE「設定 → 帳號 → 已連動的應用程式」自行解除（登入頁與停用確認視窗都有寫）。
- 端點：`GET /api/auth/line/start`（用 LINE 登入）、`POST /api/auth/line/start`（邀請頁，權杖放表單）、`GET /api/auth/line/callback`、`POST /api/auth/invite`（查邀請）、`POST /api/auth/logout`、`GET /api/auth/me`；管理者：`GET /api/admin/users`、`POST /api/admin/invites`（`mode` 為 new 或 rebind）、`DELETE /api/admin/invites/{id}`、`PATCH /api/admin/users/{id}`。
- 第一位管理者（或網頁進不去時）在主機上產生邀請連結：`cd /opt/litian && sudo docker compose exec api python -m litian.auth invite 帳號 --role admin`（另有 `list`、`disable 帳號`、`enable 帳號`、`role 帳號 admin|reviewer`）。舊版用密碼建立的帳號保留帳號名稱與角色，用 `invite 原帳號名稱` 產生換綁連結即可改用 LINE 登入。

## 資料來源與授權

- 法律、命令條文：法務部全國法規資料庫 Open API（https://law.moj.gov.tw/api），依「政府資料開放授權條款－第 1 版」使用，須標示出處。
- 行政規則與附件（審查及查驗作業基準、附件三消防圖說圖示範例）：內政部消防署消防法令查詢系統（https://law.nfa.gov.tw）。
- 兩站皆聲明「與主管機關公布文字不同時，以主管機關公布為準」。本系統輸出為輔助資訊，設計簽證依消防設備師判斷。

## 逐項檢核（第 1 期 M3～M6）

- 上傳的 DWG／DXF 抽取完後，含平面圖的檔案自動檢核（狀態「檢核中」）：認房間（`src/litian/plan/`）→ 認設備（附件三圖例名稱＋圖塊字典）→ 逐條規則（`src/litian/review/`）→ 缺失與各層標示圖。
- 規則：撒水頭、室內消防栓、揚聲器的水平距離；滅火器步行距離、效能值、電氣室另設；探測器數量；出口標示燈位置、避難方向指示燈有效範圍、緊急照明；排煙口距離與開口面積；連結送水管出水口；立管與末端查驗閥管徑；依場所判定應設設備（第 14～30-1 條）並比對各層有無。每條缺失附法規節點，測試會確認節點存在。
- 條件不明（感度、天花板高度、場所類別等）以上下限判定：寬鬆仍不符＝不符，只有嚴格不符＝資料不足並列出要補的資料。工作台「檢核條件」補填後只重跑檢核，不重新轉檔。
- 條文有不同讀法之處（管道間是否納入水平距離、樓梯廣播、滅火器是否只檢討居室等）集中在「法規解讀設定」（`checks.DEFAULT_POLICY`），預設只降嚴重度並寫明兩種讀法，事務所可在工作台切換。
- 大型圖：配置頁（paper space）每頁視為一張圖；主圖引用的外部參考（建築底圖）若同案件有上傳，自動綁定後再檢核（參考檔後上傳時主圖自動重排）。設備位置以圖塊的圖形中心為準；挑空上層（屋頂板下）的探測器投影到下層空間檢核。
- 圖塊字典 `data/review/blocks.yaml`：事務所自訂圖塊名稱 → 附件三圖例（可帶預設規格）；只放有證據的對應（符號形狀、圖層、圖例表數量互相核對），推測的不放。
- 審核：每條缺失可接受／退回（附備註），識別碼由規則＋樓層＋房間＋範圍算出，重跑後仍對得上。匯出報告（HTML，可列印成 PDF）與 CSV；退回者不列入報告。
- 合成測試圖：`tools/synth_fire/make_synthetic.py` 在建築平面上自動配置設備並埋入已知缺失（輸出只放私人資料夾）。

### CAD 原樣圖（看原圖、缺失疊在上面）

- 工作台的樓層圖照 CAD 原樣顯示：圖層顏色、線型、線寬（照出圖紙粗細）、文字、圖框、配置頁的視埠內容，白底；可縮放、平移（OpenSeadragon，從 cdnjs 載入），缺失標示疊在圖上，可用「顯示檢核標示」開關。原圖還沒好或產生失敗時先顯示簡化標示圖。
- 產生方式（`src/litian/review/cadview.py`）：ezdxf 繪圖模組＋matplotlib（Agg 點陣，授權寬鬆），每張樓層圖長邊約 5000～8000 像素，切成 Deep Zoom 圖磚放在檢核資料夾 `cad/`；`meta.json` 記公尺座標 → 像素的轉換，缺失位置（`<樓層>.overlay.json`，公尺）照這個換算。CAD 專用字型（.shx）一律改用中文字型（容器內 Noto Sans CJK）。
- 背景排隊（`case_file.cad_state`）：檢核完成的檔排入佇列，**佇列裡沒有其他檔要處理時**才開始畫，畫圖期間照常處理新上傳的檔；一次畫一個檔（子行程限記憶體 3.5 GB、限 CPU 一小時、降低優先順序）。竣工圖等大型圖約 5～20 分鐘。工作台顯示「原圖排隊中／產生中」並每 30 秒自動檢查，好了就換上原圖。
- 重新部署（worker 收到 SIGTERM）時畫到一半的退回排隊、不算次數，部署完接著畫；被系統砍掉（記憶體不足）或 worker 當掉的重新排隊，最多畫 3 次；逾時、程式錯誤直接記失敗。worker 重啟前其實已畫完的照結果記，不重畫。
- 只重跑檢核（改檢核條件）不重畫：底圖沒變，疊圖資料由檢核更新；但還沒畫過或上次畫失敗的會趁這次排隊（改一次檢核條件就能重畫失敗的原圖）。整個檔重新處理（例：後來上傳了外部參考）時舊圖作廢重畫，期間顯示簡化圖。檢核失敗的檔不畫。
- 這個功能上線前已檢核的檔會自動補排隊；舊版檢核沒有產生缺失疊圖資料（`<樓層>.overlay.json`）的，先自動只重跑檢核再畫。疊圖資料載不到時工作台退回附標示的簡化圖，不顯示沒有標示的原圖。
- 主機實測（ARM 2 核，竣工圖 11 張配置頁）：約 4 分 40 秒、記憶體峰值約 1.2 GB、圖磚約 78 MB。

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/api/cases/{id}` | 案件、檔案（`note` 白話說明、`review` 檢核狀態、`xref_of` 被哪個主圖當外部參考併入）、圖紙 |
| GET | `/api/cases/{id}/reviews` | 檢核結果、引用條文、審核結果、檢核條件；各樓層 `cad`＝原圖狀態（done／pending／rendering／failed） |
| PUT | `/api/cases/{id}/context` | 存檢核條件並排入重跑檢核 |
| POST | `/api/cases/{id}/files/{fid}/decisions` | 缺失接受／退回／撤回 |
| GET | `/api/cases/{id}/files/{fid}/review/{樓層}.svg` | 各層檢核標示圖（簡化） |
| GET | `/api/cases/{id}/files/{fid}/review/{樓層}.overlay.json` | 疊在原圖上的缺失（公尺座標） |
| GET | `/api/cases/{id}/files/{fid}/cad/{樓層}/meta.json`、`/cad/{樓層}/{層級}/{欄}_{列}.png` | CAD 原樣圖的圖磚資訊與圖磚 |
| GET | `/api/cases/{id}/report`、`/report.csv` | 匯出報告 |
