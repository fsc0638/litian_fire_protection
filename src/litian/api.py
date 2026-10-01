"""消防圖審系統 API（第 0 期：法規庫）。

環境變數：DATABASE_URL、MEILI_URL、MEILI_MASTER_KEY
啟動：uvicorn litian.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .lawdb import search as S

pool: ConnectionPool | None = None
LEGEND_DIR = Path("data/lawdb/legend")
LEGEND_FILE_RE = re.compile(r"[A-Za-z0-9_]+\.png")   # 只允許建置產生的檔名，防路徑穿越

NODE_COLS = "node_id, pcode, article, level, path, text, parent_id, citation, chapter, has_table, pdf_table_url, deleted, children"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
    yield
    pool.close()


app = FastAPI(title="消防圖審系統 API", version="0.1.0", lifespan=lifespan, root_path="")


def _one(sql: str, *args):
    with pool.connection() as c:
        return c.execute(sql, args).fetchone()


def _all(sql: str, *args):
    with pool.connection() as c:
        return c.execute(sql, args).fetchall()


def _node(node_id: str) -> dict | None:
    return _one(f"SELECT {NODE_COLS} FROM law_node WHERE node_id = %s", node_id)


def _law_names() -> dict[str, dict]:
    return {r["pcode"]: r for r in _all("SELECT pcode, name, short, modified, effective, source_update FROM law")}


def _present(n: dict, laws: dict, with_article: bool = True) -> dict:
    law = laws[n["pcode"]]
    out = {"node_id": n["node_id"], "citation": n["citation"], "law_name": law["name"], "law_modified": law["modified"],
           "level": n["level"], "chapter": n["chapter"], "text": n["text"]}
    art = n if n["level"] == "article" else _node(f'{n["pcode"]}/{n["article"]}')
    if with_article and n["level"] != "article":
        out["article_text"] = art["text"]
    if n["level"] == "legend":
        lg = _one("SELECT category, name, note, symbols, note_images FROM drawing_legend WHERE node_id = %s", n["node_id"])
        if lg:
            out["legend"] = {**lg, "symbol_urls": [f"/api/law/legend/image/{f}" for f in lg["symbols"]],
                             "note_image_urls": [f"/api/law/legend/image/{f}" for f in lg["note_images"]]}
    tbl = _one("SELECT node_id, citation, status, verified_by, verified_at FROM law_table WHERE node_id = %s",
               f'{n["pcode"]}/{n["article"]}')
    if tbl:
        out["table"] = {**tbl, "url": f"/api/law/tables/{tbl['node_id']}"}
        if tbl["status"] != "verified":
            out["table_warning"] = "本條表格已結構化，但為開發者轉錄草稿，尚未經消防設備師校對簽名；判定請以官方原文為準"
    if art["pdf_table_url"]:
        out["warning"] = "本條的表格只在官方「完整條文」PDF 中，網頁與 API 文字不完整，請以 PDF 為準"
        out["pdf_table_url"] = art["pdf_table_url"]
    return out


@app.get("/api/health")
def health():
    r = _one("SELECT count(*) AS nodes FROM law_node")
    return {"status": "ok", "law_nodes": r["nodes"]}


@app.get("/api/law/laws")
def laws():
    return _all("SELECT pcode, name, short, level, modified, effective, article_count, deleted_count, source_update "
                "FROM law ORDER BY pcode")


@app.get("/api/law/nodes/{node_id:path}")
def node(node_id: str):
    n = _node(node_id)
    if not n:
        raise HTTPException(404, f"法規庫沒有這個節點：{node_id}")
    laws = _law_names()
    out = _present(n, laws)
    out["children"] = [_present(c, laws, with_article=False) for c in
                       _all(f"SELECT {NODE_COLS} FROM law_node WHERE parent_id = %s ORDER BY seq", node_id)]
    out["references"] = _all("SELECT raw, target FROM law_xref WHERE src = %s AND resolved", node_id)
    out["referenced_by"] = _all("SELECT src, raw FROM law_xref WHERE target = %s", node_id)
    return out


@app.get("/api/law/occupancy")
def occupancy_codes():
    return _all("SELECT code, cls, number, node_id, citation, text FROM occupancy_code ORDER BY cls, number")


@app.get("/api/law/search")
def law_search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20)):
    exists = lambda nid: _one("SELECT 1 AS x FROM law_node WHERE node_id = %s", nid) is not None
    occ_rows = _all("SELECT code, node_id, text FROM occupancy_code")
    occ = {r["code"]: r["node_id"] for r in occ_rows}
    base, key = os.environ["MEILI_URL"], os.environ["MEILI_MASTER_KEY"]
    pinned = S.structural(q, exists) + S.occupancy(q, occ)
    law = S.detect_law(q)
    routes = {"keyword": S.keyword(q, base, key, law),
              "keyword_last": S.keyword(q, base, key, law, strategy="last"),
              "occupancy": S.occupancy_route(q, S.place_terms(occ_rows), list(occ), base, key),
              "legend": S.legend_route(q, base, key)}
    hits = S.fuse(routes, pinned)[:limit]
    laws = _law_names()
    results = []
    for h in hits:
        n = _node(h.node_id)
        if n:
            results.append({**_present(n, laws), "routes": h.routes, "score": round(h.score, 4)})
    return {"query": q, "normalized": S.normalize_query(q), "results": results,
            "note": "向量檢索尚未啟用（待 VOYAGE_API_KEY）"}


@app.get("/api/law/legend")
def legend(category: str | None = None):
    """消防署審查及查驗作業基準附件三「消防圖說圖示範例」：圖例清單（可依類別篩選）。"""
    rows = _all("SELECT node_id, seq, category, name, note, symbols FROM drawing_legend "
                "WHERE (%s::text IS NULL OR category = %s) ORDER BY seq", category, category)
    for r in rows:
        r["symbol_urls"] = [f"/api/law/legend/image/{f}" for f in r.pop("symbols")]
    return rows


@app.get("/api/law/legend/image/{filename}")
def legend_image(filename: str):
    if not LEGEND_FILE_RE.fullmatch(filename):
        raise HTTPException(400, "檔名不合法")
    f = LEGEND_DIR / filename
    if not f.is_file():
        raise HTTPException(404, "沒有這張圖")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/law/tables")
def tables():
    """已結構化的法定表格清單（status：draft＝未經消防設備師校對；verified＝已校對簽名）。"""
    return _all("SELECT node_id, citation, title, status, verified_by, verified_at FROM law_table ORDER BY node_id")


@app.get("/api/law/tables/{node_id:path}")
def table(node_id: str):
    t = _one("SELECT data FROM law_table WHERE node_id = %s", node_id)
    if not t:
        raise HTTPException(404, f"這一條沒有結構化表格：{node_id}")
    data = t["data"]
    if data["status"] != "verified":
        data["warning"] = "開發者轉錄草稿，尚未經消防設備師校對簽名；判定請以官方原文為準"
    return data
