#!/usr/bin/env bash
# 安裝（或更新）自動部署：systemd 計時器每 2 分鐘執行 10_autodeploy.sh（推上 GitHub main 就測試並部署）。
#   安裝／更新設定：bash /opt/litian/repo/deploy/oracle/11_install_autodeploy.sh
#   移除：bash /opt/litian/repo/deploy/oracle/11_install_autodeploy.sh --uninstall
# 只加本系統自己的兩個 systemd 單元（litian-autodeploy.service／.timer），不動其他服務。
# 啟用前請確認：GitHub main 有分支保護（只能經負責人核准的 PR 合併）——能推上 main 就等於能在主機執行程式。
set -euo pipefail
RUN=/opt/litian
USER_NAME=$(stat -c %U "$RUN/repo")       # 以 repo 擁有者身分執行（git 操作不會改到檔案擁有者）
say() { echo "[autodeploy] $*"; }

if [ "${1:-}" = "--uninstall" ]; then
  sudo systemctl disable --now litian-autodeploy.timer 2>/dev/null || true
  sudo rm -f /etc/systemd/system/litian-autodeploy.service /etc/systemd/system/litian-autodeploy.timer
  sudo systemctl daemon-reload
  say "已移除自動部署（之後請手動執行 06_update_from_git.sh 部署）"
  exit 0
fi

[ "$USER_NAME" != root ] || { say "$RUN/repo 是 root 擁有：請改為一般使用者擁有（自動部署不以 root 執行）"; exit 1; }
sudo -u "$USER_NAME" sh -c 'sudo -k; sudo -n true' || { say "$USER_NAME 不能免密碼執行 sudo，自動部署無法運作"; exit 1; }
sudo install -d -o "$USER_NAME" -g "$USER_NAME" -m 755 "$RUN/autodeploy"
sudo chown -R "$USER_NAME:$USER_NAME" "$RUN/autodeploy"     # 曾用 sudo 跑過時，狀態檔可能變成 root 擁有
sudo tee /etc/systemd/system/litian-autodeploy.service >/dev/null <<EOF
[Unit]
Description=litian 自動部署：GitHub main 有新提交時先測試、通過才部署
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=$USER_NAME
UMask=0022
ExecStart=/bin/bash $RUN/repo/deploy/oracle/10_autodeploy.sh run
# 最壞情況：候選映像建置＋測試＋正式建置（轉檔容器沒有快取時要重新編譯）
TimeoutStartSec=90min
# 被停止時讓腳本有時間換回前一版
TimeoutStopSec=5min
EOF
sudo tee /etc/systemd/system/litian-autodeploy.timer >/dev/null <<EOF
[Unit]
Description=每 2 分鐘檢查 litian 是否有新版本

[Timer]
OnBootSec=3min
OnUnitInactiveSec=2min
RandomizedDelaySec=15s

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable litian-autodeploy.timer
sudo systemctl restart litian-autodeploy.timer        # 重新安裝時套用新設定
say "已啟用：每 2 分鐘檢查一次 GitHub main。看狀態：bash $RUN/repo/deploy/oracle/10_autodeploy.sh status"
systemctl list-timers litian-autodeploy.timer --no-pager | sed -n 1,2p
