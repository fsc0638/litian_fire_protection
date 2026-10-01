#!/usr/bin/env bash
# 啟動第 0 期基礎服務並做健康檢查。可重複執行。
set -euo pipefail
cd /opt/litian
say() { echo "[up] $*"; }

# 第一次：從範本建 .env，自動產生內部密碼（不印出）
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
  sed -i "s|^MEILI_MASTER_KEY=.*|MEILI_MASTER_KEY=$(openssl rand -hex 24)|" .env
  chmod 600 .env
  say "已建立 .env 並產生內部密碼"
fi
if [ -n "${SITE_ADDRESS_OVERRIDE:-}" ]; then
  sed -i "s|^SITE_ADDRESS=.*|SITE_ADDRESS=${SITE_ADDRESS_OVERRIDE}|" .env
fi

mkdir -p data/{postgres,meili,redis,caddy,caddy_config}
PROFILE=""
[ "${LITIAN_STANDALONE:-0}" = "1" ] && PROFILE="--profile standalone"
sudo docker compose $PROFILE pull
sudo docker compose $PROFILE up -d

say "等待 PostgreSQL 健康檢查"
for i in $(seq 1 30); do
  st=$(sudo docker inspect -f '{{.State.Health.Status}}' litian-postgres 2>/dev/null || echo none)
  [ "$st" = "healthy" ] && break; sleep 3
done
say "PostgreSQL：$st"
sudo docker exec litian-postgres psql -U litian -d litian -tAc "CREATE EXTENSION IF NOT EXISTS vector; SELECT 'pgvector ' || extversion FROM pg_extension WHERE extname='vector';" || say "pgvector 檢查失敗"
sudo docker exec litian-meilisearch sh -c 'wget -qO- http://127.0.0.1:7700/health || curl -s http://127.0.0.1:7700/health' || say "Meilisearch 檢查失敗"
echo
sudo docker exec litian-redis redis-cli ping || say "Redis 檢查失敗"
sleep 5
SITE=$(grep -E '^SITE_ADDRESS=' .env | cut -d= -f2-)
if [ -n "$SITE" ] && [ "${SITE#:}" = "$SITE" ]; then
  say "本機 HTTPS 檢查（$SITE）：$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$SITE:443:127.0.0.1" "https://$SITE/healthz" || echo 失敗)"
else
  say "本機 HTTP 檢查：$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/healthz || echo 失敗)"
fi
echo
sudo docker compose ps --format 'table {{.Name}}\t{{.Image}}\t{{.Status}}'
echo
say "記憶體用量："
sudo docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
free -h | sed -n '1,2p'
