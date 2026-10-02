"""消防搶救上必要設備的逐項檢核：排煙設備（第 188、190 條）、連結送水管出水口（第 180 條）。

只在本層圖上有該種設備時檢核「設得對不對」；該不該設由 required.py 判定。
"""

from __future__ import annotations

import re

from shapely.geometry import Point

from litian.plan.floor import Floor
from litian.review import coverage as C
from litian.review.checks import ORANGE, RED, YELLOW, Context, Finding, Note, _farthest, _fmt, _of, _room_names, _rooms_touching, _sev_for
from litian.review.equipment import Equipment
from litian.review.required import _level

SMOKE_ZONE = 500.0          # 第 188 條第 1 款：每 500 ㎡ 以防煙壁區劃
SMOKE_DIST = 30.0           # 第 188 條第 3 款：防煙區劃內任一點至排煙口水平距離
SMOKE_RATIO = 0.02          # 第 188 條第 7 款：排煙口開口面積 ≥ 防煙區劃面積 2%
OUTLET_DIST = 50.0          # 第 180 條第 1 款：各層任一點至出水口水平距離
OUTLET_STAIR = 5.0          # 第 180 條第 1 款：設於樓梯間或緊急升降機間（含 5 m 內）
SMOKE_EXEMPT = r"儲藏|廁|洗手間"


SMOKE_EXEMPT_OCC = {"乙-7": "集合住宅", "乙-9": "室內溜冰場、室內游泳池"}          # 第 190 條第 1 項第 7 款
SMOKE_MAYBE_OCC = {"乙-3": "學校教室（補習班等不適用）", "乙-8": "學校活動中心、體育館（一般活動中心不適用）"}


def smoke_vents(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    vents = _of(eq, "smoke_vent")
    if not vents:
        return [], []
    if ctx.occupancy in SMOKE_EXEMPT_OCC:
        return [], [Note("SMK-190", f"{SMOKE_EXEMPT_OCC[ctx.occupancy]}得免設排煙設備，未檢核排煙口", ["D0120029/190/1/7"])]
    maybe = SMOKE_MAYBE_OCC.get(ctx.occupancy or "")
    mechanical = bool(_of(eq, "smoke_fan"))
    findings, exempt = [], []
    for room in floor.rooms:
        if room.kind in ("void", "outdoor") or room.area < 2:
            continue
        if not room.conflict and (room.kind in ("stair", "elevator", "shaft", "toilet") or re.search(SMOKE_EXEMPT, " ".join(room.labels))):
            exempt.append(room)
            continue
        if any("special_suppression" in e.kinds and room.polygon.buffer(0.3).covers(Point(e.x, e.y)) for e in eq):
            exempt.append(room)                        # 設有二氧化碳、乾粉等滅火設備之場所（第 190 條第 1 項第 5 款）
            continue
        zone = room.polygon.intersection(floor.region)
        inside = [v for v in vents if room.polygon.buffer(0.3).covers(Point(v.x, v.y))]
        if not inside:
            sev = RED if room.area > 100 else ORANGE
            findings.append(Finding(
                "SMK-188", _sev_for([room], sev), "未設置", floor.label or "", f"{room.name}（{_fmt(room.area)} ㎡）未設排煙口",
                "本層設有排煙設備，此防煙區劃內沒有排煙口" + ("；100 ㎡ 以下居室符合第 190 條區劃與裝修條件者得免設，請確認" if sev == ORANGE else ""),
                f"在 {room.name} 天花板或其下方 80 cm 內設置排煙口，使任一點 30 m 內可達；或註明免設依據",
                ["D0120029/188/1/3", "D0120029/190/1"], rooms=[room.name], area=room.area, geom=room.polygon))
            continue
        for p in C.uncovered(zone, [(v.x, v.y) for v in inside], SMOKE_DIST):
            findings.append(Finding(
                "SMK-188", _sev_for([room], RED), "距離超過", floor.label or "", f"{room.name} 有 {_fmt(p.area)} ㎡ 離排煙口超過 30 m",
                f"防煙區劃內任一位置至排煙口之水平距離應在 30 m 以下；最遠點約 {_fmt(_farthest(p, inside))} m",
                "在標示範圍附近增設排煙口", ["D0120029/188/1/3"], rooms=[room.name], area=p.area, geom=p))
        if room.area > SMOKE_ZONE:
            findings.append(Finding(
                "SMK-188", ORANGE, "需確認", floor.label or "", f"{room.name} 約 {_fmt(room.area)} ㎡，超過防煙區劃上限 500 ㎡",
                "每層樓地板面積每 500 ㎡ 內應以防煙壁（自天花板下垂 50 cm 以上）區劃；圖上未能辨識防煙壁位置",
                "以防煙壁將此空間區劃為每區 500 ㎡ 以下並標示於圖上；工廠等天花板高 5 m 以上且以耐燃一級材料裝修者得不受此限，請註明",
                ["D0120029/188/1/1", "D0120029/188/2"], rooms=[room.name], area=room.area, geom=room.polygon))
        areas = [v.spec.get("open_area") for v in inside]
        need = room.area * SMOKE_RATIO
        if mechanical:
            pass                                       # 設排煙機（機械排煙）：依第 188 條第 8 款排煙量檢討，不適用自然排煙 2%
        elif all(a is not None for a in areas):
            have = sum(areas)
            if have < need:
                findings.append(Finding(
                    "SMK-188", RED, "規格不符", floor.label or "", f"{room.name} 排煙口開口面積不足（{have:.2f} ㎡ < {need:.2f} ㎡）",
                    f"排煙口開口面積應在防煙區劃面積（約 {_fmt(room.area)} ㎡）之 2% 以上",
                    f"加大或增設排煙口，使開口面積合計達 {need:.2f} ㎡ 以上；無法自然排煙者應設排煙機",
                    ["D0120029/188/1/7"], rooms=[room.name], area=room.area, geom=room.polygon,
                    metrics={"need": round(need, 2), "have": round(have, 2)}))
        else:
            findings.append(Finding(
                "SMK-188", YELLOW, "資料不足", floor.label or "", f"{room.name} 排煙口開口面積未標示",
                f"排煙口開口面積應達防煙區劃面積之 2%（本範圍約需 {need:.2f} ㎡），圖上排煙口沒有尺寸或面積",
                "於設備表或圖例標示排煙口尺寸後重新檢核", ["D0120029/188/1/7"],
                missing=["排煙口開口尺寸或面積"], rooms=[room.name], area=room.area, geom=room.polygon))
    if maybe:
        for f in findings:
            if f.severity == RED:
                f.severity = ORANGE
                f.why += f"；若屬{maybe}得免設排煙設備（第 190 條第 1 項第 7 款），請確認"
                f.law = f.law + ["D0120029/190/1/7"]
    notes = [Note("SMK-190", "免設排煙處所（樓梯間、昇降路、管道間、儲藏室、廁所、設有二氧化碳等滅火設備之場所）："
                  + "、".join(r.name for r in exempt[:12]), ["D0120029/190/1/4", "D0120029/190/1/5"])] if exempt else []
    if mechanical:
        notes.append(Note("SMK-188", "本層設有排煙機：排煙量應每分鐘 120 m³ 以上，且依防煙區劃面積計算（第 188 條第 1 項第 8 款），請檢附計算",
                          ["D0120029/188/1/8"]))
    return findings, notes


def standpipe_outlets(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    outs = _of(eq, "standpipe_outlet")
    kind, lv = _level(floor.label or "")
    underground = ctx.occupancy == "戊-3" and kind == "base"          # 地下建築物各層
    if not outs or not ((kind == "above" and lv >= 3) or underground):
        return [], []
    findings = []
    for p in C.uncovered(floor.region, [(o.x, o.y) for o in outs], OUTLET_DIST):
        rooms = _rooms_touching(floor, p)
        findings.append(Finding(
            "SDP-180", _sev_for(rooms, RED), "距離超過", floor.label or "",
            f"{'、'.join(_room_names(rooms)[:2]) or '標示範圍'} 有 {_fmt(p.area)} ㎡ 離連結送水管出水口超過 50 m",
            f"各層任一點至出水口之水平距離應在 50 m 以下；最遠點約 {_fmt(_farthest(p, outs))} m",
            "於樓梯間或緊急昇降機間增設出水口", ["D0120029/180/1/1"], rooms=_room_names(rooms), area=p.area, geom=p))
    stairs = [r.polygon for r in floor.rooms if r.kind == "stair" or re.search(r"緊急昇降|緊急升降", " ".join(r.labels))]
    for o in outs:
        pt = Point(o.x, o.y)
        d = min((s.distance(pt) for s in stairs), default=None)
        if d is None or d > OUTLET_STAIR:
            g = pt.buffer(1.2)
            findings.append(Finding(
                "SDP-180", ORANGE, "需確認", floor.label or "", "連結送水管出水口未設於樓梯間或緊急昇降機間 5 m 內",
                "出水口應設於樓梯間或緊急升降機間等（含該處 5 m 以內）消防人員易於施行救火之位置"
                + (f"；最近的樓梯間約 {_fmt(d)} m" if d is not None else "；本層未辨識出樓梯間"),
                "將出水口移至樓梯間或緊急昇降機間 5 m 內", ["D0120029/180/1/1"],
                rooms=_room_names(_rooms_touching(floor, g)), geom=g))
    return findings, []


RULES = [
    ("SMK-188", "排煙口位置與面積", smoke_vents),
    ("SDP-180", "連結送水管出水口", standpipe_outlets),
]
