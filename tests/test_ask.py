"""法規問答：文件組裝、條號查核、串流事件轉換、端點的存取碼與限流。

不連資料庫、不呼叫真的 Claude：檢索與 Claude 用假物件代替（TestClient 不進 lifespan，不會開資料庫連線池）。
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


def test_build_documents_shape():
    docs = A.build_documents(SOURCES)
    assert len(docs) == 2
    d0, d1 = docs
    assert d0["type"] == "document" and d0["citations"] == {"enabled": True}
    assert d0["source"]["type"] == "content"
    assert d0["source"]["content"] == [{"type": "text", "text": SOURCES[0]["text"]}]
    assert d0["title"] == "設置標準第17條第1項第1款"
    assert "節點編號：D0120029/17/1/1" in d0["context"] and "上層條文：下列場所" in d0["context"]
    assert len(d1["source"]["content"]) == 2
    assert "草稿" in d1["source"]["content"][1]["text"]          # 未校對的表格要標明
    assert "注意：本條表格為草稿" in d1["context"]
    msgs = A.build_messages("KTV要不要裝撒水", SOURCES)
    assert msgs[0]["role"] == "user" and msgs[0]["content"][-1] == {"type": "text", "text": "問題：KTV要不要裝撒水"}


def test_clip_long_text():
    assert A.clip("短") == "短"
    long = "字" * (A.MAX_DOC_CHARS + 10)
    assert A.clip(long).endswith("（以下略，請看原文）") and len(A.clip(long)) < len(long) + 20


def test_mentioned_articles():
    assert A.mentioned_articles("依第十二條、第22條之1及第 157 條；第二十二條之一") == {"12", "22-1", "157"}


def test_unverified_mentions_allows_articles_quoted_in_sources():
    # 第 12 條出現在第 17 條的內文裡，不算未經檢索；第 30 條完全沒出現
    ans = "依第17條第1項第1款，供第12條第1款第1目場所使用者應設置；另見第30條。"
    assert A.unverified_mentions(ans, SOURCES) == ["30"]


def test_sse_format():
    s = A.sse("text", {"text": "撒水"})
    assert s == 'event: text\ndata: {"text": "撒水"}\n\n'


class FakeMessages:
    def __init__(self, events):
        self.events, self.kwargs = events, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        events = self.events

        async def gen():
            for e in events:
                yield e
        return gen()


def fake_events():
    return [
        NS(type="message_start", message=NS(usage=NS(input_tokens=1200))),
        NS(type="content_block_start", index=0, content_block=NS(type="thinking")),
        NS(type="content_block_delta", index=0, delta=NS(type="thinking_delta", thinking="")),
        NS(type="content_block_start", index=1, content_block=NS(type="text")),
        NS(type="content_block_delta", index=1, delta=NS(type="text_delta", text="KTV 屬甲類，樓地板面積合計三百平方公尺以上要設。")),
        NS(type="content_block_delta", index=1, delta=NS(type="citations_delta", citation=NS(
            type="content_block_location", cited_text="一、十層以下…", document_index=0))),
        NS(type="content_block_delta", index=1, delta=NS(type="citations_delta", citation=NS(
            type="content_block_location", cited_text="越界", document_index=9))),   # 不在來源內：忽略
        NS(type="content_block_start", index=2, content_block=NS(type="text")),
        NS(type="content_block_delta", index=2, delta=NS(type="text_delta", text="另依第30條……")),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=88)),
        NS(type="message_stop"),
    ]


def test_stream_answer_maps_citations_to_nodes():
    msgs = FakeMessages(fake_events())
    client = NS(messages=msgs)

    async def collect():
        return [x async for x in A.stream_answer(client, "KTV要不要裝撒水", SOURCES)]
    out = asyncio.run(collect())
    names = [n for n, _ in out]
    assert names == ["block", "text", "cite", "block", "text", "done"]
    cite = out[2][1]
    assert cite == {"index": 1, "n": 1, "node_id": "D0120029/17/1/1", "cited_text": "一、十層以下…"}
    done = out[-1][1]
    assert done["stop_reason"] == "end_turn" and done["unverified"] == ["30"]
    assert done["usage"] == {"input_tokens": 1200, "output_tokens": 88}
    kw = msgs.kwargs
    assert kw["model"] == A.MODEL and kw["stream"] is True and kw["system"] == A.SYSTEM_PROMPT
    assert "temperature" not in kw and "thinking" not in kw          # Opus 5.5：不送 temperature／thinking
    assert len(kw["messages"][0]["content"]) == len(SOURCES) + 1


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
    monkeypatch.setattr(api, "_client", lambda: NS(messages=FakeMessages(fake_events())))
    api._daily.update(day="", count=0)
    api._recent.clear()
    for k in ("ANTHROPIC_API_KEY", "ASK_ACCESS_CODE", "ASK_DAILY_LIMIT"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(api.app)


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


def test_key_without_access_code_stays_disabled(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    st = client.get("/api/law/ask/status").json()
    assert st["ai_enabled"] is False and "存取碼" in st["message"]


def test_ask_requires_access_code(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", "fire-code-2026")
    assert client.post("/api/law/ask", json={"question": "KTV"}).status_code == 401
    assert client.post("/api/law/ask", json={"question": "KTV"}, headers={"X-Access-Code": "wrong"}).status_code == 401
    r = client.post("/api/law/ask", json={"question": "KTV要不要裝撒水"}, headers={"X-Access-Code": "fire-code-2026"})
    assert r.status_code == 200
    evs = parse_sse(r.text)
    assert [e for e, _ in evs] == ["sources", "block", "text", "cite", "block", "text", "done"]
    assert "usage" not in evs[-1][1]                                   # 用量只記在主機日誌，不給前端


def test_ask_rate_limits(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", "fire-code-2026")
    h = {"X-Access-Code": "fire-code-2026", "X-Forwarded-For": "203.0.113.5"}
    codes = [client.post("/api/law/ask", json={"question": "KTV"}, headers=h).status_code for _ in range(api.ASK_PER_MINUTE + 1)]
    assert codes[:-1] == [200] * api.ASK_PER_MINUTE and codes[-1] == 429
    other = {"X-Access-Code": "fire-code-2026", "X-Forwarded-For": "203.0.113.9"}
    assert client.post("/api/law/ask", json={"question": "KTV"}, headers=other).status_code == 200


def test_ask_daily_limit(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", "fire-code-2026")
    monkeypatch.setenv("ASK_DAILY_LIMIT", "2")
    codes = [client.post("/api/law/ask", json={"question": "KTV"},
                         headers={"X-Access-Code": "fire-code-2026", "X-Forwarded-For": f"198.51.100.{i}"}).status_code
             for i in range(3)]
    assert codes == [200, 200, 429]


def test_question_length_limit(client):
    assert client.post("/api/law/ask", json={"question": "字" * (A.QUESTION_MAX + 1)}).status_code == 422
    assert client.post("/api/law/ask", json={"question": "   "}).status_code == 422


def test_short_access_code_keeps_ai_disabled(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", "short")
    st = client.get("/api/law/ask/status").json()
    assert st["ai_enabled"] is False and "太短" in st["message"]


def test_access_code_bruteforce_lockout(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", "fire-code-2026")
    api._fails.clear()
    h = {"X-Forwarded-For": "192.0.2.7"}
    codes = [client.post("/api/law/ask", json={"question": "KTV"}, headers={**h, "X-Access-Code": f"guess{i}"}).status_code
             for i in range(api.CODE_FAIL_MAX)]
    assert codes == [401] * api.CODE_FAIL_MAX
    # 鎖定後，連正確的存取碼也先擋下
    r = client.post("/api/law/ask", json={"question": "KTV"}, headers={**h, "X-Access-Code": "fire-code-2026"})
    assert r.status_code == 429
    api._fails.clear()


def test_midstream_disconnect_sends_error_event(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ASK_ACCESS_CODE", "fire-code-2026")

    class Broken(FakeMessages):
        async def create(self, **kwargs):
            async def gen():
                for e in fake_events()[:5]:
                    yield e
                raise ConnectionResetError("上游中途斷線")   # SDK 1.x 串流中斷時丟的是底層連線錯誤，不是 APIError
            return gen()

    monkeypatch.setattr(api, "_client", lambda: NS(messages=Broken([])))
    r = client.post("/api/law/ask", json={"question": "KTV"}, headers={"X-Access-Code": "fire-code-2026"})
    evs = parse_sse(r.text)
    assert [e for e, _ in evs] == ["sources", "block", "text", "error"]
