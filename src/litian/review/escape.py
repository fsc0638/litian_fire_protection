"""避難逃生設備的逐項檢核：出口標示燈、避難方向指示燈（第 146-2、146-3 條）、緊急照明（第 24、178、179 條）。

只在本層圖上有該種設備時檢核「設得對不對」；該不該設由 required.py 判定（應設卻沒有 → 另列缺失）。
依第 23 條判定非應設（自主設置）時的降級由 engine 處理（它才有應設判定結果），本檔的 why 都寫成可再附加一句說明的形式。
"""

from __future__ import annotations

import math
import re

import numpy as np
import shapely
from scipy.sparse import coo_matrix, diags
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import Point
from shapely.ops import unary_union

from litian.plan.floor import Floor, Room, room_kind
from litian.review import coverage as C
from litian.review.checks import ORANGE, RED, YELLOW, Context, Finding, Note, _fmt, _of, _room_names, _rooms_touching, _sev_for
from litian.review.equipment import Equipment
from litian.review.required import _level

EXIT_NEAR = 3.0                       # 出口標示燈「設於出入口上方或其緊鄰」：門 3 m 內
DIR_RANGE = {"A": 20.0, "B": 15.0, "C": 10.0}      # 第 146-2 條：避難方向指示燈有效範圍（步行距離）
EXIT_RANGE = {"A": (60.0, 40.0), "B": (30.0, 20.0), "C": (15.0, 15.0)}   # 第 146-2 條：出口標示燈（未顯示／顯示避難方向符號）
REFUGE_EXIT = 30.0                    # 第 179 條第 1 款：避難層居室 30 m 內可達屋外出口得免設緊急照明
EXIT_EXEMPT_WALK = {True: 20.0, False: 10.0}       # 第 146 條第 1 項第 1 款第 1 目：至主要出入口步行距離（避難層／其他樓層）
STAIR_MAX = 200.0                     # 標示樓梯卻大於這個面積（㎡）：樓梯沒圍成獨立房間，不當樓梯
DOOR_REACH = 2.0                      # 貼外框的門：從門往外 2 m 內不穿牆可走到外框外，才算通往屋外
SERVE_R = 1.2                         # 門口兩側各 1.2 m 內的可走格當作出入口的起點
LIGHT_TOL = 0.5                       # 燈的圖形中心落在牆上、門弧上（不在任何房間內）時，0.5 m 內的房間都算有燈


def _dedupe(points, tol=1.0):
    out = []
    for p in points:
        if all(abs(p[0] - q[0]) > tol or abs(p[1] - q[1]) > tol for q in out):
            out.append(p)
    return out


def _refuge(floor: Floor) -> bool:
    """避難層：以地上一層計（圖上沒有其他資訊可判斷）。"""
    return _level(floor.label or "") == ("above", 1)


def _door_to_outside(floor: Floor, d) -> str:
    """貼著外框的門是否真的通往屋外。
    'open'：門在外框外，或從門往外 2 m 內不穿牆可走到外框外；
    'wall'：門畫在連續的牆線上（牆線沒在門的位置斷開：寬度超過 6 m 的大型拉門、鐵捲門，或門沒畫開口）；
    'inside'：四周都被牆擋住（外牆凹處旁的室內門，只是離外框近）。"""
    p = Point(d)
    shapely.prepare(floor.walls)
    if shapely.dwithin(floor.walls, p, 0.01):
        return "wall"
    if not floor.outline.covers(p):
        return "open"
    angs = np.linspace(0, 2 * math.pi, 32, endpoint=False)
    ends = np.column_stack([d[0] + DOOR_REACH * np.cos(angs), d[1] + DOOR_REACH * np.sin(angs)])
    rays = shapely.linestrings([[d, tuple(e)] for e in ends])
    ok = ~shapely.intersects(floor.walls, rays) & ~shapely.covers(floor.outline, shapely.points(ends))
    return "open" if ok.any() else "inside"


def _served_walk(grid: C.WalkGrid, d, block=None) -> float | None:
    """從門口往樓地板側走（不穿過 block，例：樓梯間本身），可到達範圍內最遠的步行距離（m）。
    門口附近沒有樓地板（通往屋頂、挑空）回 None。門口 1.2 m 內兩側的格子都當起點，範圍只會算大（偏保守）。"""
    near = grid.free & ((grid.gx - d[0]) ** 2 + (grid.gy - d[1]) ** 2 <= SERVE_R ** 2)
    keep = np.ones(grid.n)
    if block is not None and not block.is_empty:
        blocked = grid.region_cells(block)
        near &= ~blocked
        keep[grid.idx[blocked]] = 0.0
    src = grid.idx[near]
    if src.size == 0:
        return None
    g = (diags(keep) @ grid.graph @ diags(keep)).tocsr()
    g.eliminate_zeros()
    # 加一個代表門口的節點，連到起點格（權重＝直線距離），距離從門口起算
    w = np.hypot(grid.gx[near] - d[0], grid.gy[near] - d[1]) + 1e-6
    g.resize((grid.n + 1, grid.n + 1))
    g = (g + coo_matrix((w, (np.full(src.size, grid.n), src)), shape=g.shape)).tocsr()
    dist = dijkstra(g, directed=False, indices=grid.n)[:grid.n]
    fin = dist[np.isfinite(dist)]
    return float(fin.max()) if fin.size else None


def exit_signs(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    signs = _of(eq, "exit_sign")
    if not signs:
        return [], []
    refuge = _refuge(floor)
    # 通往戶外之出入口：只在避難層。貼外框但四周被牆擋住的是室內門，不算
    ext, inner, upper = [], [], []
    for d in _dedupe(floor.exterior_doors()):
        side = _door_to_outside(floor, d)
        if side == "inside":
            inner.append(d)
        elif not refuge:
            if side == "open":
                upper.append(d)
        else:
            ext.append((d, side))
    targets = [(d, "通往戶外之出入口", "D0120029/146-3/1/1", side) for d, side in ext]
    seen = [d for d, _ in ext]
    # 通往直通樓梯之出入口：名稱無衝突、面積合理的樓梯間
    stairs = [r for r in floor.rooms if r.kind == "stair" and not r.conflict and r.area <= STAIR_MAX]
    stair_doors = [d for d in floor.doors if any(s.polygon.boundary.distance(Point(d)) <= 1.5 for s in stairs)]
    targets += [(d, "通往直通樓梯之出入口", "D0120029/146-3/1/2", "stair") for d in _dedupe(stair_doors)
                if all(abs(d[0] - q[0]) > 1 or abs(d[1] - q[1]) > 1 for q in seen)]
    stair_zone = unary_union([s.polygon for s in stairs]) if stairs else None
    level = _level(floor.label or "")[0]
    # 非避難層通往屋外的門只在說明列出；屋突層的外框是整片屋頂，不列；樓梯間的門已另外檢查
    upper = [] if level == "roof" else [d for d in upper if all(abs(d[0] - q[0]) > 1 or abs(d[1] - q[1]) > 1
                                                             for q, *_ in targets)]
    # 第 146 條第 1 項第 1 款的主要出入口：避難層為通往戶外之出入口，其他樓層為通往直通樓梯之出入口；地下層、無開口樓層不適用
    exemptable = level != "base" and (floor.label or "") not in ctx.no_opening
    findings = []
    pts = np.array([(e.x, e.y) for e in signs])
    for (x, y), what, law, side in targets:
        dist = float(np.hypot(pts[:, 0] - x, pts[:, 1] - y).min())
        if dist <= EXIT_NEAR:
            continue
        sev, extra, laws = RED, "", [law]
        if side == "wall":
            sev, extra = ORANGE, ("；此門畫在連續的牆線上（牆線在門的位置沒有斷開），可能是寬度超過 6 m 的大型拉門、鐵捲門等貨物出入口。"
                                  "作為避難出口使用時應設出口標示燈；不作避難出口使用者，請在圖上註明")
        else:
            if grid is None and not floor.walkable.is_empty:
                grid = C.WalkGrid(floor.walkable)
            served = (_served_walk(grid, (x, y), stair_zone if side == "stair" else None)
                      if grid is not None and grid.n else math.inf)          # 算不了步行距離 → 不套免設
            limit = EXIT_EXEMPT_WALK[refuge]
            if served is None and side == "stair":
                sev, extra = ORANGE, ("；此樓梯門的另一側沒有樓地板（屋頂、挑空），沒有人員自該側經此門避難（例：屋突層通往屋頂的門），"
                                      "是否仍需設置請確認")
            elif exemptable and served is not None and served <= limit and (side != "stair" if refuge else side == "stair"):
                sev = ORANGE
                extra = (f"；經此出入口避難的範圍不大，任一點至此出入口的步行距離約 {_fmt(served)} m，"
                         f"在{'避難層' if refuge else '避難層以外之樓層'} {limit:.0f} m 以下：若自居室任一點易於觀察識別此出入口，"
                         "得依第 146 條第 1 項第 1 款第 1 目免設，請確認")
                laws.append("D0120029/146/1/1/1")
        g = Point(x, y).buffer(1.2)
        rooms = _rooms_touching(floor, g)
        findings.append(Finding(
            "EXIT-146-3", sev, "未設置", floor.label or "", f"{'、'.join(_room_names(rooms)[:2]) or '標示位置'}旁的{what}未設出口標示燈",
            f"出口標示燈應設於{what}上方或其緊鄰之有效引導避難處；此出入口最近的出口標示燈約 {_fmt(dist)} m{extra}",
            "於該出入口上方設置出口標示燈；若符合第 146 條免設條件（可直接看見出口且步行距離短等），請在圖上註明",
            laws, rooms=_room_names(rooms), area=None, geom=g, metrics={"nearest": round(dist, 2)}))
    notes = [Note("EXIT-146-3", f"已檢查 {len(targets)} 處通往戶外或直通樓梯的出入口（依門圖塊位置判讀；通往戶外之出入口只檢查避難層）",
                  ["D0120029/146-3/1"])] if targets else []
    if upper:
        near = sorted({n for d in upper for n in _room_names(_rooms_touching(floor, Point(d).buffer(1.2)))})
        notes.append(Note("EXIT-146-3", f"本層不是避難層，外牆上通往屋外的門 {len(upper)} 處未列為出口標示燈設置處"
                          + (f"（{'、'.join(near[:3])}旁）" if near else "")
                          + "；若通往室外直通樓梯，應設出口標示燈", ["D0120029/146-3/1/2"]))
    if inner and refuge:
        notes.append(Note("EXIT-146-3", f"{len(inner)} 處貼近外牆的門四周都有牆，判讀為室內門，未當作通往戶外之出入口",
                          ["D0120029/146-3/1/1"]))
    return findings, notes


def _exit_reach(lenient: bool):
    """出口標示燈有效範圍：等級未標示時寬鬆取 A 級、嚴格取 C 級；有無避難方向符號未標示時寬鬆取無、嚴格取有。"""
    def f(e: Equipment) -> float:
        g = e.spec.get("grade") if e.spec.get("grade") in EXIT_RANGE else ("A" if lenient else "C")
        plain, arrow = EXIT_RANGE[g]
        a = e.spec.get("arrow")
        return arrow if (a is True or (a is None and not lenient)) else plain
    return f


def direction_lights(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    lights = _of(eq, "direction_light")
    if not lights:
        return [], []
    corridors = [r for r in floor.rooms if r.kind == "corridor"]
    if not corridors:
        return [], [Note("DIR-146-3", "本層未辨識出走廊或通道（房名需含「走廊」「通道」「門廳」等），未檢核避難方向指示燈涵蓋範圍")]
    grid = grid or C.WalkGrid(floor.walkable)
    cells = grid.region_cells(unary_union([r.polygon for r in corridors]))

    def reach(items, rng, factor: float):
        groups: dict[float, list] = {}
        for e in items:
            groups.setdefault(rng(e), []).append((e.x, e.y))
        cov = np.zeros_like(cells)
        for r, pts in groups.items():
            d = grid.distances(pts)
            cov |= np.nan_to_num(d, nan=np.inf) <= r * factor
        return cov

    def dir_reach(lenient: bool):
        return lambda e: DIR_RANGE[e.spec.get("grade") if e.spec.get("grade") in DIR_RANGE else ("A" if lenient else "C")]

    hard = cells & ~reach(lights, dir_reach(True), 1 + C.WALK_TOL)
    near = cells & ~reach(lights, dir_reach(True), 1.0) & ~hard
    soft = cells & ~reach(lights, dir_reach(False), 1.0) & ~hard & ~near
    # 依解讀（exit_sign_counts_for_direction）：出口標示燈有效範圍也算入走廊涵蓋，但只靠它涵蓋的部分列需確認
    signs = _of(eq, "exit_sign") if ctx.rule("exit_sign_counts_for_direction") else []
    interp = np.zeros_like(cells)
    if signs:
        by_exit = reach(signs, _exit_reach(True), 1 + C.WALK_TOL)
        interp = (hard | near) & by_exit
        hard, near = hard & ~by_exit, near & ~by_exit
    unknown_grade = any(e.spec.get("grade") not in DIR_RANGE for e in lights)
    exit_unknown = any(e.spec.get("grade") not in EXIT_RANGE for e in signs)
    findings = []
    for sev, mask in ((RED, hard), (ORANGE, near), ("INTERP", interp), (YELLOW, soft)):
        for p in grid.cells_to_polygons(mask):
            rooms = _rooms_touching(floor, p)
            names = '、'.join(_room_names(rooms)[:2]) or '走廊'
            if sev == "INTERP":
                findings.append(Finding(
                    "DIR-146-3", ORANGE, "需確認", floor.label or "",
                    f"{names}有 {_fmt(p.area)} ㎡ 只在出口標示燈有效範圍內，不在避難方向指示燈有效範圍內",
                    "依解讀：出口標示燈範圍是否可替代方向指示燈。第 146-3 條第 2 項第 3 款只寫「避難方向指示燈有效範圍」"
                    "（A 級 20 m、B 級 15 m、C 級 10 m）；此段走廊不在任何避難方向指示燈有效範圍內，但在出口標示燈有效範圍內"
                    "（第 146-2 條：A 級 60 m、B 級 30 m、C 級 15 m，顯示避難方向符號者 A 級 40 m、B 級 20 m）"
                    + ("，出口標示燈等級未標示者以 A 級計" if exit_unknown else "")
                    + "。若出口標示燈範圍可併計則符合，若不可併計則需增設避難方向指示燈",
                    "確認出口標示燈範圍可否併計；不可併計時，在此段走廊（優先轉彎處）增設避難方向指示燈",
                    ["D0120029/146-3/2/3", "D0120029/146-2/1/1"], missing=["出口標示燈範圍可否併計避難方向指示燈涵蓋（法規解讀）"],
                    rooms=_room_names(rooms), area=p.area, geom=p, metrics={"exit_sign_counted": True}))
                continue
            if sev == YELLOW:
                why = "避難方向指示燈等級未標示；若為 C 級（有效範圍 10 m），此段走廊不在任何指示燈有效範圍內"
                missing = ["避難方向指示燈等級（A／B／C 級）"] if unknown_grade else []
            else:
                why = ("走廊、通道各部分應在避難方向指示燈有效範圍內（A 級 20 m、B 級 15 m、C 級 10 m，步行距離）"
                       + ("；此範圍略超過，在計算誤差內，請人工確認" if sev == ORANGE else "")
                       + ("；出口標示燈有效範圍也算入時仍不在範圍內" if signs else ""))
                missing = []
            findings.append(Finding(
                "DIR-146-3", _sev_for(rooms, sev), "距離超過" if sev != YELLOW else "資料不足", floor.label or "",
                f"{names}有 {_fmt(p.area)} ㎡ 不在避難方向指示燈有效範圍內",
                why, "在此段走廊（優先轉彎處）增設避難方向指示燈，或改用較高等級", ["D0120029/146-3/2/3", "D0120029/146-2/1/1"],
                missing=missing, rooms=_room_names(rooms), area=p.area, geom=p))
    return findings, []


EML_EXEMPT_LABEL = r"廁|洗手間|浴室|盥洗|儲藏|機械室|機房"
EML_EXEMPT_KINDS = ("toilet", "machine", "elevator", "shaft")
WARDROBE = r"衣帽間"


def _label_class(labels: list[str]) -> set[str]:
    """逐個標示判斷：'exempt'（第 179 條第 6 款處所、昇降機道、管道間）、'wardrobe'（衣帽間）、'other'（其他房名）。
    判斷不了種類的註記（「位移」「增設」等）不計。"""
    out = set()
    for s in labels:
        k, _ = room_kind([s])
        if k == "unknown":
            continue
        if k in EML_EXEMPT_KINDS or re.search(EML_EXEMPT_LABEL, s):
            out.add("exempt")
        elif re.search(WARDROBE, s):
            out.add("wardrobe")
        else:
            out.add("other")
    return out


def emergency_lights(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    lights = _of(eq, "emergency_light")
    if not lights:
        return [], []
    refuge = _refuge(floor)
    # 屋外出口：貼外框的門扣掉四周被牆擋住的室內門
    ext = [d for d in floor.exterior_doors() if _door_to_outside(floor, d) != "inside"]
    d_ext = None
    if refuge and ext:
        grid = grid or C.WalkGrid(floor.walkable)
        d_ext = grid.distances(ext)
    # 燈在哪間房：圖形中心落在房內；不在任何房內（壓在牆線、門弧上）時，0.5 m 內的房間都算
    homes = [(Point(e.x, e.y), floor.room_at(e.x, e.y)) for e in lights]

    def lit(room: Room) -> bool:
        return any(h is room or (h is None and room.polygon.distance(p) <= LIGHT_TOL) for p, h in homes)

    findings, exempt, refuge_ok, housing, outside = [], [], [], [], []
    stairs_in = [r for r in floor.rooms if r.kind == "stair" and not r.conflict and floor.in_region(r)]
    for room in floor.rooms:
        if room.kind in ("void", "outdoor", "shaft", "elevator") or room.area < 2:
            continue
        if not floor.in_region(room):
            # 不在樓地板範圍（屋突層未標示名稱的屋頂、天溝等）：不檢核；但不貼外框（不是屋頂周邊）、
            # 又緊鄰樓梯間的未命名範圍可能是梯廳、樓梯平台，列資料不足
            if (room.kind == "unknown" and not lit(room) and floor.outline.exterior.distance(room.polygon) > 1.0
                    and any(s.polygon.distance(room.polygon) <= 0.5 for s in stairs_in)):
                dmin = min(room.polygon.distance(p) for p, _ in homes)
                why0 = "屋突層未標示名稱的範圍視為屋頂" if _level(floor.label or "")[0] == "roof" else "例：挑空內的範圍"
                findings.append(Finding(
                    "EML-24", YELLOW, "資料不足", floor.label or "", f"{room.name}（{_fmt(room.area)} ㎡，緊鄰樓梯間）用途不明，無法判定是否應設緊急照明",
                    f"此範圍沒有房間名稱，不在本層樓地板範圍內（{why0}）；但它緊鄰樓梯間，可能是梯廳或樓梯平台，"
                    f"若屬自居室通達避難層所經過的走廊、樓梯間，應設緊急照明；最近的緊急照明燈約 {_fmt(dmin)} m",
                    "在圖上補標此範圍的名稱（梯廳、平台或屋頂）；若為梯廳、樓梯間的一部分，確認緊急照明涵蓋",
                    ["D0120029/24/1/5"], missing=["此範圍的用途（梯廳、樓梯平台或屋頂）"],
                    rooms=[room.name], area=room.area, geom=room.polygon, metrics={"nearest": round(dmin, 2)}))
            else:
                outside.append(room)
            continue
        cls = _label_class(room.labels)
        if cls == {"exempt"}:                          # 名稱衝突（例：客貨梯＋機械室）但每個標示都是免設處所，照樣免設
            exempt.append(room)
            continue
        if lit(room):
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
        extra = ""
        if what == "居室" and ctx.occupancy in ("戊-1", "戊-2") and sev == RED:
            sev = ORANGE                               # 複合用途：住宅部分之居室得免設，需確認用途
        if what == "居室" and "wardrobe" in cls and "other" not in cls:
            sev = ORANGE                               # 衣帽間：建築技術規則不視為居室（法規庫未收錄該條，不引用節點）
            extra = ("；衣帽間有兩種讀法：依建築技術規則建築設計施工編第 1 條第 4 款，衣帽間不視為居室（法規庫未收錄該條，請核對條文），照此讀法得免設；"
                     "若當作有人使用的空間，則應設置")
        findings.append(Finding(
            "EML-24", sev, "未設置", floor.label or "", f"{room.name}（{what}，{_fmt(room.area)} ㎡）未設緊急照明燈",
            f"本層設有緊急照明設備，{what}應設置（自居室通達避難層之走廊、樓梯間亦同）；此範圍內沒有緊急照明燈{extra}",
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
    if outside:
        notes.append(Note("EML-24", f"{len(outside)} 個範圍不在本層樓地板範圍內（屋突層未標示名稱的屋頂、天溝，或挑空內的範圍），未檢核緊急照明，"
                                    f"合計 {_fmt(sum(r.area for r in outside))} ㎡；若其中有室內空間，請補標房間名稱"))
    return findings, notes


RULES = [
    ("EXIT-146-3", "出口標示燈位置", exit_signs),
    ("DIR-146-3", "避難方向指示燈有效範圍", direction_lights),
    ("EML-24", "緊急照明設置處所", emergency_lights),
]
