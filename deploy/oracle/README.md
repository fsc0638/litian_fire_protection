# Oracle 雲端架設（第 0 期，Always Free）

目標主機：Oracle Always Free 的 Ampere A1（ARM64），建議東京區。規格與限制見設計文件 §10.3。

## 部署模式

- **獨立主機**：`01_bootstrap.sh` 開本機防火牆 80/443，`LITIAN_STANDALONE=1 02_up.sh` 啟用自帶的 Caddy。
- **主機層 Caddy**（主機上已有 Caddy 佔用 80/443）：`LITIAN_HOST_CADDY=1 01_bootstrap.sh`，再以 `04_host_caddy_site.sh`／`05_route_api.sh` 在主機的 Caddy 加本系統的網站區塊。腳本先備份、驗證通過才平滑重載；去掉本系統區塊後新舊設定必須逐字相同；`.env` 填了 `CHECK_SITE` 時，會比對該網址變更前後的狀態，不一致就還原。
- 主機上若還有其他容器，不要執行 `docker builder prune` 或 `docker system prune`。

## 需要你人工做的事（AI 不能代做的部分）

| # | 什麼時候 | 你要做什麼 | 為什麼 AI 不能做 |
|---|---|---|---|
| 1 | 開始前 | 告訴我：主機 IP、SSH 帳號（Ubuntu 映像通常是 `ubuntu`，Oracle Linux 是 `opc`）、要用 `~/.ssh` 裡哪一把私鑰；並說明這台主機上是否已有其他專案在跑 | 連線目標與金鑰要由你明確指定，不能由 AI 猜測或試誤 |
| 2 | ~~第 1 步跑完後~~ | ~~Oracle 主控台開 80／443~~ **已完成**：2026-10-01 查主控台，安全清單已有這兩條規則 | — |
| 3 | 正式給人用前 | 測試期可用 `litian.<主機IP以連字號>.sslip.io`。sslip.io 常被企業網路攔截，正式期請在你的網域 DNS 加一筆 A 記錄指向主機公開 IP，告訴我網域名稱，我改 Caddy 區塊 | 需要登入網域商後台 |
| 3b | 第 1 期前 | 主控台擴大開機碟（免費額度共 200 GB） | 需要登入主控台 |
| 4 | 寫程式後 | 登入主機，自行把 `OPENAI_API_KEY`、`ASK_ACCESS_CODE`（選填）、`LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN` 填進 `/opt/litian/.env` | API 金鑰由你本人處理，AI 不經手 |
| 5 | 上線前 | 在 Oracle 物件儲存建一個備份用 bucket（免費 20 GB）並建立存取金鑰，放進主機 | 需要登入主控台、產生金鑰 |

## AI 自動執行的步驟（你給第 1 項資訊後）

```
# 1) 本機 → 主機暫存區，唯讀盤點
ssh <帳號>@<IP> 'mkdir -p /tmp/litian'
scp deploy/oracle/{00_inspect.sh,01_bootstrap.sh,02_up.sh,03_libredwg_test.sh,docker-compose.yml,Caddyfile,.env.example} <帳號>@<IP>:/tmp/litian/
ssh <帳號>@<IP> 'bash /tmp/litian/00_inspect.sh'      # 有其他服務佔 80/443 就停下來問你

# 2) 準備主機，再放設定檔、轉檔容器與樣本
ssh <帳號>@<IP> 'bash /tmp/litian/01_bootstrap.sh'    # 裝 Docker（若沒有）、開本機防火牆 80/443、建 /opt/litian
ssh <帳號>@<IP> 'cp /tmp/litian/{02_up.sh,03_libredwg_test.sh,docker-compose.yml,Caddyfile,.env.example} /opt/litian/'
scp deploy/oracle/libredwg/{Dockerfile,pipeline_check.py} tools/dwg2dxf/repair_dxf.py <帳號>@<IP>:/opt/litian/libredwg/
scp <DWG樣本資料夾>/*.dwg <帳號>@<IP>:/opt/litian/samples/

# 3) 啟動與實測
ssh <帳號>@<IP> 'bash /opt/litian/02_up.sh'           # 起 PostgreSQL+pgvector、Meilisearch、Redis、Caddy，健康檢查
ssh <帳號>@<IP> 'bash /opt/litian/03_libredwg_test.sh' # 在 ARM 上從原始碼編 LibreDWG 0.14，沙箱跑 19 個樣本

# 本機驗證
curl https://<對外網址>/healthz    # 應回 "litian ok"
```

## 安全設計

- 不停止、不修改主機上任何既有容器或服務；80/443 已被佔用時 `01_bootstrap.sh` 直接停下。
- 資料庫、搜尋、佇列只在內部網路，沒有對外開埠，也連不到外網。
- 內部密碼由 `02_up.sh` 在主機上隨機產生，存在權限 600 的 `.env`，不印出、不回傳本機。
- LibreDWG 原始碼包用 GitHub 官方 SHA-256 驗證；轉檔容器以無網路、512 MB、1 核、唯讀根目錄、無特權執行。

## 檔案

| 檔案 | 用途 |
|---|---|
| `00_inspect.sh` | 唯讀盤點主機 |
| `01_bootstrap.sh` | 一次性準備（可重複執行） |
| `02_up.sh` | 啟動與健康檢查（可重複執行） |
| `03_libredwg_test.sh` | 編譯 LibreDWG 並沙箱實測 |
| `docker-compose.yml` | 四個基礎服務，映像都已確認有 arm64 版 |
| `Caddyfile` | 對外 HTTPS 與健康檢查；程式完成後改成反向代理 |
| `.env.example` | 環境變數範本（不含真實金鑰） |
| `libredwg/Dockerfile`、`libredwg/pipeline_check.py` | 轉檔容器與三步管線檢查 |
| `04_host_caddy_site.sh` | 在主機層 Caddy 新增本系統網站區塊（主機層 Caddy 模式） |
| `05_route_api.sh` | 更新本系統網站區塊，把 `/api/*` 轉送到 API 容器 |
| `06_update_from_git.sh` | 從 GitHub 更新並重新部署 |
| `07_add_developer.sh` | 新增協作開發者帳號：只能金鑰登入、無 sudo、不在 docker 群組、限 CPU 與記憶體；`--disable` 停用 |
| `08_export_ask_log.sh` | 匯出法規問答的提問紀錄成 CSV（Excel 可開） |
