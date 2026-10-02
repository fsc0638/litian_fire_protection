"""避難逃生設備的逐項檢核：出口標示燈、避難方向指示燈（第 146-2、146-3 條）、緊急照明（第 24、178、179 條）。

只在本層圖上有該種設備時檢核「設得對不對」；該不該設由 required.py 判定（應設卻沒有 → 另列缺失）。
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union

from litian.plan.floor import Floor
from litian.review import coverage as C
from litian.review.checks import ORANGE, RED, YELLOW, Context, Finding, Note, _fmt, _of, _room_names, _rooms_touching, _sev_for
from litian.review.equipment import Equipment

EXIT_NEAR = 3.0                       # 出口標示燈「設於出入口上方或其緊鄰」：門 3 m 內
DIR_RANGE = {"A": 20.0, "B": 15.0, "C": 10.0}      # 第 146-2 條：避難方向指示燈有效範圍（步行距離）
REFUGE_EXIT = 30.0                    # 第 179 條第 1 款：避難層居室 30 m 內可達屋外出口得免設緊急照明


def _dedupe(points, tol=1.0):
    out = []
    for p in points:
        if all(abs(p[0] - q[0]) > tol or abs(p[1] - q[1]) > tol for q in out):
            out.append(p)
    return out


def exit_signs(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    signs = _of(eq, "exit_sign")
    if not signs:
        return [], []
    targets = [(d, "通往戶外之出入口", "D0120029/146-3/1/1") for d in _dedupe(floor.exterior_doors())]
    seen = [d for d, _, _ in targets]
    targets += [(d, "通往直通樓梯之出入口", "D0120029/146-3/1/2") for d in _dedupe(floor.stair_doors())
                if all(abs(d[0] - q[0]) > 1 or abs(d[1] - q[1]) > 1 for q in seen)]
    findings = []
    pts = np.array([(e.x, e.y) for e in signs])
    for (x, y), what, law in targets:
        dist = float(np.hypot(pts[:, 0] - x, pts[:, 1] - y).min())
        if dist <= EXIT_NEAR:
            continue
        g = Point(x, y).buffer(1.2)
        rooms = _rooms_touching(floor, g)
        findings.append(Finding(
            "EXIT-146-3", RED, "未設置", floor.label or "", f"{'、'.join(_room_names(rooms)[:2]) or '標示位置'}旁的{what}未設出口標示燈",
            f"出口標示燈應設於{what}上方或其緊鄰之有效引導避難處；此出入口最近的出口標示燈約 {_fmt(dist)} m",
            "於該出入口上方設置出口標示燈；若符合第 146 條免設條件（可直接看見出口且步行距離短等），請在圖上註明",
            [law], rooms=_room_names(rooms), area=None, geom=g, metrics={"nearest": round(dist, 2)}))
    notes = [Note("EXIT-146-3", f"已檢查 {len(targets)} 處通往戶外或直通樓梯的出入口（依門圖塊位置判讀）",
                  ["D0120029/146-3/1"])] if targets else []
    return findings, notes


def direction_lights(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    lights = _of(eq, "direction_light")
    if not lights:
        return [], []
    corridors = [r for r in floor.rooms if r.kind == "corridor"]
    if not corridors:
        return [], [Note("DIR-146-3", "本層未辨識出走廊或通道（房名需含「走廊」「通道」「門廳」等），未檢核避難方向指示燈涵蓋範圍")]
    grid = grid or C.WalkGrid(floor.walkable)
    cells = grid.region_cells(unary_union([r.polygon for r in corridors]))

    def covered(lenient: bool, factor: float):
        groups: dict[str, list] = {}
        for e in lights:
            g = e.spec.get("grade") if e.spec.get("grade") in DIR_RANGE else ("A" if lenient else "C")
            groups.setdefault(g, []).append((e.x, e.y))
        cov = np.zeros_like(cells)
        for g, pts in groups.items():
            d = grid.distances(pts)
            cov |= np.nan_to_num(d, nan=np.inf) <= DIR_RANGE[g] * factor
        return cov

    hard = cells & ~covered(True, 1 + C.WALK_TOL)
    near = cells & ~covered(True, 1.0) & ~hard
    soft = cells & ~covered(False, 1.0) & ~hard & ~near
    unknown_grade = any(e.spec.get("grade") not in DIR_RANGE for e in lights)
    findings = []
    for sev, mask in ((RED, hard), (ORANGE, near), (YELLOW, soft)):
        for p in grid.cells_to_polygons(mask):
            rooms = _rooms_touching(floor, p)
            if sev == YELLOW:
                why = "避難方向指示燈等級未標示；若為 C 級（有效範圍 10 m），此段走廊不在任何指示燈有效範圍內"
                missing = ["避難方向指示燈等級（A／B／C 級）"] if unknown_grade else []
            else:
                why = ("走廊、通道各部分應在避難方向指示燈有效範圍內（A 級 20 m、B 級 15 m、C 級 10 m，步行距離）"
                       + ("；此範圍略超過，在計算誤差內，請人工確認" if sev == ORANGE else ""))
                missing = []
            findings.append(Finding(
                "DIR-146-3", _sev_for(rooms, sev), "距離超過" if sev != YELLOW else "資料不足", floor.label or "",
                f"{'、'.join(_room_names(rooms)[:2]) or '走廊'}有 {_fmt(p.area)} ㎡ 不在避難方向指示燈有效範圍內",
                why, "在此段走廊（優先轉彎處）增設避難方向指示燈，或改用較高等級", ["D0120029/146-3/2/3", "D0120029/146-2/1/1"],
                missing=missing, rooms=_room_names(rooms), area=p.area, geom=p))
    return findings, []


EML_EXEMPT_LABEL = r"廁|洗手間|浴室|盥洗|儲藏|機械室|機房"


def emergency_lights(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    import re
    lights = _of(eq, "emergency_light")
    if not lights:
        return [], []
    refuge = floor.label == "1F"
    ext = floor.exterior_doors()
    d_ext = None
    if refuge and ext:
        grid = grid or C.WalkGrid(floor.walkable)
        d_ext = grid.distances(ext)
    findings, exempt, refuge_ok, housing = [], [], [], []
    for room in floor.rooms:
        if room.kind in ("void", "outdoor", "shaft", "elevator") or room.area < 2:
            continue
        names = " ".join(room.labels)
        if not room.conflict and (room.kind in ("toilet", "machine") or re.search(EML_EXEMPT_LABEL, names)):
            exempt.append(room)
            continue
        inside = [e for e in lights if room.polygon.buffer(0.3).covers(Point(e.x, e.y))]
        if inside:
            continue
        if d_ext is not None and room.kind not in ("corridor", "stair"):
            sub = grid.region_cells(room.polygon)
            vals = d_ext[sub]
            if vals.size and np.isfinite(vals).all() and vals.max() <= REFUGE_EXIT:
                refuge_ok.append(room)
                continue
        what = {"corridor": "走廊／通道", "stair": "樓梯間"}.get(room.kind, "居室")
        if what == "居室" and ctx.occupancy == "乙-7":
            housing.append(room)                       # 集合住宅之居室得免設（第 179 條第 1 項第 3 款）
            continue
        sev = _sev_for([room], RED)
        if what == "居室" and ctx.occupancy in ("戊-1", "戊-2") and sev == RED:
            sev = ORANGE                               # 複合用途：住宅部分之居室得免設，需確認用途
        findings.append(Finding(
            "EML-24", sev, "未設置", floor.label or "", f"{room.name}（{what}，{_fmt(room.area)} ㎡）未設緊急照明燈",
            f"本層設有緊急照明設備，{what}應設置（自居室通達避難層之走廊、樓梯間亦同）；此範圍內沒有緊急照明燈",
            f"在 {room.name} 設置緊急照明燈，並以照度計算確認地面水平照度達 2 lx 以上；"
            "若屬第 179 條得免設處所（設有固定機械之工作場所部分等），請在圖上註明",
            ["D0120029/24/1/5" if room.kind in ("corridor", "stair") else "D0120029/24/1", "D0120029/179/1"],
            rooms=[room.name], area=room.area, geom=room.polygon))
    notes = [Note("EML-178", "緊急照明燈地面水平照度應達 2 lx 以上（地下建築物地下通道 10 lx），走廊曲折點應增設；"
                             "照度無法由平面圖判定，請檢附照度計算", ["D0120029/178/1"])]
    if exempt:
        notes.append(Note("EML-179", "免設緊急照明處所（洗手間、儲藏室、機械室等）：" + "、".join(r.name for r in exempt[:12]),
                          ["D0120029/179/1/6"]))
    if housing:
        notes.append(Note("EML-179", "集合住宅之居室得免設緊急照明：" + "、".join(r.name for r in housing[:12]), ["D0120029/179/1/3"]))
    if refuge_ok:
        notes.append(Note("EML-179", "避難層居室任一點 30 m 內可達屋外出口，得免設：" + "、".join(r.name for r in refuge_ok[:12]),
                          ["D0120029/179/1/1"]))
    return findings, notes


RULES = [
    ("EXIT-146-3", "出口標示燈位置", exit_signs),
    ("DIR-146-3", "避難方向指示燈有效範圍", direction_lights),
    ("EML-24", "緊急照明設置處所", emergency_lights),
]
