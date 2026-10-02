"""樓層對位與挑空投影。

一張圖只畫一層，但挑空（貫穿上下層的大空間）的天花板在上層：設計者常把下層大空間的探測器畫在
上層圖的挑空範圍內（屋頂板下），或畫在屋突層圖的房間外。只看同一張圖，下層會被誤判「未設探測器」。

做法：
1. 對位：兩張圖都有的電梯、管道間、樓梯（名稱相同，或種類相同且大小相近）的質心兩兩配對，
   位移相近的配對多數決；至少 2 個錨點、殘差中位數 < 0.3 m 才採用。同一樓層的不同系統圖
   常並排在模型空間（座標差一整張圖寬），所以每兩張圖都各自對位，不假設同層座標相同。
2. 投影：上層落在挑空房間內（屋突層：落在任何房間外）的偵測類設備換算到下一層；下一層同位置
   仍是挑空就繼續往下，直到落在非挑空房間，加到該層「已有偵測類設備的圖」（不在疏散圖等其他系統圖上冒出缺失）。
   投影來的設備是複本（spec["projected_from"] 標來源圖號），只參與檢核，不計入該圖設備數量。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from statistics import median

from litian.plan import floor as F
from litian.review.equipment import Equipment
from litian.review.required import _level

ANCHOR_KINDS = ("elevator", "shaft", "stair")
DETECT_KINDS = ("detector", "flame_detector")
MIN_ANCHORS = 2         # 至少幾個錨點才採用對位
MAX_RESID = 0.3         # 錨點位移與採用位移之差（中位數）上限（m）
VOTE_R = 0.5            # 位移相差 0.5 m 內的配對算同一組
NAME_RATIO = 0.5        # 名稱相同的錨點：面積比下限（屋突層的管道間常比下層小）
SIZE_RATIO = 0.8        # 名稱不同、種類相同的錨點：面積比下限（大小相近）
DUP_TOL = 0.5           # 投影位置 0.5 m 內已有同種設備：下層圖上已畫，不重複加入


@dataclass
class Shift:
    """a 圖座標 + (dx, dy) ＝ b 圖座標。"""
    dx: float
    dy: float
    anchors: int
    resid: float


def _names(room: F.Room) -> frozenset[str]:
    """錨點名稱：能判斷種類的標示（去括號、空白），「增設」「人孔」等註記不算。"""
    pat = dict(F.ROOM_KINDS).get(room.kind)
    return frozenset(re.sub(r"[\s()（）]", "", s) for s in room.labels if pat and re.search(pat, s))


def anchors(fl: F.Floor) -> list[tuple[str, frozenset, float, float, float]]:
    out = []
    for r in fl.rooms:
        if r.kind in ANCHOR_KINDS and not r.conflict:
            c = r.polygon.centroid
            out.append((r.kind, _names(r), r.area, c.x, c.y))
    return out


def _match(pairs, cx: float, cy: float):
    """位移在 (cx, cy) 附近的配對，一對一（每個錨點只用一次，離中心近的優先）。"""
    near = sorted((math.hypot(p[2] - cx, p[3] - cy), p) for p in pairs if math.hypot(p[2] - cx, p[3] - cy) <= VOTE_R)
    used_a, used_b, out = set(), set(), []
    for _, p in near:
        if p[0] not in used_a and p[1] not in used_b:
            used_a.add(p[0])
            used_b.add(p[1])
            out.append(p)
    return out


def align(a: F.Floor, b: F.Floor) -> Shift | None:
    """兩張圖的位移；錨點不足、殘差過大或有兩組同樣多的位移（無法確定）回 None。"""
    A, B = anchors(a), anchors(b)
    pairs = []
    for i, (ka, na, sa, xa, ya) in enumerate(A):
        for j, (kb, nb, sb, xb, yb) in enumerate(B):
            if ka != kb or min(sa, sb) <= 0:
                continue
            ratio = min(sa, sb) / max(sa, sb)
            if (na & nb and ratio >= NAME_RATIO) or ratio >= SIZE_RATIO:
                pairs.append((i, j, xb - xa, yb - ya, bool(na & nb)))
    groups = []
    for p in pairs:
        m = _match(pairs, p[2], p[3])
        dx, dy = median(q[2] for q in m), median(q[3] for q in m)
        m = _match(pairs, dx, dy)
        groups.append((len(m), sum(q[4] for q in m), m))
    if not groups:
        return None
    groups.sort(key=lambda g: (-g[0], -g[1]))
    n, _, m = groups[0]
    dx, dy = median(q[2] for q in m), median(q[3] for q in m)
    resid = median(math.hypot(q[2] - dx, q[3] - dy) for q in m)
    rival = [g for g in groups[1:] if g[0] >= n and math.hypot(median(q[2] for q in g[2]) - dx,
                                                               median(q[3] for q in g[2]) - dy) > 2 * VOTE_R]
    if n < MIN_ANCHORS or resid >= MAX_RESID or rival:
        return None
    return Shift(round(dx, 3), round(dy, 3), n, round(resid, 3))


def _rank(label: str, top: int | None, stories: int | None) -> int | None:
    """樓層高低順序（相鄰層差 1）：地下 B1＝0、B2＝-1；地上 nF＝n；屋突 R1F（RF）＝最高地上層＋1。
    夾層不參與投影（只占部分範圍）；圖上最高層與填寫的地上層數不同（圖不齊）時，屋突層不知道接在哪層，不參與。"""
    kind, lv = _level(label)
    if kind == "above":
        return lv
    if kind == "base":
        return lv + 1
    if kind == "roof" and top is not None and (stories is None or stories == top):
        m = re.fullmatch(r"R(\d+)F", label)
        return top + (int(m.group(1)) if m else 1)
    return None


@dataclass
class Projection:
    target: int                     # 目標圖（索引）
    source: int                     # 來源圖（索引）
    shift: Shift                    # 來源圖 → 目標圖的位移
    roof: bool                      # True：來源是屋突層房間外；False：來源圖的挑空範圍內
    equipment: list[Equipment] = field(default_factory=list)


def project_detectors(floors: list[F.Floor], names: list[str], equipment: list[list[Equipment]],
                      stories: int | None = None) -> tuple[list[Projection], list[str]]:
    """floors／names／equipment：各張主圖的平面、圖號、設備（同一索引）。回傳（投影結果, 警告）。"""
    labels = [fl.label or "" for fl in floors]
    tops = [lv for lab in labels for kind, lv in [_level(lab)] if kind == "above"]
    top = max(tops) if tops else None
    rank = [_rank(lab, top, stories) for lab in labels]
    by_rank: dict[int, list[int]] = {}
    for i, r in enumerate(rank):
        if r is not None:
            by_rank.setdefault(r, []).append(i)
    detect = [any(k in DETECT_KINDS for e in eq for k in e.kinds) for eq in equipment]
    cache: dict[tuple[int, int], Shift | None] = {}

    def shift(a: int, b: int) -> Shift | None:
        if (a, b) not in cache:
            cache[(a, b)] = align(floors[a], floors[b])
        return cache[(a, b)]

    out: dict[tuple[int, int, bool], Projection] = {}
    warnings: list[str] = []
    for s, eq in enumerate(equipment):
        if rank[s] is None or not detect[s]:
            continue
        roof = _level(labels[s])[0] == "roof"
        lost, unaligned = 0, set()
        for e in eq:
            if not any(k in DETECT_KINDS for k in e.kinds):
                continue
            room = floors[s].room_near(e.x, e.y)
            if not ((room is not None and room.kind == "void") or (roof and room is None)):
                continue
            r, placed = rank[s] - 1, False
            while r in by_rank:
                # 下層同位置是什麼房間：用能對位的圖判斷（同層各圖平面相同；有偵測類設備的圖優先）
                rep = next(((i, sh) for i in sorted(by_rank[r], key=lambda i: not detect[i])
                            if (sh := shift(s, i)) is not None), None)
                if rep is None:
                    unaligned.update(names[i] for i in by_rank[r])
                    break
                i, sh = rep
                below = floors[i].room_near(e.x + sh.dx, e.y + sh.dy)
                if below is None:
                    break
                if below.kind == "void":
                    r -= 1
                    continue
                for t in by_rank[r]:
                    if not detect[t]:
                        continue
                    sh_t = shift(s, t)
                    if sh_t is None:
                        unaligned.add(names[t])
                        continue
                    x, y = e.x + sh_t.dx, e.y + sh_t.dy
                    p = out.setdefault((t, s, roof), Projection(t, s, sh_t, roof))
                    have = equipment[t] + p.equipment
                    if any(set(e.kinds) & set(o.kinds) and abs(o.x - x) <= DUP_TOL and abs(o.y - y) <= DUP_TOL for o in have):
                        placed = True                  # 下層圖上已畫了同一個設備
                        continue
                    p.equipment.append(replace(e, x=x, y=y, spec={**e.spec, "projected_from": names[s]}))
                    placed = True
                break
            lost += not placed
        where = "屋突層房間外" if roof else "挑空範圍內"
        if unaligned:
            warnings.append(f"{names[s]} 與 {'、'.join(sorted(unaligned))} 無法對位（兩圖共同的電梯、管道間、樓梯不足 "
                            f"{MIN_ANCHORS} 處或位置不一致），{where}的探測器未投影到下層")
        elif lost:
            warnings.append(f"{names[s]} {where}有 {lost} 個探測器對不到下層的房間（或下層沒有含探測器的圖），未納入下層檢核")
    return [p for p in out.values() if p.equipment], warnings
