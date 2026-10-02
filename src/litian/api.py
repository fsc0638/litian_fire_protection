"""消防圖審系統 API（第 0 期：法規庫與法規問答網頁；第 1 期：審核工作台）。

環境變數：DATABASE_URL、MEILI_URL、MEILI_MASTER_KEY
  法規問答（選填）：OPENAI_API_KEY（有才啟用 AI 回答，模型 gpt-5.6-sol）、ASK_ACCESS_CODE（選填，設定後才要求存取碼）、ASK_DAILY_LIMIT（每日 AI 問答上限，預設 500；台北時間每天 23:59 重新計算，計數存在資料庫）
啟動：uvicorn litian.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import openai
from fastapi import Cookie, Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import ask as A
from . import auth as AU
from .drawing import store as DS
from .drawing.cli import safe_name
from .lawdb import search as S
from .lawdb import tables as T
from .lawdb import vectors as V

log = logging.getLogger("litian.ask")
if not log.handlers:   # uvicorn 不會替自訂 logger 設輸出；沒有這段，INFO 等級的用量紀錄會被丟掉
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

pool: ConnectionPool | None = None
LEGEND_DIR = Path("data/lawdb/legend")
LEGEND_FILE_RE = re.compile(r"[A-Za-z0-9_]+\.png")   # 只允許建置產生的檔名，防路徑穿越
WEB_INDEX = Path(__file__).parent / "web" / "index.html"
WEB_WORKBENCH = Path(__file__).parent / "web" / "workbench.html"
TW = timezone(timedelta(hours=8))
ASK_SOURCES = 8          # 每題送給 AI 的條文數
ARTICLE_CONTEXT_MIN = 3  # 同一條被檢索到幾個款目以上，就把整條原文也交給 AI（最多補 2 條）
PLACE_PIN_MAX = 3        # 問題提到的場所代碼不超過此數時，自動帶入其第 12 條分類條文
ACCESS_CODE_MIN = 12     # 存取碼最短長度（太短容易被猜中）
CODE_FAIL_MAX = 10       # 同一來源一小時內存取碼錯誤上限，超過就暫停一小時

NODE_COLS = "node_id, pcode, article, level, path, text, parent_id, citation, chapter, has_table, pdf_table_url, deleted, children"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
    with pool.connection() as c:
        c.execute(USAGE_SCHEMA)
        c.execute(ASK_LOG_SCHEMA)
        AU.ensure_schema(c)
        DS.ensure_schema(c)
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


_openai_sync: openai.OpenAI | None = None


@lru_cache(maxsize=1024)
def _embed_query(q: str) -> tuple[float, ...]:
    global _openai_sync
    if _openai_sync is None:
        _openai_sync = openai.OpenAI(timeout=8, max_retries=1)   # 讀 OPENAI_API_KEY
    r = _openai_sync.embeddings.create(model=V.MODEL, input=[q], dimensions=V.DIM)
    return tuple(r.data[0].embedding)


def _vector_route(q: str, pcode: str | None) -> list[str]:
    """向量檢索路線；沒有金鑰、索引還沒建或 OpenAI 連不上時回空清單，不影響其他路線。"""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return []
    try:
        vec = list(_embed_query(q))
        with pool.connection() as c:
            return V.search(c, vec, pcode=pcode, allow_legend=bool(S.LEGEND_INTENT.search(q)))
    except Exception as e:
        log.warning("vector route failed: %s", type(e).__name__)
        return []


@lru_cache(maxsize=1)
def _known_bigrams() -> frozenset[str]:
    """法規全文字表（相鄰兩字），用來拿掉查詢裡法規沒有的詞。程式重啟才更新（法規庫更新會重建容器）。"""
    try:
        texts = [r["t"] for r in _all("SELECT text || ' ' || citation || ' ' || coalesce(chapter, '') AS t FROM law_node")]
        texts += [T.rows_text(r["data"]) for r in _all("SELECT data FROM law_table")]
        return frozenset(S.corpus_bigrams(texts))
    except Exception as e:
        log.warning("known bigrams failed: %s", type(e).__name__)
        return frozenset()


@lru_cache(maxsize=1)
def _table_index() -> tuple:
    """結構化表格的檢索索引（程式重啟才更新；法規庫更新會重建容器）。"""
    try:
        tables = [r["data"] for r in _all("SELECT data FROM law_table")]
        node_of = lambda art: art + "/1" if _node(art + "/1") else art
        return tuple(S.table_index(tables, node_of))
    except Exception as e:
        log.warning("table index failed: %s", type(e).__name__)
        return ()


def _retrieve(q: str, limit: int, vector_weight: float | None = None) -> list[dict]:
    exists = lambda nid: _one("SELECT 1 AS x FROM law_node WHERE node_id = %s", nid) is not None
    occ_rows = _all("SELECT code, node_id, text FROM occupancy_code")
    occ = {r["code"]: r["node_id"] for r in occ_rows}
    base, key = os.environ["MEILI_URL"], os.environ["MEILI_MASTER_KEY"]
    pinned = S.structural(q, exists) + S.occupancy(q, occ)
    law = S.detect_law(q)
    known = _known_bigrams() or None
    routes = {"keyword": S.keyword(q, base, key, law, known=known),
              "keyword_last": S.keyword(q, base, key, law, strategy="last", known=known),
              "occupancy": S.occupancy_route(q, S.place_terms(occ_rows), list(occ), base, key),
              "legend": S.legend_route(q, base, key),
              "table": S.table_route(q, list(_table_index()))}
    weights = None
    if vector_weight != 0:
        routes["vector"] = _vector_route(q, law)
    if vector_weight is not None:
        weights = {**S.ROUTE_WEIGHT, "vector": vector_weight}
    hits = S.fuse(routes, pinned, weights)[:limit]
    laws = _law_names()
    results = []
    for h in hits:
        n = _node(h.node_id)
        if n:
            results.append({**_present(n, laws), "routes": h.routes, "score": round(h.score, 4)})
    return results


@app.get("/api/law/search")
def law_search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20),
               vw: float | None = Query(None, ge=0, le=5, include_in_schema=False)):
    """vw：評測用，暫時改向量路線權重（0＝不走向量路線）。"""
    vec = "已啟用（OpenAI text-embedding-3-large）" if os.environ.get("OPENAI_API_KEY", "").strip() else "未啟用（沒有 OPENAI_API_KEY）"
    return {"query": q, "normalized": S.normalize_query(q), "results": _retrieve(q, limit, vw),
            "note": "向量檢索" + vec}


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

_openai: openai.AsyncOpenAI | None = None
USAGE_SCHEMA = "CREATE TABLE IF NOT EXISTS ask_usage (day text PRIMARY KEY, count integer NOT NULL)"
# 測試期提問紀錄：每次按「查詢」一筆（問題、檢索結果、AI 回答與查核結果）。來源只存雜湊，不存 IP。
ASK_LOG_SCHEMA = """CREATE TABLE IF NOT EXISTS ask_log (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT now(),
  client text,
  question text NOT NULL,
  mode text NOT NULL,
  sources jsonb NOT NULL,
  answer text,
  cited integer[],
  invalid_cites integer[],
  unverified text[],
  stop_reason text,
  error text,
  input_tokens integer,
  output_tokens integer,
  duration_ms integer)"""
ASK_LOG_INSERT = ("INSERT INTO ask_log (client, question, mode, sources, answer, cited, invalid_cites, unverified, "
                  "stop_reason, error, input_tokens, output_tokens, duration_ms) "
                  "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
# 原子地加一；已達上限時不加、不回傳列
USAGE_BUMP = ("INSERT INTO ask_usage (day, count) VALUES (%s, 1) "
              "ON CONFLICT (day) DO UPDATE SET count = ask_usage.count + 1 WHERE ask_usage.count < %s RETURNING count")
_fails: dict[str, deque] = {}


def _access_code() -> str:
    """存取碼（選填）：空白＝不要求存取碼（2026-10-01 使用者決定先不用）。"""
    return os.environ.get("ASK_ACCESS_CODE", "").strip()


def _ai_state() -> tuple[bool, str]:
    """有 AI 金鑰才啟用 AI 回答；沒有就只列檢索結果（不花錢）。"""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return False, "AI 回答尚未啟用：管理者還沒設定 AI 金鑰。先列出檢索到的相關條文。"
    if _access_code() and len(_access_code()) < ACCESS_CODE_MIN:
        return False, f"AI 回答尚未啟用：存取碼太短，至少要 {ACCESS_CODE_MIN} 個字元。先列出檢索到的相關條文。"
    return True, ""


def _daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("ASK_DAILY_LIMIT") or 500))
    except ValueError:
        return 500


def _usage_day(now: datetime | None = None) -> str:
    """用量計算日：台北時間每天 23:59 換日（使用者指定），所以 23:59 之後算到隔天。"""
    now = now or datetime.now(TW)
    return (now.astimezone(TW) + timedelta(minutes=1)).date().isoformat()


def _bump_daily(day: str, limit: int) -> bool:
    """當日用量加一；已達上限回傳 False。存在資料庫，重新部署也不會歸零。"""
    if limit <= 0:
        return False
    with pool.connection() as c:
        return c.execute(USAGE_BUMP, (day, limit)).fetchone() is not None


def _used_today() -> int:
    r = _one("SELECT count FROM ask_usage WHERE day = %s", _usage_day())
    return r["count"] if r else 0


def _client_ip(request: Request) -> str:
    # 本 API 只綁主機本機，由主機層 Caddy 轉送；Caddy 會以實際來源覆寫 X-Forwarded-For
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() or (request.client.host if request.client else "unknown")


def _take_quota() -> None:
    """每日 AI 問答總數上限（不分來源；使用者決定不設每分鐘限制）。"""
    limit = _daily_limit()
    if not _bump_daily(_usage_day(), limit):
        raise HTTPException(429, f"今天的 AI 問答次數已達上限（{limit} 次），每天 23:59 重新計算。檢索條文仍可使用。")


def _client_tag(request: Request) -> str:
    """來源 IP 的 HMAC 雜湊前 12 碼：只用來區分不同測試者，無法還原成 IP。"""
    key = os.environ.get("MEILI_MASTER_KEY", "").encode("utf-8")
    return hmac.new(key, _client_ip(request).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def _source_summary(sources: list[dict]) -> list[dict]:
    return [{"n": i + 1, "node_id": s["node_id"], "citation": s.get("citation"), "routes": s.get("routes"),
             "score": s.get("score")} for i, s in enumerate(sources)]


def _save_log(rec: dict) -> None:
    """寫一筆提問紀錄；失敗只記警告，不影響使用者。"""
    try:
        u = rec.get("usage") or {}
        with pool.connection() as c:
            c.execute(ASK_LOG_INSERT, (rec.get("client"), rec["question"], rec["mode"],
                                       json.dumps(rec.get("sources", []), ensure_ascii=False), rec.get("answer"),
                                       rec.get("cited"), rec.get("invalid_cites"), rec.get("unverified"),
                                       rec.get("stop_reason"), rec.get("error"), u.get("input_tokens"),
                                       u.get("output_tokens"), rec.get("duration_ms")))
    except Exception as e:
        log.warning("ask_log write failed: %s", type(e).__name__)


def _check_code(ip: str, given: str, code: str) -> None:
    """存取碼比對；同一來源一小時內錯太多次就暫停，防止暴力猜碼。"""
    now = time.monotonic()
    f = _fails.setdefault(ip, deque())
    while f and now - f[0] > 3600:
        f.popleft()
    if len(f) >= CODE_FAIL_MAX:
        raise HTTPException(429, "存取碼錯誤次數太多，請一小時後再試。")
    if not hmac.compare_digest(given.encode("utf-8"), code.encode("utf-8")):
        f.append(now)
        raise HTTPException(401, "存取碼不正確")


def _client() -> openai.AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = openai.AsyncOpenAI()   # 讀 OPENAI_API_KEY
    return _openai


def _place_nodes(q: str) -> list[str]:
    """問題提到的場所（例：KTV → 甲-1）對應的第 12 條分類條文節點。
    只提到類別（例：「甲類場所」會展開成 7 個代碼）時不帶，避免條文數暴增。"""
    occ_rows = _all("SELECT code, node_id, text FROM occupancy_code")
    codes, _ = S.detect_places(q, S.place_terms(occ_rows), [r["code"] for r in occ_rows])
    if not codes or len(codes) > PLACE_PIN_MAX:
        return []
    by_code = {r["code"]: r["node_id"] for r in occ_rows}
    return [by_code[c] for c in sorted(codes) if c in by_code]


def _ask_sources(q: str) -> list[dict]:
    """檢索結果補上「上層條文」與結構化表格文字，讓 AI 看得懂第幾款第幾目在講什麼。
    問題提到具體場所時，該場所的第 12 條分類條文排在最前面（檢索沒抓到時自動補上）。"""
    results = _retrieve(q, ASK_SOURCES)
    have = {r["node_id"] for r in results}
    pins, laws = [], None
    for nid in _place_nodes(q):
        if nid in have:
            continue
        n = _node(nid)
        if n:
            laws = laws or _law_names()
            pins.append({**_present(n, laws), "routes": ["place"], "score": None})
    out = [_enrich(r, q) for r in pins + results]
    # 同一條被抓到很多零碎款目時（例：「免設排煙條件」→ 第 190 條各款各目），補上整條原文，AI 才不會只看到片段
    have = {r["node_id"] for r in out}
    arts = Counter("/".join(r["node_id"].split("/")[:2]) for r in out
                   if r.get("level") not in ("article", "legend", "attachment"))
    added = 0
    for art, c in arts.most_common():
        if c < ARTICLE_CONTEXT_MIN or added >= 2:
            break
        if art in have:
            continue
        n = _node(art)
        if n:
            laws = laws or _law_names()
            out.append(_enrich({**_present(n, laws), "routes": ["article_context"], "score": None}, q))
            added += 1
    return out


def _enrich(r: dict, q: str | None = None) -> dict:
    """補上層條文（不含整條）與結構化表格文字（大表只留和問題相關的列）。"""
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
            r["table_text"] = T.rows_text(row["data"], focus=q, limit=A.MAX_DOC_CHARS)
    return r


@app.get("/api/law/ask/status")
def ask_status():
    ai, message = _ai_state()
    return {"ai_enabled": ai, "message": message, "model": A.MODEL if ai else None,
            "access_code_required": ai and bool(_access_code()),
            "used_today": _used_today() if ai else 0,
            "daily_limit": _daily_limit(), "question_max": A.QUESTION_MAX}


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=A.QUESTION_MAX)


@app.post("/api/law/ask")
async def ask(body: AskBody, request: Request, x_access_code: str = Header("")):
    """法規問答（SSE 串流）。事件：sources → (notice | block/text/cite…) → done；出錯時 error。"""
    q = body.question.strip()
    if not q:
        raise HTTPException(422, "請輸入問題")
    t0 = time.monotonic()
    rec = {"client": _client_tag(request), "question": q}
    ai, message = _ai_state()
    if ai:
        try:
            if _access_code():
                _check_code(_client_ip(request), unquote(x_access_code), _access_code())
            _take_quota()
        except HTTPException as e:
            await run_in_threadpool(_save_log, {**rec, "mode": "rejected", "error": f"{e.status_code} {e.detail}"})
            raise
    sources = await run_in_threadpool(_ask_sources, q)
    rec["sources"] = _source_summary(sources)
    rec["mode"] = "search_only" if not ai else ("ai" if sources else "no_sources")

    async def events():
        parts: list[str] = []
        finished = False
        try:
            yield A.sse("sources", {"query": q, "sources": sources})
            if not ai:
                yield A.sse("notice", {"message": message})
                yield A.sse("done", {"stop_reason": None, "unverified": []})
                finished = True
                return
            if not sources:
                yield A.sse("notice", {"message": "沒有查到相關條文，請換個說法，或直接輸入條號（例如「設置標準第17條」）。"})
                yield A.sse("done", {"stop_reason": None, "unverified": []})
                finished = True
                return
            try:
                async for name, data in A.stream_answer(_client(), q, sources):
                    if name == "text":
                        parts.append(data["text"])
                    elif name == "done":
                        log.info("ask done stop=%s usage=%s qlen=%d", data.get("stop_reason"), data.get("usage"), len(q))
                        rec.update({k: data.get(k) for k in ("stop_reason", "cited", "invalid_cites", "unverified", "usage")})
                        data = {k: v for k, v in data.items() if k != "usage"}
                    yield A.sse(name, data)
            except Exception as e:   # API 錯誤，或串流途中連線中斷（SDK 會直接丟出底層連線錯誤）
                log.warning("ask failed: %s status=%s request_id=%s", type(e).__name__,
                            getattr(e, "status_code", None), getattr(e, "request_id", None))
                rec["error"] = f"{type(e).__name__} status={getattr(e, 'status_code', None)}"
                yield A.sse("error", {"message": "AI 服務暫時無法回應或連線中斷，請稍後再試。下面的條文仍可參考。"})
            finished = True
        finally:
            # 使用者中途關掉頁面時也會走到這裡（串流被取消），一樣記下已收到的部分
            if not finished and not rec.get("error"):
                rec["error"] = "client_disconnected"
            rec["answer"] = "".join(parts) or None
            rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
            _save_log(rec)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 審核工作台（第 1 期 M1b）：登入、案件、上傳、圖紙 ----------------

CASES_DIR = Path(os.environ.get("CASES_DIR", "/data/cases"))
UPLOAD_MAX = 200 * 1024 * 1024        # 單檔上限
LOGIN_FAIL_MAX = 10                   # 同一來源一小時內登入失敗上限
_login_fails: dict[str, deque] = {}


def _login_rate(ip: str, failed: bool = False) -> None:
    now = time.monotonic()
    f = _login_fails.setdefault(ip, deque())
    while f and now - f[0] > 3600:
        f.popleft()
    if failed:
        f.append(now)
    elif len(f) >= LOGIN_FAIL_MAX:
        raise HTTPException(429, "登入失敗次數太多，請一小時後再試。")


def current_user(fr_session: str | None = Cookie(None)) -> dict:
    with pool.connection() as c:
        u = AU.session_user(c, fr_session)
    if not u:
        raise HTTPException(401, "請先登入")
    return u


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/auth/login")
def auth_login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    _login_rate(ip)
    with pool.connection() as c:
        r = AU.login(c, body.username.strip(), body.password)
    if not r:
        _login_rate(ip, failed=True)
        raise HTTPException(401, "帳號或密碼不正確")
    token, user = r
    response.set_cookie(AU.COOKIE, token, max_age=AU.SESSION_HOURS * 3600, httponly=True, secure=True,
                        samesite="lax", path="/")
    return {"user": user}


@app.post("/api/auth/logout")
def auth_logout(response: Response, fr_session: str | None = Cookie(None)):
    with pool.connection() as c:
        AU.logout(c, fr_session)
    response.delete_cookie(AU.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(current_user)):
    return user


@app.get("/workbench", include_in_schema=False)
def workbench():
    return HTMLResponse(WEB_WORKBENCH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


class CaseBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@app.get("/api/cases")
def cases_list(user: dict = Depends(current_user)):
    return _all("SELECT c.id, c.name, c.created_by, c.created_at, count(f.id) AS files, "
                "count(f.id) FILTER (WHERE f.status = 'done') AS done, "
                "count(f.id) FILTER (WHERE f.status IN ('queued', 'processing', 'reviewing')) AS pending, "
                "count(f.id) FILTER (WHERE f.status = 'failed') AS failed "
                "FROM review_case c LEFT JOIN case_file f ON f.case_id = c.id "
                "GROUP BY c.id ORDER BY c.id DESC LIMIT 200")


@app.post("/api/cases")
def cases_create(body: CaseBody, user: dict = Depends(current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "請輸入案件名稱")
    with pool.connection() as c:
        return {"id": DS.create_case(c, name, user["username"])}


def _case_or_404(case_id: int) -> dict:
    row = _one("SELECT id, name, created_by, created_at FROM review_case WHERE id = %s", case_id)
    if not row:
        raise HTTPException(404, "沒有這個案件")
    return row


@app.post("/api/cases/{case_id}/files")
async def cases_upload(case_id: int, files: list[UploadFile] = File(...), user: dict = Depends(current_user)):
    _case_or_404(case_id)
    d = CASES_DIR / str(case_id)
    d.mkdir(parents=True, exist_ok=True)
    start = _one("SELECT count(*) AS n FROM case_file WHERE case_id = %s", case_id)["n"]
    out = []
    for i, f in enumerate(files, start + 1):
        name = (f.filename or "file")[:200]
        dst = d / f"{i:03d}_{safe_name(name)}"
        h, size = hashlib.sha256(), 0
        with open(dst, "wb") as fh:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > UPLOAD_MAX:
                    fh.close()
                    dst.unlink(missing_ok=True)
                    raise HTTPException(413, f"「{name}」超過單檔上限 {UPLOAD_MAX // (1024 * 1024)} MB")
                h.update(chunk)
                fh.write(chunk)
        with pool.connection() as c:
            fid = DS.add_file(c, case_id, name, size, h.hexdigest(), str(dst))
        out.append({"id": fid, "name": name, "size": size})
    return {"files": out}


@app.get("/api/cases/{case_id}")
def cases_detail(case_id: int, user: dict = Depends(current_user)):
    case = _case_or_404(case_id)
    with pool.connection() as c:
        files = DS.case_status(c, case_id)
    sheets = _all("SELECT s.id, s.file_id, s.idx, s.number, s.title, s.scale, s.unit FROM case_sheet s "
                  "JOIN case_file f ON f.id = s.file_id WHERE f.case_id = %s ORDER BY s.number NULLS LAST, s.id", case_id)
    return {"case": case, "files": files, "sheets": sheets}


@app.get("/api/cases/{case_id}/sheets/{sheet_id}/texts")
def cases_sheet_texts(case_id: int, sheet_id: int, user: dict = Depends(current_user)):
    s = _one("SELECT s.file_id, s.idx, s.number, s.title FROM case_sheet s JOIN case_file f ON f.id = s.file_id "
             "WHERE s.id = %s AND f.case_id = %s", sheet_id, case_id)
    if not s:
        raise HTTPException(404, "沒有這張圖")
    rows = _all("SELECT t->>'t' AS t, (t->>'x')::float AS x, (t->>'y')::float AS y, t->>'layer' AS layer "
                "FROM file_ir i, jsonb_array_elements(i.ir->'texts') t "
                "WHERE i.file_id = %s AND t->>'f' IS NOT NULL AND (t->>'f')::int = %s", s["file_id"], s["idx"])
    rows.sort(key=lambda r: (-r["y"], r["x"]))
    return {"sheet": s, "texts": rows}


# ---------- 檢核結果（review.engine 由 worker 產生）----------
REVIEW_LABEL = re.compile(r"^[0-9A-Z]{1,6}(-\d{1,4})?$")       # 樓層代號或「樓層-圖紙序號」


def _svg_name(fl: dict) -> str:
    return fl.get("svg_name") or fl["label"]


def _review_bundle(case_id: int) -> dict:
    """檢核結果＋引用條文＋審核結果＋檢核條件（工作台與報告共用）。"""
    rows = _all("SELECT r.file_id, f.name, r.status, r.error, r.result, r.svg_dir, r.created_at FROM file_review r "
                "JOIN case_file f ON f.id = r.file_id WHERE f.case_id = %s ORDER BY f.name", case_id)
    ids = set()
    for r in rows:
        res = r["result"] or {}
        b = res.get("building") or {}
        for item in b.get("findings", []) + b.get("requirements", []) + b.get("notes", []):
            ids.update(item["law"])
        for fl in res.get("floors", []):
            fl["svg"] = f"/api/cases/{case_id}/files/{r['file_id']}/review/{_svg_name(fl)}.svg"
            for item in fl["findings"] + fl["notes"]:
                ids.update(item["law"])
    laws = {}
    if ids:
        for x in _all("SELECT node_id, citation, text FROM law_node WHERE node_id = ANY(%s)", sorted(ids)):
            laws[x["node_id"]] = {"citation": x["citation"], "text": (x["text"] or "")[:600]}
    with pool.connection() as c:
        ctx = DS.get_context(c, case_id)
        dec = DS.decisions(c, case_id)
    return {"reviews": rows, "laws": laws, "context": ctx, "decisions": dec}


@app.get("/api/cases/{case_id}/reviews")
def cases_reviews(case_id: int, user: dict = Depends(current_user)):
    _case_or_404(case_id)
    b = _review_bundle(case_id)
    for r in b["reviews"]:
        r.pop("svg_dir", None)
    return b


class ContextBody(BaseModel):
    occupancy: str | None = Field(None, max_length=8)
    fireproof: bool | None = None
    stories: int | None = Field(None, ge=1, le=200)
    height: float | None = Field(None, gt=0, le=1000)
    site_area: float | None = Field(None, gt=0, le=10_000_000)
    ceiling_height: dict[str, float] = Field(default_factory=dict)
    no_opening: list[str] = Field(default_factory=list, max_length=200)
    floor_area: dict[str, float] = Field(default_factory=dict)
    policy: dict[str, bool] = Field(default_factory=dict)          # 法規解讀設定（只存與預設不同的）


FLOOR_LABEL = re.compile(r"^(\d{1,3}M?F|B\d{1,2}|R\d?F)$")


@app.put("/api/cases/{case_id}/context")
def cases_context(case_id: int, body: ContextBody, user: dict = Depends(current_user)):
    """存檢核條件，並把已檢核的檔案排入「只重跑檢核」。"""
    _case_or_404(case_id)
    if body.occupancy and not _one("SELECT 1 AS ok FROM occupancy_code WHERE code = %s", body.occupancy):
        raise HTTPException(422, f"沒有這個場所類別：{body.occupancy}")
    for d, lo, hi, what in ((body.ceiling_height, 0.5, 100, "天花板高度"), (body.floor_area, 1, 1_000_000, "樓地板面積")):
        for k, v in d.items():
            if not FLOOR_LABEL.match(k) or not (lo <= v <= hi):
                raise HTTPException(422, f"{what}格式不符：{k} = {v}")
    if any(not FLOOR_LABEL.match(k) for k in body.no_opening):
        raise HTTPException(422, "無開口樓層代號格式不符")
    from .review.checks import DEFAULT_POLICY
    if bad := [k for k in body.policy if k not in DEFAULT_POLICY]:
        raise HTTPException(422, f"沒有這個法規解讀設定：{bad[0][:40]}")
    ctx = body.model_dump()
    with pool.connection() as c:
        DS.save_context(c, case_id, ctx, user["username"])
        n = DS.requeue_reviews(c, case_id)
    return {"saved": True, "requeued": n}


class DecisionBody(BaseModel):
    key: str = Field(pattern=r"^[0-9a-f]{12}$")
    decision: str | None = Field(None, pattern=r"^(accept|reject)$")
    note: str | None = Field(None, max_length=500)


@app.post("/api/cases/{case_id}/files/{file_id}/decisions")
def cases_decide(case_id: int, file_id: int, body: DecisionBody, user: dict = Depends(current_user)):
    if not _one("SELECT 1 AS ok FROM case_file WHERE id = %s AND case_id = %s", file_id, case_id):
        raise HTTPException(404, "沒有這個檔案")
    with pool.connection() as c:
        DS.decide(c, file_id, body.key, body.decision, (body.note or "").strip() or None, user["username"])
    return {"ok": True}


def _report_inputs(case_id: int):
    case = _case_or_404(case_id)
    b = _review_bundle(case_id)
    occ = {r["code"]: r["text"] for r in _all("SELECT code, text FROM occupancy_code")}
    svgs = {}
    root = CASES_DIR.resolve()
    for r in b["reviews"]:
        for fl in (r["result"] or {}).get("floors", []):
            if r.get("svg_dir") and REVIEW_LABEL.match(_svg_name(fl) or ""):
                p = (Path(r["svg_dir"]) / f"{_svg_name(fl)}.svg").resolve()
                if root in p.parents and p.is_file():
                    svgs[(r["file_id"], _svg_name(fl))] = p.read_text(encoding="utf-8")
    return case, b, occ, svgs


@app.get("/api/cases/{case_id}/report", include_in_schema=False)
def cases_report(case_id: int, user: dict = Depends(current_user)):
    from .review import report as RP
    case, b, occ, svgs = _report_inputs(case_id)
    html = RP.build_html(case, b["context"], occ, b["reviews"], b["decisions"], b["laws"], svgs, user["username"])
    return HTMLResponse(html, headers={"Cache-Control": "private, no-store"})


@app.get("/api/cases/{case_id}/report.csv", include_in_schema=False)
def cases_report_csv(case_id: int, user: dict = Depends(current_user)):
    from .review import report as RP
    case, b, _occ, _svgs = _report_inputs(case_id)
    data = RP.build_csv(case, b["reviews"], b["decisions"], b["laws"])
    return Response(data.encode("utf-8"), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"case-{case_id}-findings.csv\"", "Cache-Control": "private, no-store"})


@app.get("/api/cases/{case_id}/files/{file_id}/review/{label}.svg", include_in_schema=False)
def cases_review_svg(case_id: int, file_id: int, label: str, user: dict = Depends(current_user)):
    r = _one("SELECT r.svg_dir FROM file_review r JOIN case_file f ON f.id = r.file_id "
             "WHERE r.file_id = %s AND f.case_id = %s", file_id, case_id)
    if not REVIEW_LABEL.match(label) or not r or not r["svg_dir"]:
        raise HTTPException(404, "沒有這張標示圖")
    p = (Path(r["svg_dir"]) / f"{label}.svg").resolve()
    if CASES_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "沒有這張標示圖")
    return FileResponse(p, media_type="image/svg+xml", headers={
        "Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
