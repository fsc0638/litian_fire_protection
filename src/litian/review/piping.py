"""管線檢查：讀圖面上的管徑標註、立管註記、配管材質說明，對照條文明定的下限（第 32、56、181 條）。

法規多寫「管徑依水力計算配置」，所以這裡只檢查條文寫死的下限與必附資料：
- 室內消防栓立管：第一種 ≥ 63 mm、第二種 ≥ 50 mm（第 32 條第 1 項第 1 款第 4 目）；
  與連結送水管共用 ≥ 100 mm（同款第 3 目、第 181 條第 1 款）
- 連結送水管立管 ≥ 100 mm（第 181 條第 1 款）
- 自動撒水末端查驗閥管徑 ≥ 25 mm（第 56 條第 1 款）；設有撒水頭的樓層要有末端查驗閥（第 2 款）
- 配管材質：CNS 6445、4626、6331 或經認可之合成樹脂管（第 32 條第 1 項第 1 款第 2 目）
各家圖面寫法不同：系統別（消防栓／撒水／連結送水管）由同一段標註文字判斷，判斷不出的標註不檢核。
"""

from __future__ import annotations

import re

from litian.drawing.ir import sheet_number, sheet_title
from litian.review.checks import RED, YELLOW, Context, Finding, Note, _of

INCH = {0.5: 15, 0.75: 20, 1.0: 25, 1.25: 32, 1.5: 40, 2.0: 50, 2.5: 65, 3.0: 80, 4.0: 100, 5.0: 125, 6.0: 150, 8.0: 200}
FRAC = {"½": 0.5, "¼": 0.25, "¾": 0.75}
MM_SIZES = {15, 20, 25, 32, 40, 50, 65, 80, 100, 125, 150, 200, 250, 300}
RE_MM = re.compile(r"(?:[Øø∅ΦφΦ]|DN)\s*(\d{2,3})|(\d{2,3})\s*(?:A|mm|MM|㎜)(?![A-Za-z])")
# 吋：2"、2-1/2"、2 1/2"、2½"、2.5"、1/2"、3/4吋
RE_INCH = re.compile(r"(?<![\d./])(?:(\d+(?:\.\d+)?)\s*(?:-\s*|\s+)?)?(\d/\d|[½¼¾])?\s*(?:\"|”|″|吋|英吋)")
SYS = {
    "standpipe": re.compile(r"連結送水|送水立管|\bS\.?D\b", re.I),
    "hydrant": re.compile(r"消防栓|\bF\.?H\b", re.I),
    "sprinkler": re.compile(r"撒水|灑水|\bS\.?P\b", re.I),
}
RISER = re.compile(r"立管|RISER|\bR\.?S\b", re.I)
BRANCH = re.compile(r"支管|BRANCH", re.I)
END_VALVE = re.compile(r"末端查驗|查驗閥|TEST\s*VALVE", re.I)
PIPE_CTX = re.compile(r"管|PIPE|查驗閥|VALVE", re.I)        # 管徑標註要有管線語境，避免把「樓板 150mm」當管徑
MATERIAL = re.compile(r"CNS\s*-?\s*(6445|4626|6331)|SCH\s*\.?\s*40|碳鋼鋼管|不[銹鏽]鋼|合成樹脂", re.I)


def parse_size(s: str) -> int | None:
    """管徑標註 → mm（Ø100、DN100、100A、100mm、4"、2-1/2"）。認不出回 None。"""
    m = RE_MM.search(s)
    if m:
        v = int(m.group(1) or m.group(2))
        return v if v in MM_SIZES else None
    for m in RE_INCH.finditer(s):
        whole, frac = m.group(1), m.group(2)
        if not whole and not frac:
            continue
        v = float(whole) if whole else 0.0
        if frac:
            if "." in (whole or ""):
                continue                               # 「2.5 1/2"」之類寫法不成立
            v += FRAC.get(frac) or (int(frac[0]) / int(frac[2]))
        return INCH.get(round(v, 2))
    return None


def systems(s: str) -> set[str]:
    return {k for k, pat in SYS.items() if pat.search(s)}


def _hydrant_class(equipment, ctx: Context | None = None) -> str | None:
    """乙-11（倉庫）與丁類（工作場所）應設第一種消防栓（第 34 條第 1 項）；其餘看圖塊屬性。"""
    if ctx is not None and ctx.occupancy and (ctx.occupancy == "乙-11" or ctx.occupancy.startswith("丁")):
        return "1"
    kinds = {e.spec.get("hydrant_class") for e in equipment if "hydrant" in e.kinds}
    kinds.discard(None)
    return kinds.pop() if len(kinds) == 1 else None


def check_texts(ir: dict, equipment: list, ctx: Context) -> tuple[list[Finding], list[Note]]:
    """全部圖紙（含系統圖、昇位圖）的文字標註。equipment：全部樓層認得的設備（判斷消防栓種類用）。"""
    sheets = {s["idx"]: s for s in ir["sheets"]}
    findings: list[Finding] = []
    sized = 0
    hclass = _hydrant_class(equipment, ctx)
    high = ctx.height is not None and ctx.height > 50
    for t in ir["texts"]:
        s = t["t"]
        size = parse_size(s)
        if size is None or not (PIPE_CTX.search(s) or systems(s)):
            continue
        sized += 1
        sh = sheets.get(t.get("f"))
        where = " ".join(x for x in ((sheet_number(sh["meta"]) or "") if sh else "", sheet_title(sh["meta"]) if sh else "") if x) or "圖面"
        sy = systems(s)
        if END_VALVE.search(s) and size < 25:
            findings.append(Finding("PIPE-56", RED, "規格不符", where, f"末端查驗閥管徑 {size} mm 小於 25 mm（標註「{s[:30]}」）",
                                    "使用密閉式撒水頭之自動撒水設備，末端查驗閥管徑應在 25 mm 以上", "改用管徑 25 mm 以上之末端查驗閥及配管",
                                    ["D0120029/56/1/1"], metrics={"size": size, "text": s[:60]}))
            continue
        if BRANCH.search(s) and {"standpipe", "hydrant"} <= sy and size < 65:
            findings.append(Finding(
                "PIPE-181", RED, "規格不符", where, f"消防栓與連結送水管共用之支管 {size} mm 小於 65 mm（標註「{s[:30]}」）",
                "與室內消防栓共用立管時，支管管徑應在 65 mm 以上", "支管改為 65 mm 以上", ["D0120029/181/1/1"],
                metrics={"size": size, "text": s[:60]}))
            continue
        if not RISER.search(s):
            continue
        if "standpipe" in sy and "hydrant" in sy and high:
            findings.append(Finding(
                "PIPE-181", RED, "規格不符", where, f"建築物高度 {ctx.height} m 超過 50 m，連結送水管不得與室內消防栓共用立管（標註「{s[:30]}」）",
                "建築物高度在 50 m 以下時，始得與室內消防栓共用立管", "連結送水管改設專用立管", ["D0120029/181/1/1"],
                metrics={"size": size, "text": s[:60]}))
        if "standpipe" in sy:
            shared = "hydrant" in sy
            if size < 100:
                findings.append(Finding(
                    "PIPE-181", RED, "規格不符", where,
                    f"{'消防栓與連結送水管共用' if shared else '連結送水管'}立管管徑 {size} mm 小於 100 mm（標註「{s[:30]}」）",
                    "連結送水管立管管徑應在 100 mm 以上；高度 50 m 以下與室內消防栓共用立管者，管徑亦應在 100 mm 以上",
                    "立管改為 100 mm 以上", ["D0120029/181/1/1"] + (["D0120029/32/1/1/3"] if shared else []),
                    metrics={"size": size, "text": s[:60]}))
        elif "hydrant" in sy:
            need = {"1": 63, "2": 50}.get(hclass or "")
            if size < 50 or (need and size < need):
                findings.append(Finding(
                    "PIPE-32", RED, "規格不符", where, f"室內消防栓立管管徑 {size} mm 小於 {need or 50} mm（標註「{s[:30]}」）",
                    "室內消防栓立管管徑：第一種消防栓 63 mm 以上、第二種消防栓 50 mm 以上",
                    f"立管改為 {need or 63} mm 以上", ["D0120029/32/1/1/4"], metrics={"size": size, "text": s[:60]}))
            elif need is None and size < 63:
                findings.append(Finding(
                    "PIPE-32", YELLOW, "資料不足", where, f"室內消防栓立管 {size} mm：第一種消防栓需 63 mm 以上",
                    "立管 50 mm 只適用第二種消防栓；圖上未能判讀消防栓種類", "確認消防栓種類；若為第一種，立管改為 63 mm 以上",
                    ["D0120029/32/1/1/4"], missing=["室內消防栓種類（第一種／第二種）"], metrics={"size": size, "text": s[:60]}))
    notes: list[Note] = []
    if sized:
        if not any(MATERIAL.search(t["t"]) for t in ir["texts"]):
            findings.append(Finding(
                "PIPE-32", YELLOW, "資料不足", "全棟", "圖面未標示消防配管材質",
                "消防配管應符合 CNS 6445、4626、6331 或具同等以上強度、耐腐蝕性及耐熱性者，或經認可之合成樹脂管",
                "於圖說註明配管材質與規格", ["D0120029/32/1/1/2", "D0120029/181/1/2"], missing=["配管材質與規格（CNS 編號）"]))
        notes.append(Note("PIPE", f"讀到 {sized} 處管徑標註；管徑除條文下限外應依水力計算配置，請檢附水力計算書",
                          ["D0120029/32/1/1/3", "D0120029/181/1/4"]))
    return findings, notes


def end_test_valve(floor, eq, ctx, grid=None):
    """設有密閉式撒水頭的樓層要有末端查驗閥（第 56 條第 2 款：接裝在各層放水壓力最低之最遠支管末端）。"""
    if not _of(eq, "sprinkler"):
        return [], []
    if _of(eq, "end_test_valve"):
        return [], [Note("PIPE-56", "末端查驗閥應位於本層放水壓力最低之最遠支管末端，需由管線系統圖確認", ["D0120029/56/1/2"])]
    return [Finding("PIPE-56", RED, "未設置", floor.label or "", "本層設有撒水頭，但未見末端查驗閥",
                    "使用密閉式撒水頭之自動撒水設備，查驗閥應依各流水檢知裝置配管系統配置，接裝在各層放水壓力最低之最遠支管末端",
                    "於本層最遠支管末端設置末端查驗閥（管徑 25 mm 以上，一次側設壓力表，距地板 2.1 m 以下）",
                    ["D0120029/56/1/2"])], []


RULES = [("PIPE-56", "末端查驗閥", end_test_valve)]
