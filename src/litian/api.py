"""消防圖審系統 API（第 0 期：法規庫與法規問答網頁）。

環境變數：DATABASE_URL、MEILI_URL、MEILI_MASTER_KEY
  法規問答（選填）：ANTHROPIC_API_KEY、ASK_ACCESS_CODE（兩者都有才啟用 AI 回答）、ASK_DAILY_LIMIT（每日 AI 問答上限，預設 200）
啟動：uvicorn litian.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import anthropic
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import ask as A
from .lawdb import search as S
from .lawdb import tables as T

log = logging.getLogger("litian.ask")

pool: ConnectionPool | None = None
LEGEND_DIR = Path("data/lawdb/legend")
LEGEND_FILE_RE = re.compile(r"[A-Za-z0-9_]+\.png")   # 只允許建置產生的檔名，防路徑穿越
WEB_INDEX = Path(__file__).parent / "web" / "index.html"
TW = timezone(timedelta(hours=8))
ASK_SOURCES = 8          # 每題送給 AI 的條文數
ASK_PER_MINUTE = 6       # 同一來源每分鐘的 AI 問答上限

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


@app.get("/", include_in_schema=False)
def index():
    return HTMLResponse(WEB_INDEX.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


def _retrieve(q: str, limit: int) -> list[dict]:
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
    return results


@app.get("/api/law/search")
def law_search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20)):
    return {"query": q, "normalized": S.normalize_query(q), "results": _retrieve(q, limit),
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


# ---------------- 法規問答 ----------------

_anthropic: anthropic.AsyncAnthropic | None = None
_recent: dict[str, deque] = {}
_daily = {"day": "", "count": 0}


def _ai_state() -> tuple[bool, str]:
    """兩樣都設定才啟用 AI 回答；缺任何一樣就只列檢索結果（不花錢、不擋人）。"""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False, "AI 回答尚未啟用：管理者還沒設定 AI 金鑰。先列出檢索到的相關條文。"
    if not os.environ.get("ASK_ACCESS_CODE", "").strip():
        return False, "AI 回答尚未啟用：管理者還沒設定存取碼。先列出檢索到的相關條文。"
    return True, ""


def _daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("ASK_DAILY_LIMIT") or 200))
    except ValueError:
        return 200


def _client_ip(request: Request) -> str:
    # 本 API 只綁主機本機，由主機層 Caddy 轉送；Caddy 會以實際來源覆寫 X-Forwarded-For
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() or (request.client.host if request.client else "unknown")


def _take_quota(ip: str) -> None:
    today = datetime.now(TW).date().isoformat()
    if _daily["day"] != today:
        _daily.update(day=today, count=0)
        _recent.clear()
    if _daily["count"] >= _daily_limit():
        raise HTTPException(429, "今天的 AI 問答次數已達上限，請明天再試。檢索條文仍可使用。")
    now = time.monotonic()
    q = _recent.setdefault(ip, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= ASK_PER_MINUTE:
        raise HTTPException(429, "問得太快了，請過一分鐘再試。")
    q.append(now)
    _daily["count"] += 1


def _client() -> anthropic.AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.AsyncAnthropic()   # 讀 ANTHROPIC_API_KEY
    return _anthropic


def _ask_sources(q: str) -> list[dict]:
    """檢索結果補上「上層條文」與結構化表格文字，讓 AI 看得懂第幾款第幾目在講什麼。"""
    out = []
    for r in _retrieve(q, ASK_SOURCES):
        n = _node(r["node_id"])
        parents, pid = [], n["parent_id"] if n else None
        while pid:
            p = _node(pid)
            if not p or p["level"] == "article":
                break
            parents.insert(0, A.clip(p["text"], 120))
            pid = p["parent_id"]
        r["parents"] = parents
        tbl = r.get("table")
        if tbl and r["node_id"] in (tbl["node_id"], tbl["node_id"] + "/1"):
            row = _one("SELECT data FROM law_table WHERE node_id = %s", tbl["node_id"])
            if row:
                r["table_text"] = T.rows_text(row["data"])
        out.append(r)
    return out


@app.get("/api/law/ask/status")
def ask_status():
    ai, message = _ai_state()
    return {"ai_enabled": ai, "message": message, "model": A.MODEL if ai else None,
            "daily_limit": _daily_limit(), "question_max": A.QUESTION_MAX}


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=A.QUESTION_MAX)


@app.post("/api/law/ask")
async def ask(body: AskBody, request: Request, x_access_code: str = Header("")):
    """法規問答（SSE 串流）。事件：sources → (notice | block/text/cite…) → done；出錯時 error。"""
    q = body.question.strip()
    if not q:
        raise HTTPException(422, "請輸入問題")
    ai, message = _ai_state()
    if ai:
        code = os.environ["ASK_ACCESS_CODE"].strip()
        if not hmac.compare_digest(unquote(x_access_code).encode("utf-8"), code.encode("utf-8")):
            raise HTTPException(401, "存取碼不正確")
        _take_quota(_client_ip(request))
    sources = await run_in_threadpool(_ask_sources, q)

    async def events():
        yield A.sse("sources", {"query": q, "sources": sources})
        if not ai:
            yield A.sse("notice", {"message": message})
            yield A.sse("done", {"stop_reason": None, "unverified": []})
            return
        if not sources:
            yield A.sse("notice", {"message": "沒有查到相關條文，請換個說法，或直接輸入條號（例如「設置標準第17條」）。"})
            yield A.sse("done", {"stop_reason": None, "unverified": []})
            return
        try:
            async for name, data in A.stream_answer(_client(), q, sources):
                if name == "done":
                    log.info("ask done stop=%s usage=%s qlen=%d", data.get("stop_reason"), data.get("usage"), len(q))
                    data = {k: v for k, v in data.items() if k != "usage"}
                yield A.sse(name, data)
        except anthropic.APIError as e:
            log.warning("ask failed: %s", type(e).__name__)
            yield A.sse("error", {"message": "AI 服務暫時無法回應，請稍後再試。下面的條文仍可參考。"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
