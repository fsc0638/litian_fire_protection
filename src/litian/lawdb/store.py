"""把建置好的法規資料檔載入 PostgreSQL（系統的正式資料來源）與 Meilisearch（關鍵詞檢索）。

用法（在主機的 api 容器內）：python -m litian.lawdb.store [--data data/lawdb]
每次整批重載：PostgreSQL 在單一交易內換新，Meilisearch 以新索引原子交換，查詢不會看到半套資料。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import psycopg

from .search import MEILI_INDEX, MEILI_SETTINGS, index_document
from .tables import rows_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS law (
  pcode text PRIMARY KEY, name text NOT NULL, short text NOT NULL, level text, category text,
  modified text, effective text, effective_note text, abandoned boolean,
  article_count int, deleted_count int, source_update text, loaded_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS law_node (
  node_id text PRIMARY KEY, pcode text NOT NULL REFERENCES law, article text NOT NULL, level text NOT NULL,
  path int[] NOT NULL, text text NOT NULL, parent_id text, citation text NOT NULL, chapter text,
  has_table boolean, pdf_table_url text, deleted boolean, seq int, children text[]);
CREATE INDEX IF NOT EXISTS law_node_article ON law_node (pcode, article);
CREATE TABLE IF NOT EXISTS occupancy_code (
  code text PRIMARY KEY, cls text NOT NULL, number int, node_id text NOT NULL REFERENCES law_node,
  citation text, text text);
CREATE TABLE IF NOT EXISTS law_xref (
  src text NOT NULL REFERENCES law_node, raw text NOT NULL, target text REFERENCES law_node,
  external boolean, resolved boolean);
CREATE INDEX IF NOT EXISTS law_xref_src ON law_xref (src);
CREATE INDEX IF NOT EXISTS law_xref_target ON law_xref (target);
CREATE TABLE IF NOT EXISTS drawing_legend (
  node_id text PRIMARY KEY REFERENCES law_node, seq int NOT NULL, attachment text NOT NULL, category text NOT NULL,
  name text NOT NULL, note text, symbols text[] NOT NULL, note_images text[]);
CREATE TABLE IF NOT EXISTS law_table (
  node_id text PRIMARY KEY REFERENCES law_node, citation text NOT NULL, title text, status text NOT NULL,
  verified_by text, verified_at text, data jsonb NOT NULL);
"""


def _read(data: Path):
    laws = json.loads((data / "laws.json").read_text(encoding="utf-8"))
    nodes = [json.loads(l) for l in (data / "nodes.jsonl").read_text(encoding="utf-8").splitlines() if l]
    occ = json.loads((data / "occupancy.json").read_text(encoding="utf-8"))
    xrefs = [json.loads(l) for l in (data / "xrefs.jsonl").read_text(encoding="utf-8").splitlines() if l]
    report = json.loads((data / "build_report.json").read_text(encoding="utf-8"))
    legend_file = data / "legend.json"
    legend = json.loads(legend_file.read_text(encoding="utf-8")) if legend_file.exists() else []
    tables_file = data / "tables.json"
    tables = json.loads(tables_file.read_text(encoding="utf-8")) if tables_file.exists() else []
    return laws, nodes, occ, xrefs, report, legend, tables


def load_postgres(dsn: str, data: Path) -> dict:
    laws, nodes, occ, xrefs, report, legend, tables = _read(data)
    update = json.dumps(report.get("source_update", {}), ensure_ascii=False)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("TRUNCATE law_table, drawing_legend, law_xref, occupancy_code, law_node, law CASCADE")
        cur.executemany(
            "INSERT INTO law VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(l["pcode"], l["name"], l["short"], l["level"], l["category"], l["modified"], l["effective"],
              l["effective_note"], l["abandoned"], l["article_count"], l["deleted_count"], update) for l in laws])
        cur.executemany(
            "INSERT INTO law_node VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(n["node_id"], n["pcode"], n["article"], n["level"], n["path"], n["text"], n["parent_id"],
              n["citation"], n["chapter"], n["has_table"], n["pdf_table_url"], n["deleted"], n["seq"],
              n["children"]) for n in nodes])
        cur.executemany("INSERT INTO occupancy_code VALUES (%s,%s,%s,%s,%s,%s)",
                        [(o["code"], o["cls"], o["number"], o["node_id"], o["citation"], o["text"]) for o in occ])
        cur.executemany("INSERT INTO law_xref VALUES (%s,%s,%s,%s,%s)",
                        [(x["src"], x["raw"], x["target"], x["external"], x["resolved"]) for x in xrefs])
        cur.executemany("INSERT INTO drawing_legend VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        [(e["node_id"], e["seq"], e["attachment"], e["category"], e["name"], e["note"],
                          e["symbols"], e["note_images"]) for e in legend])
        cur.executemany("INSERT INTO law_table VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [(t["node_id"], t["citation"], t.get("title"), t["status"], t.get("verified_by"),
                          t.get("verified_at"), json.dumps(t, ensure_ascii=False)) for t in tables])
    return {"laws": len(laws), "nodes": len(nodes), "occupancy": len(occ), "xrefs": len(xrefs),
            "legend": len(legend), "tables": len(tables)}


def _meili(method: str, url: str, key: str, **kw) -> dict:
    r = httpx.request(method, url, headers={"Authorization": f"Bearer {key}"}, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def _wait(base: str, key: str, task: dict) -> None:
    uid = task["taskUid"]
    for _ in range(600):
        t = _meili("GET", f"{base}/tasks/{uid}", key)
        if t["status"] == "succeeded":
            return
        if t["status"] in ("failed", "canceled"):
            raise RuntimeError(f"Meilisearch 工作失敗：{t.get('error')}")
        time.sleep(0.2)
    raise TimeoutError(f"Meilisearch 工作逾時：{uid}")


def load_meili(base: str, key: str, data: Path) -> dict:
    laws, nodes, occ, xrefs, _, _, tables = _read(data)
    names = {l["pcode"]: (l["name"], l["short"]) for l in laws}
    by = {n["node_id"]: n for n in nodes}
    refs: dict[str, list[str]] = {}
    for x in xrefs:
        if x["resolved"]:
            refs.setdefault(x["src"], []).append(x["target"])
    occ_by_node = {o["node_id"]: o["code"] for o in occ}
    occ_codes = [o["code"] for o in occ]
    table_text = {t["node_id"]: rows_text(t) for t in tables}
    docs = [index_document(n, by, names, refs.get(n["node_id"], []), occ_by_node, occ_codes, table_text)
            for n in nodes if n["level"] != "article" and not n["deleted"]]
    tmp = f"{MEILI_INDEX}_new"
    if _exists(base, key, tmp):
        _wait(base, key, _meili("DELETE", f"{base}/indexes/{tmp}", key))
    _wait(base, key, _meili("POST", f"{base}/indexes", key, json={"uid": tmp, "primaryKey": "id"}))
    _wait(base, key, _meili("PATCH", f"{base}/indexes/{tmp}/settings", key, json=MEILI_SETTINGS))
    _wait(base, key, _meili("POST", f"{base}/indexes/{tmp}/documents", key, json=docs))
    if not _exists(base, key, MEILI_INDEX):
        _wait(base, key, _meili("POST", f"{base}/indexes", key, json={"uid": MEILI_INDEX, "primaryKey": "id"}))
    _wait(base, key, _meili("POST", f"{base}/swap-indexes", key, json=[{"indexes": [MEILI_INDEX, tmp]}]))
    _wait(base, key, _meili("DELETE", f"{base}/indexes/{tmp}", key))
    stats = _meili("GET", f"{base}/indexes/{MEILI_INDEX}/stats", key)
    return {"documents": stats.get("numberOfDocuments")}


def _exists(base: str, key: str, uid: str) -> bool:
    r = httpx.get(f"{base}/indexes/{uid}", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    return r.status_code == 200


def main() -> None:
    ap = argparse.ArgumentParser(description="載入法規庫到 PostgreSQL 與 Meilisearch")
    ap.add_argument("--data", type=Path, default=Path("data/lawdb"))
    a = ap.parse_args()
    pg = load_postgres(os.environ["DATABASE_URL"], a.data)
    ms = load_meili(os.environ["MEILI_URL"], os.environ["MEILI_MASTER_KEY"], a.data)
    print(json.dumps({"postgres": pg, "meilisearch": ms}, ensure_ascii=False))


if __name__ == "__main__":
    main()
