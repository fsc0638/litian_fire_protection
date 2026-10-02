"""距離涵蓋計算：水平距離（直線，不管牆）與步行距離（沿可走區域繞牆）。

法規裡兩種量法不同：
- 水平距離（消防栓 25 m、撒水頭 2.1～2.6 m、揚聲器 10 m…）：以設備為圓心畫圓，
  範圍扣掉所有圓，剩下的就是涵蓋不到的地方。
- 步行距離（滅火器 20 m、標示燈有效範圍…）：把可走區域切成方格，從所有設備同時往外走
  （最短路徑，16 個方向），每一格得到「走到最近設備的距離」。
  16 方向的格點距離與真實最短路徑的誤差在 3% 內（測試有量），加上格子大小的誤差；
  所以超過門檻但在誤差帶內的，檢核端標「需確認」而不是「不符」。
"""

from __future__ import annotations

import math

import numpy as np
import shapely
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import Polygon
from shapely.ops import unary_union

MIN_PIECE = 0.2        # 小於這個面積（㎡）的未涵蓋碎片不報（圓的多邊形近似、牆角縫）
SLIVER = 0.05          # 寬度不到 2×5 cm 的細條不報
WALK_TOL = 0.04        # 步行距離誤差帶（格點近似）


def pieces(g, min_area: float = MIN_PIECE) -> list[Polygon]:
    """把幾何拆成多邊形，去掉碎片與細條。"""
    if g is None or g.is_empty:
        return []
    polys = [g] if isinstance(g, Polygon) else [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
    out = []
    for p in polys:
        if p.area < min_area or p.buffer(-SLIVER).is_empty:
            continue
        out.append(p)
    return out


def circles(points: list[tuple[float, float]], radius: float):
    if not points:
        return Polygon()
    return unary_union(shapely.buffer(shapely.points(points), radius, quad_segs=32))


def uncovered(region, points: list[tuple[float, float]], radius: float) -> list[Polygon]:
    """region 內離所有 points 水平距離都超過 radius 的範圍。"""
    if region.is_empty:
        return []
    return pieces(region.difference(circles(points, radius)))


# 16 方向（取一半，圖是無向的）：(dx, dy, 路徑經過、必須可走的格子)
_STEPS = [
    (1, 0, []), (0, 1, []),
    (1, 1, [(1, 0), (0, 1)]), (1, -1, [(1, 0), (0, -1)]),
    (2, 1, [(1, 0), (1, 1)]), (1, 2, [(0, 1), (1, 1)]),
    (2, -1, [(1, 0), (1, -1)]), (1, -2, [(0, -1), (1, -1)]),
]


class WalkGrid:
    """可走區域的方格圖。cell：格子邊長（m）。"""

    def __init__(self, walkable, cell: float = 0.2):
        self.cell = cell
        x0, y0, x1, y1 = walkable.bounds
        self.x0, self.y0 = x0, y0
        self.nx = max(1, int(math.ceil((x1 - x0) / cell)))
        self.ny = max(1, int(math.ceil((y1 - y0) / cell)))
        xs = x0 + (np.arange(self.nx) + 0.5) * cell
        ys = y0 + (np.arange(self.ny) + 0.5) * cell
        gx, gy = np.meshgrid(xs, ys)
        shapely.prepare(walkable)
        self.free = shapely.contains_xy(walkable, gx, gy)          # (ny, nx)
        self.gx, self.gy = gx, gy
        idx = np.full(self.free.shape, -1, dtype=np.int64)
        idx[self.free] = np.arange(int(self.free.sum()))
        self.idx = idx
        self.n = int(self.free.sum())
        rows, cols, w = [], [], []
        F = self.free
        for dx, dy, via in _STEPS:
            # 來源格 (r, c) → 目標格 (r+dy, c+dx)；用切片一次算整張圖
            ok = np.zeros_like(F)
            r0, r1 = max(0, -dy), self.ny - max(0, dy)
            c0, c1 = max(0, -dx), self.nx - max(0, dx)
            if r1 <= r0 or c1 <= c0:
                continue
            src = F[r0:r1, c0:c1]
            dst = F[r0 + dy:r1 + dy, c0 + dx:c1 + dx]
            m = src & dst
            for vx, vy in via:
                m = m & F[r0 + vy:r1 + vy, c0 + vx:c1 + vx]
            ok[r0:r1, c0:c1] = m
            rr, cc = np.nonzero(ok)
            rows.append(idx[rr, cc])
            cols.append(idx[rr + dy, cc + dx])
            w.append(np.full(len(rr), cell * math.hypot(dx, dy)))
        rows = np.concatenate(rows) if rows else np.zeros(0, dtype=np.int64)
        cols = np.concatenate(cols) if cols else np.zeros(0, dtype=np.int64)
        w = np.concatenate(w) if w else np.zeros(0)
        self.graph = coo_matrix((w, (rows, cols)), shape=(self.n, self.n)).tocsr()

    def cell_of(self, x: float, y: float, snap: float = 1.5) -> int | None:
        """點所在的可走格；點落在牆上（壁掛設備）時找 snap 公尺內最近的可走格。"""
        c = int((x - self.x0) / self.cell)
        r = int((y - self.y0) / self.cell)
        if 0 <= r < self.ny and 0 <= c < self.nx and self.free[r, c]:
            return int(self.idx[r, c])
        k = int(math.ceil(snap / self.cell))
        r0, r1 = max(0, r - k), min(self.ny, r + k + 1)
        c0, c1 = max(0, c - k), min(self.nx, c + k + 1)
        if r0 >= r1 or c0 >= c1:
            return None
        sub = self.free[r0:r1, c0:c1]
        if not sub.any():
            return None
        rr, cc = np.nonzero(sub)
        d = (rr + r0 - r) ** 2 + (cc + c0 - c) ** 2
        j = int(np.argmin(d))
        return int(self.idx[rr[j] + r0, cc[j] + c0])

    def distances(self, points: list[tuple[float, float]]) -> np.ndarray:
        """每一可走格走到最近一個 point 的距離（m）；走不到為 inf。回傳 (ny, nx)，牆上的格子為 nan。"""
        out = np.full(self.free.shape, np.nan)
        src = sorted({c for p in points if (c := self.cell_of(*p)) is not None})
        if not src or self.n == 0:
            out[self.free] = np.inf
            return out
        d = dijkstra(self.graph, directed=False, indices=src, min_only=True)
        out[self.free] = d
        return out

    def region_cells(self, region) -> np.ndarray:
        """可走格中、中心點落在 region 內的布林遮罩。"""
        shapely.prepare(region)
        return self.free & shapely.contains_xy(region, self.gx, self.gy)

    def cells_to_polygons(self, mask: np.ndarray) -> list[Polygon]:
        """把格子遮罩轉回多邊形（逐列合併成長條再聯集）。"""
        boxes = []
        for r in np.nonzero(mask.any(axis=1))[0]:
            row = mask[r]
            c = 0
            while c < self.nx:
                if row[c]:
                    s = c
                    while c < self.nx and row[c]:
                        c += 1
                    boxes.append(shapely.box(self.x0 + s * self.cell, self.y0 + r * self.cell,
                                             self.x0 + c * self.cell, self.y0 + (r + 1) * self.cell))
                else:
                    c += 1
        return pieces(unary_union(boxes)) if boxes else []
