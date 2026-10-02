"""DXF 幾何攤平：圖層 → 折線。平面理解（認牆、柱、門、房間）用。

門、窗、柱常畫成圖塊，所以要展開圖塊（含巢狀）才看得到線；弧與圓轉成折線。
圖塊裡放在 0 層的實體沿用插入時的圖層（AutoCAD 的顯示規則），
所以「門」圖塊即使內部畫在 0 層，也會歸到插入時的 door 圖層。
DXF 一律當不可信輸入：限制展開深度與總數。
"""

from __future__ import annotations

from collections import defaultdict

MAX_DEPTH = 6
MAX_PRIMS = 600_000
FLATTEN = 2.0                # 弧線轉折線的最大誤差（圖面單位）
SKIP = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "HATCH", "DIMENSION", "SOLID", "POINT", "IMAGE",
        "WIPEOUT", "VIEWPORT", "LEADER", "MULTILEADER", "MLEADER", "TOLERANCE", "TRACE"}


class TooManyPrimitives(ValueError):
    pass


def _walk(entities, inherit: str | None, depth: int):
    for e in entities:
        t = e.dxftype()
        if t in SKIP:
            continue
        layer = e.dxf.get("layer", "0")
        if inherit and layer == "0":
            layer = inherit
        if t == "INSERT":
            if depth >= MAX_DEPTH:
                continue
            try:
                children = list(e.virtual_entities())
            except Exception:          # 壞圖塊（比例 0、遞迴參照等）略過
                continue
            yield from _walk(children, layer, depth + 1)
        else:
            yield e, layer


def explode(doc) -> list[tuple[str, list[tuple[float, float]]]]:
    """回傳 [(圖層, [(x, y), ...]), ...]；座標為圖面單位。"""
    from ezdxf import disassemble

    out = []
    for e, layer in _walk(doc.modelspace(), None, 0):
        try:
            prim = disassemble.make_primitive(e, max_flattening_distance=FLATTEN)
            pts = [(float(v.x), float(v.y)) for v in prim.vertices()]
        except Exception:
            continue
        if len(pts) >= 2:
            out.append((layer, pts))
            if len(out) > MAX_PRIMS:
                raise TooManyPrimitives(f"展開後線段超過 {MAX_PRIMS}")
    return out


def by_bbox(prims, bbox) -> dict[str, list[list[tuple[float, float]]]]:
    """取中心點落在 bbox（[x0, y0, x1, y1]）內的折線，依圖層分組。bbox 為 None 時全收。"""
    out: dict[str, list] = defaultdict(list)
    for layer, pts in prims:
        if bbox is not None:
            cx = sum(x for x, _ in pts) / len(pts)
            cy = sum(y for _, y in pts) / len(pts)
            if not (bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]):
                continue
        out[layer].append(pts)
    return dict(out)
