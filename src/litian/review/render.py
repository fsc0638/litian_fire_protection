"""檢核結果畫成 SVG：牆、房間、設備符號、缺失範圍（依嚴重度上色並編號）。

座標是公尺；輸出時換成像素並把 y 軸翻正（CAD 的 y 向上，SVG 向下）。
"""

from __future__ import annotations

from html import escape

from shapely.geometry import Polygon

from litian.review import checks as K

SEV_COLOR = {K.RED: "#d93025", K.ORANGE: "#e8710a", K.YELLOW: "#c9a100", K.BLUE: "#1a73e8"}
KIND_STYLE = {   # 種類 → (顏色, 形狀)
    "sprinkler": ("#1a73e8", "dot"), "sprinkler_sidewall": ("#1a73e8", "dot"),
    "detector": ("#9334e6", "square"), "extinguisher": ("#d93025", "tri"),
    "hydrant": ("#b31412", "box"), "standpipe_outlet": ("#b31412", "box"),
    "speaker": ("#0b8043", "dot"), "exit_sign": ("#188038", "box"), "direction_light": ("#188038", "tri"),
    "emergency_light": ("#f29900", "dot"), "smoke_vent": ("#5f6368", "square"),
}
KIND_NAME = {"sprinkler": "撒水頭", "detector": "探測器", "extinguisher": "滅火器", "hydrant": "消防栓",
             "speaker": "揚聲器", "exit_sign": "出口標示燈", "direction_light": "避難方向指示燈",
             "emergency_light": "緊急照明", "smoke_vent": "排煙口", "standpipe_outlet": "送水口"}
ROOM_FILL = {"void": "#eceff1", "outdoor": "#eceff1", "toilet": "#e6f4ea", "stair": "#fce8e6", "elevator": "#f3e8fd",
             "shaft": "#e8eaed", "electrical": "#fef7e0", "machine": "#fef7e0", "corridor": "#fff8e1"}


class _T:
    def __init__(self, bounds, width: float, pad: float = 24):
        x0, y0, x1, y1 = bounds
        self.k = (width - 2 * pad) / max(x1 - x0, 1e-6)
        self.x0, self.y1, self.pad = x0, y1, pad
        self.w = width
        self.h = (y1 - y0) * self.k + 2 * pad

    def p(self, x, y):
        return (x - self.x0) * self.k + self.pad, (self.y1 - y) * self.k + self.pad

    def path(self, g, tol: float = 0.0) -> str:
        if g is None or g.is_empty:
            return ""
        if tol:
            g = g.simplify(tol)
        out = []
        polys = [g] if isinstance(g, Polygon) else [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
        for poly in polys:
            for ring in [poly.exterior, *poly.interiors]:
                pts = [self.p(x, y) for x, y in ring.coords]
                out.append("M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in pts) + "Z")
        return "".join(out)


def _symbol(kind: str, x: float, y: float) -> str:
    color, shape = KIND_STYLE.get(kind, ("#5f6368", "dot"))
    r = 3.2
    if shape == "dot":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}"/>'
    if shape == "square":
        return f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" fill="none" stroke="{color}" stroke-width="1.6"/>'
    if shape == "box":
        return f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" fill="{color}"/>'
    return f'<path d="M{x:.1f},{y - 4.5:.1f}L{x + 4.5:.1f},{y + 3.5:.1f}L{x - 4.5:.1f},{y + 3.5:.1f}Z" fill="{color}"/>'


def floor_svg(fr, width: float = 1600, show_rooms: bool = True) -> str:
    fl = fr.floor
    t = _T(fl.outline.bounds, width)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {t.w:.0f} {t.h:.0f}" '
         f'style="background:#fff;font-family:system-ui,\'Noto Sans TC\',sans-serif">']
    o.append(f'<path d="{t.path(fl.outline)}" fill="#fafaf8" stroke="#3c4043" stroke-width="1.2"/>')
    if show_rooms:
        for r in fl.rooms:
            fill = "none" if r.conflict else ROOM_FILL.get(r.kind, "none")
            o.append(f'<path d="{t.path(r.polygon, 0.05)}" fill="{fill}" stroke="#dadce0" stroke-width="0.6"/>')
    o.append(f'<path d="{t.path(fl.walls, 0.03)}" fill="#80868b" fill-rule="evenodd"/>')
    for i, f in enumerate(fr.findings, 1):
        if f.geom is None:
            continue
        c = SEV_COLOR[f.severity]
        o.append(f'<path d="{t.path(f.geom, 0.05)}" fill="{c}" fill-opacity="0.32" stroke="{c}" stroke-width="1.4" '
                 f'fill-rule="evenodd"><title>{i}. {escape(f.title)}</title></path>')
    for e in fr.equipment:
        x, y = t.p(e.x, e.y)
        o.append(_symbol(e.kinds[0], x, y))
    if show_rooms:
        for r in fl.rooms:
            if r.area < 12 or not r.labels:
                continue
            c = r.polygon.representative_point()
            x, y = t.p(c.x, c.y)
            o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="11" fill="#5f6368" text-anchor="middle">{escape(r.labels[0])}</text>')
    for i, f in enumerate(fr.findings, 1):
        if f.geom is None:
            continue
        c = f.geom.representative_point()
        x, y = t.p(c.x, c.y)
        col = SEV_COLOR[f.severity]
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="{col}"/>'
                 f'<text x="{x:.1f}" y="{y + 4:.1f}" font-size="11" font-weight="700" fill="#fff" text-anchor="middle">{i}</text>')
    kinds = sorted({k for e in fr.equipment for k in e.kinds[:1]})
    lx, ly = 30, t.h - 18 - 18 * len(kinds)
    for j, k in enumerate(kinds):
        o.append(_symbol(k, lx, ly + 18 * j))
        o.append(f'<text x="{lx + 10}" y="{ly + 18 * j + 4}" font-size="12" fill="#3c4043">{escape(KIND_NAME.get(k, k))}</text>')
    o.append("</svg>")
    return "\n".join(o)
