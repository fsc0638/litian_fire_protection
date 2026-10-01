"""條文間交叉引用：把「第十二條第一款第一目」「前項」「同款」「本法第六條」等寫法解析成節點編號。

規則（依 2026-10-01 對實際條文的觀察）：
- 「第X條(之Y)(第X項)(第X款)(第X目)」：明確引用。前面是「本法」或「消防法（以下簡稱本法）」→ 消防法；
  前面是其他已收錄法規名稱 → 該法規；前面是未收錄的法規（如建築技術規則）→ 外部引用。
- 沒寫條號的「第X項…」「第X款…」：若只用頓號／或／及／至接在前一個引用後面 → 沿用前一個引用的條（與項）；
  否則先找本條，本條沒有這個位置再退回本節點中前一個被引用的條。
  例：第 17 條「供第十二條第一款第一目…；供同款其他各目及第二款第一目」的「第二款第一目」＝第 12 條第 2 款第 1 目。
- 「前項／前款／前目」以本節點位置往前一格；「同條／同項／同款」指前一個被引用的位置（沒有就指本節點）。
- 單一項的條文，引用常省略「第一項」：「第十二條第一款第一目」＝ D0120029/12/1/1/1。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .numerals import CN_NUM_CHARS, to_int
from .parse import Node, article_key
from .sources import BY_PCODE, THIS_ACT

N = f"([{CN_NUM_CHARS}0-9０-９]+)"
REF_RE = re.compile(rf"(本法|本標準|本辦法|本細則)?第{N}條(?:之{N})?(?:第{N}項)?(?:第{N}款)?(?:第{N}目)?"
                    rf"|第{N}項(?:第{N}款)?(?:第{N}目)?"
                    rf"|第{N}款(?:第{N}目)?"
                    rf"|(前|同)(條|項|款|目)"
                    rf"|第{N}目")
CHAIN_GAP = re.compile(r"^[、，,及或與至和]?$")
EXTERNAL_BEFORE = re.compile(r"(規則|[^本]法|辦法|基準|標準|細則|編|條例|公約)$")
ALIASES = sorted(((a, s.pcode) for s in BY_PCODE.values() for a in s.aliases), key=lambda t: -len(t[0]))


@dataclass
class XRef:
    src: str             # 引用出現在哪個節點
    raw: str             # 原文片段
    target: str | None   # 解析出的節點編號；外部法規或無法解析為 None
    external: bool = False
    resolved: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _n(s: str | None) -> int | None:
    return to_int(s) if s else None


def _law_of(prefix: str | None, before: str, pcode: str) -> str | None:
    """回傳被引用法規的 pcode；外部法規回 None。"""
    if prefix == "本法":
        return THIS_ACT
    if prefix:
        return pcode
    b = before.rstrip()
    if b.endswith(("本法）", "本法)")):
        return THIS_ACT
    for alias, code in ALIASES:
        if b.endswith(alias):
            return code
    if EXTERNAL_BEFORE.search(b):
        return None
    return pcode


def extract_xrefs(nodes: list[Node]) -> list[XRef]:
    ids = {n.node_id for n in nodes}
    single_para = {n.node_id: len(n.children) <= 1 for n in nodes if n.level == "article"}
    prev_article: dict[str, str] = {}
    last_art_by_law: dict[str, str] = {}
    for a in nodes:
        if a.level == "article":
            if a.pcode in last_art_by_law:
                prev_article[a.node_id] = last_art_by_law[a.pcode]
            last_art_by_law[a.pcode] = a.article

    def tid(pcode: str, art: str, p: int | None, k: int | None, m: int | None) -> str:
        if p is None and (k is not None or m is not None) and single_para.get(f"{pcode}/{art}", True):
            p = 1
        return "/".join([pcode, art] + [str(x) for x in (p, k, m) if x is not None])

    out: list[XRef] = []
    for n in nodes:
        if n.level == "article" or n.deleted:
            continue
        cur = (n.pcode, n.article, *(list(n.path) + [None, None, None])[:3])
        last: tuple | None = None          # (pcode, art, p, k, m) 上一個被引用的位置
        last_end = -1
        for mt in REF_RE.finditer(n.text):
            g = mt.groups()
            gap = n.text[last_end:mt.start()].strip() if last_end >= 0 else "x"
            chained = last is not None and bool(CHAIN_GAP.match(gap))
            cands: list[tuple] = []
            external = False
            if g[1]:                                            # 第X條…
                law = (last[0] if chained and not g[0] else
                       _law_of(g[0], n.text[max(0, mt.start() - 20):mt.start()], n.pcode))
                art = str(to_int(g[1])) + (f"-{to_int(g[2])}" if g[2] else "")
                if law is None:
                    external = True
                else:
                    cands = [(law, art, _n(g[3]), _n(g[4]), _n(g[5]))]
            elif g[6]:                                          # 第X項…（無條號）
                p, k, m = _n(g[6]), _n(g[7]), _n(g[8])
                here = (n.pcode, n.article, p, k, m)
                there = (last[0], last[1], p, k, m) if last else None
                cands = [there, here] if chained else [here, there]
            elif g[9]:                                          # 第X款…（無條號、無項號）
                k, m = _n(g[9]), _n(g[10])
                here = (n.pcode, n.article, cur[2], k, m)
                there = (last[0], last[1], last[2], k, m) if last else None
                cands = [there, here] if chained else [here, there]
            elif g[13]:                                         # 第X目（無條、項、款號）
                m = _n(g[13])
                there = (last[0], last[1], last[2], last[3], m) if last else None
                here = (n.pcode, n.article, cur[2], cur[3], m)
                cands = [there, here] if chained else [here, there]
            else:                                               # 前X／同X
                rel, unit = g[11], g[12]
                depth = {"條": 0, "項": 1, "款": 2, "目": 3}[unit]
                if rel == "同":
                    base = last or cur
                    cands = [tuple(list(base[:2]) + [base[2 + i] if i < depth else None for i in range(3)])]
                elif depth == 0:                                # 前條：同一法規的上一條
                    prev = prev_article.get(f"{n.pcode}/{n.article}")
                    tail = re.match(rf"第{N}項(?:第{N}款)?(?:第{N}目)?|第{N}款(?:第{N}目)?", n.text[mt.end():])
                    if prev and tail:
                        tg = tail.groups()
                        p, k, m = (_n(tg[0]), _n(tg[1]), _n(tg[2])) if tg[0] else (None, _n(tg[3]), _n(tg[4]))
                        cands = [(n.pcode, prev, p, k, m)]
                        last_end = mt.end() + tail.end()
                        hit = cands[0] if tid(*cands[0]) in ids else None
                        out.append(XRef(n.node_id, mt.group(0) + tail.group(0), tid(*hit) if hit else None, resolved=bool(hit)))
                        last = hit or (n.pcode, prev, None, None, None)
                        continue
                    cands = [(n.pcode, prev, None, None, None)] if prev else []
                else:
                    path = list(n.path)
                    if len(path) < depth or path[depth - 1] <= 1:
                        out.append(XRef(n.node_id, mt.group(0), None))
                        last_end = mt.end()
                        continue
                    path = path[:depth]
                    path[-1] -= 1
                    cands = [(n.pcode, n.article, *(path + [None, None, None])[:3])]
            last_end = mt.end()
            if external:
                out.append(XRef(n.node_id, mt.group(0), None, external=True))
                last = None
                continue
            hit = next((c for c in cands if c and tid(*c) in ids), None)
            if hit:
                out.append(XRef(n.node_id, mt.group(0), tid(*hit), resolved=True))
                last = hit
            else:
                out.append(XRef(n.node_id, mt.group(0), None))
    return out


def sort_key(node_id: str) -> tuple:
    pcode, art, *rest = node_id.split("/")
    return (pcode, article_key(art), *map(int, rest))
