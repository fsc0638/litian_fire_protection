#!/usr/bin/env bash
# 一次性準備：安裝 Docker（若沒有）、開本機防火牆 80/443、建立 /opt/litian。
# 可重複執行（已做過的步驟會跳過）。不會停止或修改任何既有的容器與服務。
set -euo pipefail

say() { echo "[bootstrap] $*"; }
. /etc/os-release

# 主機層 Caddy 模式（LITIAN_HOST_CADDY=1）：80/443 由主機上的 Caddy 負責，
# 跳過連接埠安全閘與防火牆設定，只確認 Docker 與建目錄。
HOSTCADDY="${LITIAN_HOST_CADDY:-0}"
[ "$HOSTCADDY" = "1" ] && say "主機層 Caddy 模式：不檢查 80/443、不動防火牆"

# 0. 安全閘：80/443 若已被其他服務佔用就停下，交給使用者決定
[ "$HOSTCADDY" = "1" ] || for p in 80 443; do
  if ss -tln | awk '{print $4}' | grep -qE "[:.]$p\$"; then
    if ! sudo docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^litian-caddy'; then
      say "連接埠 $p 已被其他服務使用，停止。請決定：讓本系統改走既有反向代理，或改用其他連接埠。"
      exit 2
    fi
  fi
done

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  case "$ID" in
    ubuntu|debian)
      say "安裝 Docker（發行版套件 docker.io + docker-compose-v2）"
      sudo apt-get update -y
      sudo apt-get install -y docker.io docker-compose-v2 || {
        say "發行版沒有 docker-compose-v2，改裝 Docker 官方 apt 套件庫"
        sudo apt-get install -y ca-certificates curl
        sudo install -m 0755 -d /etc/apt/keyrings
        sudo curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${VERSION_CODENAME} stable" \
          | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
        sudo apt-get update -y
        sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
      }
      ;;
    ol|rhel|centos|rocky|almalinux)
      say "Oracle Linux 系列：安裝 Docker 官方 dnf 套件庫"
      sudo dnf install -y dnf-plugins-core
      sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
      sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
      ;;
    *) say "未支援的作業系統：$ID，停止"; exit 3 ;;
  esac
  sudo systemctl enable --now docker
else
  say "Docker 已安裝：$(docker --version)"
fi
sudo docker compose version >/dev/null || { say "缺 docker compose 外掛"; exit 4; }

# 2. 本機防火牆開 80/443（Oracle 的 Ubuntu 映像預設用 iptables 擋掉 22 以外的連線）
if [ "$HOSTCADDY" = "1" ]; then
  say "主機層 Caddy 模式：略過防火牆"
elif command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-service=http --add-service=https
  sudo firewall-cmd --reload
  say "firewalld 已開 http/https"
elif sudo iptables -S INPUT >/dev/null 2>&1; then
  for p in 80 443; do
    if ! sudo iptables -C INPUT -p tcp -m state --state NEW --dport "$p" -j ACCEPT 2>/dev/null; then
      # 插在第一條 REJECT 之前；沒有 REJECT 就放最後
      pos=$(sudo iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
      if [ -n "${pos:-}" ]; then
        sudo iptables -I INPUT "$pos" -p tcp -m state --state NEW --dport "$p" -j ACCEPT
      else
        sudo iptables -A INPUT -p tcp -m state --state NEW --dport "$p" -j ACCEPT
      fi
      say "iptables 已開 $p"
    fi
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save
  else
    say "注意：沒有 netfilter-persistent，iptables 規則重開機後會消失"
  fi
fi

# 3. 目錄
sudo mkdir -p /opt/litian/{data,samples,out,libredwg}
sudo chown -R "$(id -u):$(id -g)" /opt/litian
say "完成。下一步：上傳 compose 檔後執行 02_up.sh"
