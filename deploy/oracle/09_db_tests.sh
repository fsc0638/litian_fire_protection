#!/usr/bin/env bash
# 在主機上跑全部測試（含資料庫整合測試）：
# - 資料庫：同一個 PostgreSQL 裡獨立的 litian_test（測試會清空其中的資料表，不碰正式資料庫 litian）
# - 程式：/opt/litian/repo（唯讀掛入），以正式映像＋pytest 的拋棄式容器執行；Linux 才有的行為（記憶體上限等）也一併驗到
# 用法：bash /opt/litian/repo/deploy/oracle/09_db_tests.sh
set -euo pipefail
RUN=/opt/litian
REPO=$RUN/repo
say() { echo "[db-tests] $*"; }

if ! sudo docker exec litian-postgres psql -U litian -d litian -tAc "SELECT 1 FROM pg_database WHERE datname = 'litian_test'" | grep -q 1; then
  sudo docker exec litian-postgres createdb -U litian litian_test
  say "已建立測試資料庫 litian_test"
fi

say "建置測試映像（正式映像＋pytest）"
printf 'FROM litian-api:0.1\nUSER 0\nRUN pip install --no-cache-dir -q pytest\nUSER 65534:65534\n' \
  | sudo docker build -q -t litian-api-test - >/dev/null

# 資料庫密碼用權限 600 的暫存檔傳入，不出現在指令列
ENVF=$(mktemp)
chmod 600 "$ENVF"
trap 'rm -f "$ENVF"' EXIT
PW=$(grep -E '^POSTGRES_PASSWORD=' "$RUN/.env" | cut -d= -f2-)
printf 'TEST_DATABASE_URL=postgresql://litian:%s@postgres:5432/litian_test\n' "$PW" > "$ENVF"

sudo docker run --rm --network litian_internal --env-file "$ENVF" \
  -e PYTHONPATH=/repo/src -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$REPO:/repo:ro" -w /repo litian-api-test \
  python -m pytest -q -p no:cacheprovider tests "$@"
