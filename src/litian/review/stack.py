"""樓層對位與挑空投影。

一張圖只畫一層，但挑空（貫穿上下層的大空間）的天花板在上層：設計者常把下層大空間的探測器畫在
上層圖的挑空範圍內（屋頂板下），或畫在屋突層圖的房間外。只看同一張圖，下層會被誤判「未設探測器」。

做法：
1. 對位：兩張圖都有的電梯、管道間、樓梯（名稱相同，或種類相同且大小相近）的質心兩兩配對，
   位移相近的配對多數決；至少 2 個錨點、殘差中位數 < 0.3 m 才採用。同一樓層的不同系統圖
   常並排在模型空間（座標差一整張圖寬），所以每兩張圖都各自對位，不假設同層座標相同。
   對位結果由 Aligner 快取，挑空投影與樓梯配對共用。
2. 投影：上層落在樓板開口（標示「挑空」「開口部」）內（屋突層：落在任何房間外）的偵測類設備換算到下一層；
   下一層同位置仍是樓板開口就繼續往下，直到落在其他房間，加到該層「已有偵測類設備的圖」
   （不在疏散圖等其他系統圖上冒出缺失）。「挑高」「中庭」「天井」可能有樓板（例：一樓挑高大廳），
   不當作開口：上層的不投影，下層同位置遇到時停止並警告。
   投影來的設備是複本（spec["projected_from"] 標來源圖號、spec["projected_levels"] 標裝置面在幾層樓高），
   只參與檢核，不計入該圖設備數量。下層圖上（或其他來源圖已投影）同位置已有同種設備的不重複加入。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from statistics import median

from litian.plan import floor as F
from litian.review.equipment import Equipment
from litian.review.required import _level

ANCHOR_KINDS = ("elevator", "shaft", "stair")
DETECT_KINDS = ("detector", "flame_detector")
OPENING = re.compile(r"挑空|開口部")   # 樓板開口；void 另含挑高、中庭、天井（底層有樓板）
MIN_ANCHORS = 2         # 至少幾個錨點才採用對位
MAX_RESID = 0.3         # 錨點位移與採用位移之差（中位數）上限（m）
VOTE_R = 0.5            # 位移相差 0.5 m 內的配對算同一組
NAME_RATIO = 0.5        # 名稱相同的錨點：面積比下限（屋突層的管道間常比下層小）
SIZE_RATIO = 0.8        # 名稱不同、種類相同的錨點：面積比下限（大小相近）
DUP_TOL = 0.5           # 投影位置 0.5 m 內已有同種設備：已畫（或已投影）的同一個設備，不重複加入


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


def is_opening(room: F.Room | None) -> bool:
    """樓板開口：挑空房間且標示含「挑空」「開口部」（挑高、中庭、天井可能有樓板，不算）。"""
    return room is not None and room.kind == "void" and bool(OPENING.search(" ".join(room.labels)))


def _cell(dx: float, dy: float) -> tuple[int, int]:
    return math.floor(dx / VOTE_R), math.floor(dy / VOTE_R)


def _match(cells: dict, cx: float, cy: float):
    """位移在 (cx, cy) 附近（VOTE_R 內）的配對，一對一（每個錨點只用一次，離中心近的優先）。
    配對已依位移分格（格寬 VOTE_R），只需看中心所在格與相鄰 8 格。"""
    bx, by = _cell(cx, cy)
    near = sorted((d, p) for i in (-1, 0, 1) for j in (-1, 0, 1) for p in cells.get((bx + i, by + j), ())
                  if (d := math.hypot(p[2] - cx, p[3] - cy)) <= VOTE_R)
    used_a, used_b, out = set(), set(), []
    for _, p in near:
        if p[0] not in used_a and p[1] not in used_b:
            used_a.add(p[0])
            used_b.add(p[1])
            out.append(p)
    return out


def align(a: F.Floor, b: F.Floor, A: list | None = None, B: list | None = None) -> Shift | None:
    """兩張圖的位移；錨點不足、殘差過大或有兩組同樣多的位移（無法確定）回 None。
    A、B：已算好的錨點（Aligner 快取用）。每一格位移只當一次候選中心，配對多時仍約與配對數成正比。"""
    A = anchors(a) if A is None else A
    B = anchors(b) if B is None else B
    cells: dict[tuple[int, int], list] = {}
    for i, (ka, na, sa, xa, ya) in enumerate(A):
        for j, (kb, nb, sb, xb, yb) in enumerate(B):
            if ka != kb or min(sa, sb) <= 0:
                continue
            ratio = min(sa, sb) / max(sa, sb)
            if (na & nb and ratio >= NAME_RATIO) or ratio >= SIZE_RATIO:
                p = (i, j, xb - xa, yb - ya, bool(na & nb))
                cells.setdefault(_cell(p[2], p[3]), []).append(p)
    groups, seen = [], set()
    for ps in cells.values():
        cx, cy = median(q[2] for q in ps), median(q[3] for q in ps)
        m = []
        for _ in range(2):                              # 以附近配對的中位數重新定中心
            m = _match(cells, cx, cy)
            if not m:
                break
            cx, cy = median(q[2] for q in m), median(q[3] for q in m)
        if not m or (key := (round(cx, 2), round(cy, 2))) in seen:
            continue
        seen.add(key)
        m = _match(cells, cx, cy)
        if m:
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


class Aligner:
    """各張圖兩兩對位（索引為 floors 的位置），錨點與結果都快取；反方向直接取相反位移。"""

    def __init__(self, floors: list[F.Floor]):
        self.floors = floors
        self._anchors: dict[int, list] = {}
        self._cache: dict[tuple[int, int], Shift | None] = {}

    def __call__(self, a: int, b: int) -> Shift | None:
        if (a, b) not in self._cache:
            if (b, a) in self._cache:
                s = self._cache[(b, a)]
                self._cache[(a, b)] = None if s is None else Shift(0.0 - s.dx, 0.0 - s.dy, s.anchors, s.resid)
            else:
                for i in (a, b):
                    if i not in self._anchors:
                        self._anchors[i] = anchors(self.floors[i])
                self._cache[(a, b)] = align(self.floors[a], self.floors[b], self._anchors[a], self._anchors[b])
        return self._cache[(a, b)]


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
    levels: int = 1                 # 裝置面（上層樓板或屋頂板）在目標層樓地板以上幾層樓高；1＝就是目標層自己的天花板
    equipment: list[Equipment] = field(default_factory=list)


def project_detectors(floors: list[F.Floor], names: list[str], equipment: list[list[Equipment]],
                      stories: int | None = None, aligner: Aligner | None = None) -> tuple[list[Projection], list[str]]:
    """floors／names／equipment：各張主圖的平面、圖號、設備（同一索引）。回傳（投影結果, 警告）。"""
    shift = aligner or Aligner(floors)
    labels = [fl.label or "" for fl in floors]
    tops = [lv for lab in labels for kind, lv in [_level(lab)] if kind == "above"]
    top = max(tops) if tops else None
    rank = [_rank(lab, top, stories) for lab in labels]
    by_rank: dict[int, list[int]] = {}
    for i, r in enumerate(rank):
        if r is not None:
            by_rank.setdefault(r, []).append(i)
    detect = [any(k in DETECT_KINDS for e in eq for k in e.kinds) for eq in equipment]
    out: dict[tuple[int, int, bool], Projection] = {}
    done: dict[int, list[Equipment]] = {}          # 各目標圖已投影的設備（跨所有來源圖，去重用）
    warnings: list[str] = []
    for s, eq in enumerate(equipment):
        if rank[s] is None or not detect[s]:
            continue
        roof = _level(labels[s])[0] == "roof"
        lost, unaligned, floored = 0, set(), Counter()
        for e in eq:
            if not any(k in DETECT_KINDS for k in e.kinds):
                continue
            room = floors[s].room_near(e.x, e.y)
            if not (is_opening(room) or (roof and room is None)):
                continue
            r, settled = rank[s] - 1, False
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
                    if is_opening(below):
                        r -= 1
                        continue
                    floored[(names[i], below.name)] += 1    # 挑高、中庭、天井：可能有樓板，不再往下
                    settled = True
                    break
                levels = rank[s] - r + (0 if roof else 1)
                for t in by_rank[r]:
                    if not detect[t]:
                        continue
                    sh_t = shift(s, t)
                    if sh_t is None:
                        unaligned.add(names[t])
                        continue
                    x, y = e.x + sh_t.dx, e.y + sh_t.dy
                    have = equipment[t] + done.get(t, [])
                    settled = True
                    if any(set(e.kinds) & set(o.kinds) and abs(o.x - x) <= DUP_TOL and abs(o.y - y) <= DUP_TOL for o in have):
                        continue                       # 下層圖上已畫（或另一張來源圖已投影）同一個設備
                    c = replace(e, x=x, y=y, spec={**e.spec, "projected_from": names[s], "projected_levels": levels})
                    out.setdefault((t, s, roof), Projection(t, s, sh_t, roof, levels)).equipment.append(c)
                    done.setdefault(t, []).append(c)
                break
            lost += not settled
        where = "屋突層房間外" if roof else "挑空範圍內"
        if unaligned:
            warnings.append(f"{names[s]} 與 {'、'.join(sorted(unaligned))} 無法對位（兩圖共同的電梯、管道間、樓梯不足 "
                            f"{MIN_ANCHORS} 處或位置不一致），{where}的探測器未投影到下層")
        elif lost:
            warnings.append(f"{names[s]} {where}有 {lost} 個探測器對不到下層的房間（或下層沒有含探測器的圖），未納入下層檢核")
        for (sheet, room), n in sorted(floored.items()):
            warnings.append(f"{names[s]} {where}有 {n} 個探測器，下層 {sheet} 同位置為「{room}」（挑高、中庭、天井等可能有樓板的空間），"
                            "未再往下投影；請確認這些探測器保護的是哪一層、該空間與更下層的探測器配置")
    return [p for p in out.values() if p.equipment], warnings
