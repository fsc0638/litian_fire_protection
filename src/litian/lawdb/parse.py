"""把全國法規資料庫的單部法規 XML 拆成節點樹：條 → 項 → 款 → 目 → 細目。

依 2026-10-01 對官方 XML 的實際觀察：
- 條文內容每一行是一個邏輯段落。「一、」開頭是款，「（一）」開頭是目，「1.」開頭是細目，其餘就是新的一項。
- 行首有空白、含方框字元（表格）、或像「H=h1+h2」的公式行，屬於上一個節點的接續內容。
- 條文欄位官方拼成 ArticleConctent（schema.csv 寫 ArticleContent），兩種都接受。
- 有 20 條的表格只在「完整條文」PDF 附件裡（LawAttachements），標記 pdf_table_url。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .numerals import CN_NUM_CHARS, to_int
from .sources import BY_PCODE

_CN = f"[{CN_NUM_CHARS}]+"
ITEM_RE = re.compile(rf"^({_CN})、")
SUB_RE = re.compile(rf"^[（(]({_CN})[）)]")
SUBSUB_RE = re.compile(r"^([0-9０-９]+)[\.．]")
FORMULA_RE = re.compile(r"^[A-Za-zＡ-Ｚａ-ｚ][A-Za-z0-9Ａ-Ｚａ-ｚ０-９]{0,3}\s*[=＝]")
BOX_CHARS = set("┌┐└┘├┤┬┴┼─│━┃╋═║")
ARTNO_RE = re.compile(r"第\s*(\d+)(?:-(\d+))?\s*條")
HEADING_RE = re.compile(rf"^第\s*({_CN})\s*(編|章|節)(之({_CN}))?\s*(.*)$")
ATTACH_RE = re.compile(r"第\s*(\d+(?:-\d+)?)\s*條")

LEVELS = ("article", "paragraph", "item", "subitem", "subsubitem")


@dataclass
class Node:
    node_id: str
    pcode: str
    article: str                 # "18" 或 "18-1"
    level: str                   # LEVELS 之一
    path: list[int]              # [項, 款, 目, 細目]（條本身為空）
    text: str                    # 本節點自身文字（不含子節點）
    parent_id: str | None
    citation: str = ""
    chapter: str = ""
    has_table: bool = False
    pdf_table_url: str | None = None
    deleted: bool = False
    seq: int = 0                 # 全法規內的排序
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Law:
    pcode: str
    name: str
    short: str
    level: str                   # 法律／命令
    category: str
    modified: str                # YYYYMMDD
    effective: str
    effective_note: str
    abandoned: bool
    article_count: int = 0
    deleted_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _content(a: ET.Element) -> str:
    c = a.findtext("ArticleConctent")
    if c is None:
        c = a.findtext("ArticleContent")
    return (c or "").replace("\r\n", "\n").replace("\r", "\n")


def article_key(article: str) -> tuple[int, int]:
    main, _, sub = article.partition("-")
    return int(main), int(sub or 0)


def article_cite(article: str, unit: str = "條") -> str:
    main, _, sub = article.partition("-")
    return f"第{main}{unit}" + (f"之{sub}" if sub else "")


def _is_continuation(line: str) -> bool:
    s = line.rstrip()
    return (line[:1] in (" ", "　", "\t")
            or any(ch in BOX_CHARS for ch in line)
            or bool(FORMULA_RE.match(line))
            # 第 183 條「全揚程＝消防水帶摩擦損失水頭＋…」這類沒縮排的中文公式：含等號且不以句號結尾
            or (("＝" in s or "=" in s) and not s.endswith("。")))


def _parse_article(pcode: str, article: str, content: str) -> tuple[list[Node], list[str]]:
    """回傳（該條所有節點，警告）。第一個節點是條本身。"""
    warnings: list[str] = []
    aid = f"{pcode}/{article}"
    art = Node(aid, pcode, article, "article", [], content.strip(), None,
               deleted=content.strip() in ("（刪除）", "(刪除)"))
    nodes = [art]
    if art.deleted:
        return nodes, warnings

    para = item = sub = subsub = None
    stack: list[Node] = []           # 目前最深的節點鏈，接續行掛在 stack[-1]

    def add(level: str, path: list[int], text: str, parent: Node) -> Node:
        n = Node(f"{aid}/" + "/".join(map(str, path)), pcode, article, level, path, text, parent.node_id)
        parent.children.append(n.node_id)
        nodes.append(n)
        return n

    def expect(kind: str, got: int, want: int) -> None:
        if got != want:
            warnings.append(f"{aid} {kind}編號不連續：預期 {want}，實際 {got}")

    for raw in content.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        s = line.lstrip()
        is_marker = bool(ITEM_RE.match(s) or SUB_RE.match(s) or SUBSUB_RE.match(s))
        if stack and not is_marker and _is_continuation(line):
            stack[-1].text += "\n" + line
            if any(ch in BOX_CHARS for ch in line):
                stack[-1].has_table = True
            continue
        if (m := ITEM_RE.match(s)):
            if para is None:
                para = add("paragraph", [1], "", art)
            n = to_int(m.group(1))
            expect("款", n, len([c for c in para.children]) + 1)
            item = add("item", para.path + [n], s, para)
            sub = subsub = None
            stack = [para, item]
        elif (m := SUB_RE.match(s)) and item is not None:
            n = to_int(m.group(1))
            expect("目", n, len(item.children) + 1)
            sub = add("subitem", item.path + [n], s, item)
            subsub = None
            stack = [para, item, sub]
        elif (m := SUBSUB_RE.match(s)) and item is not None:
            parent = sub or item
            n = to_int(m.group(1))
            subsub = add("subsubitem", parent.path + ([0] if sub is None else []) + [n], s, parent)
            stack = [para, item] + ([sub] if sub else []) + [subsub]
        else:
            pno = (para.path[0] + 1) if para else 1
            para = add("paragraph", [pno], s, art)
            item = sub = subsub = None
            stack = [para]
    return nodes, warnings


def cite(short: str, n: Node, multi_para: bool, unit: str = "條") -> str:
    c = short + article_cite(n.article, unit)
    labels = ("項", "款", "目", "細目")
    for i, v in enumerate(n.path):
        if i == 0 and not multi_para:
            continue
        if v == 0:
            continue
        c += f"第{v}{labels[i]}"
    return c


def parse_law(xml_path: Path) -> tuple[Law, list[Node], list[str]]:
    el = ET.parse(xml_path).getroot()
    m = re.search(r"pcode=([A-Z0-9]+)", el.findtext("LawURL") or "")
    pcode = m.group(1)
    src = BY_PCODE[pcode]
    law = Law(pcode=pcode, name=(el.findtext("LawName") or "").strip(), short=src.short,
              level=(el.findtext("LawLevel") or "").strip(), category=(el.findtext("LawCategory") or "").strip(),
              modified=(el.findtext("LawModifiedDate") or "").strip(),
              effective=(el.findtext("LawEffectiveDate") or "").strip(),
              effective_note=(el.findtext("LawEffectiveNote") or "").strip(),
              abandoned=bool((el.findtext("LawAbandonNote") or "").strip()))

    pdf_tables: dict[str, str] = {}
    atts = el.find("LawAttachements")
    for f in (atts if atts is not None else []):
        am = ATTACH_RE.search(f.findtext("FileName") or "")
        if am:
            pdf_tables[am.group(1)] = (f.findtext("FileURL") or "").strip()

    nodes: list[Node] = []
    warnings: list[str] = []
    heading: dict[str, str] = {}
    for a in el.find("LawArticles"):
        content = _content(a)
        if (a.findtext("ArticleType") or "").strip() == "C":
            hm = HEADING_RE.match(content.strip())
            if not hm:
                warnings.append(f"{pcode} 無法辨識的編章節標題：{content.strip()[:30]}")
                continue
            num, kind, _, sub_num, title = hm.groups()
            label = f"第{num}{kind}" + (f"之{sub_num}" if sub_num else "") + (f" {title.strip()}" if title.strip() else "")
            heading[kind] = label
            if kind == "編":
                heading.pop("章", None); heading.pop("節", None)
            elif kind == "章":
                heading.pop("節", None)
            continue
        am = ARTNO_RE.search(a.findtext("ArticleNo") or "")
        if not am:
            warnings.append(f"{pcode} 無法辨識的條號：{a.findtext('ArticleNo')}")
            continue
        article = am.group(1) + (f"-{am.group(2)}" if am.group(2) else "")
        art_nodes, w = _parse_article(pcode, article, content)
        warnings += w
        chapter = " > ".join(heading[k] for k in ("編", "章", "節") if k in heading)
        multi = len(art_nodes[0].children) > 1
        for n in art_nodes:
            n.chapter = chapter
            n.citation = cite(law.short, n, multi)
        if article in pdf_tables:
            art_nodes[0].pdf_table_url = pdf_tables[article]
        nodes += art_nodes

    for i, n in enumerate(nodes):
        n.seq = i
    arts = [n for n in nodes if n.level == "article"]
    law.article_count = len(arts)
    law.deleted_count = sum(1 for n in arts if n.deleted)
    return law, nodes, warnings
