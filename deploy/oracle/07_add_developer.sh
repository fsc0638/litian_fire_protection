#!/usr/bin/env bash
# 新增協作開發者的主機帳號：只能用金鑰登入、沒有 sudo、不在 docker 群組，
# 並限制該帳號所有程序合計的 CPU 與記憶體，避免影響主機上的其他服務。
#
# 用法（在主機上，以有 sudo 的帳號執行）：
#   sudo bash 07_add_developer.sh <帳號> <公鑰檔.pub>   新增帳號；帳號已存在時只加這把公鑰
#   sudo bash 07_add_developer.sh --disable <帳號>      停用：移走公鑰、讓帳號到期、結束連線（保留家目錄）
# 環境變數：
#   DRY_RUN=1      只檢查並列出會做的事，不改主機
#   DEV_MEM=2G     該帳號所有程序合計的記憶體上限
#   DEV_CPU=100%   該帳號所有程序合計的 CPU 上限（100% 等於 1 核）
set -euo pipefail
say() { echo "[add-dev] $*"; }
die() { echo "[add-dev] 錯誤：$*" >&2; exit 1; }
DRY="${DRY_RUN:-0}"
MEM="${DEV_MEM:-2G}"
CPU="${DEV_CPU:-100%}"
run() { if [ "$DRY" = 1 ]; then say "（預演）$*"; else "$@"; fi; }

[ "$(id -u)" = 0 ] || die "請用 sudo 執行"

valid_user() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "帳號只能用小寫英文、數字、連字號，2 到 31 字，英文開頭"
  case "$1" in root|ubuntu|opc|admin|caddy|docker|litian) die "$1 是保留名稱";; esac
}
is_admin() { id -nG "$1" | tr ' ' '\n' | grep -qxE 'sudo|admin|wheel|docker'; }

# ---- 停用 ----
if [ "${1:-}" = "--disable" ]; then
  U="${2:-}"; valid_user "$U"
  id "$U" >/dev/null 2>&1 || die "沒有帳號 $U"
  is_admin "$U" && die "$U 有管理權限，不是本腳本建立的協作帳號，請手動處理"
  H=$(getent passwd "$U" | cut -d: -f6)
  TS=$(date +%Y%m%d-%H%M%S)
  [ -f "$H/.ssh/authorized_keys" ] && run mv "$H/.ssh/authorized_keys" "$H/.ssh/authorized_keys.disabled-$TS"
  run usermod --expiredate 1 "$U"
  run pkill -KILL -u "$U" || true
  say "已停用 $U，家目錄保留。要恢復：usermod --expiredate '' $U，再把公鑰檔改回 authorized_keys"
  exit 0
fi

# ---- 新增 ----
U="${1:-}"; KEY="${2:-}"
[ -n "$U" ] && [ -n "$KEY" ] || die "用法：sudo bash $0 <帳號> <公鑰檔.pub>"
valid_user "$U"
[ -f "$KEY" ] || die "找不到公鑰檔 $KEY"
grep -q 'PRIVATE KEY' "$KEY" && die "這是私鑰。只收 .pub 公鑰檔；私鑰要留在同仁自己的電腦"
[ "$(grep -cv '^[[:space:]]*$' "$KEY")" = 1 ] || die "公鑰檔應該只有一行"
KEYLINE=$(grep -v '^[[:space:]]*$' "$KEY" | tr -d '\r')
[[ "$KEYLINE" =~ ^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa)\  ]] || die "公鑰格式不對，開頭應是 ssh-ed25519"
T=$(mktemp); echo "$KEYLINE" > "$T"
FP=$(ssh-keygen -lf "$T" 2>/dev/null) || { rm -f "$T"; die "不是有效的 SSH 公鑰"; }
rm -f "$T"
BITS=$(echo "$FP" | awk '{print $1}'); TYPE=$(echo "$FP" | awk '{print $NF}')
case "$TYPE" in
  "(ED25519)"|"(ECDSA)") ;;
  "(RSA)") [ "$BITS" -ge 3072 ] || die "RSA 金鑰至少要 3072 位元，建議改用 ed25519" ;;
  *) die "不支援的金鑰類型 $TYPE，請用 ed25519" ;;
esac
say "公鑰指紋：$FP"

if id "$U" >/dev/null 2>&1; then
  [ "$(id -u "$U")" -ge 1000 ] || die "$U 是系統帳號"
  is_admin "$U" && die "$U 有管理權限，不是本腳本建立的協作帳號，停止"
  say "帳號 $U 已存在，只加公鑰"
else
  run adduser --disabled-password --gecos "" "$U"
fi

H="/home/$U"; id "$U" >/dev/null 2>&1 && H=$(getent passwd "$U" | cut -d: -f6)
AK="$H/.ssh/authorized_keys"
run install -d -m 700 -o "$U" -g "$U" "$H/.ssh"
if [ -f "$AK" ] && grep -qxF "$KEYLINE" "$AK"; then
  say "這把公鑰已經在 authorized_keys"
elif [ "$DRY" = 1 ]; then
  say "（預演）把公鑰加入 $AK"
else
  echo "$KEYLINE" >> "$AK"
fi
run chown "$U:$U" "$AK"
run chmod 600 "$AK"
run chmod 750 "$H"

# 資源上限：寫在該帳號的 systemd user slice
UIDN="<新帳號的 uid>"; id "$U" >/dev/null 2>&1 && UIDN=$(id -u "$U")
DROP="/etc/systemd/system/user-$UIDN.slice.d/50-developer-limits.conf"
if [ "$DRY" = 1 ]; then
  say "（預演）寫入 $DROP：MemoryMax=$MEM、CPUQuota=$CPU"
else
  install -d -m 755 "$(dirname "$DROP")"
  printf '[Slice]\nMemoryMax=%s\nCPUQuota=%s\n' "$MEM" "$CPU" > "$DROP"
  if [ -d /run/systemd/system ]; then systemctl daemon-reload; else say "沒有 systemd，略過重新載入"; fi
fi

# 開發用前置：Python 虛擬環境套件（只新增，不升級既有套件、不自動重啟服務）
if command -v python3 >/dev/null 2>&1; then
  VENV_PKG=$(python3 -c 'import sys; print(f"python3.{sys.version_info.minor}-venv")')
  if dpkg -s "$VENV_PKG" >/dev/null 2>&1; then
    say "$VENV_PKG 已安裝"
  elif [ "$DRY" = 1 ]; then
    say "（預演）安裝 $VENV_PKG"
  else
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y --no-upgrade "$VENV_PKG" >/tmp/add-dev-apt.log 2>&1 \
      || { tail -5 /tmp/add-dev-apt.log; die "安裝 $VENV_PKG 失敗"; }
    say "已安裝 $VENV_PKG"
  fi
fi

[ "$DRY" = 1 ] && { say "預演結束，沒有改動主機"; exit 0; }
is_admin "$U" && die "檢查失敗：$U 竟然有管理權限"
say "完成：$U 只能用金鑰登入，沒有 sudo，不在 docker 群組；CPU 上限 $CPU，記憶體上限 $MEM"
say "群組：$(id -nG "$U")"
for k in /etc/ssh/ssh_host_ed25519_key.pub; do [ -f "$k" ] && say "主機指紋（給同仁第一次連線時比對）：$(ssh-keygen -lf "$k" | awk '{print $2}')"; done
exit 0
