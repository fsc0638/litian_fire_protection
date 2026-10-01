#!/usr/bin/env bash
# 匯出法規問答的提問紀錄（測試期用）成 CSV（UTF-8 含 BOM，Excel 可直接開）。
# 用法（主機上）：bash 08_export_ask_log.sh [起始日 YYYY-MM-DD]
#   預設匯出全部；輸出到 /opt/litian/exports/ask_log_<時間>.csv
# 欄位：時間（台北）、來源代號（IP 雜湊）、模式、問題、AI 回答、引用、無效引用、未經檢索條號、結束原因、錯誤、
#       輸入／輸出 token、耗時（毫秒）、檢索結果（編號:條文）
set -euo pipefail
SINCE="${1:-1970-01-01}"
[[ "$SINCE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "起始日格式應為 YYYY-MM-DD"; exit 2; }
OUT_DIR=/opt/litian/exports
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/ask_log_$(date +%Y%m%d-%H%M%S).csv"
SQL="COPY (
  SELECT to_char(at AT TIME ZONE 'Asia/Taipei', 'YYYY-MM-DD HH24:MI:SS') AS 時間,
         client AS 來源代號, mode AS 模式, question AS 問題, answer AS AI回答,
         array_to_string(cited, ',') AS 引用, array_to_string(invalid_cites, ',') AS 無效引用,
         array_to_string(unverified, ',') AS 未經檢索條號, stop_reason AS 結束原因, error AS 錯誤,
         input_tokens AS 輸入token, output_tokens AS 輸出token, duration_ms AS 耗時毫秒,
         (SELECT string_agg((e->>'n') || ':' || coalesce(e->>'citation', e->>'node_id'), '；' ORDER BY (e->>'n')::int)
            FROM jsonb_array_elements(sources) e) AS 檢索結果
  FROM ask_log WHERE at >= '$SINCE'::date ORDER BY at
) TO STDOUT WITH (FORMAT csv, HEADER true)"
printf '\xEF\xBB\xBF' > "$OUT"
sudo docker exec litian-postgres psql -U litian -d litian -v ON_ERROR_STOP=1 -c "$SQL" >> "$OUT"
N=$(sudo docker exec litian-postgres psql -U litian -d litian -At -c "SELECT count(*) FROM ask_log WHERE at >= '$SINCE'::date")
echo "已匯出 $N 筆：$OUT"
