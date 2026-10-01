"""法規檢索：條號直取＋場所代碼直取＋Meilisearch 關鍵詞＋場所展開，以 RRF（倒數排名融合）合併。

場所展開：白話問題常只講場所（「KTV 要不要裝撒水」），條文卻寫「第十二條第一款第一目所列場所」。
先把 KTV 對到甲-1，再找「涉及甲-1（直接引用該目、引用整個甲類、或文字寫甲類場所）且符合其餘查詢詞」的節點。

向量檢索（Voyage API）在取得金鑰後加入，作為另一路；介面不變。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .numerals import CN_NUM_CHARS, to_cn, to_int
from .sources import LAWS

MEILI_INDEX = "law_nodes"
RRF_K = 60
# keyword 與 keyword_last 是同一份查詢的兩種比對方式（見 keyword() 說明），兩路互補
# 兩種比對方式是同一份查詢的同一種訊號，各 0.5、合計 1，維持與場所展開路（2）的相對份量
ROUTE_WEIGHT = {"keyword": 0.5, "keyword_last": 0.5, "occupancy": 2.0, "legend": 2.0}
# 圖例（附件三消防圖說圖示範例）只在問到圖例時才進檢索：圖例名稱都是設備名，混進一般檢索會擠掉法條
LEGEND_INTENT = re.compile(r"圖例|圖示|符號|標示記號|記號|怎麼畫|畫法")

# 消防實務常見的同義／俗稱寫法（雙向）。注意：只放法律上等義的詞（例如不把「民宿」當「旅館」）。
SYNONYM_GROUPS = [
    ["撒水", "灑水"], ["撒水頭", "灑水頭"], ["自動撒水設備", "自動灑水設備", "撒水系統", "灑水系統"],
    ["消防栓", "消防拴"], ["探測器", "感知器", "偵測器"], ["火警自動警報設備", "火警系統", "火警警報"],
    ["視聽歌唱場所", "KTV", "卡拉OK"], ["錄影節目帶播映場所", "MTV"], ["樓地板面積", "面積"],
    ["出口標示燈", "逃生指示燈", "出口燈"], ["緊急照明設備", "緊急照明燈", "緊急照明"],
    ["避難器具", "逃生器具"], ["瓦斯漏氣火警自動警報設備", "瓦斯偵測器", "瓦斯警報"],
    ["連結送水管", "送水口"], ["室內消防栓設備", "室內消防栓"], ["幼兒園", "幼稚園", "托兒所"],
    ["醫院", "病院"], ["旅館", "飯店"], ["補習班", "補教"],
    ["倉庫", "倉儲"], ["排煙設備", "排煙"], ["一一九火災通報裝置", "119通報", "一一九通報"],
    ["建築物", "大樓", "樓房"], ["地下層", "地下室"], ["室內停車空間", "停車場", "室內停車場"],
    ["監造", "監工"], ["受信總機", "火警總機"],
]

MEILI_SETTINGS = {
    # 表格（方框字元）與 PDF 列名放最後：避免大表格因列滿場所名稱而搶走排名
    "searchableAttributes": ["text", "context", "chapter", "citation", "law_name", "table"],
    "filterableAttributes": ["pcode", "level", "article", "occupancy"],
    "sortableAttributes": ["priority"],
    # priority（設置標準第 12～30-1 條、消防法核心條文較高）放在 attribute 之前：
    # 本系統主要用途是「該不該設」，同樣符合查詢詞時優先回應設置門檻條文
    "rankingRules": ["words", "typo", "priority:desc", "proximity", "attribute", "sort", "exactness"],
    "localizedAttributes": [{"attributePatterns": ["*"], "locales": ["cmn"]}],
    "synonyms": {w: [x for x in g if x != w] for g in SYNONYM_GROUPS for w in g},
    "typoTolerance": {"enabled": False},
}

_N = f"[{CN_NUM_CHARS}0-9０-９]+"
ART_RE = re.compile(rf"(?:第\s*)?({_N})\s*條(?:之\s*({_N}))?(?:\s*第?\s*({_N})\s*項)?(?:\s*第?\s*({_N})\s*款)?(?:\s*第?\s*({_N})\s*目)?")
SECTION_RE = re.compile(r"§\s*(\d+)(?:-(\d+))?")
# 只認明確寫法：「甲-3」「甲－3」「甲類第3目」「甲類場所第三目」（避免把「甲類場所 3樓」誤判成甲-3）
OCC_RE = re.compile(rf"([甲乙丙丁戊])\s*(?:[-－]\s*({_N})|類\s*(?:場所)?\s*第?\s*({_N})\s*目)")
DIGITS_RE = re.compile(r"[0-9０-９]+")
FLOOR_RE = re.compile(r"([0-9０-９]+)\s*樓")
# 問句贅字：只去多字詞與句尾語助詞，避免把「設置」的「設」這類字拆掉
STOP_PHRASES = sorted(["要不要", "是不是", "需不需要", "有沒有", "一定要", "需要", "哪些", "哪一類", "哪一種", "什麼",
                       "甚麼", "怎麼", "如何", "多少", "情況", "情形", "誰可以", "可以", "請問", "還是", "幾種", "哪裡",
                       "要送", "屬於", "第幾目", "第幾款", "第幾條", "第幾類", "超過", "誰"],
                      key=len, reverse=True)
TAIL_PARTICLES = re.compile(r"[嗎呢吧啊？?！!。]+$")
# 口語 → 法條用語。法條幾乎不用「的」「裝」「放」，這些字在索引裡是「罕見字」，若留在查詢中，
# 依詞頻比對時會被優先保留，把結果拉向極少數剛好含這些字的條文（2026-10-01 評測實際踩到）。
COLLOQUIAL = [
    (re.compile(r"119"), "一一九"),                                   # 法條寫「一一九」，不是「一百十九」
    (re.compile(r"(要|需|得)?(安裝|裝設|加裝)"), "設置"),
    (re.compile(r"(要|需|得)?裝(?![置修])"), "設置"),
    (re.compile(r"(要|需|得)?放(?![映射置出水流電])"), "設置"),
    (re.compile(r"(要|需|得)設(?![置計備有於])"), "設置"),
    (re.compile(r"(要|需|得)(?=設置)"), ""),
    (re.compile(r"[的了喔啦和跟]|幾|做|是(?!否)"), " "),
    (re.compile(r"要(?![點件求旨])"), " "),                          # 「要書面知會」「要查核」的「要」也是法條罕見字
]
CLASS_MENTION_RE = re.compile(r"([甲乙丙丁戊](?:[、及或][甲乙丙丁戊])*)類場所")
GENERIC = re.compile(r"[甲乙丙丁戊]類|場所|分類|類別|第幾目|屬於|類|的|是|要|裝|放|做|一定")

PRIORITY_STANDARD = {str(i) for i in range(12, 31)} | {"22-1", "30-1"}
PRIORITY_ACT = {"6", "7", "9", "10", "11", "13", "15-5"}
TABLE_CHARS = set("┌┐└┘├┤┬┴┼─│")


@dataclass
class Hit:
    node_id: str
    score: float
    routes: list[str]


# ---------- 直取 ----------

def detect_law(q: str) -> str | None:
    for s in sorted(LAWS, key=lambda s: -max(len(a) for a in s.aliases)):
        for a in sorted(s.aliases, key=len, reverse=True):
            if a in q:
                return s.pcode
    return None


def structural(q: str, exists) -> list[str]:
    """條號直取。exists(node_id) -> bool。沒指名法規時依序試設置標準、消防法、其他。"""
    law = detect_law(q)
    order = [law] if law else ["D0120029", "D0120001"] + [s.pcode for s in LAWS if s.pcode not in ("D0120029", "D0120001")]
    found: list[str] = []
    for m in list(ART_RE.finditer(q)) + list(SECTION_RE.finditer(q)):
        g = m.groups()
        try:
            art = str(to_int(g[0])) + (f"-{to_int(g[1])}" if len(g) > 1 and g[1] else "")
            rest = [to_int(x) for x in g[2:] if x] if len(g) > 2 else []
            p_given = len(g) > 2 and g[2] is not None
        except ValueError:
            continue
        for pcode in order:
            base = f"{pcode}/{art}"
            cands = []
            if rest:
                if p_given:
                    cands.append(base + "/" + "/".join(map(str, rest)))
                else:
                    cands.append(base + "/1/" + "/".join(map(str, rest)))  # 單一項條文省略「第1項」
                    cands.append(base + "/" + "/".join(map(str, rest)))
            cands.append(base)
            hit = next((c for c in cands if exists(c)), None)
            if hit:
                found.append(hit)
                break
    return list(dict.fromkeys(found))


def occupancy(q: str, occ_codes: dict[str, str]) -> list[str]:
    """「甲-3」「甲類第3目」「甲類場所第三目」→ 第 12 條對應的目。occ_codes: {code: node_id}"""
    out = []
    for m in OCC_RE.finditer(q):
        try:
            code = f"{m.group(1)}-{to_int(m.group(2) or m.group(3))}"
        except ValueError:
            continue
        if code in occ_codes:
            out.append(occ_codes[code])
    return out


# ---------- 關鍵詞 ----------

def normalize_query(q: str) -> str:
    """把查詢裡的阿拉伯數字改成法條用的中文數字（「11樓」→「十一層」、「300平方公尺」→「三百平方公尺」）。"""
    q = FLOOR_RE.sub(lambda m: to_cn(to_int(m.group(1))) + "層", q)
    return DIGITS_RE.sub(lambda m: to_cn(to_int(m.group(0))) if to_int(m.group(0)) < 10000 else m.group(0), q)


def clean_query(q: str) -> str:
    q = TAIL_PARTICLES.sub("", q.strip())
    for w in STOP_PHRASES:
        q = q.replace(w, " ")
    for pat, rep in COLLOQUIAL:
        q = pat.sub(rep, q)
    return re.sub(r"\s+", " ", normalize_query(q)).strip()


def keyword(q: str, base: str, key: str, pcode: str | None, limit: int = 20,
            filters: list[str] | None = None, legend: bool = False, strategy: str = "frequency") -> list[str]:
    """Meilisearch 關鍵詞檢索。

    strategy＝frequency：詞不夠時先丟最常見的詞。弱點：法條裡完全不存在的詞「最罕見」，永遠不會被丟，
      整句就零結果（2026-10-01 評測 N02 實際發生）；或只剩罕見雜詞，結果被帶偏（N05）。
    strategy＝last：從句尾開始丟詞。弱點：句尾的關鍵詞可能先被丟掉。兩者並行再用 RRF 合併，互補盲點。
    """
    cq = clean_query(LEGEND_INTENT.sub(" ", q) if legend else q)
    if not cq:
        return []
    filters = (filters or []) + ['level = "legend"' if legend else 'level != "legend"']
    body = {"q": cq, "limit": limit, "locales": ["cmn"], "attributesToRetrieve": ["node_id"],
            "matchingStrategy": strategy}
    f = ([f'pcode = "{pcode}"'] if pcode else []) + filters
    if f:
        body["filter"] = " AND ".join(f"({x})" for x in f)
    r = httpx.post(f"{base}/indexes/{MEILI_INDEX}/search", json=body,
                   headers={"Authorization": f"Bearer {key}"}, timeout=10)
    r.raise_for_status()
    return [h["node_id"] for h in r.json()["hits"]]


# ---------- 場所展開 ----------

def place_terms(occ_rows: list[dict]) -> dict[str, set[str]]:
    """{場所用語: {代碼}}，用語取自第 12 條各目原文（以頓號、括號切開）加上同義詞。"""
    terms: dict[str, set[str]] = {}
    for o in occ_rows:
        for t in re.split(r"[、，（）()；。]|等|及其他類似場所|限", o["text"]):
            t = t.strip()
            if len(t) >= 2:
                terms.setdefault(t, set()).add(o["code"])
    for g in SYNONYM_GROUPS:
        hit = set().union(*(terms.get(w, set()) for w in g))
        if hit:
            for w in g:
                terms.setdefault(w, set()).update(hit)
    return terms


def detect_places(q: str, terms: dict[str, set[str]], occ_codes: list[str]) -> tuple[set[str], str]:
    """回傳（查詢涉及的場所代碼，去掉場所用語後的剩餘查詢）。"""
    codes: set[str] = set()
    rest = q
    for t in sorted(terms, key=len, reverse=True):
        if t in rest:
            codes |= terms[t]
            rest = rest.replace(t, " ")
    for m in CLASS_MENTION_RE.finditer(q):
        for cls in re.findall(r"[甲乙丙丁戊]", m.group(1)):
            codes |= {c for c in occ_codes if c.startswith(cls + "-")}
    return codes, rest


def legend_route(q: str, base: str, key: str) -> list[str]:
    """問「撒水頭的圖例」「消防栓符號怎麼畫」→ 只在附件三圖例裡找。"""
    if not LEGEND_INTENT.search(q):
        return []
    return keyword(q, base, key, None, limit=10, legend=True)


def occupancy_route(q: str, terms: dict[str, set[str]], occ_codes: list[str], base: str, key: str) -> list[str]:
    """KTV → 甲-1 → 找「涉及甲-1 且符合其餘查詢詞」的節點。沒有場所或沒有其餘主題詞就不走這一路。"""
    codes, rest = detect_places(q, terms, occ_codes)
    topic = GENERIC.sub(" ", clean_query(rest)).strip()
    if not codes or not topic:
        return []
    flt = " OR ".join(f'occupancy = "{c}"' for c in sorted(codes))
    return keyword(topic, base, key, None, limit=10, filters=[flt])


def occupancy_of(text: str, refs: list[str], occ_by_node: dict[str, str], occ_codes: list[str]) -> list[str]:
    """節點涉及的場所代碼：引用到第 12 條某目／某款，或文字寫「甲類場所」「乙、丙、丁類場所」。"""
    codes: set[str] = set()
    for t in refs:
        if t in occ_by_node:
            codes.add(occ_by_node[t])
        m = re.fullmatch(r"D0120029/12/1/([1-5])", t)
        if m:
            cls = "甲乙丙丁戊"[int(m.group(1)) - 1]
            codes |= {c for c in occ_codes if c.startswith(cls + "-")}
    for m in CLASS_MENTION_RE.finditer(text):
        for cls in re.findall(r"[甲乙丙丁戊]", m.group(1)):
            codes |= {c for c in occ_codes if c.startswith(cls + "-")}
    return sorted(codes)


# ---------- 合併 ----------

def fuse(routes: dict[str, list[str]], pinned: list[str]) -> list[Hit]:
    """RRF 合併；條號／代碼直取的結果固定排最前面。場所展開路權重較高（已同時對上場所與主題兩個條件）。"""
    scores: dict[str, Hit] = {}
    for name, ids in routes.items():
        for rank, nid in enumerate(ids):
            h = scores.setdefault(nid, Hit(nid, 0.0, []))
            h.score += ROUTE_WEIGHT.get(name, 1.0) / (RRF_K + rank + 1)
            h.routes.append(name)
    for i, nid in enumerate(pinned):
        h = scores.setdefault(nid, Hit(nid, 0.0, []))
        h.score += 10 - i * 0.01
        if "direct" not in h.routes:
            h.routes.insert(0, "direct")
    return sorted(scores.values(), key=lambda h: -h.score)


# ---------- 索引文件 ----------

def index_document(n: dict, by: dict, names: dict, refs: list[str] | None = None,
                   occ_by_node: dict[str, str] | None = None, occ_codes: list[str] | None = None,
                   table_text: dict[str, str] | None = None) -> dict:
    """一個節點在 Meilisearch 的文件。context 放上層文字（條的引言、款的本文），讓「二、十一層以上…」也帶得到「自動撒水設備」。"""
    ctx, p = [], n["parent_id"]
    if n["level"] == "legend":            # 圖例上下文只放所屬類別（不放整個附件的類別清單，免得全部圖例都對上「撒水」）
        p = None
        ctx.append(n["chapter"].split(" > ")[-1])
    while p and by[p]["level"] not in ("article", "attachment"):
        ctx.append(by[p]["text"].split("\n")[0])
        p = by[p]["parent_id"]
    art = by[f'{n["pcode"]}/{n["article"]}']
    first = art["text"].split("\n")[0]
    if n["level"] != "legend" and first not in ctx and first != n["text"].split("\n")[0]:
        ctx.append(first)
    name, short = names[n["pcode"]]
    lines = n["text"].split("\n")
    body = [l for l in lines if not any(ch in TABLE_CHARS for ch in l)]
    table = [l for l in lines if any(ch in TABLE_CHARS for ch in l)]
    tt = (table_text or {}).get(f'{n["pcode"]}/{n["article"]}')
    if tt and n["path"] == [1]:          # 結構化表格的各列文字掛在第 1 項（表格所屬的項）
        table.append(tt)
    priority = 3 if (n["pcode"] == "D0120029" and n["article"] in PRIORITY_STANDARD) else \
        2 if (n["pcode"] == "D0120001" and n["article"] in PRIORITY_ACT) else 1
    return {"id": n["node_id"].replace("/", "_"), "node_id": n["node_id"], "pcode": n["pcode"],
            "article": n["article"], "level": n["level"], "text": "\n".join(body), "table": "\n".join(table),
            "context": " ／ ".join(reversed(ctx)), "chapter": n["chapter"], "citation": n["citation"],
            "law_name": f"{name} {short}", "priority": priority,
            "occupancy": occupancy_of(n["text"], refs or [], occ_by_node or {}, occ_codes or [])}
