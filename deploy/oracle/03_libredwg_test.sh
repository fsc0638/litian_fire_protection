#!/usr/bin/env bash
# 在主機上建 LibreDWG 容器（從原始碼編譯、驗 SHA-256），並在沙箱條件下跑 19 個樣本。
# 前置：樣本 DWG 已上傳到 /opt/litian/samples；本資料夾內容已上傳到 /opt/litian/libredwg（含 repair_dxf.py）。
set -euo pipefail
cd /opt/litian/libredwg
echo "[libredwg] 建置映像（ARM 上編譯約需數分鐘）"
sudo docker build -t litian-libredwg:0.14 .
rm -rf /opt/litian/out/* && chmod 777 /opt/litian/out
echo "[libredwg] 沙箱執行：無網路、512 MB、1 核、唯讀根目錄"
sudo docker run --rm \
  --network none --memory 512m --cpus 1 --pids-limit 64 \
  --read-only --tmpfs /tmp:size=64m --security-opt no-new-privileges --cap-drop ALL \
  -v /opt/litian/samples:/in:ro -v /opt/litian/out:/out \
  litian-libredwg:0.14 /in /out | tail -3
echo "[libredwg] 報告：/opt/litian/out/report.md"
