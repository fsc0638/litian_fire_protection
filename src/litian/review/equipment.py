"""設備辨識：圖塊 → 設備種類。

標準清單＝消防署附件三「消防圖說圖示範例」284 個圖例（data/lawdb/legend.json）。
- 圖塊名稱等於圖例名稱（全形／半形括號、空白不計）就直接認得。
- 各事務所自己的圖塊名稱，用「圖塊字典」對應到圖例名稱（正規表示式 → 圖例名稱）。
- 認不出的圖塊不猜，列在 unknown 給人看、補進字典。

規格（撒水頭感度、標示燈等級、滅火效能值…）從圖塊屬性讀；讀不到就是「未知」，
檢核時用上下限判斷，真的影響結論才列為「要補的資料」。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

LEGEND_JSON = Path("data/lawdb/legend.json")

# 檢核用的設備種類 ← 圖例名稱（一個圖例可以同時是兩種，例：綜合消防栓箱含連結送水管出水口）
KIND_RULES = [
    ("extinguisher", r"^(乾粉滅火器|大型滅火器)$"),
    ("hydrant", r"^室內消防栓$|^綜合消防栓"),
    ("outdoor_hydrant", r"^室外消防栓"),
    ("standpipe_outlet", r"連結送水管出水口"),
    ("sprinkler", r"^密閉式撒水頭|^撒水頭（附防護板）$"),
    ("sprinkler_sidewall", r"^撒水頭（側壁式）$"),
    ("detector", r"[局侷]限型探測器"),
    ("manual_alarm", r"^手動警報機$"),
    ("end_test_valve", r"^末端查驗閥$"),
    ("alarm_valve", r"^自動警報逆止閥"),
    ("emergency_outlet", r"緊急電源插座"),
    ("speaker", r"^揚聲器"),
    ("exit_sign", r"^出口標示燈$"),
    ("direction_light", r"^避難方向指示燈|兼樓梯避難方向指示燈"),
    ("emergency_light", r"^緊急照明燈"),
    ("smoke_vent", r"^排煙口"),
]
KIND_LABEL = {
    "extinguisher": "滅火器", "hydrant": "室內消防栓", "standpipe_outlet": "連結送水管出水口",
    "sprinkler": "撒水頭", "sprinkler_sidewall": "側壁型撒水頭", "detector": "探測器", "speaker": "揚聲器",
    "exit_sign": "出口標示燈", "direction_light": "避難方向指示燈", "emergency_light": "緊急照明燈",
    "smoke_vent": "排煙口", "outdoor_hydrant": "室外消防栓", "manual_alarm": "手動警報機",
    "end_test_valve": "末端查驗閥", "alarm_valve": "自動警報逆止閥", "emergency_outlet": "緊急電源插座",
}
DETECTOR_RE = re.compile(r"(差動式|定溫式|補償式|偵煙式)[局侷]限型探測器（(特種|[123])")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").replace("(", "（").replace(")", "）")


def kinds_of(legend: str) -> tuple[str, ...]:
    return tuple(k for k, pat in KIND_RULES if re.search(pat, legend))


def load_legend(path: Path = LEGEND_JSON) -> list[str]:
    return [it["name"] for it in json.loads(Path(path).read_text(encoding="utf-8"))]


@dataclass
class Dictionary:
    """圖塊名稱 → 圖例名稱。legend：附件三名稱；blocks：事務所圖塊字典 [(正規表示式, 圖例名稱)]。"""
    legend: list[str]
    blocks: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self):
        self._exact = {norm(n): n for n in self.legend}

    def lookup(self, block: str) -> str | None:
        n = norm(block)
        if n in self._exact:
            return self._exact[n]
        for pat, legend in self.blocks:
            if re.search(pat, block):
                return legend
        return None


def _attr(attrs: dict, *keys: str) -> str | None:
    for k, v in attrs.items():
        if v and any(key in k.upper() for key in keys):
            return str(v).strip()
    return None


def specs(legend: str, attrs: dict) -> dict:
    """從圖例名稱與圖塊屬性整理出檢核要用的規格；讀不到的鍵不放。"""
    out: dict = {}
    m = DETECTOR_RE.search(legend)
    if m:
        out["detector_type"], out["detector_class"] = m.group(1), m.group(2)
    s = _attr(attrs, "型式", "感度", "TYPE", "RESP")
    if s:
        if re.search(r"快速|第一種感度|QR|QUICK", s, re.I):
            out["response"] = "quick"
        elif re.search(r"一般|第二種感度|SR|STANDARD", s, re.I):
            out["response"] = "standard"
    s = _attr(attrs, "等級", "CLASS", "級")
    if s and (m := re.search(r"\b([ABCLMS])\b|([ABCLMS])級", s.upper())):
        out["grade"] = m.group(1) or m.group(2)
    s = _attr(attrs, "效能值", "EFF")
    if s and (m := re.search(r"A\s*[-－]?\s*(\d+)", s.upper())):
        out["a_value"] = int(m.group(1))
    return out


@dataclass
class Equipment:
    handle: str
    block: str
    legend: str
    kinds: tuple[str, ...]
    x: float                  # 公尺
    y: float
    layer: str
    spec: dict

    @property
    def label(self) -> str:
        return self.legend


def recognize(inserts: list[dict], scale: float, dictionary: Dictionary) -> tuple[list[Equipment], Counter]:
    """inserts：IR 的圖塊清單（圖面單位）。回傳（認得的消防設備, 認不出名稱的圖塊計數）。
    認得但不屬於檢核種類的圖例（閥、幫浦等）不列入設備，也不算認不出。"""
    found, unknown = [], Counter()
    for ins in inserts:
        legend = dictionary.lookup(ins["name"])
        if legend is None:
            unknown[ins["name"]] += 1
            continue
        kinds = kinds_of(legend)
        if not kinds:
            continue
        found.append(Equipment(ins.get("h", ""), ins["name"], legend, kinds, ins["x"] * scale, ins["y"] * scale,
                               ins.get("layer", ""), specs(legend, ins.get("attribs") or {})))
    return found, unknown
