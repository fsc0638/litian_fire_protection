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
    ("smoke_fan", r"^排煙機"),
    ("gas_detector", r"^瓦斯漏氣檢知器"),
    ("simple_suppression", r"^簡易自動滅火設備$"),
    ("special_suppression", r"^水霧噴頭$|^泡沫噴頭$|^泡沫頭|^CO2噴頭|^乾粉（海龍替代品）噴頭|^乾粉（海龍替代品）套裝型"),
]
KIND_LABEL = {
    "extinguisher": "滅火器", "hydrant": "室內消防栓", "standpipe_outlet": "連結送水管出水口",
    "sprinkler": "撒水頭", "sprinkler_sidewall": "側壁型撒水頭", "detector": "探測器", "speaker": "揚聲器",
    "exit_sign": "出口標示燈", "direction_light": "避難方向指示燈", "emergency_light": "緊急照明燈",
    "smoke_vent": "排煙口", "outdoor_hydrant": "室外消防栓", "manual_alarm": "手動警報機",
    "end_test_valve": "末端查驗閥", "alarm_valve": "自動警報逆止閥", "emergency_outlet": "緊急電源插座",
}
DETECTOR_RE = re.compile(r"(差動式|定溫式|補償式|偵煙式)[局侷]限型探測器（(特種|[123])")


BLOCKS_YAML = Path("data/review/blocks.yaml")


def norm(s: str) -> str:
    """名稱比對用：去空白、括號與逗號全半形一致、「侷／局」一致（圖例與事務所寫法常混用）。"""
    s = re.sub(r"\s+", "", s or "").replace("(", "（").replace(")", "）")
    return s.replace("，", "、").replace(",", "、").replace("侷", "局")


def kinds_of(legend: str) -> tuple[str, ...]:
    return tuple(k for k, pat in KIND_RULES if re.search(pat, legend))


def load_legend(path: Path = LEGEND_JSON) -> list[str]:
    return [it["name"] for it in json.loads(Path(path).read_text(encoding="utf-8"))]


def load_blocks(path: Path = BLOCKS_YAML) -> tuple[list, list]:
    """圖塊字典檔 → (圖塊名稱規則, 圖層名稱規則)；每條 (正規表示式, 圖例名稱, 預設規格)。檔案不存在回空。"""
    import yaml
    p = Path(path)
    if not p.exists():
        return [], []
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    conv = lambda items: [(it["match"], it["legend"], dict(it.get("spec") or {})) for it in items or []]  # noqa: E731
    return conv(d.get("blocks")), conv(d.get("layers"))


@dataclass
class Dictionary:
    """圖塊 → 圖例名稱（＋預設規格）。
    legend：附件三名稱（名稱正規化後相同即認得）；blocks／layers：事務所圖塊字典，(正規表示式, 圖例名稱[, 規格])。
    圖塊名稱認不出時（例：匿名圖塊 A$C…）再用所在圖層的名稱辨識。"""
    legend: list[str]
    blocks: list[tuple] = field(default_factory=list)
    layers: list[tuple] = field(default_factory=list)

    def __post_init__(self):
        self._exact = {norm(n): n for n in self.legend}
        self.blocks = [(b[0], b[1], b[2] if len(b) > 2 else {}) for b in self.blocks]
        self.layers = [(b[0], b[1], b[2] if len(b) > 2 else {}) for b in self.layers]
        bad = [b[1] for b in self.blocks + self.layers if b[1] not in set(self.legend)]
        if bad:
            raise ValueError(f"圖塊字典裡有不在附件三圖例中的名稱：{bad[:5]}")

    @classmethod
    def default(cls) -> "Dictionary":
        blocks, layers = load_blocks()
        return cls(load_legend(), blocks, layers)

    def _by(self, rules, name: str):
        n = norm(name)
        if n in self._exact:
            return self._exact[n], {}
        for pat, legend, spec in rules:
            if re.search(pat, name):
                return legend, spec
        return None

    def lookup(self, block: str) -> str | None:
        hit = self._by(self.blocks, block)
        return hit[0] if hit else None

    def match(self, block: str, layer: str = "") -> tuple[str, dict] | None:
        return self._by(self.blocks, block) or (self._by(self.layers, layer) if layer else None)


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
    if re.match(r"室內消防栓|綜合消防栓", legend):
        s = _attr(attrs, "種類", "CLASS", "TYPE")
        if s and (m := re.search(r"第?\s*([一二12])\s*種", s)):
            out["hydrant_class"] = {"一": "1", "二": "2"}.get(m.group(1), m.group(1))
    s = _attr(attrs, "開口面積", "面積", "AREA")
    if s and (m := re.search(r"(\d+(?:\.\d+)?)", s)):
        out["open_area"] = float(m.group(1))                      # ㎡
    s = _attr(attrs, "尺寸", "SIZE")
    if "open_area" not in out and s and (m := re.search(r"(\d+(?:\.\d+)?)\s*[xX×*＊]\s*(\d+(?:\.\d+)?)", s)):
        a, b = float(m.group(1)), float(m.group(2))
        k = 0.001 if max(a, b) > 20 else (0.01 if max(a, b) > 5 else 1.0)    # mm／cm／m
        out["open_area"] = round(a * k * b * k, 4)
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
        hit = dictionary.match(ins["name"], ins.get("layer", ""))
        if hit is None:
            unknown[ins["name"]] += 1
            continue
        legend, defaults = hit
        kinds = kinds_of(legend)
        if not kinds:
            continue
        spec = {**defaults, **specs(legend, ins.get("attribs") or {})}     # 圖塊屬性優先於字典預設
        found.append(Equipment(ins.get("h", ""), ins["name"], legend, kinds, ins["x"] * scale, ins["y"] * scale,
                               ins.get("layer", ""), spec))
    return found, unknown
