"""設置標準第 12 條場所分類代碼表：甲-1 … 戊-3，加上第 6 款「其他公告場所」。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .parse import Node

STANDARD = "D0120029"
CLASS_RE = re.compile(r"^[一二三四五六]、(.)類場所")


@dataclass
class Occupancy:
    code: str          # 例：甲-3
    cls: str           # 甲／乙／丙／丁／戊／其他
    number: int        # 目序；「其他」為 0
    node_id: str
    citation: str
    text: str          # 目的原文（去掉「（三）」編號）

    def to_dict(self) -> dict:
        return asdict(self)


def build_occupancy(nodes: list[Node]) -> list[Occupancy]:
    by = {n.node_id: n for n in nodes}
    para = by[f"{STANDARD}/12/1"]
    out: list[Occupancy] = []
    for item_id in para.children:
        item = by[item_id]
        m = CLASS_RE.match(item.text)
        if not m:  # 第 6 款：其他經中央主管機關公告之場所
            out.append(Occupancy("其他", "其他", 0, item.node_id, item.citation, item.text.split("、", 1)[1]))
            continue
        cls = m.group(1)
        for k, sub_id in enumerate(item.children, start=1):
            sub = by[sub_id]
            text = re.sub(r"^[（(][^）)]+[）)]", "", sub.text).strip()
            out.append(Occupancy(f"{cls}-{k}", cls, k, sub.node_id, sub.citation, text))
    return out
