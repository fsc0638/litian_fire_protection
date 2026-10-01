"""法規問答：把檢索到的條文編號後交給 OpenAI GPT-5.6 Sol，回答只能引用這些條文。

防幻覺做法（設計文件 §8.5）：
- 條文以 [1]、[2]… 編號放進輸入，要求模型在每個論點後標註編號。
- OpenAI 沒有 API 層級的引用保證，所以由程式檢查：編號超出範圍者列為「無效引用」；
  回答提到的條號若不在這次的條文（含條文內文、上層條文）中，列為「未經檢索」。前端兩者都會警告。
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from .lawdb.numerals import to_int

MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "medium"      # GPT-5.6 預設值；法規問答以檢索為主，不需更高
MAX_OUTPUT_TOKENS = 16000        # 含推理 token
MAX_DOC_CHARS = 3000
QUESTION_MAX = 300

SYSTEM_PROMPT = """你是台灣的消防法規查詢助理，使用者是消防設備師、消防設備士與繪圖人員。
使用者訊息會附上這次檢索到的法規條文，每條以 [編號] 開頭。

回答規則：
1. 只能根據這些條文回答。每個論點後面緊接著標註依據的條文編號，例如「……應設置自動撒水設備[1]」；多條時寫成 [1][3]。只能使用附上的編號，不可自行編造。
2. 條文沒有涵蓋的，直接說「這次查到的條文沒有涵蓋這一點」，建議換個問法或指明條號；不要用自己記得的法規補充。
3. 條文怎麼寫就怎麼轉述，條件要完整（樓層、面積、場所類別、收容人數等）；不要自行推論、換算或補上門檻。
4. 保留條文的適用對象與限定詞，不可擴大：但書與「……之舞臺」「……之樓層」「限……者」這類限定，只適用於條文寫明的對象，不能說成適用於整個場所或其他情形。例如條文規定某類場所「之舞臺」應設開放式，就只能說舞臺要設開放式，不能說整個場所要設。拿不準是否適用時，引用原文並說明適用對象。
5. 是否應設置、屬於哪一類場所，只轉述條文；若需要更多資訊才能判斷（用途、樓層、面積等），說出缺哪些資訊，並提醒最後由消防設備師判斷。
6. 條文註明「草稿」「未經校對」或「以官方原文為準」時，回答要轉達這個提醒。
7. 用繁體中文，精簡、白話：先給結論，再列依據。可用短段落與「- 」條列；不要用表格、不要用標題。
8. 提到條文時用條文標題的寫法，例如「設置標準第12條第1款第1目」。
9. 送出前逐句核對：每個論點的主詞、適用對象與條件，都要和所標編號的條文原文一致；不一致就改寫或刪掉。"""


def clip(text: str, n: int = MAX_DOC_CHARS) -> str:
    return text if len(text) <= n else text[:n] + "……（以下略，請看原文）"


def source_block(n: int, s: dict) -> str:
    """一條檢索結果的文字：編號與標題、出處、上層條文、注意事項、內文、結構化表格。"""
    lines = [f"[{n}] {s['citation']}",
             f"出處：{s['law_name']}（修正日 {s.get('law_modified') or '未知'}）；節點編號 {s['node_id']}"]
    if s.get("chapter"):
        lines.append(f"位置：{s['chapter']}")
    if s.get("parents"):
        lines.append("上層條文：" + " ＞ ".join(s["parents"]))
    for w in ("table_warning", "warning"):
        if s.get(w):
            lines.append("注意：" + s[w])
    lines.append("內文：" + clip(s["text"]))
    if s.get("table_text"):
        status = (s.get("table") or {}).get("status")
        label = "結構化表格" if status == "verified" else "結構化表格（開發者轉錄草稿，未經消防設備師校對，判定以官方原文為準）"
        lines.append(f"{label}：\n{clip(s['table_text'])}")
    return "\n".join(lines)


def build_input(question: str, sources: list[dict]) -> list[dict]:
    body = "以下是這次檢索到的法規條文：\n\n" + "\n\n".join(source_block(i + 1, s) for i, s in enumerate(sources))
    return [{"role": "user", "content": [{"type": "input_text", "text": f"{body}\n\n問題：{question}"}]}]


CITE_RE = re.compile(r"\[(\d+(?:\s*[,，、]\s*\d+)*)\]")


def cite_numbers(text: str) -> list[int]:
    """回答裡的 [1]、[2,3] 引用編號（依出現順序、不重複）。"""
    out: list[int] = []
    for m in CITE_RE.finditer(text):
        for x in re.split(r"\s*[,，、]\s*", m.group(1)):
            n = int(x)
            if n not in out:
                out.append(n)
    return out


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


class AnswerFailed(Exception):
    """OpenAI 回報回應失敗（response.failed 或 error 事件）。"""


async def stream_answer(client, question: str, sources: list[dict]) -> AsyncIterator[tuple[str, dict]]:
    """呼叫 OpenAI Responses API（串流），產生 (事件名, 資料)：block／text／done。"""
    stream = await client.responses.create(
        model=MODEL,
        instructions=SYSTEM_PROMPT,
        input=build_input(question, sources),
        reasoning={"effort": REASONING_EFFORT},
        max_output_tokens=MAX_OUTPUT_TOKENS,
        store=False,            # 不在 OpenAI 端保存這次對話
        stream=True,
    )
    parts: list[str] = []
    refusal: list[str] = []
    status, reason = None, None
    usage = {"input_tokens": None, "output_tokens": None}
    yield "block", {"index": 0}
    async for ev in stream:
        t = ev.type
        if t == "response.output_text.delta":
            parts.append(ev.delta)
            yield "text", {"index": 0, "text": ev.delta}
        elif t == "response.refusal.delta":
            refusal.append(ev.delta)
        elif t in ("response.completed", "response.incomplete"):
            r = ev.response
            status = r.status
            reason = r.incomplete_details.reason if r.incomplete_details else None
            if r.usage:
                usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
        elif t == "response.failed":
            err = ev.response.error
            raise AnswerFailed(err.message if err else "response.failed")
        elif t == "error":
            raise AnswerFailed(ev.message)
    answer = "".join(parts)
    cited = cite_numbers(answer)
    if refusal and not parts:
        stop = "refusal"
    elif reason == "max_output_tokens":
        stop = "max_tokens"
    elif reason == "content_filter":
        stop = "refusal"
    else:
        stop = "end_turn" if status == "completed" else (status or "unknown")
    yield "done", {"stop_reason": stop,
                   "cited": [n for n in cited if 1 <= n <= len(sources)],
                   "invalid_cites": [n for n in cited if not 1 <= n <= len(sources)],
                   "unverified": unverified_mentions(answer, sources),
                   "usage": usage}
