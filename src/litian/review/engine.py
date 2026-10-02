"""一份圖檔的檢核流程：DXF → 各樓層平面理解 → 設備辨識 → 逐條規則 → 缺失。

命令列（開發與驗收用）：python -m litian.review.engine <in.dxf> <out.json> [--svg 資料夾]
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, mapping

from litian.drawing import ir as IR
from litian.plan import floor as F
from litian.plan import geometry as G
from litian.review import checks as K
from litian.review import coverage as C
from litian.review import equipment as E

EQUIP_MARGIN = 2.0      # 外框外 2 m 內的設備（送水口、壁掛）仍算這層；更遠的多半是圖例表


@dataclass
class FloorResult:
    sheet: int
    number: str | None
    title: str
    floor: F.Floor
    equipment: list[E.Equipment]
    findings: list[K.Finding]
    notes: list[K.Note]
    outside: int = 0


@dataclass
class Result:
    floors: list[FloorResult] = field(default_factory=list)
    unknown_blocks: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


def review_floor(fl: F.Floor, eq: list[E.Equipment], ctx: K.Context):
    findings, notes = [], []
    grid = C.WalkGrid(fl.walkable) if any("extinguisher" in e.kinds for e in eq) and not fl.walkable.is_empty else None
    for rid, _title, fn in K.RULES:
        if fn is K.extinguisher_walk:
            f, n = fn(fl, eq, ctx, grid=grid) if grid else ([], [])
        else:
            f, n = fn(fl, eq, ctx)
        findings.extend(f)
        notes.extend(n)
    order = {K.RED: 0, K.ORANGE: 1, K.YELLOW: 2, K.BLUE: 3}
    findings.sort(key=lambda x: (order[x.severity], -(x.area or 0)))
    return findings, notes


def review_dxf(path: str | Path, *, ctx: K.Context | None = None, dictionary: E.Dictionary | None = None,
               profile: F.LayerProfile | None = None, ir: dict | None = None) -> Result:
    from ezdxf import recover

    t0 = time.time()
    ctx = ctx or K.Context()
    dictionary = dictionary or E.Dictionary(E.load_legend())
    ir = ir or IR.extract(path)
    doc, _ = recover.readfile(str(path))
    prims = G.explode(doc)
    res = Result()
    for s in ir["sheets"]:
        title = IR.sheet_title(s["meta"])
        label = F.floor_label(title)
        if not label:
            continue
        number = IR.sheet_number(s["meta"])
        scale = F.unit_scale(s["meta"], ir["dxf"].get("insunits"))
        if scale is None:
            res.warnings.append(f"{number or title}：無法判斷圖面單位（圖框沒有「單位」欄、DXF 也沒設），未檢核")
            continue
        try:
            fl = F.analyze(G.by_bbox(prims, s["bbox"]), [t for t in ir["texts"] if t["f"] == s["idx"]],
                           scale=scale, title=title, profile=profile)
        except ValueError as e:
            res.warnings.append(f"{number or title}：{e}")
            continue
        eq, unknown = E.recognize([i for i in ir["inserts"] if i["f"] == s["idx"]], scale, dictionary)
        res.unknown_blocks.update(unknown)
        zone = fl.outline.buffer(EQUIP_MARGIN)
        inside = [e for e in eq if zone.covers(Point(e.x, e.y))]
        findings, notes = review_floor(fl, inside, ctx)
        res.floors.append(FloorResult(s["idx"], number, title, fl, inside, findings, notes, len(eq) - len(inside)))
    res.seconds = round(time.time() - t0, 1)
    return res


def _round(c, nd: int):
    return round(c, nd) if isinstance(c, (int, float)) else [_round(x, nd) for x in c]


def _geo(g, nd: int = 2):
    """shapely 幾何 → GeoJSON（座標四捨五入到公分）。"""
    if g is None or g.is_empty:
        return None
    m = mapping(g)
    return {"type": m["type"], "coordinates": _round(m["coordinates"], nd)}


def to_dict(res: Result, geom: bool = True) -> dict:
    """geom=False：缺失只留外框（bbox），存資料庫用；範圍圖形已畫在標示圖上。"""
    floors = []
    for fr in res.floors:
        fl = fr.floor
        kinds = Counter(k for e in fr.equipment for k in e.kinds)
        floors.append({
            "sheet": fr.sheet, "number": fr.number, "title": fr.title, "label": fl.label,
            "area": round(fl.area, 2), "outline_area": round(fl.outline.area, 2), "fireproof": fl.fireproof,
            "rooms": [{"id": r.id, "name": r.name, "kind": r.kind, "conflict": r.conflict, "area": round(r.area, 2)}
                      for r in fl.rooms],
            "equipment": dict(kinds), "equipment_outside": fr.outside,
            "warnings": fl.warnings,
            "notes": [{"rule": n.rule, "text": n.text, "law": n.law} for n in fr.notes],
            "findings": [{"no": i, "rule": f.rule, "severity": f.severity, "category": f.category, "floor": f.floor,
                          "title": f.title, "why": f.why, "fix": f.fix, "law": f.law, "missing": f.missing,
                          "rooms": f.rooms, "area": round(f.area, 2) if f.area else None, "metrics": f.metrics,
                          "bbox": [round(v, 2) for v in f.geom.bounds] if f.geom is not None and not f.geom.is_empty else None,
                          **({"geom": _geo(f.geom)} if geom else {})}
                         for i, f in enumerate(fr.findings, 1)],
        })
    return {"floors": floors, "unknown_blocks": dict(res.unknown_blocks.most_common(50)),
            "warnings": res.warnings, "seconds": res.seconds}


def main(argv: list[str]) -> int:
    """python -m litian.review.engine <in.dxf> <out.json> [--ir ir.json] [--svg 資料夾] [--no-geom]"""
    src, dst = argv[1], argv[2]
    ir = json.loads(Path(argv[argv.index("--ir") + 1]).read_text(encoding="utf-8")) if "--ir" in argv else None
    res = review_dxf(src, ir=ir)
    Path(dst).write_text(json.dumps(to_dict(res, geom="--no-geom" not in argv), ensure_ascii=False), encoding="utf-8")
    if "--svg" in argv:
        from litian.review import render
        out = Path(argv[argv.index("--svg") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for fr in res.floors:
            (out / f"{fr.floor.label}.svg").write_text(render.floor_svg(fr), encoding="utf-8")
    for fr in res.floors:
        c = Counter(f.severity for f in fr.findings)
        print(f"{fr.floor.label:5} {fr.title}：設備 {len(fr.equipment)}，缺失 {dict(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
