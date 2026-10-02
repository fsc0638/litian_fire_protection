"""逐項檢核規則（第一批）：設備的距離涵蓋、數量。

每條規則：輸入一層樓的平面理解結果（plan.floor.Floor）＋該層設備＋建物條件（Context），
輸出缺失（Finding）與說明（免設處所等）。法源一律用法規庫的節點編號，測試會逐一驗證存在。

條件不明時（撒水頭感度、天花板高度、是否防火構造、場所類別…）用上下限各算一次：
- 寬鬆條件下也不符 → 🔴 不符（不論真實條件為何都不符）
- 只有嚴格條件下不符 → 🟡 資料不足，並列出要補的資料
- 落在格點誤差帶、或房間名稱判讀有衝突 → 🟠 需確認
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from litian.plan.floor import Floor, Room
from litian.review import coverage as C
from litian.review.equipment import Equipment

RED, ORANGE, YELLOW, BLUE = "RED", "ORANGE", "YELLOW", "BLUE"
SEVERITY_LABEL = {RED: "不符", ORANGE: "需確認", YELLOW: "資料不足", BLUE: "建議"}
UNSURE_KINDS = ("unknown", "mixed")


@dataclass
class Context:
    """建物條件：審圖人員在工作台補填（之後也可由面積計算表帶入）；None＝未知。"""
    occupancy: str | None = None              # 第 12 條場所代碼，例："丁-2"、"乙-6"
    occupancy_group: str | None = None        # 第 12 條款別："1-5"（第一、五款）或 "2-4"（第二至四款）；未給時由 occupancy 推得
    ceiling_height: dict[str, float] = field(default_factory=dict)   # 樓層代號 → 天花板（裝置面）高度 m
    fireproof: bool | None = None             # 覆寫圖上判讀
    stories: int | None = None                # 地上層數；未給時由平面圖樓層推得
    height: float | None = None               # 建築物高度 m
    site_area: float | None = None            # 基地面積 ㎡
    no_opening: list[str] = field(default_factory=list)              # 無開口樓層（樓層代號）
    floor_area: dict[str, float] = field(default_factory=dict)       # 樓地板面積覆寫（面積計算表數字）

    def __post_init__(self):
        if self.occupancy_group is None and self.occupancy:
            cls = self.occupancy.split("-")[0]
            self.occupancy_group = "1-5" if cls in ("甲", "戊") else ("2-4" if cls in ("乙", "丙", "丁") else None)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Context":
        d = d or {}
        num = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
        return cls(
            occupancy=d.get("occupancy") or None,
            ceiling_height={k: float(v) for k, v in (d.get("ceiling_height") or {}).items() if v not in (None, "")},
            fireproof=d.get("fireproof") if d.get("fireproof") in (True, False) else None,
            stories=int(d["stories"]) if d.get("stories") not in (None, "") else None,
            height=num(d.get("height")), site_area=num(d.get("site_area")),
            no_opening=list(d.get("no_opening") or []),
            floor_area={k: float(v) for k, v in (d.get("floor_area") or {}).items() if v not in (None, "")})


@dataclass
class Finding:
    rule: str
    severity: str
    category: str
    floor: str
    title: str
    why: str
    fix: str
    law: list[str]
    missing: list[str] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)
    area: float | None = None
    geom: object | None = None
    metrics: dict = field(default_factory=dict)


@dataclass
class Note:
    rule: str
    text: str
    law: list[str] = field(default_factory=list)


def _of(eq: list[Equipment], kind: str) -> list[Equipment]:
    return [e for e in eq if kind in e.kinds]


def _fmt(v: float) -> str:
    return f"{v:,.1f}".rstrip("0").rstrip(".")


def _rooms_touching(floor: Floor, g) -> list[Room]:
    return [r for r in floor.rooms if r.polygon.intersects(g) and r.polygon.intersection(g).area > 0.05]


def _room_names(rooms: list[Room]) -> list[str]:
    return [r.name for r in rooms]


def _fireproof(floor: Floor, ctx: Context) -> bool | None:
    return ctx.fireproof if ctx.fireproof is not None else floor.fireproof


def _sev_for(rooms: list[Room], base: str) -> str:
    """問題範圍只落在名稱不明或衝突的房間時，降為需確認。"""
    if base == RED and rooms and all(r.kind in UNSURE_KINDS or r.conflict for r in rooms):
        return ORANGE
    return base


# ── 撒水頭：第 46 條水平距離；第 49 條免設處所 ─────────────────────────────

def _sprinkler_radius(room: Room, response: str | None, fireproof: bool | None, lenient: bool) -> tuple[float, str]:
    names = " ".join(room.labels)
    if re.search(r"舞[臺台]|道具室|放映室", names):
        return 1.7, "D0120029/46/1/1"
    if re.search(r"停車|汽車修理", names):
        return 2.1, "D0120029/46/1/2"
    quick = response == "quick" or (response is None and lenient)
    fp = fireproof if fireproof is not None else lenient
    if quick:
        return (2.6 if fp else 2.3), "D0120029/46/1/3/2"
    return (2.3 if fp else 2.1), "D0120029/46/1/3/1"


def _sprinkler_exempt(room: Room, fireproof: bool | None) -> str | None:
    """回傳免設依據的節點編號；不免設回 None。名稱衝突的範圍不免設（保守）。"""
    if room.conflict:
        return None
    names = " ".join(room.labels)
    if room.kind == "toilet":
        return "D0120029/49/1/1"
    if room.kind == "stair":
        return "D0120029/49/1/2"
    if room.kind in ("elevator", "shaft") and fireproof:
        return "D0120029/49/1/3"
    if room.kind == "machine" and re.search(r"昇降機|升降機|電梯|通風|換氣|空調", names):
        return "D0120029/49/1/4"
    if room.kind == "electrical":
        return "D0120029/49/1/5" if re.search(r"電信|電腦", names) else "D0120029/49/1/6"
    return None


def sprinkler_distance(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    heads = _of(eq, "sprinkler")
    if not heads:
        return [], []
    fp = _fireproof(floor, ctx)
    findings, notes = [], []
    exempt = []
    responses = {h.spec.get("response") for h in heads}
    unknown_resp = None in responses
    for room in floor.rooms:
        if room.kind in ("void", "outdoor"):
            continue
        basis = _sprinkler_exempt(room, fp)
        if basis:
            exempt.append((room, basis))
            continue
        area = room.polygon.intersection(floor.region)
        if area.is_empty or area.area < C.MIN_PIECE:
            continue
        res = {}
        for lenient in (True, False):
            pts_by_r: dict[float, list] = {}
            law = None
            for h in heads:
                r, law = _sprinkler_radius(room, h.spec.get("response"), fp, lenient)
                pts_by_r.setdefault(r, []).append((h.x, h.y))
            covered = unary_union([C.circles(p, r) for r, p in pts_by_r.items()])
            res[lenient] = (C.pieces(area.difference(covered)), law, max(pts_by_r))
        hard, law_l, r_l = res[True]
        soft, law_s, r_s = res[False]
        if hard:
            g = unary_union(hard)
            n = math.ceil(g.area / (2 * r_l * r_l))
            far = _farthest(g, heads)
            findings.append(Finding(
                "SPK-46", _sev_for([room], RED), "距離超過", floor.label or "", f"{room.name} 有 {_fmt(g.area)} ㎡ 不在任何撒水頭 {r_l} m 範圍內",
                f"任一點至撒水頭之水平距離應在 {r_l} m 以下（依撒水頭感度、構造取最寬鬆值仍不符）；"
                f"範圍內最遠點離最近撒水頭約 {_fmt(far)} m",
                f"在標示範圍內增設約 {n} 個撒水頭（間距不超過 {math.floor(r_l * math.sqrt(2) * 20) / 20} m），或調整配置使任一點在 {r_l} m 內",
                [law_l], rooms=[room.name], area=g.area, geom=g, metrics={"radius": r_l, "add": n, "farthest": round(far, 2)}))
        elif soft:
            g = unary_union(soft)
            missing = []
            if unknown_resp:
                missing.append("撒水頭感度（一般反應型／快速反應型，設備表或圖例註記）")
            if fp is None:
                missing.append("建築物是否為防火構造")
            findings.append(Finding(
                "SPK-46", YELLOW, "資料不足", floor.label or "", f"{room.name} 有 {_fmt(g.area)} ㎡ 只在較嚴格的條件下超出撒水頭範圍",
                f"若為一般反應型撒水頭{'、非防火構造' if fp is None else ''}，水平距離上限為 {r_s} m，此範圍會超出；條件確認後才能判定",
                "補齊下列資料後重新檢核；若確為較嚴格的條件，需在標示範圍增設撒水頭",
                [law_s], missing=missing, rooms=[room.name], area=g.area, geom=g, metrics={"radius": r_s}))
    if exempt:
        notes.append(Note("SPK-46", "免設撒水頭處所：" + "、".join(f"{r.name}（{r.area:.0f} ㎡）" for r, _ in exempt),
                          sorted({b for _, b in exempt})))
    return findings, notes


def _farthest(g, eqs: list[Equipment], max_samples: int = 4000) -> float:
    """範圍內離最近設備最遠的距離（在範圍內取樣點＋頂點計算；最遠點常在範圍中間，不在邊界上）。"""
    if not eqs or g is None or g.is_empty:
        return float("nan")
    import shapely
    x0, y0, x1, y1 = g.bounds
    step = max(0.1, math.sqrt((x1 - x0) * (y1 - y0) / max_samples))
    xs, ys = np.meshgrid(np.arange(x0, x1 + step, step), np.arange(y0, y1 + step, step))
    inside = shapely.contains_xy(g, xs, ys)
    pts = [np.column_stack([xs[inside], ys[inside]])]
    for p in getattr(g, "geoms", [g]):
        pts.append(np.asarray(p.exterior.coords))
    P = np.vstack(pts)
    E = np.array([(e.x, e.y) for e in eqs])
    best = np.full(len(P), np.inf)
    for i in range(0, len(E), 256):                       # 分批避免大矩陣
        d = np.hypot(P[:, None, 0] - E[None, i:i + 256, 0], P[:, None, 1] - E[None, i:i + 256, 1]).min(axis=1)
        best = np.minimum(best, d)
    return float(best.max())


# ── 水平距離（消防栓 25 m、揚聲器 10 m）──────────────────────────────────

def _horizontal(rule: str, label: str, kind: str, radius: float, law: list[str], floor: Floor,
                eq: list[Equipment], small_room_rule: bool = False):
    items = _of(eq, kind)
    if not items:
        return [], []
    findings = []
    for p in C.uncovered(floor.region, [(e.x, e.y) for e in items], radius):
        rooms = _rooms_touching(floor, p)
        sev = _sev_for(rooms, RED)
        note = ""
        if small_room_rule and rooms and all(r.area <= (6 if r.kind == "room" else 30) for r in rooms):
            sev, note = ORANGE, "；此範圍屬小面積房間，若相鄰區域揚聲器在 8 m 內得免設（第 133 條第 2 款第 4 目但書），請確認"
        c = p.representative_point()
        near = floor.room_at(c.x, c.y)
        far = _farthest(p, items)
        findings.append(Finding(
            rule, sev, "距離超過", floor.label or "", f"{'、'.join(_room_names(rooms)[:3]) or '標示範圍'} 有 {_fmt(p.area)} ㎡ 不在任何{label} {_fmt(radius)} m 範圍內",
            f"各層任一點至{label}之水平距離應在 {_fmt(radius)} m 以下；範圍內最遠點離最近{label}約 {_fmt(far)} m{note}",
            f"在 {near.name if near else '標示範圍'} 附近增設{label}，或調整既有{label}位置，使標示範圍在 {_fmt(radius)} m 內",
            law, rooms=_room_names(rooms), area=p.area, geom=p, metrics={"radius": radius, "farthest": round(far, 2)}))
    return findings, []


def hydrant_distance(floor, eq, ctx, grid=None):
    return _horizontal("HYD-34", "室內消防栓", "hydrant", 25.0, ["D0120029/34/1/1/1", "D0120029/34/1/2/1"], floor, eq)


def speaker_distance(floor, eq, ctx, grid=None):
    return _horizontal("SPKR-133", "揚聲器", "speaker", 10.0, ["D0120029/133/1/2/4"], floor, eq, small_room_rule=True)


# ── 滅火器：第 31 條 ─────────────────────────────────────────────────────

EXT_LIMIT = 20.0


def extinguisher_walk(floor: Floor, eq: list[Equipment], ctx: Context, grid: C.WalkGrid | None = None):
    ext = _of(eq, "extinguisher")
    if not ext:
        return [], []
    grid = grid or C.WalkGrid(floor.walkable)
    d = grid.distances([(e.x, e.y) for e in ext])
    skip = unary_union([r.polygon for r in floor.rooms if r.kind in ("elevator", "shaft")])
    region = floor.region.difference(skip) if not skip.is_empty else floor.region
    cells = grid.region_cells(region)
    findings = []
    hard_lim = EXT_LIMIT * (1 + C.WALK_TOL)
    for sev, mask, desc in (
        (RED, cells & (d > hard_lim) & ~np.isinf(d), "超過"),
        (ORANGE, cells & (d > EXT_LIMIT) & (d <= hard_lim), "略超過（在計算誤差範圍內）"),
    ):
        for p in grid.cells_to_polygons(mask):
            rooms = _rooms_touching(floor, p)
            sub = grid.region_cells(p) & mask
            dmax = float(d[sub].max()) if sub.any() else float("nan")
            findings.append(Finding(
                "EXT-31-3", _sev_for(rooms, sev), "距離超過", floor.label or "",
                f"{'、'.join(_room_names(rooms)[:3]) or '標示範圍'} 有 {_fmt(p.area)} ㎡ 步行到最近滅火器{desc} {EXT_LIMIT:.0f} m",
                f"樓面居室任一點至滅火器之步行距離應在 {EXT_LIMIT:.0f} m 以下；範圍內最遠點沿走道約需走 {_fmt(dmax)} m"
                + ("（格點近似誤差約 ±4%，請人工量測確認）" if sev == ORANGE else ""),
                "在標示範圍附近（走道或出入口旁）增設滅火器，或移動既有滅火器，使任一點步行 20 m 內可取得",
                ["D0120029/31/1/3"], rooms=_room_names(rooms), area=p.area, geom=p, metrics={"max_walk": dmax}))
    unreachable = cells & np.isinf(d)
    if unreachable.any():
        polys = grid.cells_to_polygons(unreachable)
        rooms = sorted({r.name for p in polys for r in _rooms_touching(floor, p)})
        if polys:
            g = unary_union(polys)
            findings.append(Finding(
                "EXT-31-3", YELLOW, "資料不足", floor.label or "", f"{_fmt(g.area)} ㎡ 無法計算步行距離（走不進去）",
                "這些範圍與滅火器之間沒有可走的路徑，通常是門沒畫在門圖層，或房間沒有開口",
                "確認標示房間的出入口；若門畫在其他圖層，請設定圖層對應後重新檢核",
                ["D0120029/31/1/3"], missing=["標示房間的出入口（門）"], rooms=rooms[:10], area=g.area, geom=g))
    return findings, []


def extinguisher_count(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    ext = _of(eq, "extinguisher")
    if not ext:
        return [], []
    A = floor.area
    base = {"1-5": (100, "D0120029/31/1/1/1"), "2-4": (200, "D0120029/31/1/1/2")}
    if ctx.occupancy_group in base:
        (u_s, law_s), (u_l, law_l) = base[ctx.occupancy_group], base[ctx.occupancy_group]
    else:
        (u_s, law_s), (u_l, law_l) = base["1-5"], base["2-4"]
    need_s, need_l = math.ceil(A / u_s), math.ceil(A / u_l)
    known = [e.spec.get("a_value") for e in ext]
    p_min = sum(a if a else 1 for a in known)
    p_max = math.inf if any(a is None for a in known) else sum(known)
    if p_min >= need_s:
        return [], []
    why = (f"本層樓地板面積約 {_fmt(A)} ㎡，每 {u_s if u_s == u_l else f'{u_s}（第一、五款場所）或 {u_l}（第二至四款場所）'} ㎡ 需一個滅火效能值，"
           f"需要 {need_s if need_s == need_l else f'{need_l}～{need_s}'} 個效能值；圖上滅火器 {len(ext)} 具"
           + (f"，效能值合計 {p_min}" if p_max != math.inf else "，效能值未標示（每具至少以 1 計）"))
    if p_max < need_l:
        return [Finding("EXT-31-1", RED, "數量不足", floor.label or "", f"本層滅火效能值不足（{p_max} < {need_l}）", why,
                        f"增設滅火器或改用效能值較高的型號，使效能值合計達 {need_s if need_s == need_l else need_l} 以上",
                        sorted({law_s, law_l}), area=A, metrics={"need": [need_l, need_s], "have": [p_min, p_max]})], []
    missing = []
    if any(a is None for a in known):
        missing.append("各滅火器之滅火效能值（設備表）")
    if ctx.occupancy_group not in base:
        missing.append("場所類別（第 12 條第幾款，決定每 100 或 200 ㎡ 一個效能值）")
    return [Finding("EXT-31-1", YELLOW, "資料不足", floor.label or "", "本層滅火效能值是否足夠需補資料才能判定", why,
                    "補齊資料後重新檢核", sorted({law_s, law_l}), missing=missing, area=A,
                    metrics={"need": [need_l, need_s], "have": [p_min, None if p_max == math.inf else p_max]})], []


def extinguisher_electrical(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    """第 31 條第 2 款：電氣設備使用之處所，每 100 ㎡（含未滿）另設一滅火器（放在該室內或出入口外 3 m 內）。
    本層圖上完全沒有滅火器時不跑（多半不是消防設備圖；是否應設由場所判定規則處理）。"""
    ext = _of(eq, "extinguisher")
    if not ext:
        return [], []
    findings = []
    for room in floor.rooms:
        if room.kind != "electrical":
            continue
        need = math.ceil(room.area / 100)
        zone = room.polygon.buffer(3.0)
        have = sum(1 for e in ext if zone.covers(Point(e.x, e.y)))
        if have < need:
            findings.append(Finding(
                "EXT-31-2", _sev_for([room], RED), "數量不足", floor.label or "", f"{room.name} 未另設滅火器（需 {need} 具，現有 {have} 具）",
                f"電氣設備使用之處所每 100 ㎡（含未滿）應另設一滅火器；此室約 {_fmt(room.area)} ㎡，需 {need} 具",
                f"在 {room.name} 內或出入口旁增設 {need - have} 具滅火器", ["D0120029/31/1/2"],
                rooms=[room.name], area=room.area, geom=room.polygon, metrics={"need": need, "have": have}))
    return findings, []


# ── 探測器：第 120 條（熱式局限型）、第 122 條（偵煙式局限型）；第 116 條免設 ───

# (種類, 種別) → {高度區間: (防火構造, 其他構造)}；None＝該高度不得使用
HEAT_TABLE = {
    ("差動式", "1"): {"lt4": (90, 50), "4to8": (45, 30)},
    ("差動式", "2"): {"lt4": (70, 40), "4to8": (35, 25)},
    ("補償式", "1"): {"lt4": (90, 50), "4to8": (45, 30)},
    ("補償式", "2"): {"lt4": (70, 40), "4to8": (35, 25)},
    ("定溫式", "特種"): {"lt4": (70, 40), "4to8": (35, 25)},
    ("定溫式", "1"): {"lt4": (60, 30), "4to8": (30, 15)},
    ("定溫式", "2"): {"lt4": (20, 15), "4to8": None},
}
SMOKE_TABLE = {"1": {"lt4": 150, "4to20": 75}, "2": {"lt4": 150, "4to20": 75}, "3": {"lt4": 50, "4to20": None}}


def _eff_area(dtype: str, dclass: str, band: str, fp: bool) -> float | None:
    if dtype == "偵煙式":
        b = "lt4" if band == "lt4" else "4to20"
        return SMOKE_TABLE.get(dclass, {}).get(b)
    row = HEAT_TABLE.get((dtype, dclass))
    if not row:
        return None
    v = row.get(band)
    return None if v is None else v[0 if fp else 1]


def detector_count(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    dets = [e for e in _of(eq, "detector") if "detector_type" in e.spec]
    if not dets:
        return [], []
    fp = _fireproof(floor, ctx)
    h = ctx.ceiling_height.get(floor.label or "")
    findings, exempt = [], []
    for room in floor.rooms:
        if room.kind in ("void", "outdoor", "elevator", "shaft", "stair", "corridor"):
            continue                                   # 走廊、樓梯、管道間依第 122 條另計，下一批
        if room.kind == "toilet" and not room.conflict:
            exempt.append(room)
            continue
        if room.area < 2:
            continue
        inside = [d for d in dets if room.polygon.covers(Point(d.x, d.y))]
        if not inside:
            findings.append(Finding(
                "DET-120", _sev_for([room], RED), "未設置", floor.label or "", f"{room.name}（{_fmt(room.area)} ㎡）未設探測器",
                "本層設有火警探測器，但此房間內沒有任何探測器；每一探測區域至少需設一個",
                f"在 {room.name} 設置探測器；若屬第 116 條得免設處所（外氣流通、冷藏庫、金庫等），請在圖上註明",
                ["D0120029/120/1/2", "D0120029/116/1"], rooms=[room.name], area=room.area, geom=room.polygon))
            continue
        # 房內以數量最多的種類計
        kinds: dict[tuple[str, str], int] = {}
        for d in inside:
            k = (d.spec["detector_type"], d.spec["detector_class"])
            kinds[k] = kinds.get(k, 0) + 1
        (dtype, dclass), _ = max(kinds.items(), key=lambda kv: kv[1])
        law = "D0120029/122/1/4" if dtype == "偵煙式" else "D0120029/120/1/2"
        bands = ["lt4"] if (h is not None and h < 4) else (["4to8"] if h is not None else ["lt4", "4to8"])
        fps = [fp] if fp is not None else [True, False]
        effs = [e for b in bands for f in fps if (e := _eff_area(dtype, dclass, b, f))]
        invalid = any(_eff_area(dtype, dclass, b, f) is None for b in bands for f in fps)
        if not effs:
            findings.append(Finding(
                "DET-120", RED, "規格不符", floor.label or "", f"{room.name} 的{dtype}{dclass}種探測器不適用此裝置面高度",
                f"裝置面高度 {h} m 時，{dtype}局限型{dclass}種不得使用（表列為「–」）", "改用適用該高度的探測器種類",
                [law], rooms=[room.name], area=room.area, geom=room.polygon))
            continue
        need_l, need_s = math.ceil(room.area / max(effs)), math.ceil(room.area / min(effs))
        have = len(inside)
        if have >= need_s and not invalid:
            continue
        hdesc = f"裝置面高度 {h} m" if h is not None else "裝置面高度未標示（以未滿 4 m 與 4～8 m 兩種情形計）"
        why = (f"{room.name} 約 {_fmt(room.area)} ㎡，{dtype}局限型{dclass}種，{hdesc}，"
               f"{'防火構造' if fp else ('非防火構造' if fp is False else '構造未知')}；"
               f"有效探測範圍 {_fmt(max(effs))}{'' if max(effs) == min(effs) else f'～{_fmt(min(effs))}'} ㎡，"
               f"需 {need_l}{'' if need_l == need_s else f'～{need_s}'} 個，現有 {have} 個")
        if have < need_l:
            findings.append(Finding(
                "DET-120", _sev_for([room], RED), "數量不足", floor.label or "", f"{room.name} 探測器不足（需 {need_l} 個，現有 {have} 個）",
                why, f"在 {room.name} 增設 {need_l - have} 個探測器，並平均配置於探測區域", [law],
                rooms=[room.name], area=room.area, geom=room.polygon, metrics={"need": [need_l, need_s], "have": have}))
        else:
            missing = []
            if h is None:
                missing.append(f"{floor.label} 天花板（裝置面）高度")
            if fp is None:
                missing.append("建築物是否為防火構造")
            findings.append(Finding(
                "DET-120", YELLOW, "資料不足", floor.label or "", f"{room.name} 探測器數量需補資料才能判定", why,
                f"補齊資料後重新檢核；若條件較嚴格，需增設至 {need_s} 個", [law], missing=missing,
                rooms=[room.name], area=room.area, geom=room.polygon, metrics={"need": [need_l, need_s], "have": have}))
    notes = []
    if exempt:
        notes.append(Note("DET-120", "免設探測器處所（廁所、浴室）：" + "、".join(r.name for r in exempt), ["D0120029/116/1/3"]))
    return findings, notes


RULES = [
    ("SPK-46", "撒水頭水平距離", sprinkler_distance),
    ("HYD-34", "室內消防栓水平距離", hydrant_distance),
    ("SPKR-133", "揚聲器水平距離", speaker_distance),
    ("EXT-31-3", "滅火器步行距離", extinguisher_walk),
    ("EXT-31-1", "滅火效能值", extinguisher_count),
    ("EXT-31-2", "電氣設備處所滅火器", extinguisher_electrical),
    ("DET-120", "探測器數量", detector_count),
]
