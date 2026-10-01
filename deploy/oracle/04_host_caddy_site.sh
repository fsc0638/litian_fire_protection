#!/usr/bin/env bash
# 在主機層 Caddy 加上本系統的網站區塊。
# 原則：先備份 → 只新增一個區塊（不改既有內容）→ 驗證通過才平滑重載 → 重載後檢查既有網址與本系統 → 任何一步失敗就還原。
set -euo pipefail
ENV_FILE="${ENV_FILE:-/opt/litian/.env}"
envval() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }
SITE="${LITIAN_SITE:-$(envval SITE_ADDRESS)}"
CHECK="${CHECK_SITE:-$(envval CHECK_SITE)}"   # 選填：同一個 Caddy 上要確認不受影響的既有網址
[ -n "$SITE" ] || { echo "需要 SITE_ADDRESS（在 $ENV_FILE 或環境變數）"; exit 9; }
CF=/etc/caddy/Caddyfile
TS=$(date +%Y%m%d-%H%M%S)
BAK="$CF.bak-$TS-before-litian"
say() { echo "[host-caddy] $*"; }
code() { [ -n "$1" ] || { echo "未設"; return; }
         curl -s -o /dev/null -m 15 -w '%{http_code}' --resolve "$1:443:127.0.0.1" "https://$1$2" || echo 000; }

before=$(code "$CHECK" /)
say "變更前 既有網址 ${CHECK:-（未設）} → $before"

if sudo grep -q "^$SITE {" "$CF"; then
  say "已有 $SITE 區塊，不重複新增"
else
  sudo cp -p "$CF" "$BAK"
  say "已備份：$BAK"
  sudo tee -a "$CF" >/dev/null <<EOF

# ---- 消防圖審系統（$TS 由 deploy/oracle/04_host_caddy_site.sh 新增）----
$SITE {
	encode zstd gzip
	handle /healthz {
		respond "litian ok" 200
	}
	# 第 0 期程式完成後，改成：reverse_proxy 127.0.0.1:<API 埠>
	handle {
		header Content-Type "text/plain; charset=utf-8"
		respond "消防圖審系統：建置中" 200
	}
}
EOF
  if ! sudo caddy validate --config "$CF" --adapter caddyfile >/tmp/litian-caddy-validate.log 2>&1; then
    say "驗證失敗，還原備份，不重載"; tail -5 /tmp/litian-caddy-validate.log
    sudo cp -p "$BAK" "$CF"; exit 1
  fi
  say "設定驗證通過，平滑重載（不中斷既有連線）"
  sudo systemctl reload caddy
fi

# 等 Caddy 申請本系統網址的 HTTPS 憑證
for i in $(seq 1 20); do
  l=$(code "$SITE" /healthz); [ "$l" = "200" ] && break; sleep 3
done
after=$(code "$CHECK" /)
say "變更後 既有網址 → $after（變更前 $before）"
say "本系統 https://$SITE/healthz → $l"

if [ "$after" != "$before" ]; then
  say "既有網址狀態改變，立即還原"
  [ -f "$BAK" ] && sudo cp -p "$BAK" "$CF" && sudo systemctl reload caddy
  exit 2
fi
[ "$l" = "200" ] || { say "本系統網址尚未就緒（憑證可能仍在申請）"; exit 3; }
say "完成"
