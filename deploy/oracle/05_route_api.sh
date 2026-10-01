#!/usr/bin/env bash
# 把主機層 Caddy 裡本系統的網站區塊換成新版（含 /api/* 轉送到 127.0.0.1:8100）。
# 只動本系統自己的區塊：舊版（04 新增、無結尾標記）或新版（BEGIN/END 標記之間）。
# 先備份 → 換區塊 → 驗證通過才平滑重載 → 比對既有網址前後狀態 → 失敗就還原。
set -euo pipefail
ENV_FILE="${ENV_FILE:-/opt/litian/.env}"
envval() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }
SITE="${LITIAN_SITE:-$(envval SITE_ADDRESS)}"
CHECK="${CHECK_SITE:-$(envval CHECK_SITE)}"   # 選填：同一個 Caddy 上要確認不受影響的既有網址
[ -n "$SITE" ] || { echo "需要 SITE_ADDRESS（在 $ENV_FILE 或環境變數）"; exit 9; }
CF=/etc/caddy/Caddyfile
BAK="$CF.bak-$(date +%Y%m%d-%H%M%S)-before-litian-api"
say() { echo "[route-api] $*"; }
code() { [ -n "$1" ] || { echo "未設"; return; }
         curl -s -o /dev/null -m 15 -w '%{http_code}' --resolve "$1:443:127.0.0.1" "https://$1$2" || echo 000; }

before=$(code "$CHECK" /)
say "變更前 既有網址 ${CHECK:-（未設）} → $before"
sudo cp -p "$CF" "$BAK"; say "已備份：$BAK"

# 刪除既有的本系統區塊（新版：BEGIN..END；舊版：本系統註解行 → 該站台區塊結束的「}」），並去掉檔尾空行
strip_litian() {
  awk '
    /^# ---- BEGIN litian ----/ {skip=1; next}
    /^# ---- END litian ----/   {skip=0; next}
    /^# ---- 消防圖審系統/    {old=1; next}
    old && /^}$/                 {old=0; next}
    skip || old                  {next}
    {print}
  ' "$1" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'
}
NEW="${NEW_FILE:-$CF.new}"
sudo cat "$BAK" > /tmp/litian-caddy-orig
strip_litian /tmp/litian-caddy-orig | sudo tee "$NEW" >/dev/null
sudo tee -a "$NEW" >/dev/null <<BLOCK

# ---- BEGIN litian ----
# 消防圖審系統（由 deploy/oracle/05_route_api.sh 管理，只改這兩行標記之間）
$SITE {
	encode zstd gzip
	handle /healthz {
		respond "litian ok" 200
	}
	handle /api/* {
		reverse_proxy 127.0.0.1:8100
	}
	handle {
		header Content-Type "text/plain; charset=utf-8"
		respond "消防圖審系統：建置中。法規檢索 API：/api/law/search?q=…" 200
	}
}
# ---- END litian ----
BLOCK

# 防護：去掉本系統區塊後，新舊設定必須逐字相同（確保既有設定一個字都沒動）
sudo cat "$NEW" > /tmp/litian-caddy-new
if ! diff -q <(strip_litian /tmp/litian-caddy-orig) <(strip_litian /tmp/litian-caddy-new) >/dev/null; then
  say "既有設定部分有差異，停止"; diff <(strip_litian /tmp/litian-caddy-orig) <(strip_litian /tmp/litian-caddy-new) | head; exit 3
fi
say "既有設定部分逐字相同"
if ! sudo caddy validate --config "$NEW" --adapter caddyfile >/tmp/litian-caddy-validate.log 2>&1; then
  say "驗證失敗，不套用"; tail -5 /tmp/litian-caddy-validate.log; exit 1
fi
say "設定驗證通過"
if [ "${DRY_RUN:-0}" = "1" ]; then
  say "預演模式：不套用。與現行設定的差異如下"; diff /tmp/litian-caddy-orig /tmp/litian-caddy-new || true
  sudo rm -f "$BAK" "$NEW"; exit 0
fi
sudo cp "$NEW" "$CF" && sudo rm -f "$NEW"
sudo systemctl reload caddy
sleep 2
after=$(code "$CHECK" /)
api=$(code "$SITE" /api/health)
say "變更後 既有網址 → $after（變更前 $before）；本系統 /api/health → $api"
if [ "$after" != "$before" ]; then
  say "既有網址狀態改變，立即還原"; sudo cp -p "$BAK" "$CF"; sudo systemctl reload caddy; exit 2
fi
say "完成"
