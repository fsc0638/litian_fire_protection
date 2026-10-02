"""案件、檔案、圖面中介資料的資料表與工作佇列（PostgreSQL，SELECT … FOR UPDATE SKIP LOCKED）。

檔案狀態：queued（排隊）→ processing（處理中）→〔reviewing（檢核中，有平面圖時）〕→ done（完成）｜failed（失敗）｜skipped（不支援的檔案類型）
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
"""

# 副檔名 → 種類；其餘（.dwl/.dwl2/.bak 等 AutoCAD 暫存檔）記為 skipped
KINDS = {".dwg": "dwg", ".dxf": "dxf", ".pdf": "pdf"}
SUPPORTED = {"dwg", "dxf"}           # PDF 擷取在後續里程碑
MAX_ATTEMPTS = 3

CLAIM_SQL = """
UPDATE case_file SET status = 'processing', attempts = attempts + 1, updated_at = now()
WHERE id = (SELECT id FROM case_file WHERE status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, case_id, name, kind, path, attempts, review_only
"""
# 處理中卻超過時間沒更新（worker 當掉）：還有次數就退回排隊，否則記失敗
RECOVER_SQL = """
UPDATE case_file SET status = CASE WHEN attempts < %s THEN 'queued' ELSE 'failed' END,
       error = CASE WHEN attempts < %s THEN error ELSE '處理逾時或中斷次數過多' END, updated_at = now()
WHERE status IN ('processing', 'reviewing') AND updated_at < now() - make_interval(secs => %s)
"""


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA)


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
    return conn.execute(RECOVER_SQL, (MAX_ATTEMPTS, MAX_ATTEMPTS, older_than_s)).rowcount


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
    conn.execute("UPDATE case_file SET status = %s, error = %s, updated_at = now() WHERE id = %s",
                 ("queued" if retry else "failed", error[:500], file_id))


def case_status(conn, case_id: int) -> list[dict]:
    return conn.execute("SELECT id, name, kind, size, status, error, stats, attempts FROM case_file "
                        "WHERE case_id = %s ORDER BY name", (case_id,)).fetchall()
