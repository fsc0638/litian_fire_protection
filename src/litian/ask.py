"""法規問答：把檢索到的條文當成文件送給 Claude 並開啟引用，回答只能引用這些條文。

防幻覺做法（設計文件 §8.5）：
- 每條檢索結果是一份 document，開 citations；API 保證引用只會指向送進去的文件，
  document_index 再由程式對回 node_id。
- 回答裡提到的條號若不在這次的條文（含條文內文提到的條號）中，程式另外標示「未經檢索」。
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from .lawdb.numerals import to_int

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_DOC_CHARS = 3000
QUESTION_MAX = 300

SYSTEM_PROMPT = """你是台灣的消防法規查詢助理，使用者是消防設備師、消防設備士與繪圖人員。

回答規則：
1. 只能根據這次提供的法規文件回答，每個論點都要引用文件。文件沒有涵蓋的，直接說「這次查到的條文沒有涵蓋這一點」，建議換個問法或指明條號；不要用自己記得的法規補充。
2. 條文怎麼寫就怎麼轉述，條件要完整（樓層、面積、場所類別、收容人數等）；不要自行推論、換算或補上門檻。
3. 是否應設置、屬於哪一類場所，只轉述條文；若需要更多資訊才能判斷（用途、樓層、面積等），說出缺哪些資訊，並提醒最後由消防設備師判斷。
4. 文件註明「草稿」「未經校對」或「以官方原文為準」時，回答要轉達這個提醒。
5. 用繁體中文，精簡、白話：先給結論，再列依據。可用短段落與「- 」條列；不要用表格、不要用標題。
6. 提到條文時，用文件標題的寫法，例如「設置標準第12條第1款第1目」。"""


def clip(text: str, n: int = MAX_DOC_CHARS) -> str:
    return text if len(text) <= n else text[:n] + "……（以下略，請看原文）"


def build_documents(sources: list[dict]) -> list[dict]:
    """每條檢索結果一份 document（custom content：不再切句，引用以整條節點為單位）。"""
    docs = []
    for s in sources:
        blocks = [{"type": "text", "text": clip(s["text"])}]
        if s.get("table_text"):
            status = (s.get("table") or {}).get("status")
            label = "結構化表格" if status == "verified" else "結構化表格（開發者轉錄草稿，未經消防設備師校對，判定以官方原文為準）"
            blocks.append({"type": "text", "text": f"【{label}】\n{clip(s['table_text'])}"})
        ctx = [f"法規：{s['law_name']}（修正日 {s.get('law_modified') or '未知'}）", f"節點編號：{s['node_id']}"]
        if s.get("chapter"):
            ctx.append(f"位置：{s['chapter']}")
        if s.get("parents"):
            ctx.append("上層條文：" + " ＞ ".join(s["parents"]))
        for w in ("table_warning", "warning"):
            if s.get(w):
                ctx.append("注意：" + s[w])
        docs.append({"type": "document",
                     "source": {"type": "content", "content": blocks},
                     "title": s["citation"][:200],
                     "context": "\n".join(ctx),
                     "citations": {"enabled": True}})
    return docs


def build_messages(question: str, sources: list[dict]) -> list[dict]:
    return [{"role": "user", "content": build_documents(sources) + [{"type": "text", "text": "問題：" + question}]}]


ARTICLE_RE = re.compile(r"第\s*([0-9０-９]+|[零〇一二三四五六七八九十百千]+)\s*條(?:\s*之\s*([0-9０-９]+|[零〇一二三四五六七八九十]+))?")


def mentioned_articles(text: str) -> set[str]:
    """抓出「第十二條」「第22條之1」這類條號，統一成 '12'、'22-1'。"""
    out = set()
    for m in ARTICLE_RE.finditer(text):
        try:
            a = str(to_int(m.group(1)))
            if m.group(2):
                a += f"-{to_int(m.group(2))}"
        except ValueError:
            continue
        out.add(a)
    return out


def unverified_mentions(answer: str, sources: list[dict]) -> list[str]:
    """回答提到、但這次檢索的條文（含其內文、上層條文）都沒有出現的條號。"""
    allowed: set[str] = set()
    for s in sources:
        allowed.add(s["node_id"].split("/")[1])
        for t in [s.get("text", ""), s.get("table_text", ""), *s.get("parents", [])]:
            allowed |= mentioned_articles(t or "")
    return sorted(mentioned_articles(answer) - allowed, key=lambda a: [int(x) for x in a.split("-")])


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_answer(client, question: str, sources: list[dict]) -> AsyncIterator[tuple[str, dict]]:
    """呼叫 Claude（串流），產生 (事件名, 資料)：block／text／cite／done。cite 的 n 是來源編號（從 1 起）。"""
    stream = await client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=build_messages(question, sources),
        stream=True,
    )
    parts: list[str] = []
    stop_reason = None
    usage = {"input_tokens": None, "output_tokens": None}
    async for ev in stream:
        if ev.type == "message_start":
            usage["input_tokens"] = ev.message.usage.input_tokens
        elif ev.type == "content_block_start" and ev.content_block.type == "text":
            yield "block", {"index": ev.index}
        elif ev.type == "content_block_delta":
            d = ev.delta
            if d.type == "text_delta":
                parts.append(d.text)
                yield "text", {"index": ev.index, "text": d.text}
            elif d.type == "citations_delta":
                c = d.citation
                i = getattr(c, "document_index", None)
                if isinstance(i, int) and 0 <= i < len(sources):
                    yield "cite", {"index": ev.index, "n": i + 1, "node_id": sources[i]["node_id"],
                                   "cited_text": getattr(c, "cited_text", "")}
        elif ev.type == "message_delta":
            stop_reason = ev.delta.stop_reason
            usage["output_tokens"] = ev.usage.output_tokens
    yield "done", {"stop_reason": stop_reason, "unverified": unverified_mentions("".join(parts), sources), "usage": usage}
