#!/usr/bin/env bash
# 在主機上跑全部測試（含資料庫整合測試）：
# - 資料庫：同一個 PostgreSQL 裡獨立的 litian_test，以專用帳號 litian_tester 連線（只擁有 litian_test，
#   碰不到正式資料庫 litian 的資料表）；測試會清空 litian_test 的資料表
# - 程式：/opt/litian/repo（唯讀掛入），以正式映像＋pytest 的拋棄式容器、無權限身分執行，限 CPU 與記憶體；
#   Linux 才有的行為（記憶體上限等）也一併驗到
# 用法：bash /opt/litian/repo/deploy/oracle/09_db_tests.sh（不要加 sudo）
# 自動部署會設 LITIAN_TEST_REPO（還沒上線的新版程式目錄）、LITIAN_TEST_BASE（依賴有變時先建的候選映像）
# 最後一行印「pytest-exit=結束碼」：1＝測試沒過；其他非 0＝測試環境出錯
set -euo pipefail
RUN=/opt/litian
REPO=${LITIAN_TEST_REPO:-$RUN/repo}
BASE=${LITIAN_TEST_BASE:-litian-api:0.1}
say() { echo "[db-tests] $*"; }

if [ "$(id -u)" = 0 ]; then
  say "請用一般使用者執行（不要加 sudo）：以 root 執行會讓密碼檔與鎖檔變成 root 擁有，自動部署之後會失敗"
  exit 1
fi
# 與部署共用一把鎖：手動測試和自動部署的測試不會同時清同一個測試資料庫
if [ -z "${LITIAN_DEPLOY_LOCKED:-}" ]; then
  mkdir -p "$RUN/autodeploy"
  exec 9>"$RUN/autodeploy/lock"
  flock -n 9 || { say "部署或另一次測試正在進行，請稍後再試"; exit 1; }
fi

# 測試專用帳號：密碼存在權限 600 的檔案，經標準輸入交給 psql（不出現在指令列）
PWF=$RUN/autodeploy/test_db_password
mkdir -p "$RUN/autodeploy"
if [ ! -s "$PWF" ]; then
  (umask 077; openssl rand -hex 24 >"$PWF")
fi
TPW=$(cat "$PWF")
# 正式資料庫 litian 不開放一般帳號連線（預設 PUBLIC 可連；本系統只用擁有者帳號 litian 連線，不受影響）
printf "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'litian_tester') THEN CREATE ROLE litian_tester LOGIN; END IF; END \$\$;\nALTER ROLE litian_tester PASSWORD '%s';\nREVOKE CONNECT ON DATABASE litian FROM PUBLIC;\n" "$TPW" \
  | sudo docker exec -i litian-postgres psql -q -U litian -d litian -v ON_ERROR_STOP=1 >/dev/null
owner=$(sudo docker exec litian-postgres psql -U litian -d litian -tAc \
  "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'litian_test'")
if [ "$owner" != "litian_tester" ]; then
  [ -n "$owner" ] && sudo docker exec litian-postgres dropdb -U litian litian_test
  sudo docker exec litian-postgres createdb -U litian -O litian_tester litian_test
  say "已建立測試資料庫 litian_test（擁有者 litian_tester）"
fi

say "建置測試映像（$BASE＋pytest）"
printf 'FROM %s\nUSER 0\nRUN pip install --no-cache-dir -q pytest\nUSER 65534:65534\n' "$BASE" \
  | sudo docker build -q -t litian-api-test - >/dev/null

ENVF=$(mktemp)
chmod 600 "$ENVF"
# 結束時刪掉暫存檔與測試映像（測試映像會留住舊版 api 映像的層，不刪會一直累積磁碟）
trap 'rm -f "$ENVF"; sudo docker rmi litian-api-test >/dev/null 2>&1 || true' EXIT
printf 'TEST_DATABASE_URL=postgresql://litian_tester:%s@postgres:5432/litian_test\n' "$TPW" >"$ENVF"

set +e
sudo docker run --rm --network litian_internal --env-file "$ENVF" --cpus 1 --memory 2g --pids-limit 512 \
  -e PYTHONPATH=/repo/src -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$REPO:/repo:ro" -w /repo litian-api-test \
  python -m pytest -q -p no:cacheprovider tests "$@"
rc=$?
set -e
say "pytest-exit=$rc"
exit "$rc"
