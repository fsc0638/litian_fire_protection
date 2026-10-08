"""消防圖審系統 API（第 0 期：法規庫與法規問答網頁；第 1 期：審核工作台）。

環境變數：DATABASE_URL、MEILI_URL、MEILI_MASTER_KEY
  法規問答（選填）：OPENAI_API_KEY（有才啟用 AI 回答，模型 gpt-5.6-sol）、ASK_ACCESS_CODE（選填，設定後才要求存取碼）、ASK_DAILY_LIMIT（每日 AI 問答上限，預設 500；台北時間每天 23:59 重新計算，計數存在資料庫）
  磁碟（選填）：UPLOAD_MIN_FREE_GB（剩餘空間扣掉這次上傳大小的兩倍後低於此數就拒收上傳，預設 3）、DISK_WARN_GB（低於此數時工作台提醒管理者，預設 8）；
    正式環境的 docker-compose.yml 沒有傳入這兩個，用預設值
啟動：uvicorn litian.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import openai
from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import ask as A
from . import auth as AU
from . import line_login as LL
from .drawing import store as DS
from .drawing import xref as XR
from .drawing.cli import safe_name
from .lawdb import boxtable as BT
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
UPLOAD_PATH = re.compile(r"/api/cases/\d+/files")


class MultipartOnlyForUpload:
    """附檔案的表單（multipart）只有上傳端點收。有表單欄位的端點，框架都會先把附的檔案存進暫存區才執行程式，
    不必登入的 POST /api/auth/line/start 也一樣，可被拿來塞滿磁碟；在讀內容前就拒收。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not UPLOAD_PATH.fullmatch(scope["path"]):
            ctype = next((v for k, v in scope["headers"] if k == b"content-type"), b"")
            if ctype.lower().startswith(b"multipart/"):
                await JSONResponse({"detail": "這個網址不接受上傳檔案"}, status_code=415)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(MultipartOnlyForUpload)


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


@lru_cache(maxsize=1024)
def _blocks(text: str | None) -> list[dict] | None:
    """條文有方框字元（表格，或公式的根號線）時切成文字／表格區塊，前端照區塊畫（表格畫成真表格、其餘原文照排，
    不必在前端猜方框字元從哪裡開始）；沒有方框字元回 None。每次請求都會用到，快取起來。"""
    return BT.blocks(text) if text and any(c in BT.BOX for c in text) else None


def _present(n: dict, laws: dict, with_article: bool = True) -> dict:
    law = laws[n["pcode"]]
    out = {"node_id": n["node_id"], "citation": n["citation"], "law_name": law["name"], "law_modified": law["modified"],
           "level": n["level"], "chapter": n["chapter"], "text": n["text"]}
    if b := _blocks(n["text"]):
        out["blocks"] = b
    art = n if n["level"] == "article" else _node(f'{n["pcode"]}/{n["article"]}')
    if with_article and n["level"] != "article":
        out["article_text"] = art["text"]
        if b := _blocks(art["text"]):
            out["article_blocks"] = b
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
GB = 1024 ** 3
UPLOAD_REQUEST_MAX = 1 * GB           # 一次上傳（整個請求）的上限：讀內容前看 Content-Length 擋掉，讀的時候也邊讀邊數
UPLOAD_MIN_FREE_GB = float(os.environ.get("UPLOAD_MIN_FREE_GB", "3"))   # 磁碟滿了資料庫寫不進去，整個網站會停擺
DISK_WARN_GB = float(os.environ.get("DISK_WARN_GB", "8"))
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
LOGIN_ERROR_RE = re.compile(r"[A-Za-z_]{1,40}")
# 不因登入失敗封鎖來源 IP：state、邀請權杖都是 256 位元亂數，沒有猜測空間；而整間辦公室共用一個對外 IP，
# 失敗封鎖反而會讓一個網頁（偷放 20 張圖片打回呼網址）就把全部同仁鎖在外面。
# 發起登入（每次存一筆暫存）只接受使用者真的點進來的頁面導覽，別的網站用圖片觸發不算；另有每個來源的寬鬆上限與全站上限。
LOGIN_START_MAX = 60                  # 同一來源 10 分鐘內發起登入的上限（正常使用遠低於此）
_login_starts: dict[str, deque] = {}


def _start_allowed(request: Request) -> str | None:
    """發起登入的請求檢查；不行時回錯誤代碼。"""
    site = request.headers.get("sec-fetch-site")
    dest = request.headers.get("sec-fetch-dest")
    if (site and site not in ("same-origin", "none")) or (dest and dest != "document"):
        return "expired"                                     # 別的網站觸發（圖片、iframe、跨站導覽）：不建立暫存
    now, f = time.monotonic(), _login_starts.setdefault(_client_ip(request), deque())
    while f and now - f[0] > 600:
        f.popleft()
    if len(f) >= LOGIN_START_MAX:
        return "slow_down"
    f.append(now)
    return None


def _line_cfg() -> LL.Config | None:
    try:
        return LL.Config.from_env()
    except ValueError as e:
        log.warning("line login config invalid: %s", e)
        return None


def current_user(session: str | None = Cookie(None, alias=AU.COOKIE)) -> dict:
    with pool.connection() as c:
        u = AU.session_user(c, session)
    if not u:
        raise HTTPException(401, "請先登入")
    return u


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "只有管理者可以使用帳號管理")
    return user


def _to_workbench(error: str | None = None) -> RedirectResponse:
    """登入流程結束一律回工作台；錯誤只帶代碼（訊息由頁面對應）。"""
    return RedirectResponse("/workbench" + (f"?login_error={error}" if error else ""), status_code=303, headers=NO_STORE)


def _line_start(request: Request, invite: str | None, no_auto: bool, browser: str | None) -> RedirectResponse:
    cfg = _line_cfg()
    if not cfg:
        return _to_workbench("not_configured")
    host = request.headers.get("host", "")
    if host and host != urlsplit(cfg.callback_url).netloc:
        # 從別的網址（舊網域、IP）進來：登入暫存 Cookie 會留在那個網址，LINE 卻回到登記的網址 → 必定失敗。先轉到正式網址
        log.warning("line login started from host %s, callback host is %s", host[:80], urlsplit(cfg.callback_url).netloc)
        return RedirectResponse(cfg.base_url + "/workbench", status_code=303, headers=NO_STORE)
    if (refused := _start_allowed(request)):
        log.info("line login start refused: %s client=%s", refused, _ip_tag(request))
        return _to_workbench(refused)
    invite_id = None
    # 同一瀏覽器已有登入暫存 Cookie 就沿用（兩個分頁同時登入時，兩個都能完成）
    browser = browser if browser and 20 <= len(browser) <= 100 else secrets.token_urlsafe(32)
    try:
        with pool.connection() as c:
            if invite is not None:
                inv = AU.invite_info(c, invite)
                if not inv:
                    return _to_workbench("invite_invalid")
                invite_id = inv["id"]
            state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)                # PKCE：86 字元（規定 43～128）
            AU.save_login_state(c, state, browser, nonce, verifier, invite_id)
    except AU.AuthError as e:
        log.warning("line login start refused: %s", e.code)
        return _to_workbench(e.code)
    r = RedirectResponse(LL.authorize_url(cfg, state, nonce, verifier, no_auto_login=no_auto), status_code=303,
                         headers=NO_STORE)
    r.set_cookie(AU.LOGIN_COOKIE, browser, max_age=AU.STATE_MINUTES * 60, httponly=True, secure=True,
                 samesite="lax", path="/")
    return r


@app.get("/api/auth/line/start")
def auth_line_start(request: Request, noauto: str | None = None,
                    login_cookie: str | None = Cookie(None, alias=AU.LOGIN_COOKIE)):
    """「用 LINE 登入」：轉到 LINE 授權頁。noauto=1：自動登入失敗後重試（不自動登入，改顯示登入畫面）。"""
    return _line_start(request, None, noauto == "1", login_cookie)


@app.post("/api/auth/line/start")
def auth_line_start_invite(request: Request, invite: str = Form(..., max_length=200), noauto: str | None = Form(None),
                           login_cookie: str | None = Cookie(None, alias=AU.LOGIN_COOKIE)):
    """邀請頁的「用 LINE 登入並開通」：權杖放表單內容，不放網址。
    第一次照常（手機上 LINE 一鍵自動登入）；自動登入失敗回來時頁面改送 noauto=1（LINE 官方建議的重試方式）。
    不一律關掉自動登入：手機上沒有 LINE 網頁登入紀錄的人會被要求輸入 LINE 的 email 密碼，多數人沒設定。"""
    return _line_start(request, invite, noauto == "1", login_cookie)


def _ip_tag(request: Request) -> str:
    return _client_tag(request)[:12]                          # 日誌只記雜湊，不記原始 IP


@app.get("/api/auth/line/callback")
def auth_line_callback(request: Request, code: str | None = Query(None, max_length=512),
                       state: str | None = Query(None, max_length=200), error: str | None = Query(None, max_length=64),
                       login_cookie: str | None = Cookie(None, alias=AU.LOGIN_COOKIE)):
    """LINE 授權後回到這裡：核對 state 與發起登入的瀏覽器 → 換 token → 驗 ID token → 開通或登入。"""
    tag = _ip_tag(request)
    cfg = _line_cfg()
    if not cfg:
        return _to_workbench("not_configured")
    with pool.connection() as c:
        st = AU.take_login_state(c, state, login_cookie)       # 只能用一次
    if error:
        err = error.upper() if LOGIN_ERROR_RE.fullmatch(error) else "OTHER"
        log.info("line login error=%s client=%s", err, tag)
        return _to_workbench("denied" if err == "ACCESS_DENIED" else "line_error")
    if not st or not code:
        # 逾時、換了瀏覽器、LINE 自動登入失敗（官方文件：此時 state 會不符），或偽造的回呼
        log.info("line login state mismatch client=%s cookie=%s", tag, bool(login_cookie))
        return _to_workbench("expired")
    try:
        id_token = LL.exchange_code(cfg, code, st["verifier"])
        claims = LL.verify_id_token(cfg, id_token, st["nonce"])
    except LL.LineError as e:
        log.warning("line login failed client=%s transient=%s: %s", tag, e.transient, e)
        return _to_workbench("line_unavailable" if e.transient else "line_error")
    except Exception:                                           # 任何意外都回工作台，不給 500
        log.exception("line login unexpected error client=%s", tag)
        return _to_workbench("line_error")
    try:
        with pool.connection() as c:
            token, user = AU.line_login(c, claims["sub"], claims["name"], st["invite_id"])
    except AU.AuthError as e:
        log.info("line login refused code=%s client=%s", e.code, tag)
        return _to_workbench(e.code)
    except Exception:
        log.exception("line login database error client=%s", tag)
        return _to_workbench("line_error")
    log.info("line login user_id=%s invite=%s", user["id"], st["invite_id"] is not None)
    r = _to_workbench()
    r.set_cookie(AU.COOKIE, token, max_age=AU.SESSION_HOURS * 3600, httponly=True, secure=True, samesite="lax", path="/")
    r.delete_cookie(AU.LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return r


class InviteCheck(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@app.post("/api/auth/invite")
def auth_invite(body: InviteCheck):
    """邀請頁：顯示這張邀請開通哪個帳號。"""
    with pool.connection() as c:
        inv = AU.invite_info(c, body.token)
    if not inv:
        raise HTTPException(404, "這條邀請連結已使用、已作廢或已過期。已開通的同仁請直接用 LINE 登入；還沒開通請向管理者索取新的連結")
    return {"username": inv["username"], "kind": inv["kind"], "role": inv["role"], "expires_at": inv["expires_at"],
            "line_login": _line_cfg() is not None}


@app.get("/api/auth/options")
def auth_options():
    return {"line_login": _line_cfg() is not None}


@app.post("/api/auth/logout")
def auth_logout(response: Response, session: str | None = Cookie(None, alias=AU.COOKIE)):
    with pool.connection() as c:
        AU.logout(c, session)
    response.delete_cookie(AU.COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(current_user)):
    if user["role"] != "admin":
        return user
    free = _disk_free_gb()                      # 管理者：磁碟快滿時工作台顯示提醒
    return {**user, "disk_free_gb": None if free is None else round(free, 1),
            "disk_low": free is not None and free < DISK_WARN_GB}


# ---- 帳號管理（管理者）：發邀請、停用／啟用、改角色 ----

class InviteCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    role: str = Field("reviewer", pattern=r"^(reviewer|admin)$")
    hours: int = Field(AU.INVITE_HOURS, ge=1, le=AU.INVITE_MAX_HOURS)
    mode: str = Field("new", pattern=r"^(new|rebind)$")        # new：開新帳號；rebind：既有帳號重新綁定 LINE


class UserPatch(BaseModel):
    disabled: bool | None = None
    role: str | None = Field(None, pattern=r"^(reviewer|admin)$")


@app.get("/api/admin/users")
def admin_users(user: dict = Depends(require_admin)):
    with pool.connection() as c:
        return {"users": AU.list_users(c), "invites": AU.pending_invites(c), "line_login": _line_cfg() is not None}


@app.post("/api/admin/invites")
def admin_invite(body: InviteCreate, user: dict = Depends(require_admin)):
    cfg = _line_cfg()
    if not cfg:
        raise HTTPException(409, "LINE 登入尚未設定，邀請連結無法使用（請先依部署說明第 4b 項設定 LINE Login）")
    if body.mode == "rebind" and body.username.strip() == user["username"]:
        raise HTTPException(422, "不能在網頁上替自己重新綁定 LINE（請由另一位管理者操作，或在主機上用命令列）")
    try:
        with pool.connection() as c:
            token, inv = AU.create_invite(c, body.username, body.role, user["username"], body.hours,
                                          mode=body.mode, created_by_id=user["id"])
    except AU.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    log.info("invite created by_id=%s kind=%s", user["id"], inv["kind"])
    return {**inv, "url": AU.invite_link(cfg.base_url, token)}


@app.delete("/api/admin/invites/{invite_id}")
def admin_revoke(invite_id: int, user: dict = Depends(require_admin)):
    with pool.connection() as c:
        if not AU.revoke_invite(c, invite_id):
            raise HTTPException(404, "沒有這張待開通的邀請")
    return {"ok": True}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserPatch, user: dict = Depends(require_admin)):
    if user_id == user["id"] and (body.disabled or (body.role and body.role != "admin")):
        raise HTTPException(422, "不能停用自己或取消自己的管理者身分（請由另一位管理者操作）")
    try:
        with pool.connection() as c:
            u = AU.update_user(c, user_id, disabled=body.disabled, role=body.role)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    log.info("user updated by_id=%s target_id=%s disabled=%s role=%s", user["id"], u["id"], u["disabled"], u["role"])
    return u


@app.get("/workbench", include_in_schema=False)
def workbench():
    # 邀請連結帶權杖：不送 Referer；不允許被別的網站嵌入（帳號管理按鈕防點擊劫持）
    return HTMLResponse(WEB_WORKBENCH.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY",
                                 "X-Content-Type-Options": "nosniff"})


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


def _disk_free_gb() -> float | None:
    """案件資料夾與上傳暫存區（tempfile 的預設資料夾）較少的那邊還剩幾 GB；都查不到時回 None。"""
    free = []
    for p in (CASES_DIR, Path(tempfile.gettempdir())):
        try:
            free.append(shutil.disk_usage(p).free / GB)
        except OSError:                          # 資料夾還不存在（本機開發）
            pass
    return min(free) if free else None


@app.post("/api/cases/{case_id}/files")
async def cases_upload(case_id: int, request: Request, user: dict = Depends(current_user)):
    # 不用 File(...) 參數：FastAPI 會先把整個上傳內容存到暫存區，才檢查登入與大小。
    # 改成登入（current_user）、案件、整個請求大小、磁碟空間都過了，才開始讀內容
    _case_or_404(case_id)
    too_big = f"一次上傳合計超過 {UPLOAD_REQUEST_MAX / GB:g} GB，請分批上傳"
    cl = request.headers.get("content-length", "")
    # 瀏覽器一定會帶大小；沒帶的（chunked）空間照上限算，讀的時候再邊讀邊數
    length = int(cl) if cl.isascii() and cl.isdigit() else UPLOAD_REQUEST_MAX
    if length > UPLOAD_REQUEST_MAX:
        raise HTTPException(413, too_big)
    free = _disk_free_gb()
    # 上傳內容先整份存進暫存區、再複製到案件資料夾，兩處通常在同一顆磁碟：照兩倍算
    if free is not None and free - 2 * length / GB < UPLOAD_MIN_FREE_GB:
        log.warning("upload refused: disk free %.1f GB, request %.1f MB", free, length / 1024 / 1024)
        raise HTTPException(507, f"伺服器磁碟空間不足（剩 {free:.1f} GB），暫時不能上傳。請通知管理者清理空間後再試")
    got = 0

    async def receive():                     # 邊讀邊數：超過上限就停，不再往暫存區寫
        nonlocal got
        msg = await request.receive()
        got += len(msg.get("body", b""))
        if got > UPLOAD_REQUEST_MAX:
            raise HTTPException(413, too_big)
        return msg

    async with Request(request.scope, receive).form() as form:
        files = [f for f in form.getlist("files") if not isinstance(f, str)]
        if not files:
            raise HTTPException(422, "沒有收到檔案，請重新選擇檔案上傳")
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


NO_FLOOR_NOTE = "沒有認出樓層平面圖，未檢核：圖框的圖名要寫出樓層（例如「一層消防平面圖」）"


def _xref_hosts(files: list[dict], stored: dict[int, str]) -> dict[int, list[dict]]:
    """被同案件別的檔當外部參考併入的檔：檔案 id → 主圖（id、檔名）。stored：檔案 id → 案件資料夾裡的存檔名（001_Area_1F.dwg）。
    主圖 stats.xref.bound_files 記了綁進來的存檔名；舊資料只有圖塊名（通常＝參考檔的檔名主體），照綁定時的規則比對
    （檔名去掉上傳序號、不分大小寫；這個規則改成「同名取最新」之前綁的舊資料，綁的是存檔名排最前的）。"""
    cad = sorted((stored[f["id"]], f["id"]) for f in files if f.get("kind") in ("dwg", "dxf") and stored.get(f["id"]))
    stems: dict[str, list[int]] = {}                   # 檔名主體 → 存檔名排序的檔案 id（綁定時同名取最前的）
    for name, fid in cad:
        stems.setdefault(XR.UPLOAD_PREFIX.sub("", Path(name).stem).lower(), []).append(fid)
    out: dict[int, list[dict]] = {}
    for m in files:
        x = (m.get("stats") or {}).get("xref") or {}
        if isinstance(x.get("bound_files"), list):
            want = {str(n).lower() for n in x["bound_files"]}
            hit = [fid for name, fid in cad if name.lower() in want]
        else:
            keys = {k for n in x.get("bound") or [] for k in (safe_name(str(n)).lower(), XR.ref_key(str(n)))}
            hit = [next(i for i in stems[k] if i != m["id"]) for k in keys if any(i != m["id"] for i in stems.get(k, []))]
        for fid in hit:
            if fid != m["id"]:
                out.setdefault(fid, []).append({"id": m["id"], "name": m["name"]})
    return out


def _file_note(f: dict, r: dict, hosts: list[dict], info: dict[int, dict] | None = None) -> str | None:
    """檔案處理狀態的白話說明；處理中的不寫。"""
    if f["status"] == "failed":
        return f.get("error") or "處理失敗"
    if f["status"] == "skipped":
        return "不支援的檔案類型，已略過" + ("（PDF 尚未支援）" if f.get("kind") == "pdf" else "")
    if f["status"] != "done":
        return None
    if r.get("review") == "failed":
        return f"檢核失敗：{r.get('review_error') or '原因不明'}"
    if r.get("review") == "done":
        n, m = r.get("floors") or 0, r.get("findings") or 0
        return f"已檢核 {n} 層，缺失 {m} 條" if n else "已檢核，但沒有認出樓層平面圖" + (f"；全棟缺失 {m} 條" if m else "")
    if hosts:
        names = "、".join(f"「{h['name']}」" for h in hosts)
        if any(((info or {}).get(h["id"]) or {}).get("review") == "done" for h in hosts):
            return f"建築底圖（外部參考），已併入{names}一起檢核"
        return f"建築底圖（外部參考），已併入{names}，但主圖沒有完成檢核（原因見主圖的說明）"
    return NO_FLOOR_NOTE


def _same_name(f: dict, files: list[dict], stored: dict[int, str]):
    """同案件裡檔名相同（去掉上傳序號、不分大小寫）的其他 CAD 底圖：（較新上傳且處理完成的, 較早上傳的）。
    同名主圖（綁進過別的同名上傳檔）不算底圖：自己是主圖時不比，別的主圖也不算進來。"""
    me = stored.get(f["id"])
    if not me or f.get("kind") not in ("dwg", "dxf") or XR.main_version(me, (f.get("stats") or {}).get("xref")):
        return [], []
    key, no = XR.name_key(me), XR.upload_no(me)
    same = [g for g in files if g["id"] != f["id"] and g.get("kind") in ("dwg", "dxf") and stored.get(g["id"])
            and XR.name_key(stored[g["id"]]) == key and not XR.main_version(stored[g["id"]], (g.get("stats") or {}).get("xref"))]
    newer = [g for g in same if XR.upload_no(stored[g["id"]]) > no and g["status"] == "done"]
    older = [g for g in same if XR.upload_no(stored[g["id"]]) < no]
    return newer, older


def _file_notes(files: list[dict], info: dict[int, dict]) -> list[dict]:
    """每個檔加上 note（白話說明）、review（檢核狀態，沒檢核過為 None）、xref_of（被哪個主圖當外部參考併入）、
    superseded（同名底圖重新上傳後，舊的這份不再使用）、xref_warn（重新上傳的底圖讀不了，主圖改用較早的）。info：store.file_reviews 的結果（檔案 id → 存檔路徑與檢核摘要）。
    同名底圖重新上傳：主圖重新處理（自動排入）前，新的那份寫「重新處理後改用這份」；之後舊的寫「不再使用」。"""
    stored = {i: Path(r["path"]).name for i, r in info.items() if r.get("path")}
    hosts = _xref_hosts(files, stored)
    by_id = {f["id"]: f for f in files}
    for f in files:
        r = info.get(f["id"]) or {}
        f["review"] = r.get("review")
        f["xref_of"] = ((hosts.get(f["id"]) or [{}])[0]).get("name")
        f["superseded"] = f["xref_warn"] = False
        f["note"] = _file_note(f, r, hosts.get(f["id"]) or [], info)
        if f["status"] != "done" or r.get("review") or hosts.get(f["id"]):
            continue
        newer, older = _same_name(f, files, stored)
        if newer:
            f["superseded"] = True
            f["note"] = "已有較新上傳的同名檔，這份不再使用"
            continue
        used = [g for g in older if hosts.get(g["id"])]
        mains = [h for g in used for h in hosts[g["id"]]]
        names = "、".join(dict.fromkeys(f"「{h['name']}」" for h in mains))
        me = (stored.get(f["id"]) or "").lower()
        bad = [x for g in files for x in ((g.get("stats") or {}).get("xref") or {}).get("failed") or []
               if XR.strip_note(x).lower() == me and not str(x).endswith("（尚未轉檔）")]
        if bad:                                            # 主圖綁定時試過、讀不了：要使用者處理，不淡化
            f["xref_warn"] = True
            why = bad[0][len(XR.strip_note(bad[0])):].strip("（）") or "原因不明"
            f["note"] = f"這份讀不了（{why}）" + (f"，{names}改用較早上傳的同名檔" if mains else "，主圖沒有用到") + \
                "；請確認檔案後重新上傳"
            continue
        if not mains:
            continue
        main = [by_id.get(h["id"]) or {} for h in mains]
        f["xref_of"] = mains[0]["name"]
        if all(r.get("sha256") and r.get("sha256") == (info.get(g["id"]) or {}).get("sha256") for g in used):
            f["note"] = f"內容與{names}已併入的同名檔相同，照用原本那份"
        elif any(m.get("status") in ("queued", "processing", "reviewing") for m in main):
            f["note"] = f"較新上傳的建築底圖：{names}重新處理中，完成後改用這份"
        else:
            f["note"] = f"較新上傳的建築底圖：{names}重新處理後改用這份"
    return files


@app.get("/api/cases/{case_id}")
def cases_detail(case_id: int, user: dict = Depends(current_user)):
    case = _case_or_404(case_id)
    with pool.connection() as c:
        files = DS.case_status(c, case_id)
        info = {r["id"]: r for r in DS.file_reviews(c, case_id)}
    sheets = _all("SELECT s.id, s.file_id, s.idx, s.number, s.title, s.scale, s.unit FROM case_sheet s "
                  "JOIN case_file f ON f.id = s.file_id WHERE f.case_id = %s ORDER BY s.number NULLS LAST, s.id", case_id)
    return {"case": case, "files": _file_notes(files, info), "sheets": sheets}


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


def _cad_status(svg_dir: str | None) -> tuple[Path, dict] | None:
    """檢核資料夾裡的 cad/status.json（沒有或壞掉當空的）；資料夾不在案件資料夾內回 None。"""
    if not svg_dir:
        return None
    d = (Path(svg_dir) / "cad").resolve()
    if CASES_DIR.resolve() not in d.parents:
        return None
    try:
        st = json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    return d, st if isinstance(st, dict) else {}


def _cad_state(cad: tuple[Path, dict] | None, name: str, queued: str | None = None) -> str | None:
    """樓層的 CAD 原樣圖：done＝圖磚可用、pending＝排隊中、rendering＝產生中、failed＝失敗；沒產生過回 None。
    整檔狀態以資料庫為準（queued＝case_file.cad_state），各樓層畫好沒有看狀態檔（開始畫時會清掉上一輪的）。
    排隊中一律不拿舊圖磚（重新處理後圖可能變了）；資料庫沒在排隊、狀態檔卻停在排隊或畫圖中的是過時的，不算。"""
    if not REVIEW_LABEL.fullmatch(name or ""):
        return None
    d, st = cad if cad else (None, {})
    sheets = st.get("sheets") if isinstance(st.get("sheets"), dict) else {}
    has_meta = d is not None and (d / name / "meta.json").is_file()
    if queued == "pending":
        return "pending"
    if sheets.get(name) == "done" and has_meta:
        return "done"
    if sheets.get(name) == "failed":
        return "failed"
    if queued == "rendering":
        return "rendering"
    if queued == "failed" or st.get("state") == "failed":
        return "failed"
    if st.get("state") in ("pending", "rendering"):      # 重新處理中（資料庫已取消排隊）：上一輪的圖磚不算
        return None
    return "done" if has_meta else None


def _cited_laws(ids) -> dict:
    """引用條文（工作台的依據浮窗）：條號、全文、表格區塊。引用的是「下列…：」這類引導句的項或款時，
    本身文字不含底下各款，把子孫節點依條文順序接上；整條（article）的文字本來就是全文。
    所屬條文的表格只在官方 PDF 時附上警語與連結（與法規問答頁相同）。"""
    rows = _all("SELECT n.node_id, n.citation, n.text, n.level, a.pdf_table_url FROM law_node n "
                "LEFT JOIN law_node a ON a.node_id = n.pcode || '/' || n.article "
                "WHERE n.node_id = ANY(%s)", sorted(ids))
    parts = [r["node_id"] for r in rows if r.get("level") != "article"]
    kids: dict[str, list[str]] = {}
    if parts:
        for d in _all("SELECT node_id, text FROM law_node WHERE node_id LIKE ANY(%s) ORDER BY seq",
                      [p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%" for p in parts]):
            for p in parts:
                if d["node_id"].startswith(p + "/") and d["text"]:
                    kids.setdefault(p, []).append(d["text"])
    out = {}
    for r in rows:
        text = "\n".join([r["text"] or ""] + kids.get(r["node_id"], []))
        out[r["node_id"]] = {"citation": r["citation"], "text": text}
        if b := _blocks(text):
            out[r["node_id"]]["blocks"] = b
        if r.get("pdf_table_url"):
            out[r["node_id"]]["warning"] = "本條的表格只在官方「完整條文」PDF 中，網頁與 API 文字不完整，請以 PDF 為準"
            out[r["node_id"]]["pdf_table_url"] = r["pdf_table_url"]
    return out


def _review_bundle(case_id: int) -> dict:
    """檢核結果＋引用條文＋審核結果＋檢核條件（工作台與報告共用）。"""
    rows = _all("SELECT r.file_id, f.name, r.status, r.error, r.result, r.svg_dir, r.created_at, f.cad_state FROM file_review r "
                "JOIN case_file f ON f.id = r.file_id WHERE f.case_id = %s ORDER BY f.name, f.id", case_id)
    ids = set()
    for r in rows:
        res = r["result"] or {}
        b = res.get("building") or {}
        for item in b.get("findings", []) + b.get("requirements", []) + b.get("notes", []):
            ids.update(item["law"])
        cad = _cad_status(r.get("svg_dir")) if res.get("floors") else None
        for fl in res.get("floors", []):
            fl["svg"] = f"/api/cases/{case_id}/files/{r['file_id']}/review/{_svg_name(fl)}.svg"
            fl["cad"] = _cad_state(cad, _svg_name(fl), r.get("cad_state"))
            for item in fl["findings"] + fl["notes"]:
                ids.update(item["law"])
    laws = _cited_laws(ids) if ids else {}
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
        r.pop("cad_state", None)
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
    """報告用的資料：檢核結果、各樓層的簡化標示圖（SVG），以及原圖已畫好的樓層的原圖資訊
    （列印圖網址、meta.json 的尺寸與座標換算、缺失疊圖資料）；原圖沒好的樓層報告照用簡化圖。"""
    case = _case_or_404(case_id)
    b = _review_bundle(case_id)
    occ = {r["code"]: r["text"] for r in _all("SELECT code, text FROM occupancy_code")}
    svgs, cads = {}, {}
    root = CASES_DIR.resolve()

    def inside(p: Path) -> Path | None:
        p = p.resolve()
        return p if root in p.parents and p.is_file() else None

    for r in b["reviews"]:
        for fl in (r["result"] or {}).get("floors", []):
            name = _svg_name(fl)
            if not r.get("svg_dir") or not REVIEW_LABEL.fullmatch(name or ""):
                continue
            base = Path(r["svg_dir"])
            if p := inside(base / f"{name}.svg"):
                svgs[(r["file_id"], name)] = p.read_text(encoding="utf-8")
            mp, op = inside(base / "cad" / name / "meta.json"), inside(base / f"{name}.overlay.json")
            if fl.get("cad") != "done" or not mp or not op:
                continue
            try:
                meta, ov = json.loads(mp.read_text(encoding="utf-8")), json.loads(op.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            v = re.sub(r"[^0-9A-Za-z:+.-]", "", str(meta.get("rendered_at") or ""))
            cads[(r["file_id"], name)] = {"src": f"/api/cases/{case_id}/files/{r['file_id']}/cad/{name}/print.png?v={v}",
                                          "meta": meta, "overlay": ov}
    return case, b, occ, svgs, cads


@app.get("/api/cases/{case_id}/report", include_in_schema=False)
def cases_report(case_id: int, user: dict = Depends(current_user)):
    from .review import report as RP
    case, b, occ, svgs, cads = _report_inputs(case_id)
    html = RP.build_html(case, b["context"], occ, b["reviews"], b["decisions"], b["laws"], svgs, user["username"], cads=cads)
    return HTMLResponse(html, headers={"Cache-Control": "private, no-store"})


@app.get("/api/cases/{case_id}/report.csv", include_in_schema=False)
def cases_report_csv(case_id: int, user: dict = Depends(current_user)):
    from .review import report as RP
    case, b, _occ, _svgs, _cads = _report_inputs(case_id)
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


# ---------- CAD 原樣圖（圖磚）與缺失疊圖：檢核資料夾裡的 cad/<圖名>/ 與 <圖名>.overlay.json ----------
CAD_TILE = re.compile(r"(\d{1,2})/(\d{1,5})_(\d{1,5})\.png")       # 層級/欄_列.png，只收整數
JSON_HEADERS = {"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"}


def _review_file(case_id: int, file_id: int, name: str, *parts: str) -> Path:
    """檢核資料夾裡的檔案；圖名格式不符、沒有檢核結果、解析後不在案件資料夾內或不存在，一律 404。"""
    if not REVIEW_LABEL.fullmatch(name):
        raise HTTPException(404, "沒有這張圖")
    r = _one("SELECT r.svg_dir FROM file_review r JOIN case_file f ON f.id = r.file_id "
             "WHERE r.file_id = %s AND f.case_id = %s", file_id, case_id)
    if not r or not r["svg_dir"]:
        raise HTTPException(404, "沒有這張圖")
    p = Path(r["svg_dir"]).joinpath(*parts).resolve()
    if CASES_DIR.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "沒有這張圖")
    return p


@app.get("/api/cases/{case_id}/files/{file_id}/cad/{name}/meta.json", include_in_schema=False)
def cases_cad_meta(case_id: int, file_id: int, name: str, user: dict = Depends(current_user)):
    """圖磚資訊：尺寸、層級、公尺座標 → 像素的換算。"""
    return FileResponse(_review_file(case_id, file_id, name, "cad", name, "meta.json"),
                        media_type="application/json", headers=JSON_HEADERS)


@app.get("/api/cases/{case_id}/files/{file_id}/cad/{name}/print.png", include_in_schema=False)
def cases_cad_print(case_id: int, file_id: int, name: str, user: dict = Depends(current_user)):
    """報告列印用的整張原圖：從圖磚拼回（第一次要幾秒，之後用快取）。"""
    from .review import cadview as CV
    meta = _review_file(case_id, file_id, name, "cad", name, "meta.json")
    try:
        p = CV.print_image(meta.parent)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("cad print image failed file=%s name=%s: %s", file_id, name, e)
        raise HTTPException(404, "原圖還沒準備好")
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.get("/api/cases/{case_id}/files/{file_id}/cad/{name}/{level}/{tile}", include_in_schema=False)
def cases_cad_tile(case_id: int, file_id: int, name: str, level: str, tile: str, user: dict = Depends(current_user)):
    m = CAD_TILE.fullmatch(f"{level}/{tile}")
    if not m:
        raise HTTPException(404, "沒有這張圖")
    lv, col, row = (int(x) for x in m.groups())
    p = _review_file(case_id, file_id, name, "cad", name, str(lv), f"{col}_{row}.png")
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.get("/api/cases/{case_id}/files/{file_id}/review/{name}.overlay.json", include_in_schema=False)
def cases_review_overlay(case_id: int, file_id: int, name: str, user: dict = Depends(current_user)):
    """缺失疊圖：各缺失的範圍（GeoJSON，公尺）與標號位置。"""
    return FileResponse(_review_file(case_id, file_id, name, f"{name}.overlay.json"),
                        media_type="application/json", headers=JSON_HEADERS)
