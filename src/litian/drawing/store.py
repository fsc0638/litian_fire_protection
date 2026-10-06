"""案件、檔案、圖面中介資料的資料表與工作佇列（PostgreSQL，SELECT … FOR UPDATE SKIP LOCKED）。

檔案狀態：queued（排隊）→ processing（處理中）→〔reviewing（檢核中，有平面圖時）〕→ done（完成）｜failed（失敗）｜skipped（不支援的檔案類型）
CAD 原樣圖（cad_state，另一條佇列，worker 閒置時在背景畫）：pending（排隊）→ rendering（畫圖中）→ done｜failed；
NULL＝不用畫（沒有樓層圖、檢核失敗）或重新處理中。cad_gen 每次重新排隊加一：舊的畫圖結果不會蓋掉新的狀態。
"""

from __future__ import annotations

import json

from . import ir as IR

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_case (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS case_file (
  id bigserial PRIMARY KEY,
  case_id bigint NOT NULL REFERENCES review_case ON DELETE CASCADE,
  name text NOT NULL,
  kind text NOT NULL,
  size bigint NOT NULL,
  sha256 text NOT NULL,
  path text NOT NULL,
  status text NOT NULL DEFAULT 'queued',
  error text,
  stats jsonb,
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS case_file_queue ON case_file (status, id);
CREATE TABLE IF NOT EXISTS file_ir (
  file_id bigint PRIMARY KEY REFERENCES case_file ON DELETE CASCADE,
  ir jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS case_sheet (
  id bigserial PRIMARY KEY,
  file_id bigint NOT NULL REFERENCES case_file ON DELETE CASCADE,
  idx integer NOT NULL,
  number text,
  title text,
  scale text,
  unit text,
  meta jsonb NOT NULL,
  bbox double precision[],
  UNIQUE (file_id, idx)
);
ALTER TABLE case_file ADD COLUMN IF NOT EXISTS review_only boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS case_context (
  case_id bigint PRIMARY KEY REFERENCES review_case ON DELETE CASCADE,
  context jsonb NOT NULL,               -- review.checks.Context 的欄位（場所類別、天花板高度…）
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS finding_decision (
  file_id bigint NOT NULL REFERENCES case_file ON DELETE CASCADE,
  key text NOT NULL,                    -- review.engine.finding_key
  decision text NOT NULL,               -- accept（接受，列入報告）｜reject（退回，不列入）
  note text,
  decided_by text,
  decided_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (file_id, key)
);
CREATE TABLE IF NOT EXISTS file_review (
  file_id bigint PRIMARY KEY REFERENCES case_file ON DELETE CASCADE,
  status text NOT NULL,                 -- done（已檢核）｜failed（檢核失敗）
  result jsonb,                         -- review.engine.to_dict（不含缺失範圍幾何，只留外框座標）
  error text,
  svg_dir text,                         -- 各樓層標示圖 <樓層>.svg 所在資料夾
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE case_file ADD COLUMN IF NOT EXISTS cad_state text;
ALTER TABLE case_file ADD COLUMN IF NOT EXISTS cad_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE case_file ADD COLUMN IF NOT EXISTS cad_gen integer NOT NULL DEFAULT 0;
ALTER TABLE case_file ADD COLUMN IF NOT EXISTS cad_started_at timestamptz;
CREATE INDEX IF NOT EXISTS case_file_cad ON case_file (id) WHERE cad_state IN ('pending', 'rendering');
"""

# 副檔名 → 種類；其餘（.dwl/.dwl2/.bak 等 AutoCAD 暫存檔）記為 skipped
KINDS = {".dwg": "dwg", ".dxf": "dxf", ".pdf": "pdf"}
SUPPORTED = {"dwg", "dxf"}           # PDF 擷取在後續里程碑
MAX_ATTEMPTS = 3
# worker 正在處理：認領後的轉檔、綁定外部參考、抽取都記 processing，檢核記 reviewing（沒有其他中間狀態）
ACTIVE = {"processing": "處理中", "reviewing": "檢核中"}

CLAIM_SQL = """
UPDATE case_file SET status = 'processing', attempts = attempts + 1, updated_at = now()
WHERE id = (SELECT id FROM case_file WHERE status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, case_id, name, kind, path, attempts, review_only
"""
# 處理中卻超過時間沒更新（worker 當掉）：還有次數就退回排隊，否則記失敗（記失敗時原圖排隊一併取消：失敗的檔不畫）
RECOVER_SQL = """
UPDATE case_file SET status = CASE WHEN attempts < %s THEN 'queued' ELSE 'failed' END,
       error = CASE WHEN attempts < %s THEN error ELSE '處理逾時或中斷次數過多' END, updated_at = now(),
       cad_state = CASE WHEN attempts >= %s AND cad_state = 'pending' THEN NULL ELSE cad_state END
WHERE status IN ('processing', 'reviewing') AND updated_at < now() - make_interval(secs => %s)
"""


# CAD 原樣圖：新上傳的優先（id 大的先畫），舊檔補畫排後面；檔案要是完成狀態、檢核成功
# （重新處理中、只重跑檢核失敗的先不畫：等檢核成功再畫，不必記失敗）
CAD_CLAIM_SQL = """
UPDATE case_file SET cad_state = 'rendering', cad_attempts = cad_attempts + 1, cad_started_at = now()
WHERE id = (SELECT f.id FROM case_file f WHERE f.cad_state = 'pending' AND f.status = 'done'
              AND EXISTS (SELECT 1 FROM file_review r WHERE r.file_id = f.id AND r.status = 'done')
            ORDER BY f.id DESC FOR UPDATE OF f SKIP LOCKED LIMIT 1)
RETURNING id, case_id, name, kind, path, cad_gen, cad_attempts
"""
# 畫圖中卻沒有人在畫（worker 重啟、當掉）：還有次數就重新排隊，否則記失敗
CAD_RECOVER_SQL = """
UPDATE case_file SET cad_state = CASE WHEN cad_attempts < %s THEN 'pending' ELSE 'failed' END
WHERE cad_state = 'rendering' AND cad_started_at < now() - make_interval(secs => %s)
RETURNING id, path, cad_state
"""
# 檢核成功、有樓層圖卻從沒排過畫圖的（這個功能上線前的檔案）：補排隊；回傳各樓層圖名（檢查疊圖資料在不在）
CAD_BACKFILL_SQL = """
UPDATE case_file f SET cad_state = 'pending', cad_attempts = 0
FROM file_review r
WHERE r.file_id = f.id AND f.cad_state IS NULL AND f.status = 'done' AND r.status = 'done'
  AND jsonb_typeof(r.result -> 'floors') = 'array' AND jsonb_array_length(r.result -> 'floors') > 0
RETURNING f.id, f.path, r.svg_dir,
  (SELECT jsonb_agg(COALESCE(fl ->> 'svg_name', fl ->> 'label')) FROM jsonb_array_elements(r.result -> 'floors') fl) AS names
"""
CAD_MAX_ATTEMPTS = 3                 # 被中斷（worker 重啟、記憶體不足被系統砍）最多畫幾次


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA)


def queue_cad(conn, file_id: int) -> None:
    conn.execute("UPDATE case_file SET cad_state = 'pending', cad_attempts = 0, cad_gen = cad_gen + 1, "
                 "cad_started_at = NULL WHERE id = %s", (file_id,))


def queue_cad_if_missing(conn, file_id: int) -> bool:
    """只重跑檢核成功時：還沒排過（第一次檢核失敗）或上次畫失敗的才排；已畫好、排隊中、畫圖中的不動。"""
    return conn.execute("UPDATE case_file SET cad_state = 'pending', cad_attempts = 0, cad_gen = cad_gen + 1, "
                        "cad_started_at = NULL WHERE id = %s AND (cad_state IS NULL OR cad_state = 'failed')",
                        (file_id,)).rowcount == 1


def requeue_cad(conn, file_id: int, gen: int) -> bool:
    """畫到一半被我們自己停掉（重新部署、暫時性錯誤）：退回排隊，這次不算次數。"""
    return conn.execute("UPDATE case_file SET cad_state = 'pending', cad_attempts = GREATEST(cad_attempts - 1, 0), "
                        "cad_started_at = NULL WHERE id = %s AND cad_gen = %s AND cad_state = 'rendering'",
                        (file_id, gen)).rowcount == 1


def rendering_cad(conn) -> list[dict]:
    return conn.execute("SELECT id, path, cad_gen, cad_started_at FROM case_file WHERE cad_state = 'rendering'").fetchall()


def requeue_job(conn, file_id: int) -> bool:
    """處理到一半被我們自己停掉（重新部署）：退回排隊，這次不算次數。"""
    return conn.execute("UPDATE case_file SET status = 'queued', attempts = GREATEST(attempts - 1, 0), updated_at = now() "
                        "WHERE id = %s AND status IN ('processing', 'reviewing')", (file_id,)).rowcount == 1


def requeue_review(conn, file_id: int) -> bool:
    """只重跑檢核（例：舊版檢核沒有產生疊圖資料）。"""
    return conn.execute("UPDATE case_file SET status = 'queued', review_only = true, attempts = 0, updated_at = now() "
                        "WHERE id = %s AND status = 'done'", (file_id,)).rowcount == 1


def lock_for_reprocess(conn, case_ids: list[int]) -> list[dict]:
    """（在交易裡呼叫）鎖住這些案件裡可以整個重新處理的檔：DWG／DXF、不在處理中。鎖到交易結束：worker 認領與背景畫圖
    （SKIP LOCKED）都會跳過；剛被認領還沒提交的，等它提交後重新判斷（變成處理中就不回傳，但 PostgreSQL 照樣鎖住
    它的新版本到交易結束：cli.reprocess 隨即查到它處理中、整個放掉）。
    FOR NO KEY UPDATE：不擋工作台寫審核決定（外鍵只要 KEY SHARE）。"""
    return conn.execute("SELECT id, case_id, name, kind, path, status, stats FROM case_file "
                        "WHERE case_id = ANY(%s) AND status IN ('queued', 'done', 'failed') AND kind IN ('dwg', 'dxf') "
                        "ORDER BY id FOR NO KEY UPDATE", (case_ids,)).fetchall()


def requeue_full(conn, file_id: int) -> bool:
    """整個重新處理（例：轉檔器升級）：完整重跑（不是只重跑檢核）、次數歸零；處理中的不動。
    舊的中介資料、檢核結果留著照常顯示，重跑時才覆蓋；原圖排隊由 worker 重跑時重設（process → reset_cad）。"""
    return conn.execute("UPDATE case_file SET status = 'queued', review_only = false, attempts = 0, updated_at = now() "
                        "WHERE id = %s AND status IN ('queued', 'done', 'failed')", (file_id,)).rowcount == 1


def reset_cad(conn, file_id: int) -> None:
    """重新處理整個檔（圖可能變了）：取消排隊，進行中的畫圖結果作廢（cad_gen 不同）。"""
    conn.execute("UPDATE case_file SET cad_state = NULL, cad_gen = cad_gen + 1, cad_started_at = NULL WHERE id = %s",
                 (file_id,))


def claim_cad(conn):
    return conn.execute(CAD_CLAIM_SQL).fetchone()


def recover_cad(conn, older_than_s: int) -> list[dict]:
    return conn.execute(CAD_RECOVER_SQL, (CAD_MAX_ATTEMPTS, older_than_s)).fetchall()


def backfill_cad(conn) -> list[dict]:
    return conn.execute(CAD_BACKFILL_SQL).fetchall()


def finish_cad(conn, file_id: int, gen: int, state: str, info: dict) -> bool:
    """畫圖結束：state＝done｜failed｜pending（被中斷、重新排隊）。檔案在畫圖期間重新排隊過（cad_gen 變了）就不動。"""
    return conn.execute(
        "UPDATE case_file SET cad_state = %s, stats = COALESCE(stats, '{}'::jsonb) || jsonb_build_object('cad', %s::jsonb) "
        "WHERE id = %s AND cad_gen = %s AND cad_state = 'rendering'",
        (state, json.dumps(info, ensure_ascii=False), file_id, gen)).rowcount == 1


def load_review(conn, file_id: int) -> dict | None:
    row = conn.execute("SELECT result FROM file_review WHERE file_id = %s AND status = 'done'", (file_id,)).fetchone()
    return row["result"] if row else None


def kind_of(name: str) -> str:
    for ext, k in KINDS.items():
        if name.lower().endswith(ext):
            return k
    return "other"


def create_case(conn, name: str, created_by: str | None = None) -> int:
    return conn.execute("INSERT INTO review_case (name, created_by) VALUES (%s, %s) RETURNING id",
                        (name, created_by)).fetchone()["id"]


def add_file(conn, case_id: int, name: str, size: int, sha256: str, path: str) -> int:
    kind = kind_of(name)
    status = "queued" if kind in SUPPORTED else "skipped"
    error = None if status == "queued" else ("PDF 擷取尚未支援（後續里程碑）" if kind == "pdf" else "不支援的檔案類型（AutoCAD 暫存檔等）")
    return conn.execute(
        "INSERT INTO case_file (case_id, name, kind, size, sha256, path, status, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (case_id, name, kind, size, sha256, path, status, error)).fetchone()["id"]


def claim(conn):
    return conn.execute(CLAIM_SQL).fetchone()


def recover_stale(conn, older_than_s: int) -> int:
    return conn.execute(RECOVER_SQL, (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, older_than_s)).rowcount


def save_result(conn, file_id: int, ir: dict, stats: dict, status: str = "done") -> None:
    conn.execute("INSERT INTO file_ir (file_id, ir) VALUES (%s, %s::jsonb) "
                 "ON CONFLICT (file_id) DO UPDATE SET ir = EXCLUDED.ir",
                 (file_id, json.dumps(ir, ensure_ascii=False)))
    conn.execute("DELETE FROM case_sheet WHERE file_id = %s", (file_id,))
    for s in ir["sheets"]:
        m = s["meta"]
        conn.execute("INSERT INTO case_sheet (file_id, idx, number, title, scale, unit, meta, bbox) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                      (file_id, s["idx"], stats["sheet_numbers"][s["idx"]],
                       IR.sheet_title(m) or None, IR.meta_field(m, "比例"), IR.meta_field(m, "單位"),
                       json.dumps(m, ensure_ascii=False), s["bbox"]))
    mark(conn, file_id, status, stats)


def mark(conn, file_id: int, status: str, stats: dict | None = None) -> None:
    if stats is None:
        conn.execute("UPDATE case_file SET status = %s, error = NULL, review_only = false, updated_at = now() "
                     "WHERE id = %s", (status, file_id))
        return
    conn.execute("UPDATE case_file SET status = %s, error = NULL, review_only = false, stats = %s::jsonb, "
                 "updated_at = now() WHERE id = %s", (status, json.dumps(stats, ensure_ascii=False), file_id))


def save_review(conn, file_id: int, status: str, result: dict | None, error: str | None, svg_dir: str | None) -> None:
    conn.execute("INSERT INTO file_review (file_id, status, result, error, svg_dir) VALUES (%s, %s, %s::jsonb, %s, %s) "
                 "ON CONFLICT (file_id) DO UPDATE SET status = EXCLUDED.status, result = EXCLUDED.result, "
                 "error = EXCLUDED.error, svg_dir = EXCLUDED.svg_dir, created_at = now()",
                 (file_id, status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                  error[:500] if error else None, svg_dir))


def get_context(conn, case_id: int) -> dict:
    row = conn.execute("SELECT context FROM case_context WHERE case_id = %s", (case_id,)).fetchone()
    return row["context"] if row else {}


def save_context(conn, case_id: int, context: dict, user: str | None) -> None:
    conn.execute("INSERT INTO case_context (case_id, context, updated_by) VALUES (%s, %s::jsonb, %s) "
                 "ON CONFLICT (case_id) DO UPDATE SET context = EXCLUDED.context, updated_by = EXCLUDED.updated_by, "
                 "updated_at = now()", (case_id, json.dumps(context, ensure_ascii=False), user))


def requeue_reviews(conn, case_id: int) -> int:
    """檢核條件改了：已檢核過的檔案只重跑檢核（不重新轉檔、抽取）。"""
    return conn.execute(
        "UPDATE case_file SET status = 'queued', review_only = true, attempts = 0, updated_at = now() "
        "WHERE case_id = %s AND status IN ('done', 'failed') AND id IN (SELECT file_id FROM file_review)",
        (case_id,)).rowcount


def decide(conn, file_id: int, key: str, decision: str | None, note: str | None, user: str | None) -> None:
    """decision 為 None：撤回審核（回到未處理）。"""
    if decision is None:
        conn.execute("DELETE FROM finding_decision WHERE file_id = %s AND key = %s", (file_id, key))
        return
    conn.execute("INSERT INTO finding_decision (file_id, key, decision, note, decided_by) VALUES (%s, %s, %s, %s, %s) "
                 "ON CONFLICT (file_id, key) DO UPDATE SET decision = EXCLUDED.decision, note = EXCLUDED.note, "
                 "decided_by = EXCLUDED.decided_by, decided_at = now()", (file_id, key, decision, note, user))


def decisions(conn, case_id: int) -> dict:
    rows = conn.execute("SELECT d.file_id, d.key, d.decision, d.note, d.decided_by, d.decided_at FROM finding_decision d "
                        "JOIN case_file f ON f.id = d.file_id WHERE f.case_id = %s", (case_id,)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(str(r["file_id"]), {})[r["key"]] = {"decision": r["decision"], "note": r["note"],
                                                           "by": r["decided_by"], "at": r["decided_at"].isoformat()}
    return out


def load_ir(conn, file_id: int) -> dict | None:
    row = conn.execute("SELECT ir FROM file_ir WHERE file_id = %s", (file_id,)).fetchone()
    return row["ir"] if row else None


def save_failure(conn, file_id: int, error: str, retry: bool) -> None:
    # 記失敗時原圖排隊一併取消（失敗的檔不會被畫，工作台不能一直顯示排隊中）；之後重跑成功會再排
    conn.execute("UPDATE case_file SET status = %s, error = %s, updated_at = now(), "
                 "cad_state = CASE WHEN %s AND cad_state = 'pending' THEN NULL ELSE cad_state END WHERE id = %s",
                 ("queued" if retry else "failed", error[:500], not retry, file_id))


def case_status(conn, case_id: int) -> list[dict]:
    return conn.execute("SELECT id, name, kind, size, status, error, stats, attempts FROM case_file "
                        "WHERE case_id = %s ORDER BY name, id", (case_id,)).fetchall()


def file_reviews(conn, case_id: int) -> list[dict]:
    """各檔的存檔路徑與檢核摘要（工作台「檔案處理狀態」的說明用）：review＝檢核狀態（沒檢核過為 NULL）、
    floors＝檢核的樓層數（同一層分好幾張系統圖時算一層）、findings＝缺失數（各層＋全棟）。直接從檢核結果算：stats.review 只在整個重新處理時寫，
    改檢核條件重跑後會過時。"""
    return conn.execute(
        "SELECT f.id, f.path, f.sha256, r.status AS review, r.error AS review_error, "
        "(SELECT count(DISTINCT l) FROM jsonb_path_query(r.result, '$.floors[*].label') l) AS floors, "
        "COALESCE(jsonb_array_length(jsonb_path_query_array(r.result, '$.floors[*].findings[*]')), 0) "
        "+ COALESCE(jsonb_array_length(jsonb_path_query_array(r.result, '$.building.findings[*]')), 0) AS findings "
        "FROM case_file f LEFT JOIN file_review r ON r.file_id = f.id WHERE f.case_id = %s", (case_id,)).fetchall()
