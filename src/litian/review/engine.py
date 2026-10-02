"""一份圖檔的檢核流程：DXF → 各樓層平面理解 → 設備辨識 → 逐條規則 → 缺失。

命令列（開發與驗收用）：python -m litian.review.engine <in.dxf> <out.json> [--svg 資料夾]
"""

from __future__ import annotations

import hashlib
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
from litian.review import escape as ESC
from litian.review import piping as PIPE
from litian.review import rescue as RES
from litian.review import required as RQ

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
    profile: RQ.Profile | None = None
    requirements: list[RQ.Requirement] = field(default_factory=list)
    building_findings: list[K.Finding] = field(default_factory=list)
    building_notes: list[K.Note] = field(default_factory=list)
    unknown_blocks: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


ALL_RULES = K.RULES + ESC.RULES + RES.RULES + PIPE.RULES
WALK_KINDS = {"extinguisher", "direction_light", "emergency_light"}      # 這些規則要算步行距離


def review_floor(fl: F.Floor, eq: list[E.Equipment], ctx: K.Context):
    findings, notes = [], []
    need_grid = any(k in WALK_KINDS for e in eq for k in e.kinds) and not fl.walkable.is_empty
    grid = C.WalkGrid(fl.walkable) if need_grid else None
    for rid, _title, fn in ALL_RULES:
        if fn is K.extinguisher_walk and grid is None:
            continue
        f, n = fn(fl, eq, ctx, grid=grid)
        findings.extend(f)
        notes.extend(n)
    sort_findings(findings)
    return findings, notes


def sort_findings(findings: list[K.Finding]) -> None:
    order = {K.RED: 0, K.ORANGE: 1, K.YELLOW: 2, K.BLUE: 3}
    findings.sort(key=lambda x: (order[x.severity], -(x.area or 0)))


def presence_findings(res: Result) -> None:
    """應設設備（第 14～30-1 條）vs 圖面：應設卻整層沒有該設備 → 缺失。"""
    plan_floors = [fr for fr in res.floors if RQ._level(fr.floor.label or "")[0] in ("above", "base")]
    if not plan_floors:
        return
    required = [r for r in res.requirements if r.status == RQ.REQUIRED and r.kinds]
    if not any(fr.equipment for fr in res.floors):
        if required:
            res.building_findings.append(K.Finding(
                "REQ", K.YELLOW, "資料不足", "全棟", "圖面未認出任何消防設備，無法比對應設設備",
                "依場所判定應設：" + "、".join(r.equipment for r in required) + "；但圖上沒有認得的消防設備符號"
                "（可能上傳的是建築圖，或設備圖塊名稱不在圖例字典中）",
                "上傳消防設備平面圖；若已上傳，請確認設備圖塊名稱並補進圖塊字典",
                sorted({law for r in required for law in r.law}), missing=["消防設備平面圖"]))
        return
    # 同一樓層常分成幾張圖（例：室內栓火警、滅火器避難廣播、排煙各一張）：設備合併看，缺失記在該層第一張圖
    by_label: dict[str, list[FloorResult]] = {}
    for fr in plan_floors:
        by_label.setdefault(fr.floor.label, []).append(fr)
    for r in required:
        targets = [frs for lab, frs in by_label.items() if r.floors is None or lab in r.floors]
        for frs in targets:
            fr = frs[0]
            eqs = [e for x in frs for e in x.equipment]
            if any(k in e.kinds for e in eqs for k in r.kinds):
                continue
            sev, extra, law = K.RED, "", list(r.law)
            has_spk = any("sprinkler" in e.kinds for e in eqs)
            if r.key == "15" and has_spk:
                sev, extra = K.ORANGE, "；本層設有自動撒水設備，若在其有效範圍內得免設（第 15 條第 2 項），請確認"
                law.append("D0120029/15/2")
            if r.key == "19" and has_spk and any("第 19 條第 2 項" in n for n in r.notes):
                sev, extra = K.ORANGE, "；本層設有自動撒水設備，符合條件者在其有效範圍內得免設（第 19 條第 2 項），請確認"
                law.append("D0120029/19/2")
            fr.findings.append(K.Finding(
                f"REQ-{r.key}", sev, "未設置", fr.floor.label or "", f"依規定應設{r.equipment}，本層圖上未見",
                f"判定理由：{r.why}{extra}", f"於本層配置{r.equipment}，並依相關設置規定檢討位置與數量",
                law, metrics={"equipment": r.equipment}))
    for fr in res.floors:
        sort_findings(fr.findings)


def review_dxf(path: str | Path, *, ctx: K.Context | None = None, dictionary: E.Dictionary | None = None,
               profile: F.LayerProfile | None = None, ir: dict | None = None) -> Result:
    from ezdxf import recover

    t0 = time.time()
    ctx = ctx or K.Context()
    dictionary = dictionary or E.Dictionary.default()
    ir = ir or IR.extract(path)
    prof_all = profile or F.LayerProfile()
    floor_sheets = [s for s in ir["sheets"] if s.get("role", "main") == "main" and F.floor_label(IR.sheet_title(s["meta"]))]
    boxes = [s["bbox"] for s in floor_sheets if s["bbox"]]
    unit = F.unit_scale(floor_sheets[0]["meta"] if floor_sheets else {}, ir["dxf"].get("insunits")) or 0.01
    doc, _ = recover.readfile(str(path))
    # 只展開平面圖範圍內的牆、柱、門、窗（認房間用）；弧線轉折誤差統一約 2 cm
    prims = G.explode(doc, keep=lambda layer: prof_all.role(layer) is not None,
                      boxes=boxes if len(boxes) == len(floor_sheets) else None, flatten=0.02 / unit)
    del doc
    res = Result()
    for s in ir["sheets"]:
        if s.get("role", "main") != "main":
            continue                                   # 細部放大圖、涵蓋檢討頁：內容已在主圖
        title = IR.sheet_title(s["meta"])
        label = F.floor_label(title)
        if not label:
            continue
        number = IR.sheet_number(s["meta"])
        scale = F.unit_scale(s["meta"], ir["dxf"].get("insunits"))
        if scale is None:
            res.warnings.append(f"{number or title}：無法判斷圖面單位（圖框沒有「單位」欄、DXF 也沒設），未檢核")
            continue
        prof = profile or F.LayerProfile()
        doors = [(i["x"], i["y"]) for i in ir["inserts"] if i["f"] == s["idx"] and prof.role(i.get("layer", "")) == "door"]
        try:
            fl = F.analyze(G.by_bbox(prims, s["bbox"]), [t for t in ir["texts"] if t["f"] == s["idx"]],
                           scale=scale, title=title, profile=profile, doors=doors)
        except ValueError as e:
            res.warnings.append(f"{number or title}：{e}")
            continue
        eq, unknown = E.recognize([i for i in ir["inserts"] if i["f"] == s["idx"]], scale, dictionary)
        res.unknown_blocks.update(unknown)
        zone = fl.outline.buffer(EQUIP_MARGIN)
        inside = [e for e in eq if zone.covers(Point(e.x, e.y))]
        findings, notes = review_floor(fl, inside, ctx)
        res.floors.append(FloorResult(s["idx"], number, title, fl, inside, findings, notes, len(eq) - len(inside)))
    if res.floors:
        res.profile = RQ.build_profile(res.floors, ctx)
        res.requirements = RQ.evaluate(res.profile)
        presence_findings(res)
    pf, pn = PIPE.check_texts(ir, [e for fr in res.floors for e in fr.equipment], ctx)
    res.building_findings.extend(pf)
    res.building_notes.extend(pn)
    sort_findings(res.building_findings)
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


def finding_key(f: K.Finding) -> str:
    """同一條缺失在重跑檢核後的識別碼：規則＋樓層＋房間＋範圍外框（取整到公尺）；沒有範圍的用設備名或標註文字。
    缺失內的數字（需幾個、多少 ㎡）會隨檢核條件變，不放進識別碼。"""
    parts = [f.rule, f.floor or "", ",".join(sorted(f.rooms))]
    if f.geom is not None and not f.geom.is_empty:
        parts.append(",".join(str(round(v)) for v in f.geom.bounds))
    else:
        parts.append(str(f.metrics.get("equipment") or f.metrics.get("text") or f.category))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _finding(i: int, f: K.Finding, geom: bool) -> dict:
    return {"no": i, "key": finding_key(f), "rule": f.rule, "severity": f.severity, "category": f.category, "floor": f.floor,
            "title": f.title, "why": f.why, "fix": f.fix, "law": f.law, "missing": f.missing,
            "rooms": f.rooms, "area": round(f.area, 2) if f.area else None, "metrics": f.metrics,
            "bbox": [round(v, 2) for v in f.geom.bounds] if f.geom is not None and not f.geom.is_empty else None,
            **({"geom": _geo(f.geom)} if geom else {})}


def svg_name(fr: FloorResult) -> str:
    """標示圖檔名：同一樓層可能有好幾張圖（各系統一張），用「樓層-圖紙序號」區分。"""
    return f"{fr.floor.label}-{fr.sheet}"


def to_dict(res: Result, geom: bool = True) -> dict:
    """geom=False：缺失只留外框（bbox），存資料庫用；範圍圖形已畫在標示圖上。"""
    floors = []
    for fr in res.floors:
        fl = fr.floor
        kinds = Counter(k for e in fr.equipment for k in e.kinds)
        floors.append({
            "sheet": fr.sheet, "number": fr.number, "title": fr.title, "label": fl.label, "svg_name": svg_name(fr),
            "area": round(fl.area, 2), "outline_area": round(fl.outline.area, 2), "fireproof": fl.fireproof,
            "rooms": [{"id": r.id, "name": r.name, "kind": r.kind, "conflict": r.conflict, "area": round(r.area, 2)}
                      for r in fl.rooms],
            "equipment": dict(kinds), "equipment_outside": fr.outside,
            "warnings": fl.warnings,
            "notes": [{"rule": n.rule, "text": n.text, "law": n.law} for n in fr.notes],
            "findings": [_finding(i, f, geom) for i, f in enumerate(fr.findings, 1)],
        })
    building = None
    if res.profile is not None or res.building_findings:
        p = res.profile
        building = {
            "profile": None if p is None else {
                "occupancy": p.occupancy, "stories": p.stories, "height": p.height, "site_area": p.site_area,
                "total_area": round(p.total_area, 2), "roof_area": round(p.roof_area, 2),
                "floors": [{"label": f.label, "level": f.level, "area": round(f.area, 2), "no_opening": f.no_opening}
                           for f in p.floors],
                "high_rise": p.high_rise, "notes": p.notes},
            "notes": [{"rule": n.rule, "text": n.text, "law": n.law} for n in res.building_notes],
            "requirements": [{"key": r.key, "equipment": r.equipment, "kinds": list(r.kinds), "status": r.status,
                              "why": r.why, "law": r.law, "floors": r.floors, "missing": r.missing, "notes": r.notes}
                             for r in res.requirements],
            "findings": [_finding(i, f, False) for i, f in enumerate(res.building_findings, 1)],
        }
    return {"floors": floors, "building": building, "unknown_blocks": dict(res.unknown_blocks.most_common(50)),
            "warnings": res.warnings, "seconds": res.seconds}


def main(argv: list[str]) -> int:
    """python -m litian.review.engine <in.dxf> <out.json> [--ir ir.json] [--ctx 條件.json] [--svg 資料夾] [--no-geom]"""
    src, dst = argv[1], argv[2]
    ir = json.loads(Path(argv[argv.index("--ir") + 1]).read_text(encoding="utf-8")) if "--ir" in argv else None
    ctx = None
    if "--ctx" in argv:
        ctx = K.Context.from_dict(json.loads(Path(argv[argv.index("--ctx") + 1]).read_text(encoding="utf-8")))
    res = review_dxf(src, ir=ir, ctx=ctx)
    Path(dst).write_text(json.dumps(to_dict(res, geom="--no-geom" not in argv), ensure_ascii=False), encoding="utf-8")
    if "--svg" in argv:
        from litian.review import render
        out = Path(argv[argv.index("--svg") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for fr in res.floors:
            (out / f"{svg_name(fr)}.svg").write_text(render.floor_svg(fr), encoding="utf-8")
    for fr in res.floors:
        c = Counter(f.severity for f in fr.findings)
        print(f"{fr.floor.label:5} {fr.title}：設備 {len(fr.equipment)}，缺失 {dict(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
