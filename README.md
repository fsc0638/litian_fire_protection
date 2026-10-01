# 消防圖審系統

目前進度：**第 0 期 法規庫**（2026-10-01 上線測試版）。

## 目錄

| 路徑 | 內容 |
|---|---|
| `src/litian/lawdb/` | 法規庫：消防署行政規則與附件（nfa）、下載（fetch）、解析（parse）、場所代碼（occupancy）、交叉引用（xref）、建置（build）、載入（store）、檢索（search）、評測（evaluate） |
| `src/litian/api.py` | FastAPI 服務 |
| `data/lawdb/` | 建置產物（納入版控，部署直接用）：laws.json、nodes.jsonl、occupancy.json、xrefs.jsonl、legend.json、tables.json、build_report.json |
| `data/lawdb/legend/` | 附件三「消防圖說圖示範例」284 個圖例的符號圖（PNG） |
| `data/tables/` | 法定表格結構化檔（YAML，人工審閱的唯一來源）：第 18 條選設表、第 157 條避難器具表；`status: draft` 表示尚未經消防設備師校對 |
| `data/certs/` | 消防署網站缺的 TWCA 中繼憑證（來源與指紋見該目錄 README） |
| `data/raw/` | 官方原始資料（不納入版控，`build` 時自動下載） |
| `eval/` | 檢索測試集與評測報告 |
| `tests/` | 單元測試 |
| `deploy/oracle/` | Oracle 主機架設腳本（見該目錄 README） |
| `tools/dwg2dxf/` | DWG 轉檔與看圖工具 |
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
6. **檢索**：條號直取＋場所代碼直取＋Meilisearch 關鍵詞（詞頻比對與句尾丟詞兩種方式並行，各半權重）＋場所展開（KTV → 甲-1 → 引用甲-1 的條文）＋圖例（只在問到圖例、圖示、符號時才搜附件三，平常排除以免擠掉法條），RRF 合併。查詢會先把口語改成法條用語（「的、裝、放、要設」等，原因見 `search.py` 註解）。向量檢索待 `VOYAGE_API_KEY`。

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

2026-10-01 結果：78 題前 3 名命中 **77/78（99%）**（原 60 題 60/60、作業基準 7/8、圖例 8/8、防退化 2/2），驗收門檻 95%。最早基準見 `eval/report_baseline.md`（60 題 47/60）。
注意：60 題由開發者出題並據以調整，有過度貼合的風險；獨立驗收要加上消防設備師提供的 40 題。

## 部署

主機上以 GitHub clone 部署（2026-10-01 起）：

```
# 第一次
git clone https://github.com/fsc0638/litian_fire_protection.git /opt/litian/repo
# 之後每次更新（git pull → 同步部署檔 → 重建 api → 重載法規庫 → 健康檢查）
bash /opt/litian/repo/deploy/oracle/06_update_from_git.sh
```

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

- 每題先檢索 8 條相關條文，每條當成一份文件送給 Claude（`claude-opus-5-5`）並開啟引用；回答的引用一定指回這些條文，前端以編號連到條文卡片。
- 回答提到的條號若不在這次檢索的條文（含其內文）中，頁面會標示「未經檢索，請自行查證」。
- 主機 `.env` 要同時填 `ANTHROPIC_API_KEY` 與 `ASK_ACCESS_CODE`（至少 12 個字元）才會啟用 AI 回答；沒填時只列檢索結果，不花費用。
- 費用防護：存取碼（同一來源一小時內錯 10 次就暫停一小時）、同一來源每分鐘 6 次、每日總數 `ASK_DAILY_LIMIT`（預設 200；計數在記憶體，重新部署當天會歸零）。用量記在 API 容器日誌。
- LINE 版本目前不做（2026-10-01 決定）。

## 資料來源與授權

- 法律、命令條文：法務部全國法規資料庫 Open API（https://law.moj.gov.tw/api），依「政府資料開放授權條款－第 1 版」使用，須標示出處。
- 行政規則與附件（審查及查驗作業基準、附件三消防圖說圖示範例）：內政部消防署消防法令查詢系統（https://law.nfa.gov.tw）。
- 兩站皆聲明「與主管機關公布文字不同時，以主管機關公布為準」。本系統輸出為輔助資訊，設計簽證依消防設備師判斷。
