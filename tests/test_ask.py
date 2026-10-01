"""法規問答：輸入組裝、引用編號與條號查核、串流事件轉換、端點的存取碼與限流。

不連資料庫、不呼叫真的 OpenAI：檢索與 OpenAI 用假物件代替（TestClient 不進 lifespan，不會開資料庫連線池）。
"""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from litian import api
from litian import ask as A

SOURCES = [
    {"node_id": "D0120029/17/1/1", "citation": "設置標準第17條第1項第1款", "law_name": "各類場所消防安全設備設置標準",
     "law_modified": "20240424", "chapter": "第二編 消防設計",
     "text": "一、十層以下建築物之樓層，供第十二條第一款第一目所列場所使用，樓地板面積合計在三百平方公尺以上者。",
     "parents": ["下列場所或樓層應設置自動撒水設備："]},
    {"node_id": "D0120029/157/1", "citation": "設置標準第157條", "law_name": "各類場所消防安全設備設置標準",
     "law_modified": "20240424", "chapter": "", "text": "避難器具，依下表選擇設置之：",
     "table": {"node_id": "D0120029/157", "status": "draft"}, "table_text": "第三層：避難梯、緩降機",
     "table_warning": "本條表格為草稿"},
]
CODE = "fire-code-2026"


def test_build_input_numbers_sources():
    inp = A.build_input("KTV要不要裝撒水", SOURCES)
    assert inp[0]["role"] == "user" and inp[0]["content"][0]["type"] == "input_text"
    text = inp[0]["content"][0]["text"]
    assert text.index("[1] 設置標準第17條第1項第1款") < text.index("[2] 設置標準第157條")
    assert "節點編號 D0120029/17/1/1" in text and "上層條文：下列場所" in text
    assert "草稿" in text and "注意：本條表格為草稿" in text          # 未校對的表格要標明
    assert text.endswith("問題：KTV要不要裝撒水")


def test_clip_long_text():
    assert A.clip("短") == "短"
    long = "字" * (A.MAX_DOC_CHARS + 10)
    assert A.clip(long).endswith("（以下略，請看原文）") and len(A.clip(long)) < len(long) + 20


def test_cite_numbers():
    assert A.cite_numbers("應設置[1]。另見[2,3]與[3]、[1，4]；2.5 公尺不是引用") == [1, 2, 3, 4]


def test_mentioned_articles():
    assert A.mentioned_articles("依第十二條、第22條之1及第 157 條；第二十二條之一") == {"12", "22-1", "157"}


def test_unverified_mentions_allows_articles_quoted_in_sources():
    # 第 12 條出現在第 17 條的內文裡，不算未經檢索；第 30 條完全沒出現
    ans = "依第17條第1項第1款，供第12條第1款第1目場所使用者應設置；另見第30條。"
    assert A.unverified_mentions(ans, SOURCES) == ["30"]


def test_sse_format():
    assert A.sse("text", {"text": "撒水"}) == 'event: text\ndata: {"text": "撒水"}\n\n'


def completed(status="completed", reason=None, usage=(1200, 300)):
    return NS(status=status, incomplete_details=NS(reason=reason) if reason else None,
              usage=NS(input_tokens=usage[0], output_tokens=usage[1]), error=None)


def fake_events(final="response.completed", resp=None):
    return [
        NS(type="response.created", response=NS(status="in_progress")),
        NS(type="response.output_text.delta", delta="KTV 屬甲類，樓地板面積合計三百平方公尺以上要設["),
        NS(type="response.output_text.delta", delta="1]。另依第30條[9]……"),      # 編號被切在兩段之間
        NS(type=final, response=resp or completed()),
    ]


class FakeResponses:
    def __init__(self, events):
        self.events, self.kwargs = events, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        events = self.events

        async def gen():
            for e in events:
                yield e
        return gen()


def run(client, sources=SOURCES):
    async def collect():
        return [x async for x in A.stream_answer(client, "KTV要不要裝撒水", sources)]
    return asyncio.run(collect())


def test_stream_answer_events_and_checks():
    resp = FakeResponses(fake_events())
    out = run(NS(responses=resp))
    assert [n for n, _ in out] == ["block", "text", "text", "done"]
    done = out[-1][1]
    assert done["stop_reason"] == "end_turn"
    assert done["cited"] == [1] and done["invalid_cites"] == [9]
    assert done["unverified"] == ["30"]
    assert done["usage"] == {"input_tokens": 1200, "output_tokens": 300}
    kw = resp.kwargs
    assert kw["model"] == "gpt-5.6-sol" and kw["stream"] is True and kw["store"] is False
    assert kw["instructions"] == A.SYSTEM_PROMPT and kw["reasoning"] == {"effort": "medium"}
    assert kw["max_output_tokens"] == A.MAX_OUTPUT_TOKENS and "temperature" not in kw


def test_stream_answer_incomplete_max_tokens():
    out = run(NS(responses=FakeResponses(fake_events("response.incomplete", completed("incomplete", "max_output_tokens")))))
    assert out[-1][1]["stop_reason"] == "max_tokens"


def test_stream_answer_refusal():
    events = [NS(type="response.refusal.delta", delta="我無法協助。"),
              NS(type="response.completed", response=completed())]
    out = run(NS(responses=FakeResponses(events)))
    assert [n for n, _ in out] == ["block", "done"] and out[-1][1]["stop_reason"] == "refusal"


def test_stream_answer_failed_raises():
    events = [NS(type="response.failed", response=NS(error=NS(message="server_error")))]
    with pytest.raises(A.AnswerFailed):
        run(NS(responses=FakeResponses(events)))


# ---------------- 端點 ----------------

def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for raw in text.strip().split("\n\n"):
        ev = data = None
        for line in raw.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((ev, data))
    return out


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "_ask_sources", lambda q: [dict(s) for s in SOURCES])
    monkeypatch.setattr(api, "_client", lambda: NS(responses=FakeResponses(fake_events())))
    usage: dict[str, int] = {}

    def fake_bump(day, limit):
        if limit <= 0 or usage.get(day, 0) >= limit:
            return False
        usage[day] = usage.get(day, 0) + 1
        return True
    monkeypatch.setattr(api, "_bump_daily", fake_bump)
    monkeypatch.setattr(api, "_used_today", lambda: usage.get(api._usage_day(), 0))
    api._fails.clear()
    for k in ("OPENAI_API_KEY", "ASK_ACCESS_CODE", "ASK_DAILY_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(api.app)


def enable_ai(monkeypatch, code=CODE):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", code)


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "消防法規問答" in r.text and "text/html" in r.headers["content-type"]


def test_ask_without_ai_returns_sources_only(client):
    st = client.get("/api/law/ask/status").json()
    assert st["ai_enabled"] is False and "金鑰" in st["message"]
    r = client.post("/api/law/ask", json={"question": "KTV要不要裝撒水"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(r.text)
    assert [e for e, _ in evs] == ["sources", "notice", "done"]
    assert evs[0][1]["sources"][0]["node_id"] == "D0120029/17/1/1"


def test_key_only_enables_ai_without_access_code(client, monkeypatch):
    """2026-10-01 使用者決定先不用存取碼、也不設每分鐘限制：只有金鑰就啟用，只受每日上限。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    st = client.get("/api/law/ask/status").json()
    assert st["ai_enabled"] is True and st["access_code_required"] is False
    r = client.post("/api/law/ask", json={"question": "KTV要不要裝撒水"})
    assert r.status_code == 200
    assert [e for e, _ in parse_sse(r.text)] == ["sources", "block", "text", "text", "done"]
    assert client.get("/api/law/ask/status").json()["used_today"] == 1
    # 同一來源連問 10 次也不會被擋（沒有每分鐘限制）
    h = {"X-Forwarded-For": "203.0.113.77"}
    assert {client.post("/api/law/ask", json={"question": "KTV"}, headers=h).status_code for _ in range(10)} == {200}
    assert client.get("/api/law/ask/status").json()["used_today"] == 11


def test_short_access_code_keeps_ai_disabled(client, monkeypatch):
    enable_ai(monkeypatch, "short")
    st = client.get("/api/law/ask/status").json()
    assert st["ai_enabled"] is False and "太短" in st["message"]


def test_status_reports_model(client, monkeypatch):
    enable_ai(monkeypatch)
    st = client.get("/api/law/ask/status").json()
    assert st["ai_enabled"] is True and st["model"] == "gpt-5.6-sol" and st["access_code_required"] is True


def test_ask_requires_access_code(client, monkeypatch, caplog):
    caplog.set_level("INFO", logger="litian.ask")
    enable_ai(monkeypatch)
    assert client.post("/api/law/ask", json={"question": "KTV"}).status_code == 401
    assert client.post("/api/law/ask", json={"question": "KTV"}, headers={"X-Access-Code": "wrong"}).status_code == 401
    r = client.post("/api/law/ask", json={"question": "KTV要不要裝撒水"}, headers={"X-Access-Code": CODE})
    assert r.status_code == 200
    evs = parse_sse(r.text)
    assert [e for e, _ in evs] == ["sources", "block", "text", "text", "done"]
    assert "usage" not in evs[-1][1]                                   # 用量只記在主機日誌，不給前端
    assert evs[-1][1]["invalid_cites"] == [9]
    assert "ask done" in caplog.text and "'input_tokens': 1200" in caplog.text


def test_ask_daily_limit(client, monkeypatch):
    enable_ai(monkeypatch)
    monkeypatch.setenv("ASK_DAILY_LIMIT", "2")
    codes = [client.post("/api/law/ask", json={"question": "KTV"},
                         headers={"X-Access-Code": CODE, "X-Forwarded-For": f"198.51.100.{i}"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]
    r = client.post("/api/law/ask", json={"question": "KTV"}, headers={"X-Access-Code": CODE})
    assert "23:59" in r.json()["detail"]


def test_daily_limit_default_500(monkeypatch):
    monkeypatch.delenv("ASK_DAILY_LIMIT", raising=False)
    assert api._daily_limit() == 500
    monkeypatch.setenv("ASK_DAILY_LIMIT", "abc")
    assert api._daily_limit() == 500


def test_usage_day_rolls_over_at_2359_taipei():
    from datetime import datetime
    assert api._usage_day(datetime(2026, 10, 1, 23, 58, 59, tzinfo=api.TW)) == "2026-10-01"
    assert api._usage_day(datetime(2026, 10, 1, 23, 59, 0, tzinfo=api.TW)) == "2026-10-02"
    assert api._usage_day(datetime(2026, 10, 2, 0, 30, tzinfo=api.TW)) == "2026-10-02"


def test_access_code_bruteforce_lockout(client, monkeypatch):
    enable_ai(monkeypatch)
    h = {"X-Forwarded-For": "192.0.2.7"}
    codes = [client.post("/api/law/ask", json={"question": "KTV"}, headers={**h, "X-Access-Code": f"guess{i}"}).status_code
             for i in range(api.CODE_FAIL_MAX)]
    assert codes == [401] * api.CODE_FAIL_MAX
    # 鎖定後，連正確的存取碼也先擋下
    r = client.post("/api/law/ask", json={"question": "KTV"}, headers={**h, "X-Access-Code": CODE})
    assert r.status_code == 429


def test_question_length_limit(client):
    assert client.post("/api/law/ask", json={"question": "字" * (A.QUESTION_MAX + 1)}).status_code == 422
    assert client.post("/api/law/ask", json={"question": "   "}).status_code == 422


def test_midstream_disconnect_sends_error_event(client, monkeypatch):
    enable_ai(monkeypatch)

    class Broken(FakeResponses):
        async def create(self, **kwargs):
            async def gen():
                for e in fake_events()[:2]:
                    yield e
                raise ConnectionResetError("上游中途斷線")   # SDK 串流中斷時可能丟底層連線錯誤
            return gen()

    monkeypatch.setattr(api, "_client", lambda: NS(responses=Broken([])))
    evs = parse_sse(client.post("/api/law/ask", json={"question": "KTV"}, headers={"X-Access-Code": CODE}).text)
    assert [e for e, _ in evs] == ["sources", "block", "text", "error"]


def test_response_failed_sends_error_event(client, monkeypatch):
    enable_ai(monkeypatch)
    events = [NS(type="response.failed", response=NS(error=NS(message="server_error")))]
    monkeypatch.setattr(api, "_client", lambda: NS(responses=FakeResponses(events)))
    evs = parse_sse(client.post("/api/law/ask", json={"question": "KTV"}, headers={"X-Access-Code": CODE}).text)
    assert [e for e, _ in evs] == ["sources", "block", "error"]


# ---------------- 場所自動帶入第 12 條分類條文 ----------------

OCC_ROWS = [
    {"code": "甲-1", "node_id": "D0120029/12/1/1/1",
     "text": "電影片映演場所（戲院、電影院）、歌廳、舞廳、夜總會、俱樂部、視聽歌唱場所（KTV等）、酒家、酒吧"},
    {"code": "甲-3", "node_id": "D0120029/12/1/1/3", "text": "觀光旅館、飯店、旅館、招待所（限有寢室客房者）"},
    {"code": "甲-5", "node_id": "D0120029/12/1/1/5", "text": "餐廳、飲食店、咖啡廳、茶藝館"},
]
NODES = {
    "D0120029/12": {"node_id": "D0120029/12", "pcode": "D0120029", "article": "12", "level": "article", "path": [],
                    "text": "各類場所按用途分類如下：……", "parent_id": None, "citation": "設置標準第12條", "chapter": "第二編",
                    "has_table": False, "pdf_table_url": None, "deleted": False, "children": 1},
    "D0120029/12/1": {"node_id": "D0120029/12/1", "pcode": "D0120029", "article": "12", "level": "paragraph", "path": [1],
                      "text": "各類場所按用途分類如下：", "parent_id": "D0120029/12", "citation": "設置標準第12條",
                      "chapter": "第二編", "has_table": False, "pdf_table_url": None, "deleted": False, "children": 1},
    "D0120029/12/1/1": {"node_id": "D0120029/12/1/1", "pcode": "D0120029", "article": "12", "level": "item", "path": [1, 1],
                        "text": "一、甲類場所：", "parent_id": "D0120029/12/1", "citation": "設置標準第12條第1款",
                        "chapter": "第二編", "has_table": False, "pdf_table_url": None, "deleted": False, "children": 7},
    "D0120029/12/1/1/1": {"node_id": "D0120029/12/1/1/1", "pcode": "D0120029", "article": "12", "level": "subitem",
                          "path": [1, 1, 1], "text": OCC_ROWS[0]["text"], "parent_id": "D0120029/12/1/1",
                          "citation": "設置標準第12條第1款第1目", "chapter": "第二編", "has_table": False,
                          "pdf_table_url": None, "deleted": False, "children": 0},
    "D0120029/17/1/1": {"node_id": "D0120029/17/1/1", "pcode": "D0120029", "article": "17", "level": "item",
                        "path": [1, 1], "text": SOURCES[0]["text"], "parent_id": None,
                        "citation": "設置標準第17條第1項第1款", "chapter": "第二編", "has_table": False,
                        "pdf_table_url": None, "deleted": False, "children": 0},
}


@pytest.fixture
def fake_db(monkeypatch):
    monkeypatch.setattr(api, "_all", lambda sql, *a: OCC_ROWS if "occupancy_code" in sql else [])
    monkeypatch.setattr(api, "_one", lambda sql, *a: None)
    monkeypatch.setattr(api, "_node", lambda nid: NODES.get(nid))
    monkeypatch.setattr(api, "_law_names", lambda: {"D0120029": {"name": "各類場所消防安全設備設置標準", "modified": "20240424"}})


def test_place_nodes_detects_specific_places(fake_db):
    assert api._place_nodes("KTV 要不要裝自動撒水設備？") == ["D0120029/12/1/1/1"]
    assert api._place_nodes("旅館和餐廳要設什麼") == ["D0120029/12/1/1/3", "D0120029/12/1/1/5"]
    assert api._place_nodes("撒水頭間距多少") == []


def test_ask_sources_pins_classification_first(fake_db, monkeypatch):
    retrieved = [{"node_id": "D0120029/17/1/1", "citation": "設置標準第17條第1項第1款", "text": SOURCES[0]["text"]}]
    monkeypatch.setattr(api, "_retrieve", lambda q, limit: [dict(r) for r in retrieved])
    out = api._ask_sources("KTV 要不要裝自動撒水設備？")
    assert [r["node_id"] for r in out] == ["D0120029/12/1/1/1", "D0120029/17/1/1"]
    assert out[0]["routes"] == ["place"]
    assert out[0]["parents"] == ["各類場所按用途分類如下：", "一、甲類場所："]


def test_ask_sources_no_duplicate_when_already_retrieved(fake_db, monkeypatch):
    retrieved = [{"node_id": "D0120029/12/1/1/1", "citation": "設置標準第12條第1款第1目", "text": OCC_ROWS[0]["text"]}]
    monkeypatch.setattr(api, "_retrieve", lambda q, limit: [dict(r) for r in retrieved])
    assert [r["node_id"] for r in api._ask_sources("KTV 屬於哪一類")] == ["D0120029/12/1/1/1"]


# ---------------- 測試期提問紀錄 ----------------

@pytest.fixture
def logs(monkeypatch):
    saved: list[dict] = []
    monkeypatch.setattr(api, "_save_log", lambda rec: saved.append(dict(rec)))
    return saved


def test_log_records_ai_answer(client, monkeypatch, logs):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client.post("/api/law/ask", json={"question": "KTV要不要裝撒水"}, headers={"X-Forwarded-For": "203.0.113.5"})
    rec = logs[-1]
    assert rec["mode"] == "ai" and rec["question"] == "KTV要不要裝撒水"
    assert rec["answer"] == "KTV 屬甲類，樓地板面積合計三百平方公尺以上要設[1]。另依第30條[9]……"
    assert rec["cited"] == [1] and rec["invalid_cites"] == [9] and rec["unverified"] == ["30"]
    assert rec["usage"] == {"input_tokens": 1200, "output_tokens": 300} and rec["stop_reason"] == "end_turn"
    assert [s["node_id"] for s in rec["sources"]] == ["D0120029/17/1/1", "D0120029/157/1"] and rec["sources"][0]["n"] == 1
    assert len(rec["client"]) == 12 and "203.0.113.5" not in json.dumps(rec, ensure_ascii=False)   # 不存 IP
    assert rec.get("error") is None and isinstance(rec["duration_ms"], int)


def test_log_records_search_only(client, logs):
    client.post("/api/law/ask", json={"question": "旅館屬於哪一類"})
    rec = logs[-1]
    assert rec["mode"] == "search_only" and rec["answer"] is None and rec.get("error") is None
    assert len(rec["sources"]) == 2


def test_log_records_rejected(client, monkeypatch, logs):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_DAILY_LIMIT", "1")
    client.post("/api/law/ask", json={"question": "第一題"})
    r = client.post("/api/law/ask", json={"question": "第二題"})
    assert r.status_code == 429
    rec = logs[-1]
    assert rec["mode"] == "rejected" and rec["question"] == "第二題" and rec["error"].startswith("429")


def test_log_records_midstream_error(client, monkeypatch, logs):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class Broken(FakeResponses):
        async def create(self, **kwargs):
            async def gen():
                for e in fake_events()[:2]:
                    yield e
                raise ConnectionResetError("上游中途斷線")
            return gen()

    monkeypatch.setattr(api, "_client", lambda: NS(responses=Broken([])))
    client.post("/api/law/ask", json={"question": "KTV"})
    rec = logs[-1]
    assert rec["mode"] == "ai" and rec["error"].startswith("ConnectionResetError")
    assert rec["answer"] == "KTV 屬甲類，樓地板面積合計三百平方公尺以上要設["


def test_ask_sources_adds_whole_article_when_fragments_cluster(fake_db, monkeypatch):
    """同一條抓到 3 個以上款目時補整條原文（例：免設排煙條件 → 第 190 條）。"""
    frag = lambda nid: {"node_id": nid, "citation": nid, "text": "片段", "level": "subitem"}
    NODES.update({
        "D0120029/190": {"node_id": "D0120029/190", "pcode": "D0120029", "article": "190", "level": "article",
                         "path": [], "text": "下列處所得免設排煙設備：……（整條）", "parent_id": None,
                         "citation": "設置標準第190條", "chapter": "", "has_table": False, "pdf_table_url": None,
                         "deleted": False, "children": 1},
    })
    retrieved = [frag("D0120029/190/1/1/1"), frag("D0120029/190/1/2/1"), frag("D0120029/190/1/3")]
    monkeypatch.setattr(api, "_retrieve", lambda q, limit: [dict(r) for r in retrieved])
    out = api._ask_sources("免設排煙條件")
    assert [r["node_id"] for r in out][-1] == "D0120029/190" and out[-1]["routes"] == ["article_context"]
    retrieved.pop()
    assert "D0120029/190" not in [r["node_id"] for r in api._ask_sources("免設排煙條件")]   # 只有 2 個片段不補
