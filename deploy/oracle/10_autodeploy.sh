#!/usr/bin/env bash
# 推上 GitHub 就自動部署。主機上由 systemd 計時器每 2 分鐘執行一次（安裝：11_install_autodeploy.sh）。
#
# 流程：問 GitHub main 有沒有比「已部署版本」新的提交 → 沒有就結束
#       → 新提交解到暫存目錄，用「已部署版本的」09 在拋棄式容器裡跑全部測試（新程式只在容器裡、以無權限身分執行）
#       → 通過才用 06 部署「這一個」提交（部署前 06 會備份資料庫）
#       → 健康檢查前失敗、健康檢查不過，或在那之前被中斷（逾時、重開機）：程式資料夾、部署檔與映像都換回前一版，
#         並暫停自動部署等人處理
#       → 健康檢查通過後的步驟（法規庫重載）失敗或被中斷：保留新版，記警告
# 測試沒過（含新程式語法錯誤、找不到測試）的提交不部署、不暫停：同一提交不再重試，有更新的提交推上來時再試。
# 測試環境本身出錯（不是測試沒過）：下一輪重試，連續 3 次才暫停。磁碟剩不到 6 GB 時整輪略過。
# 主機只從公開的 GitHub repo 讀取，不需要任何金鑰或 webhook。
# 注意：能推上 main 的人，新版的 06 與下一版的本腳本會在主機上以可 sudo 的身分執行（等同主機 root，
# 同主機的其他網站與 .env 金鑰都在範圍內）；main 的分支保護必須維持（只能經負責人核准的 PR 合併）。
# 測試固定用「已部署版本的」09：某個提交若需要新版 09 才測得起來，只能手動用 06 部署那一版。
#
# 手動指令（在主機上，不要加 sudo）：
#   bash /opt/litian/repo/deploy/oracle/10_autodeploy.sh status   狀態與最近紀錄
#   bash /opt/litian/repo/deploy/oracle/10_autodeploy.sh pause    暫停（例：要手動維護時）
#   bash /opt/litian/repo/deploy/oracle/10_autodeploy.sh resume   解除暫停並清掉失敗紀錄（處理完原因後）
#   bash /opt/litian/repo/deploy/oracle/10_autodeploy.sh run      立刻檢查一次（不必等計時器）
set -euo pipefail
RUN=/opt/litian
REPO=$RUN/repo
STATE=$RUN/autodeploy
LOG=$STATE/autodeploy.log
PAUSED=$STATE/paused
FAILED=$STATE/failed_commit       # 測試沒過的提交（不重試）
INFRA=$STATE/infra_failures       # 「提交 次數」：測試環境出錯的次數
DEPLOYED=$STATE/deployed          # 最後一次成功部署的提交（06 健康檢查通過時寫入）
GOOD=$STATE/good_images           # 最後一次成功部署時的「api 映像 ID 轉檔映像 ID」（06 寫入）
INPROG=$STATE/in_progress         # 「前一版 新版 前一版api映像名 前一版轉檔映像名」：部署進行中；06 健康檢查通過時刪除
LOWDISK=$STATE/low_disk
CAND=$STATE/candidate
MIN_FREE_GB=6
DEPLOY_FILES="docker-compose.yml Caddyfile .env.example 02_up.sh 03_libredwg_test.sh 04_host_caddy_site.sh 05_route_api.sh"  # 與 06 相同

log() { local m; m="$(date '+%F %T') $*"; { echo "$m" >>"$LOG"; } 2>/dev/null || true; echo "$*"; }
pause_with() { { echo "$* ｜$(date '+%F %T')" >"$PAUSED"; } 2>/dev/null || true; log "【暫停】$*"; }
img_id() { sudo docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }
deployed_sha() { if [ -s "$DEPLOYED" ]; then cat "$DEPLOYED"; else git -C "$REPO" rev-parse HEAD; fi; }
# compose 檔裡用的映像名稱（例：litian-api:0.1）；以 compose 為準，不寫死
img_name() {   # $1＝compose 檔，$2＝名稱開頭，$3＝取不到時的預設
  local n
  n=$(awk -v p="$2" '$1=="image:" && index($2,p)==1 {print $2; exit}' "$1" 2>/dev/null || true)
  echo "${n:-$3}"
}
api_name() { img_name "$1" "litian-api:" "litian-api:0.1"; }
conv_name() { img_name "$1" "litian-libredwg:" "litian-libredwg:0.14"; }

health() {
  local i
  for i in $(seq 1 30); do
    curl -sf --max-time 5 http://127.0.0.1:8100/api/health >/dev/null && return 0
    sleep 2
  done
  return 1
}

rollback() {   # $1＝要換回的提交，$2＝原因，$3/$4＝前一版的映像名稱。每一步都做到底，不因單一步驟失敗而中止
  set +e
  local prev=$1 why=$2 old_api=$3 old_conv=$4 new_api new_conv id f
  pause_with "$why；已換回 ${prev:0:7}，處理後執行 resume"
  # 先用「目前的」compose 記下新映像，之後才還原部署檔
  new_api=$(img_id "$(api_name "$RUN/docker-compose.yml")")
  new_conv=$(img_id "$(conv_name "$RUN/docker-compose.yml")")
  if [ -z "$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    git -C "$REPO" reset -q --hard "$prev"
  else
    log "程式資料夾有未提交的修改，沒有還原程式碼（部署檔與映像仍會換回），請人工確認"
  fi
  for f in $DEPLOY_FILES; do git -C "$REPO" show "$prev:deploy/oracle/$f" >"$RUN/$f" 2>/dev/null; done
  { echo "$prev" >"$DEPLOYED"; } 2>/dev/null       # 換回後實際在跑的就是前一版：resume 後會重新測試並部署新版
  if [ -n "$(img_id litian-api:rollback)" ]; then
    sudo docker tag litian-api:rollback "$old_api"
    sudo docker tag litian-libredwg:rollback "$old_conv"
  fi
  (cd "$RUN" && sudo docker compose up -d api worker converter >/dev/null 2>&1)
  if health; then log "已換回 ${prev:0:7}，服務正常"; else log "【嚴重】換回後健康檢查仍不過，請立刻人工處理"; fi
  # 失敗的新映像用 ID 刪掉（只刪本系統的映像，不用 prune；同主機其他網站的映像不受影響）
  for id in $new_api $new_conv; do
    [ "$id" = "$(img_id "$old_api")" ] || [ "$id" = "$(img_id "$old_conv")" ] || sudo docker rmi "$id" >/dev/null 2>&1
  done
  sudo docker rmi litian-api:rollback litian-libredwg:rollback litian-api:candidate >/dev/null 2>&1
  rm -f "$INPROG"
  set -e
}

rollback_from_inprog() {   # $1＝原因；依 in_progress 記錄換回
  local prev new oa oc
  read -r prev new oa oc <"$INPROG" || true
  rollback "$prev" "$1（${new:0:7}）" "${oa:-litian-api:0.1}" "${oc:-litian-libredwg:0.14}"
}

infra_fail() {   # $1＝提交，$2＝說明；同一提交連續 3 次才暫停
  local n=1
  if [ -s "$INFRA" ] && [ "$(cut -d' ' -f1 "$INFRA")" = "$1" ]; then n=$(( $(cut -d' ' -f2 "$INFRA") + 1 )); fi
  echo "$1 $n" >"$INFRA"
  if [ "$n" -ge 3 ]; then pause_with "${1:0:7} $2（連續 $n 次），請人工確認"; else log "${1:0:7} $2（第 $n 次，下一輪重試）"; fi
}

disk_free_gb() { (sudo df -BG --output=avail /var/lib/docker 2>/dev/null || true) | tail -n 1 | tr -dc 0-9; }

status() {
  local d
  d=$(deployed_sha)
  echo "已部署：$(git -C "$REPO" log -1 --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' "$d" 2>/dev/null || echo "$d")"
  echo "GitHub main：$( (timeout 30 git -C "$REPO" ls-remote origin refs/heads/main 2>/dev/null || true) | cut -c1-7)"
  if [ -f "$PAUSED" ]; then echo "狀態：已暫停｜$(cat "$PAUSED")"; else echo "狀態：啟用中（每 2 分鐘檢查）"; fi
  [ -f "$INPROG" ] && echo "部署進行中（或上一輪被中斷）：$(cat "$INPROG")"
  [ -f "$FAILED" ] && echo "測試沒過、不會重試的提交：$(cut -c1-7 "$FAILED")"
  [ -f "$INFRA" ] && echo "測試環境出錯紀錄：$(cat "$INFRA")"
  echo "磁碟剩餘：$(disk_free_gb)G"
  systemctl list-timers litian-autodeploy.timer --no-pager 2>/dev/null | sed -n 2p || true
  echo "--- 最近紀錄（$LOG）"
  tail -n 15 "$LOG" 2>/dev/null || echo "（還沒有紀錄）"
}

deploy_new() {
  local prev cur remote short subject base rc free old_api old_conv good_api good_conv
  free=$(disk_free_gb)
  if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_GB" ]; then
    [ -f "$LOWDISK" ] || log "磁碟只剩 ${free}G（門檻 ${MIN_FREE_GB}G），暫不部署；清出空間後會自動繼續"
    touch "$LOWDISK"
    return 0
  fi
  rm -f "$LOWDISK"
  prev=$(deployed_sha)
  cur=$(git -C "$REPO" rev-parse HEAD)
  remote=$( (timeout 60 git -C "$REPO" ls-remote origin refs/heads/main || true) | cut -f1)
  [ -n "$remote" ] || return 0                          # GitHub 暫時連不上：下一輪再試
  [ "$remote" = "$prev" ] && return 0
  [ -f "$FAILED" ] && [ "$(cat "$FAILED")" = "$remote" ] && return 0
  if ! timeout 300 git -C "$REPO" -c gc.auto=0 fetch -q origin main 9>&-; then
    log "從 GitHub 抓取失敗，下一輪再試"
    return 0
  fi
  short=${remote:0:7}
  if ! git -C "$REPO" merge-base --is-ancestor "$cur" "$remote"; then
    pause_with "GitHub 上的 main（$short）不是目前版本（${cur:0:7}）的延續（歷史被改寫？），需要人工處理"
    return 1
  fi
  subject=$(git -C "$REPO" log -1 --format=%s "$remote")
  log "發現新版本 ${prev:0:7} → $short：$subject"

  # 1) 新版程式放暫存目錄，用已部署版本的 09 測試（新程式只在測試容器裡以無權限身分執行）
  rm -rf "$CAND"
  mkdir -p "$CAND"
  git -C "$REPO" archive "$remote" | tar -x -C "$CAND"
  old_api=$(api_name "$RUN/docker-compose.yml")
  old_conv=$(conv_name "$RUN/docker-compose.yml")
  base=$old_api
  if ! git -C "$REPO" diff --quiet "$prev" "$remote" -- pyproject.toml Dockerfile; then
    log "依賴或映像設定有變，先建候選映像再測"
    if ! timeout 40m sudo docker build -q -t litian-api:candidate "$CAND" >"$STATE/last_build.log" 2>&1; then
      sudo docker rmi litian-api:candidate >/dev/null 2>&1 || true
      infra_fail "$remote" "候選映像建置失敗（見 $STATE/last_build.log）"
      return 1
    fi
    base=litian-api:candidate
  fi
  rc=0
  LITIAN_DEPLOY_LOCKED=1 LITIAN_TEST_REPO="$CAND" LITIAN_TEST_BASE="$base" \
    timeout 30m bash "$REPO/deploy/oracle/09_db_tests.sh" >"$STATE/last_test.log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    sudo docker rmi litian-api:candidate >/dev/null 2>&1 || true
    # pytest：1＝有測試沒過，2＝收集測試時出錯（語法錯誤、匯入失敗），5＝找不到測試 → 都是程式的問題
    if grep -qE "pytest-exit=(1|2|5)$" "$STATE/last_test.log"; then
      echo "$remote" >"$FAILED"
      log "【不部署】$short 測試沒過（完整輸出：$STATE/last_test.log）："
      { grep -E "^(FAILED|ERROR) |[0-9]+ (passed|failed)|error" "$STATE/last_test.log" | tail -n 8 | tee -a "$LOG"; } 2>/dev/null || true
      return 0
    fi
    infra_fail "$remote" "測試環境出錯（不是測試沒過，結束碼 $rc；見 $STATE/last_test.log）"
    return 1
  fi
  log "測試通過：$(grep -E "[0-9]+ passed" "$STATE/last_test.log" | tail -n 1 || true)"
  rm -f "$FAILED" "$INFRA"

  # 2) 部署這一個提交；先替「最後一次成功部署」的映像加上 rollback 標籤，失敗或被中斷時換回
  #    （若目前的映像是手動部署失敗留下的壞映像，用記錄下來的好映像 ID，不用當下的標籤）
  good_api=""; good_conv=""
  [ -s "$GOOD" ] && read -r good_api good_conv <"$GOOD" || true
  [ -n "$good_api" ] && [ -n "$(img_id "$good_api")" ] || good_api=$old_api
  [ -n "$good_conv" ] && [ -n "$(img_id "$good_conv")" ] || good_conv=$old_conv
  if ! { sudo docker tag "$good_api" litian-api:rollback && sudo docker tag "$good_conv" litian-libredwg:rollback; }; then
    sudo docker rmi litian-api:candidate >/dev/null 2>&1 || true
    pause_with "無法替現有映像加上 rollback 標籤（docker 異常？），沒有部署 $short"
    return 1
  fi
  echo "$prev $remote $old_api $old_conv" >"$INPROG"
  # 被中斷：還在 in_progress（健康檢查還沒通過）才換回；06 健康檢查通過時會刪掉 in_progress
  trap '[ -f "$INPROG" ] && rollback_from_inprog "部署被中斷"; exit 1' TERM INT HUP
  rc=0
  LITIAN_DEPLOY_LOCKED=1 LITIAN_DEPLOY_SHA="$remote" bash "$REPO/deploy/oracle/06_update_from_git.sh" >"$STATE/last_deploy.log" 2>&1 || rc=$?
  trap - TERM INT HUP
  if [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ]; then
    echo "$remote" >"$DEPLOYED"
    rm -f "$INPROG"
    sudo docker rmi litian-api:rollback litian-libredwg:rollback litian-api:candidate >/dev/null 2>&1 || true
    rm -rf "$CAND"
    if [ "$rc" -eq 3 ]; then
      log "【已部署，但後續步驟失敗】$short：服務正常，法規庫重載沒有完成（見 $STATE/last_deploy.log），請人工重跑"
    else
      log "【已部署】$short：$subject"
    fi
    { grep "【警告】" "$STATE/last_deploy.log" | tee -a "$LOG"; } 2>/dev/null || true
    return 0
  fi
  log "【部署失敗】$short（結束碼 $rc，完整輸出：$STATE/last_deploy.log）："
  { tail -n 8 "$STATE/last_deploy.log" | tee -a "$LOG"; } 2>/dev/null || true
  rollback "$prev" "部署 $short 失敗" "$old_api" "$old_conv"
  return 1
}

main() {
  if [ "$(id -u)" = 0 ]; then echo "請用一般使用者執行（不要加 sudo）"; return 1; fi
  mkdir -p "$STATE"
  case "${1:-run}" in
    status) status; return 0 ;;
    pause) echo "手動暫停｜$(date '+%F %T')" >"$PAUSED"; log "已手動暫停自動部署"; return 0 ;;
    resume) rm -f "$PAUSED" "$FAILED" "$INFRA"; log "已解除暫停"; return 0 ;;
    run) ;;
    *) echo "用法：$0 [run|status|pause|resume]"; return 2 ;;
  esac
  exec 9>"$STATE/lock"
  if ! flock -n 9; then
    [ -t 1 ] && echo "另一個部署正在進行，這次略過"
    return 0
  fi
  if [ -f "$INPROG" ]; then                          # 上一輪部署在健康檢查通過前被中斷（逾時、重開機、手動停止）
    rollback_from_inprog "上一輪部署中途被中斷"
    return 1
  fi
  [ -f "$PAUSED" ] && return 0
  if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 4000 ]; then   # 紀錄只留最後 3000 行
    tail -n 3000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
  deploy_new
}

# 整個流程包在函式裡、同一行結束：部署會更新本檔，bash 不會讀到改到一半的內容
main "$@"; exit $?
