#!/usr/bin/env bash
# 唯讀盤點：只看不改。輸出主機規格、既有服務、佔用的連接埠、防火牆規則。
# 用途：架設前確認這台主機上有沒有其他服務在跑，避免衝突。
set -u
echo "===== 系統 ====="
. /etc/os-release 2>/dev/null && echo "OS: $PRETTY_NAME"
echo "架構: $(uname -m)   核心: $(uname -r)"
echo "CPU 核數: $(nproc)"
free -h | sed -n '1,2p'
df -h / | tail -1 | awk '{print "根目錄磁碟: 總 "$2"，已用 "$3"，可用 "$4}'
echo
echo "===== Docker ====="
if command -v docker >/dev/null 2>&1; then
  docker --version
  docker compose version 2>/dev/null || echo "docker compose 外掛：未安裝"
  echo "--- 執行中的容器 ---"
  sudo -n docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null || docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null || echo "(無權限查看容器)"
else
  echo "Docker：未安裝"
fi
echo
echo "===== 正在聽的連接埠 ====="
sudo -n ss -tlnp 2>/dev/null | awk 'NR==1 || /LISTEN/' || ss -tln
echo
echo "===== 80／443 是否被佔用 ====="
for p in 80 443; do
  if ss -tln | awk '{print $4}' | grep -qE "[:.]$p\$"; then echo "連接埠 $p：已被佔用"; else echo "連接埠 $p：空閒"; fi
done
echo
echo "===== 執行中的系統服務（排除系統內建常見項） ====="
systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -vE '^(systemd-|dbus|cron|ssh|rsyslog|snapd|polkit|udisks|multipathd|irqbalance|unattended|getty|serial-getty|chrony|ModemManager|networkd-dispatcher|oracle-cloud-agent|qemu-guest|iscsid|user@|containerd|docker|packagekit|accounts-daemon|atd|fwupd|thermald|upower|wpa_supplicant|NetworkManager|tuned|firewalld|auditd|sssd|rngd|kdump|gssproxy|nfs|rpcbind|php|apparmor|ufw|netfilter|setvl|google|amazon|walinuxagent|cloud-|haveged)' || true
echo
echo "===== 防火牆（INPUT 鏈） ====="
sudo -n iptables -S INPUT 2>/dev/null || echo "(無 sudo 權限或未使用 iptables)"
command -v firewall-cmd >/dev/null 2>&1 && sudo -n firewall-cmd --list-all 2>/dev/null
echo
echo "===== /opt/litian 是否已存在 ====="
ls -la /opt/litian 2>/dev/null || echo "不存在（全新架設）"
