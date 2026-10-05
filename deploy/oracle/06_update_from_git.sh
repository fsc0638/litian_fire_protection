#!/usr/bin/env bash
# 從 GitHub 更新並重新部署（主機上執行，不要加 sudo）。
#   第一次：git clone https://github.com/fsc0638/litian_fire_protection.git /opt/litian/repo
#   之後：bash /opt/litian/repo/deploy/oracle/06_update_from_git.sh（已啟用自動部署時，推上 main 就會自動執行）
# 步驟：git pull（只接受快轉）→ 同步部署檔到 /opt/litian → 備份資料庫 → 重建映像 → 健康檢查 → 重載法規庫。
# 不碰 .env、資料目錄與其他服務；Caddy 設定變更另由 05_route_api.sh 處理。
# 結束碼：0 成功；1 失敗（健康檢查前或健康檢查不過）；3 服務已正常、但後續的法規庫重載失敗（不需換回）。
set -euo pipefail
REPO=/opt/litian/repo
RUN=/opt/litian
say() { echo "[update] $*"; }

if [ "$(id -u)" = 0 ]; then
  say "請用一般使用者執行（不要加 sudo）：以 root 執行會讓程式資料夾與鎖檔變成 root 擁有，自動部署之後會失敗"
  exit 1
fi
# 同一時間只跑一個部署（手動執行與自動部署共用一把鎖；自動部署呼叫本腳本時已持有，會設 LITIAN_DEPLOY_LOCKED=1）
if [ -z "${LITIAN_DEPLOY_LOCKED:-}" ]; then
  mkdir -p "$RUN/autodeploy"
  exec 9>"$RUN/autodeploy/lock"
  flock -n 9 || { say "另一個部署正在進行（可能是自動部署），請稍後再試"; exit 1; }
  export LITIAN_DEPLOY_LOCKED=1
fi

cd "$REPO"
before=$(git rev-parse --short HEAD)
if [ -n "${LITIAN_DEPLOY_SHA:-}" ]; then
  # 自動部署：只部署測試通過的那一個提交（測試期間 GitHub 又有新提交也不會混進來）；提交已在本機就不再連網
  git cat-file -e "${LITIAN_DEPLOY_SHA}^{commit}" 2>/dev/null || timeout 120 git -c gc.auto=0 fetch -q origin 9>&-
  git merge --ff-only -q "$LITIAN_DEPLOY_SHA"
else
  timeout 300 git -c gc.auto=0 pull --ff-only --quiet 9>&-
fi
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
# 審核工作台只能用 LINE 登入：.env 沒填 LINE Login 三個設定就部署，所有人都會登不進去
n=$(sudo grep -cE '^LINE_LOGIN_(CHANNEL_ID|CHANNEL_SECRET|CALLBACK_URL)=.+' "$RUN/.env" || true)
if [ "${n:-0}" -lt 3 ] && [ -z "${LITIAN_ALLOW_NO_LINE:-}" ]; then
  say "【停止】/opt/litian/.env 的 LINE_LOGIN_CHANNEL_ID、LINE_LOGIN_CHANNEL_SECRET、LINE_LOGIN_CALLBACK_URL 沒有都填（目前 ${n:-0} 個）。"
  say "審核工作台只能用 LINE 登入，請先依 deploy/oracle/README.md 第 4b 項設定；確定要先部署請加 LITIAN_ALLOW_NO_LINE=1 重跑。"
  exit 1
fi
# 部署前備份資料庫（結構變更不一定能回退；保留最近 7 份）。先寫暫存檔，完整寫完才換成正式檔名
sudo install -d -m 700 "$RUN/backup"
bk="$RUN/backup/litian-$(date +%Y%m%d-%H%M%S).sql.gz"
sudo docker compose exec -T postgres pg_dump -U litian litian </dev/null | gzip | sudo tee "$bk.part" >/dev/null
sudo chmod 600 "$bk.part"
sudo mv "$bk.part" "$bk"
say "資料庫備份：$bk（$(sudo du -h "$bk" | cut -f1)）"
# 備份資料夾只有 root 能讀，列檔也要 sudo（不加 sudo 會讓 set -e 在這裡中止部署）
sudo find "$RUN/backup" -maxdepth 1 -name 'litian-*.sql.gz' -printf '%T@ %p\n' | sort -rn | tail -n +8 | cut -d' ' -f2- | xargs -r sudo rm -f
sudo find "$RUN/backup" -maxdepth 1 -name 'litian-*.sql.gz.part' -mmin +60 -delete
# 圖面處理的資料夾（worker 與 converter 都以 65534 身分執行）
sudo install -d -o 65534 -g 65534 "$RUN/data/cases" "$RUN/data/convert" "$RUN/data/convert/in" "$RUN/data/convert/out" "$RUN/data/convert/work"
nice -n 19 sudo docker compose build api converter >/tmp/litian-build.log 2>&1 || { tail -20 /tmp/litian-build.log; exit 1; }
sudo docker compose up -d api worker converter
ok=""
for i in $(seq 1 30); do curl -sf --max-time 5 http://127.0.0.1:8100/api/health >/dev/null && { ok=1; break; }; sleep 2; done
if [ -z "$ok" ]; then
  say "【失敗】API 啟動後 60 秒內健康檢查沒有通過，最後的日誌："
  sudo docker compose logs --tail 40 api
  exit 1
fi
# worker、converter 不對外，健康檢查看不到：確認沒有一直重啟
sleep 10
bad=$(sudo docker compose ps -a --format '{{.Name}} {{.State}}' worker converter | grep -v ' running$' || true)
if [ -n "$bad" ]; then
  say "【失敗】背景服務沒有正常運作：$bad"
  sudo docker compose logs --tail 20 worker converter
  exit 1
fi
# 服務已正常：記下這一版與映像（自動部署據此判斷「已部署到哪一版」、失敗時換回哪個映像），並結束「進行中」狀態，
# 之後就算被中斷也不會換回
mkdir -p "$RUN/autodeploy"
git -C "$REPO" rev-parse HEAD >"$RUN/autodeploy/deployed"
api_img=$(awk '$1=="image:" && index($2,"litian-api:")==1 {print $2; exit}' "$RUN/docker-compose.yml")
conv_img=$(awk '$1=="image:" && index($2,"litian-libredwg:")==1 {print $2; exit}' "$RUN/docker-compose.yml")
echo "$(sudo docker image inspect -f '{{.Id}}' "$api_img") $(sudo docker image inspect -f '{{.Id}}' "$conv_img")" >"$RUN/autodeploy/good_images" || true
rm -f "$RUN/autodeploy/in_progress"
# 以下失敗不影響服務（已正常），回傳 3：自動部署只記警告、不換回
post=0
sudo docker compose exec -T api python -m litian.lawdb.store </dev/null || { say "【警告】法規庫重載失敗，請稍後手動重跑"; post=3; }
# 向量索引：只補算有變動的節點（沒有 OPENAI_API_KEY 時自動略過）；失敗只影響語意檢索的新條文
sudo docker compose exec -T api python -m litian.lawdb.vectors </dev/null || say "【警告】向量索引沒有更新（OpenAI 額度或連線？），稍後可手動重跑"
say "健康檢查：$(curl -s http://127.0.0.1:8100/api/health)"
exit "$post"
