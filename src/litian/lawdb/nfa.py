"""消防署法令查詢系統（law.nfa.gov.tw）的行政規則與附件。消防署沒有 API，這裡抓行動版網頁與附件檔。

已知坑（2026-10-01 查證）：
- 該站憑證由「TWCA Secure SSL Certification Authority」簽發，伺服器卻附上不相干的中華電信 HiPKI 中繼憑證，
  一般 TLS 用戶端驗證失敗。解法是補上正確的中繼憑證（data/certs/，來源與指紋見該目錄 README），不關閉驗證。
- 行動版每一「點」放在一個 <pre>，依固定寬度硬斷行，接續行有縮排。結構以「行首沒縮排＋編號接得上」判斷。
- 附件三「消防圖說圖示範例」以 ODT 讀取：17 張表、一張一類設備，每列是「符號圖｜名稱｜備註」。
"""

from __future__ import annotations

import html as htmllib
import re
import ssl
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import certifi
import httpx

from .numerals import CN_NUM_CHARS, to_int
from .parse import Law, Node, cite
from .sources import LawSource

NFA_MOBILE = "https://law.nfa.gov.tw/MOBILE/"
DEFAULT_CA = Path("data/certs/twca_secure_ssl_ca_2023g3.pem")


@dataclass(frozen=True)
class Attachment:
    key: str     # 節點編號用，例：A3
    label: str   # 附件三：消防圖說圖示範例
    odt: str     # 消防署 GetFile pfid
    pdf: str


ATTACHMENTS: dict[str, tuple[Attachment, ...]] = {
    "FL019489": (Attachment("A3", "附件三：消防圖說圖示範例", "0000259805", "0000253965"),),
}

_CN = f"[{CN_NUM_CHARS}]+"
POINT_RE = re.compile(rf"^({_CN})、")
ITEM_RE = re.compile(rf"^[（(]({_CN})[）)]")
SUB_RE = re.compile(r"^([0-9０-９]+)[\.．]")
SUBSUB_RE = re.compile(r"^[（(]([0-9０-９]+)[）)]")


def ssl_context(ca_file: Path = DEFAULT_CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(ca_file))
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _get(url: str, dest: Path, ca_file: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, verify=ssl_context(ca_file), timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def raw_paths(raw_dir: Path, code: str) -> dict[str, Path]:
    out = {"html": raw_dir / f"nfa_{code}.html"}
    for a in ATTACHMENTS.get(code, ()):
        out[f"{a.key}.odt"] = raw_dir / f"nfa_{code}_{a.key}.odt"
        out[f"{a.key}.pdf"] = raw_dir / f"nfa_{code}_{a.key}.pdf"
    return out


def fetch(code: str, raw_dir: Path, refresh: bool = False, ca_file: Path = DEFAULT_CA) -> dict[str, Path]:
    paths = raw_paths(raw_dir, code)
    if refresh or not paths["html"].exists():
        _get(f"{NFA_MOBILE}law.aspx?lsid={code}", paths["html"], ca_file)
    for a in ATTACHMENTS.get(code, ()):
        for ext, pfid in (("odt", a.odt), ("pdf", a.pdf)):
            p = paths[f"{a.key}.{ext}"]
            if refresh or not p.exists():
                _get(f"{NFA_MOBILE}GetFile.ashx?pfid={pfid}", p, ca_file)
    return paths


# ---------- 本文 ----------

def _roc_date(s: str) -> str:
    m = re.search(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s)
    return f"{int(m.group(1)) + 1911:04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else ""


def parse_rule(html_path: Path, src: LawSource) -> tuple[Law, list[Node], list[str]]:
    page = html_path.read_text(encoding="utf-8", errors="replace")
    warnings: list[str] = []
    name_m = re.search(r"(消防[^<>\n]{4,60}?(?:作業基準|要點|注意事項|基準))\s*<", page)
    modified = _roc_date((re.search(r"修正日期：([^<]+)", page) or re.search(r"發布日期：([^<]+)", page)).group(1))
    issue = re.search(r"發布文號：([^<]+)", page)
    law = Law(pcode=src.pcode, name=(name_m.group(1) if name_m else src.aliases[0]).strip(), short=src.short,
              level="行政規則", category="消防署法令查詢系統", modified=modified, effective=modified,
              effective_note=f"發布文號：{issue.group(1).strip()}" if issue else "", abandoned=False)

    nodes: list[Node] = []
    for block in re.findall(r"<pre>(.*?)</pre>", page, re.S):
        raw_lines = htmllib.unescape(re.sub(r"<[^>]+>", "", block)).replace("\r", "").split("\n")
        units: list[list] = []               # [level, number, text]
        counts = {"item": 0, "sub": 0, "subsub": 0}
        for ln in raw_lines:
            if not ln.strip():
                continue
            s = ln.rstrip()
            indented = s[:1] in (" ", "　", "\t")
            body = s.strip()
            kind = None
            if not units and (m := POINT_RE.match(body)):
                kind, num = "point", to_int(m.group(1))
            elif (m := ITEM_RE.match(body)) and to_int(m.group(1)) == counts["item"] + 1 and not indented:
                kind, num = "item", to_int(m.group(1))
            elif (m := SUB_RE.match(body)) and counts["item"] and to_int(m.group(1)) == counts["sub"] + 1:
                kind, num = "sub", to_int(m.group(1))
            elif (m := SUBSUB_RE.match(body)) and counts["sub"] and to_int(m.group(1)) == counts["subsub"] + 1:
                kind, num = "subsub", to_int(m.group(1))
            if kind is None:
                if units:
                    units[-1][2] += body
                else:
                    warnings.append(f"{src.pcode} 無法辨識點號：{body[:20]}")
                continue
            if kind == "item":
                counts.update(item=num, sub=0, subsub=0)
            elif kind == "sub":
                counts.update(sub=num, subsub=0)
            elif kind == "subsub":
                counts["subsub"] = num
            units.append([kind, num, body])
        if not units or units[0][0] != "point":
            continue
        point = str(units[0][1])
        aid = f"{src.pcode}/{point}"
        art = Node(aid, src.pcode, point, "article", [], "\n".join(u[2] for u in units), None)
        para = Node(f"{aid}/1", src.pcode, point, "paragraph", [1], units[0][2], aid)
        art.children.append(para.node_id)
        nodes += [art, para]
        item = sub = None
        for kind, num, text in units[1:]:
            if kind == "item":
                item = Node(f"{aid}/1/{num}", src.pcode, point, "item", [1, num], text, para.node_id)
                para.children.append(item.node_id); nodes.append(item); sub = None
            elif kind == "sub":
                sub = Node(f"{item.node_id}/{num}", src.pcode, point, "subitem", [1, item.path[1], num], text, item.node_id)
                item.children.append(sub.node_id); nodes.append(sub)
            else:
                n = Node(f"{sub.node_id}/{num}", src.pcode, point, "subsubitem", sub.path + [num], text, sub.node_id)
                sub.children.append(n.node_id); nodes.append(n)

    points = [int(n.article) for n in nodes if n.level == "article"]
    if points != list(range(1, len(points) + 1)):
        warnings.append(f"{src.pcode} 點號不連續：{points}")
    for n in nodes:
        n.citation = cite(law.short, n, False, src.unit)
    law.article_count = len(points)
    return law, nodes, warnings


# ---------- 附件三：圖例 ----------

_NS = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
       "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
       "xlink": "http://www.w3.org/1999/xlink"}
_T = "{%s}" % _NS["table"]
_D = "{%s}" % _NS["draw"]
_X = "{%s}" % _NS["xlink"]


@dataclass
class LegendEntry:
    node_id: str
    seq: int
    attachment: str
    category: str
    name: str
    note: str
    symbols: list[str] = field(default_factory=list)       # 相對於 legend 圖檔目錄的檔名
    note_images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _cell_text(c: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(c.itertext())).strip()


def _cell_images(c: ET.Element) -> list[str]:
    out = []
    for frame in c.iter(_D + "frame"):
        imgs = [i.get(_X + "href") for i in frame.iter(_D + "image")]
        png = [h for h in imgs if h and h.lower().endswith(".png")]   # 同一框可能另附 .wdp 備用圖，取 PNG
        out += png[:1] or imgs[:1]
    return out


def parse_legend(odt_path: Path, src: LawSource, att: Attachment, img_dir: Path) -> tuple[list[Node], list[LegendEntry], list[str]]:
    warnings: list[str] = []
    img_dir.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(odt_path)
    root = ET.fromstring(z.read("content.xml"))
    entries: list[LegendEntry] = []
    for table in root.iter(_T + "table"):
        category = ""
        for row in table.iter(_T + "table-row"):
            cells = [c for c in row if c.tag in (_T + "table-cell", _T + "covered-table-cell")]
            texts = [_cell_text(c) for c in cells]
            imgs = [_cell_images(c) for c in cells]
            nonempty = [t for t in texts if t]
            if not nonempty and not any(imgs):
                continue
            if "名稱" in texts and ("圖例" in texts or "備註" in texts):
                continue                                         # 表頭
            if len(nonempty) == 1 and not any(imgs) and not category:
                category = nonempty[0]                           # 類別標題列
                continue
            name_idx = next((i for i, t in enumerate(texts) if t), None)
            if name_idx is None:
                warnings.append(f"{att.label}「{category}」有一列只有圖、沒有名稱")
                continue
            symbols = [h for i in range(name_idx) for h in imgs[i]]
            note = " ".join(t for t in texts[name_idx + 1:] if t)
            note_imgs = [h for i in range(name_idx + 1, len(cells)) for h in imgs[i]]
            seq = len(entries) + 1
            nid = f"{src.pcode}/{att.key}/{seq}"
            e = LegendEntry(nid, seq, att.label, category or "（未分類）", texts[name_idx], note)
            for k, href in enumerate(symbols):
                fn = f"{src.pcode}_{att.key}_{seq:03d}" + (f"_{k + 1}" if k else "") + ".png"
                (img_dir / fn).write_bytes(z.read(href))
                e.symbols.append(fn)
            for k, href in enumerate(note_imgs):
                fn = f"{src.pcode}_{att.key}_{seq:03d}_note{k + 1}.png"
                (img_dir / fn).write_bytes(z.read(href))
                e.note_images.append(fn)
            if not e.symbols:
                warnings.append(f"{att.label}「{e.category}」{e.name}：沒有符號圖")
            entries.append(e)

    aid = f"{src.pcode}/{att.key}"
    cats = list(dict.fromkeys(e.category for e in entries))
    att_label = att.label.split("：")[0]
    att_node = Node(aid, src.pcode, att.key, "attachment", [],
                    f"{att.label}（{len(entries)} 個圖例，{len(cats)} 類：{'、'.join(cats)}）", None,
                    citation=f"{src.short}{att_label}", chapter=att.label)
    nodes = [att_node]
    for e in entries:
        text = e.name + (f"\n備註：{e.note}" if e.note else "")
        n = Node(e.node_id, src.pcode, att.key, "legend", [e.seq], text, aid,
                 citation=f"{src.short}{att_label}「{e.category}」{e.name}", chapter=f"{att.label} > {e.category}")
        att_node.children.append(n.node_id)
        nodes.append(n)
    return nodes, entries, warnings
