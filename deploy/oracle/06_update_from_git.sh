#!/usr/bin/env bash
# 從 GitHub 更新並重新部署（主機上執行）。
#   第一次：git clone https://github.com/fsc0638/litian_fire_protection.git /opt/litian/repo
#   之後：bash /opt/litian/repo/deploy/oracle/06_update_from_git.sh
# 步驟：git pull（只接受快轉）→ 同步部署檔到 /opt/litian → 重建 api 映像 → 重載法規庫 → 健康檢查。
# 不碰 .env、資料目錄與其他服務；Caddy 設定變更另由 05_route_api.sh 處理。
set -euo pipefail
REPO=/opt/litian/repo
RUN=/opt/litian
say() { echo "[update] $*"; }

cd "$REPO"
before=$(git rev-parse --short HEAD)
git pull --ff-only --quiet
after=$(git rev-parse --short HEAD)
say "程式碼 $before → $after"
# 本腳本可能剛被 pull 更新；執行中的 bash 仍讀舊版內容，改用新版重跑一次（只重跑一次）
if [ "$before" != "$after" ] && [ -z "${LITIAN_UPDATE_REEXEC:-}" ]; then
  LITIAN_UPDATE_REEXEC=1 exec bash "$REPO/deploy/oracle/06_update_from_git.sh"
fi

for f in docker-compose.yml Caddyfile .env.example 02_up.sh 03_libredwg_test.sh 04_host_caddy_site.sh 05_route_api.sh; do
  cp "$REPO/deploy/oracle/$f" "$RUN/$f"
done

cd "$RUN"
# 圖面處理的資料夾（worker 與 converter 都以 65534 身分執行）
sudo install -d -o 65534 -g 65534 "$RUN/data/cases" "$RUN/data/convert" "$RUN/data/convert/in" "$RUN/data/convert/out" "$RUN/data/convert/work"
nice -n 19 sudo docker compose build api converter >/tmp/litian-build.log 2>&1 || { tail -20 /tmp/litian-build.log; exit 1; }
sudo docker compose up -d api worker converter
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8100/api/health >/dev/null && break; sleep 2; done
sudo docker compose exec -T api python -m litian.lawdb.store </dev/null
# 向量索引：只補算有變動的節點（沒有 OPENAI_API_KEY 時自動略過）
sudo docker compose exec -T api python -m litian.lawdb.vectors </dev/null
say "健康檢查：$(curl -s http://127.0.0.1:8100/api/health)"
