"""向量檢索：OpenAI text-embedding-3-large（1024 維），向量存在 PostgreSQL（pgvector）。

建索引（在主機的 api 容器內，需要 OPENAI_API_KEY）：python -m litian.lawdb.vectors
- 每個節點的嵌入文字＝引用寫法＋法規名＋編章節＋上層條文＋內文（＋結構化表格文字）。
- 文字雜湊沒變的節點不重算，所以法規庫重載後再跑一次，只會補算有變動的節點；已刪除的節點會移除。
查詢：search() 依餘弦距離取最相近的節點；沒問圖例時排除圖例與附件總覽（與關鍵詞路線一致）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from . import tables as T

MODEL = "text-embedding-3-large"
DIM = 1024
BATCH = 96
MAX_CHARS = 6000           # 單節點嵌入文字上限（模型上限 8191 token，中文約一字一 token 以內）

SCHEMA = ("CREATE EXTENSION IF NOT EXISTS vector;"
          f"CREATE TABLE IF NOT EXISTS law_embedding (node_id text PRIMARY KEY, model text NOT NULL, "
          f"text_hash text NOT NULL, embedding vector({DIM}) NOT NULL)")


def embed_text(n: dict, law_name: str, parents: list[str], table_text: str | None = None) -> str:
    parts = [f"{n['citation']}｜{law_name}"]
    if n.get("chapter"):
        parts.append(n["chapter"])
    if parents:
        parts.append(" ＞ ".join(parents))
    parts.append(n["text"])
    if table_text:
        parts.append(table_text)
    return "\n".join(parts)[:MAX_CHARS]


def text_hash(text: str) -> str:
    return hashlib.sha256(f"{MODEL}:{DIM}:{text}".encode("utf-8")).hexdigest()


def parent_texts(n: dict, by: dict[str, dict], limit: int = 120) -> list[str]:
    """上層條文（不含整條 article 節點，因為那是全文）。"""
    out, pid = [], n.get("parent_id")
    while pid and pid in by and by[pid]["level"] != "article":
        t = by[pid]["text"]
        out.insert(0, t if len(t) <= limit else t[:limit] + "……")
        pid = by[pid].get("parent_id")
    return out


def plan(inputs: dict[str, str], existing: dict[str, str]) -> tuple[list[str], list[str]]:
    """回傳（要重算的節點，要刪除的節點）。existing：{node_id: text_hash}。"""
    todo = [nid for nid, t in inputs.items() if existing.get(nid) != text_hash(t)]
    stale = [nid for nid in existing if nid not in inputs]
    return todo, stale


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def build_inputs(conn) -> dict[str, str]:
    rows = conn.execute("SELECT node_id, pcode, article, level, text, parent_id, citation, chapter "
                        "FROM law_node WHERE NOT deleted").fetchall()
    by = {r["node_id"]: r for r in rows}
    laws = {r["pcode"]: r["name"] for r in conn.execute("SELECT pcode, name FROM law").fetchall()}
    tables = {r["node_id"]: r["data"] for r in conn.execute("SELECT node_id, data FROM law_table").fetchall()}
    inputs = {}
    for r in rows:
        art = f"{r['pcode']}/{r['article']}"
        table = T.rows_text(tables[art]) if art in tables and r["node_id"] in (art, art + "/1") else None
        inputs[r["node_id"]] = embed_text(r, laws.get(r["pcode"], ""), parent_texts(r, by), table)
    return inputs


def build(conn, client) -> dict:
    conn.execute(SCHEMA)
    inputs = build_inputs(conn)
    existing = {r["node_id"]: r["text_hash"] for r in
                conn.execute("SELECT node_id, text_hash FROM law_embedding WHERE model = %s", (MODEL,)).fetchall()}
    todo, stale = plan(inputs, existing)
    tokens = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        resp = client.embeddings.create(model=MODEL, input=[inputs[n] for n in batch], dimensions=DIM)
        tokens += resp.usage.total_tokens if resp.usage else 0
        with conn.transaction():
            for nid, d in zip(batch, resp.data):
                conn.execute("INSERT INTO law_embedding (node_id, model, text_hash, embedding) VALUES (%s, %s, %s, %s::vector) "
                             "ON CONFLICT (node_id) DO UPDATE SET model = EXCLUDED.model, text_hash = EXCLUDED.text_hash, "
                             "embedding = EXCLUDED.embedding",
                             (nid, MODEL, text_hash(inputs[nid]), vec_literal(d.embedding)))
    if stale:
        conn.execute("DELETE FROM law_embedding WHERE node_id = ANY(%s)", (stale,))
    return {"model": MODEL, "dim": DIM, "nodes": len(inputs), "embedded": len(todo), "deleted": len(stale), "tokens": tokens}


SEARCH_SQL = ("SELECT e.node_id FROM law_embedding e JOIN law_node n USING (node_id) "
              "WHERE e.model = %s AND (%s::text IS NULL OR n.pcode = %s) "
              "AND (%s OR n.level NOT IN ('legend', 'attachment')) "
              "ORDER BY e.embedding <=> %s::vector LIMIT %s")


def search(conn, vec: list[float], pcode: str | None = None, allow_legend: bool = False, k: int = 20) -> list[str]:
    rows = conn.execute(SEARCH_SQL, (MODEL, pcode, pcode, allow_legend, vec_literal(vec), k)).fetchall()
    return [r["node_id"] for r in rows]


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print(json.dumps({"skipped": "沒有 OPENAI_API_KEY，不建向量索引"}, ensure_ascii=False))
        return 0
    import openai
    import psycopg
    from psycopg.rows import dict_row
    client = openai.OpenAI(max_retries=3, timeout=120)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        print(json.dumps(build(conn, client), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
