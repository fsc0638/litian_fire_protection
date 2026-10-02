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
import shapely
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from litian.plan.floor import Floor, Room, _parts
from litian.review import coverage as C
from litian.review.equipment import Equipment

RED, ORANGE, YELLOW, BLUE = "RED", "ORANGE", "YELLOW", "BLUE"
SEVERITY_LABEL = {RED: "不符", ORANGE: "需確認", YELLOW: "資料不足", BLUE: "建議"}
UNSURE_KINDS = ("unknown", "mixed")

# 法規解讀的預設（條文沒寫死、實務有不同讀法的地方）。事務所可在案件條件 policy 覆寫；
# 預設只降嚴重度並在缺失裡寫明兩種讀法，不直接刪除缺失。
DEFAULT_POLICY = {
    "shaft_in_coverage": False,              # 管道間、昇降機道算不算水平距離檢討範圍（無樓地板、無人員停留）
    "stair_speaker_vertical": True,          # 樓梯間廣播依第 133 條第 2 款第 5 目（垂直每 15 m 一個 L 級），不套水平 10 m
    "habitable_only_extinguisher": True,     # 滅火器步行距離只檢討居室（第 31 條第 3 款「樓面居室任一點」）
    "exit_sign_counts_for_direction": True,  # 出口標示燈有效範圍併入走廊避難方向指示燈涵蓋（結果標需確認）
    "voluntary_signs_note": True,            # 依第 23 條非應設的標示設備（自主設置），涵蓋缺失改為建議
}


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
    policy: dict = field(default_factory=dict)                       # 法規解讀覆寫（鍵見 DEFAULT_POLICY）

    def rule(self, key: str) -> bool:
        """法規解讀設定：案件有覆寫用覆寫值，否則用預設。"""
        return bool(self.policy.get(key, DEFAULT_POLICY[key]))

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
            floor_area={k: float(v) for k, v in (d.get("floor_area") or {}).items() if v not in (None, "")},
            policy={k: bool(v) for k, v in (d.get("policy") or {}).items() if k in DEFAULT_POLICY})


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
    if room.kind == "stair" and re.search(r"安全梯|排煙室", names):
        return "D0120029/49/1/2"                 # 只限室內安全梯間、特別安全梯間（一般樓梯不免設）
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


# ── 檢核範圍：樓地板上的房間＋沒圍成房間的開放樓地板（不含牆厚帶）─────────────

OPEN_GAP = 0.6         # 開放樓地板：離所有房間這個距離以外才算（房間之間、外牆的牆厚帶不算）
OPEN_MIN = 5.0         # 開放樓地板最小面積（㎡）；更小的多半是牆角、柱邊空隙
MARGIN = 0.3           # 邊際超出：最遠點只超出半徑這麼多以內、且面積不到 1 ㎡（圖面誤差等級）


def _floor_rooms(floor: Floor, skip: tuple[str, ...] = ()) -> list[Room]:
    """逐房檢核的房間：算樓地板的（屋突層屋頂碎塊不算）、不是挑空或室外、不在 skip 種類內。"""
    return [r for r in floor.rooms if r.kind not in ("void", "outdoor", *skip) and floor.in_region(r)]


def _open_floor(floor: Floor) -> list:
    """沒圍成房間的開放樓地板：樓地板扣掉所有房間（外擴 0.6 m）後，寬 0.6 m 以上、5 ㎡ 以上的部分。"""
    cut = unary_union([r.hole or r.polygon for r in floor.rooms])
    rest = floor.region.difference(cut.buffer(OPEN_GAP)) if not cut.is_empty else floor.region
    return [p for p in _parts(rest) if p.area >= OPEN_MIN and not p.buffer(-0.3).is_empty]


def _check_area(floor: Floor, rooms: list[Room]):
    return unary_union([r.polygon.intersection(floor.region) for r in rooms] + _open_floor(floor))


def _list(items: list[str], n: int = 8) -> str:
    return "、".join(items[:n]) + (f" 等 {len(items)} 處" if len(items) > n else "")


OPEN_WHY = "；此範圍沒有圍成房間、也沒有房名（可能是開放空間、梯廳或挑空邊緣），請確認是否為樓地板"


def _sev_at(rooms: list[Room], base: str) -> str:
    """同 _sev_for；沒圍成房間的開放樓地板比照名稱不明的房間，降為需確認。"""
    return (ORANGE if base == RED else base) if not rooms else _sev_for(rooms, base)


# ── 水平距離（消防栓 25 m、揚聲器 10 m）──────────────────────────────────

def _proviso_limit(r: Room) -> float:
    """第 133 條第 2 款第 4 目但書的面積上限：居室、主要走廊通道 6 ㎡，其他非居室 30 ㎡。"""
    if r.kind in ("room", "corridor", "kitchen", "unknown", "mixed") and not any("儲藏" in s for s in r.labels):
        return 6.0
    return 30.0


def _stair_core(rooms: list[Room], stairs: list[Room]) -> bool:
    """未涵蓋塊只在樓梯間，或在緊鄰（0.5 m 內）未涵蓋樓梯間、不到 10 ㎡ 的附屬小房間（梯內儲藏室等）。"""
    return bool(rooms) and all((r.kind == "stair" and not r.conflict) or
                               (r.area < 10 and any(s.polygon.distance(r.polygon) <= 0.5 for s in stairs)) for r in rooms)


def _horizontal(rule: str, label: str, kind: str, radius: float, law: list[str], floor: Floor,
                eq: list[Equipment], ctx: Context, speaker: bool = False):
    """量測範圍＝樓地板上的房間內部＋開放樓地板（不含牆厚帶）。未涵蓋塊依所在房間分流：
    管道間、昇降機道（解讀設定）→ 說明；揚聲器遇樓梯間（第 5 目另計）→ 說明；
    消防栓只差在樓梯核 → 需確認；揚聲器小房間符合但書（實算 8 m）→ 說明；其餘列缺失。"""
    items = _of(eq, kind)
    if not items:
        return [], []
    pts = np.array([(e.x, e.y) for e in items])
    nearest = lambda g: min(g.distance(Point(x, y)) for x, y in pts)  # noqa: E731
    pieces = [(p, _rooms_touching(floor, p)) for p in C.uncovered(_check_area(floor, _floor_rooms(floor)), pts.tolist(), radius)]
    plain = lambda rs, kinds: rs and all(r.kind in kinds and not r.conflict for r in rs)  # noqa: E731
    stairs_hit = [r for _, rs in pieces for r in rs if r.kind == "stair" and not r.conflict]
    findings, shafts, stairs, proviso = [], [], [], []
    for p, rooms in pieces:
        far = _farthest(p, items)
        desc = f"{'、'.join(_room_names(rooms)[:3])} {_fmt(p.area)} ㎡（最遠 {_fmt(far)} m）"
        if plain(rooms, ("shaft", "elevator")) and not ctx.rule("shaft_in_coverage"):
            shafts.append(desc)
            continue
        if speaker and plain(rooms, ("stair",)) and ctx.rule("stair_speaker_vertical"):
            stairs.append(desc)
            continue
        if speaker and rooms and all(r.area <= _proviso_limit(r) for r in rooms):
            gap = max(nearest(r.polygon) for r in rooms)
            if gap <= 8.0:
                proviso.append(f"{desc}，離相鄰揚聲器 {_fmt(gap)} m")
                continue
        sev, why_extra = _sev_at(rooms, RED), ("" if rooms else OPEN_WHY)
        c = p.representative_point()
        near = floor.room_at(c.x, c.y)
        fix = f"在 {near.name if near else '標示範圍'} 附近增設{label}，或調整既有{label}位置，使標示範圍在 {_fmt(radius)} m 內"
        if not speaker and _stair_core(rooms, stairs_hit):
            sev = ORANGE if sev == RED else sev
            why_extra = ("。此範圍為樓梯間（或附屬於樓梯間的小房間），有兩種讀法：（一）嚴格：各層任一點都要在同層消防栓 25 m 內，"
                         "則不符；（二）實務：樓梯間（例如挑空層中的樓梯核）由上下層消防栓經樓梯取用，視為涵蓋。請確認採用哪一種讀法")
            fix = ("採嚴格讀法時，在樓梯間附近增設室內消防栓；採實務讀法時，請在圖上註明由上下層哪一支消防栓涵蓋此樓梯核，"
                   "並確認其水平距離在 25 m 以下")
        where = "、".join(_room_names(rooms)[:3]) or "未圍成房間的樓地板"
        marginal = far <= radius + MARGIN and p.area < 1.0
        title = f"{where} 有 {_fmt(p.area)} ㎡ 不在任何{label} {_fmt(radius)} m 範圍內" + (f"（邊際超出，最遠 {_fmt(far)} m）" if marginal else "")
        cite = list(law)
        if marginal:
            fix = f"超出幅度很小：將相鄰的{label}移動約 0.3 m（或在附近增設），使標示範圍在 {_fmt(radius)} m 內"
            if speaker:
                fix += "；或依第 133 條第 3 款以音壓計算替代（廣播區域內距樓地板 1 m 處音壓在 75 分貝以上）"
                cite.append("D0120029/133/1/3")
        findings.append(Finding(
            rule, sev, "距離超過", floor.label or "", title,
            f"各層任一點至{label}之水平距離應在 {_fmt(radius)} m 以下；範圍內最遠點離最近{label}約 {_fmt(far)} m{why_extra}",
            fix, cite, rooms=_room_names(rooms), area=p.area, geom=p,
            metrics={"radius": radius, "farthest": round(far, 2), **({"marginal": True} if marginal else {})}))
    notes = []
    if shafts:
        other = "廣播區域" if speaker else "「各層任一點」"
        notes.append(Note(rule, f"管道間、昇降機道無樓地板、無人員停留，未納入{label} {_fmt(radius)} m 水平距離檢討：{_list(shafts)}。"
                                f"另一種讀法：管道間、昇降機道也屬{other}，則上列範圍超出 {_fmt(radius)} m，請確認", law))
    if stairs:
        notes.append(Note(rule, f"樓梯間不套揚聲器水平 10 m（依第 133 條第 2 款第 5 目，樓梯垂直距離每 15 m 設一個 L 級揚聲器，"
                                f"由建築物層級規則 SPKR-133-5 檢核）；本層超出 10 m 的樓梯間：{_list(stairs)}。"
                                f"另一種讀法：樓梯間也屬廣播區域，需符合水平 10 m，請確認", ["D0120029/133/1/2/5"]))
    if proviso:
        notes.append(Note(rule, f"小面積房間（居室、走廊 6 ㎡ 以下，其他非居室 30 ㎡ 以下）且與相鄰區域揚聲器相距 8 m 以下，"
                                f"依第 133 條第 2 款第 4 目但書得免設：{_list(proviso)}", law))
    return findings, notes


def hydrant_distance(floor, eq, ctx, grid=None):
    return _horizontal("HYD-34", "室內消防栓", "hydrant", 25.0, ["D0120029/34/1/1/1", "D0120029/34/1/2/1"], floor, eq, ctx)


def speaker_distance(floor, eq, ctx, grid=None):
    return _horizontal("SPKR-133", "揚聲器", "speaker", 10.0, ["D0120029/133/1/2/4"], floor, eq, ctx, speaker=True)


# ── 滅火器：第 31 條 ─────────────────────────────────────────────────────

EXT_LIMIT = 20.0


OUTSIDE_RING = 5.0     # 避難層、屋突層：外框外這個寬度的屋外當作可通行的連接路徑


def _non_habitable(r: Room) -> bool:
    """第 31 條第 3 款「樓面居室」以外的房間：樓梯間、廁所、管道間、儲藏室（倉庫是居室，不算）。名稱衝突的不算。"""
    return not r.conflict and (r.kind in ("stair", "toilet", "shaft") or any("儲藏" in s for s in r.labels))


def _cells_in(grid: C.WalkGrid, g) -> np.ndarray:
    """可走格中、中心點落在 g 內的遮罩（只算 g 外接矩形內的格子，逐房計算才不會每次掃整張圖）。"""
    m = np.zeros_like(grid.free)
    if g.is_empty:
        return m
    x0, y0, x1, y1 = g.bounds
    c0, c1 = max(0, int((x0 - grid.x0) / grid.cell)), min(grid.nx, int((x1 - grid.x0) / grid.cell) + 1)
    r0, r1 = max(0, int((y0 - grid.y0) / grid.cell)), min(grid.ny, int((y1 - grid.y0) / grid.cell) + 1)
    if c0 < c1 and r0 < r1:
        shapely.prepare(g)
        m[r0:r1, c0:c1] = grid.free[r0:r1, c0:c1] & shapely.contains_xy(g, grid.gx[r0:r1, c0:c1], grid.gy[r0:r1, c0:c1])
    return m


def _outdoor_link(floor: Floor) -> bool:
    """避難層（1F）與屋突層：樓梯間常只能從屋外、屋頂進出，室內走不到的範圍允許經屋外、屋頂走到滅火器。"""
    lab = floor.label or ""
    return lab == "1F" or lab.startswith("R")


def _outdoor_distances(floor: Floor, grid: C.WalkGrid, pts, d: np.ndarray, todo: np.ndarray) -> np.ndarray:
    """室內走不到的格子（todo）改用「室內＋外框外 5 m 屋外（屋突層再加屋頂面）」的步行距離；仍走不到為 inf。
    只補室內走不到的格子，不拿屋外路徑縮短室內距離。"""
    extra = floor.outline.buffer(OUTSIDE_RING).difference(floor.outline)
    if (floor.label or "").startswith("R"):
        extra = extra.union(floor.outline.difference(floor.region))         # 屋頂面
    out = C.WalkGrid(floor.walkable.union(extra.difference(floor.walls)), grid.cell)
    de = out.distances(pts)
    de[np.isnan(de)] = np.inf
    rr, cc = np.nonzero(todo)
    ci = np.clip(((grid.gx[rr, cc] - out.x0) / out.cell).astype(int), 0, out.nx - 1)
    ri = np.clip(((grid.gy[rr, cc] - out.y0) / out.cell).astype(int), 0, out.ny - 1)
    d = d.copy()
    d[rr, cc] = de[ri, ci]
    return d


def _unreachable_why(floor: Floor, room: Room | None) -> tuple[str, str]:
    """走不進去的原因說明（依房間類型）與改善方式。"""
    if room is None:
        return ("這塊未圍成房間的樓地板與滅火器之間沒有可走的路徑，可能只能從挑空、電梯進出，或是牆線、門畫法造成的封閉範圍",
                "確認此範圍的出入口；若門畫在其他圖層，請設定圖層對應後重新檢核")
    if room.kind == "stair":
        return ("樓梯間在本層的門外不屬本層樓地板（挑空、屋外或屋頂），或門沒畫在門圖層，本層算不出步行路徑",
                "確認樓梯間在本層的出入口；若只能從上下層或屋外進出，請確認經樓梯、屋外至最近滅火器的步行距離")
    edge = [r.polygon for r in floor.rooms if r.kind in ("void", "outdoor")]
    if not _outdoor_link(floor):
        edge.append(floor.outline.exterior)
    if any(room.polygon.distance(g) <= OPEN_GAP for g in edge):
        return ("此房間緊鄰挑空或外牆，可能只能從挑空、屋外或上下層進出；若有通往本層的門，可能是門沒畫在門圖層",
                "確認此房間的出入口；若門畫在其他圖層，請設定圖層對應後重新檢核")
    return ("此房間與滅火器之間沒有可走的路徑，通常是門沒畫在門圖層，或房間沒有開口",
            "確認此房間的出入口；若門畫在其他圖層，請設定圖層對應後重新檢核")


def extinguisher_walk(floor: Floor, eq: list[Equipment], ctx: Context, grid: C.WalkGrid | None = None):
    """樓地板上的房間逐房檢核（昇降機道、管道間不檢核）＋開放樓地板；每個房間各列一條。
    走不進去的範圍寬度不到 0.6 m 或不到 1 ㎡ 的是牆縫，不列。避難層、屋突層室內走不到的範圍改算經屋外、屋頂的距離。
    解讀設定 habitable_only_extinguisher：樓梯間、廁所、儲藏室等非居室只列說明，不列缺失。"""
    ext = _of(eq, "extinguisher")
    if not ext:
        return [], []
    pts = [(e.x, e.y) for e in ext]
    grid = grid or C.WalkGrid(floor.walkable)
    d = grid.distances(pts)
    zones = [(r, r.polygon.intersection(floor.region)) for r in _floor_rooms(floor, skip=("elevator", "shaft"))]
    zones = [(r, _cells_in(grid, z)) for r, z in zones + [(None, p) for p in _open_floor(floor)]]
    todo = np.isinf(d) & np.logical_or.reduce([c for _, c in zones] + [np.zeros_like(grid.free)])
    via_out = np.zeros_like(grid.free)
    if _outdoor_link(floor) and todo.any():
        d = _outdoor_distances(floor, grid, pts, d, todo)
        via_out = todo & np.isfinite(d)
    hard_lim = EXT_LIMIT * (1 + C.WALK_TOL)
    habitable_only = ctx.rule("habitable_only_extinguisher")
    findings, skipped = [], []
    for room, cells in zones:
        if not cells.any():
            continue
        rooms = [room] if room else []
        where = room.name if room else "未圍成房間的樓地板"
        reference = habitable_only and room is not None and _non_habitable(room)
        for sev, mask, desc in (
            (RED, cells & (d > hard_lim) & ~np.isinf(d), "超過"),
            (ORANGE, cells & (d > EXT_LIMIT) & (d <= hard_lim), "略超過（在計算誤差範圍內）"),
        ):
            polys = grid.cells_to_polygons(mask)
            if not polys:
                continue
            g = unary_union(polys)
            dmax = float(d[mask].max())
            outside = bool((mask & via_out).any())
            if reference:
                skipped.append(f"{where} {_fmt(g.area)} ㎡ 步行約 {_fmt(dmax)} m" + ("（經屋外）" if outside else "")
                               + ("（誤差內）" if sev == ORANGE else ""))
                continue
            findings.append(Finding(
                "EXT-31-3", _sev_at(rooms, sev), "距離超過", floor.label or "",
                f"{where} 有 {_fmt(g.area)} ㎡ 步行到最近滅火器{desc} {EXT_LIMIT:.0f} m",
                f"樓面居室任一點至滅火器之步行距離應在 {EXT_LIMIT:.0f} m 以下；範圍內最遠點沿走道約需走 {_fmt(dmax)} m"
                + ("（本層室內走不到，此為經屋外、屋頂繞行的距離；若有室內出入口沒畫出，請補正後重新檢核）" if outside else "")
                + ("（格點近似誤差約 ±4%，請人工量測確認）" if sev == ORANGE else "") + ("" if rooms else OPEN_WHY),
                "在標示範圍附近（走道或出入口旁）增設滅火器，或移動既有滅火器，使任一點步行 20 m 內可取得",
                ["D0120029/31/1/3"], rooms=_room_names(rooms), area=g.area, geom=g,
                metrics={"max_walk": round(dmax, 2), **({"via_outdoor": True} if outside else {})}))
        # 走不進去：寬度不到 0.6 m 或不到 1 ㎡ 的是牆縫、窗邊縫，不列
        polys = [p for p in grid.cells_to_polygons(cells & np.isinf(d)) if p.area >= 1.0 and not p.buffer(-0.3).is_empty]
        if not polys:
            continue
        g = unary_union(polys)
        if reference:
            skipped.append(f"{where} {_fmt(g.area)} ㎡ 走不進去")
            continue
        why, fix = _unreachable_why(floor, room)
        findings.append(Finding(
            "EXT-31-3", YELLOW, "資料不足", floor.label or "", f"{where} 有 {_fmt(g.area)} ㎡ 無法計算步行距離（走不進去）",
            why, fix, ["D0120029/31/1/3"], missing=["標示房間的出入口（門）"], rooms=_room_names(rooms), area=g.area, geom=g))
    notes = []
    if skipped:
        notes.append(Note("EXT-31-3", f"非居室（樓梯間、廁所、儲藏室），依第 31 條第 3 款「樓面居室任一點」屬參考，未列缺失：{_list(skipped)}。"
                                      "另一種讀法：整層樓面都要在步行 20 m 內，則上列範圍需人工確認", ["D0120029/31/1/3"]))
    return findings, notes


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

# (種類, 種別) → {高度區間: (防火構造, 其他構造)}；None＝該高度不得使用。
# 第 114 條選用表：熱式局限型只能用在未滿 8 m（定溫式 4～8 m 限特種、一種）；偵煙式局限型 4 m 以上限一、二種，15～20 m 限一種
HEAT_TABLE = {
    ("差動式", "1"): {"lt4": (90, 50), "4to8": (45, 30)},
    ("差動式", "2"): {"lt4": (70, 40), "4to8": (35, 25)},
    ("補償式", "1"): {"lt4": (90, 50), "4to8": (45, 30)},
    ("補償式", "2"): {"lt4": (70, 40), "4to8": (35, 25)},
    ("定溫式", "特種"): {"lt4": (70, 40), "4to8": (35, 25)},
    ("定溫式", "1"): {"lt4": (60, 30), "4to8": (30, 15)},
    ("定溫式", "2"): {"lt4": (20, 15), "4to8": None},
}
SMOKE_TABLE = {"1": {"lt4": 150, "4to15": 75, "15to20": 75}, "2": {"lt4": 150, "4to15": 75, "15to20": None},
               "3": {"lt4": 50, "4to15": None, "15to20": None}}


def height_band(h: float) -> str:
    return "lt4" if h < 4 else ("4to8" if h < 8 else ("8to15" if h < 15 else ("15to20" if h < 20 else "ge20")))


def _eff_area(dtype: str, dclass: str, band: str, fp: bool) -> float | None:
    if dtype == "偵煙式":
        b = {"4to8": "4to15", "8to15": "4to15"}.get(band, band)
        return SMOKE_TABLE.get(dclass, {}).get(b)
    row = HEAT_TABLE.get((dtype, dclass))
    if not row:
        return None
    v = row.get(band)                             # 8 m 以上不在表內 → None（第 114 條不得使用）
    return None if v is None else v[0 if fp else 1]


def detector_count(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    """逐房比對探測器數量（第 120、122 條面積表）。探測器歸房間用 room_near（壓在牆線、門弧、標籤缺口上也算，每個只歸一間）；
    engine 從上層挑空投影來的探測器（spec 的 projected_from）照常計入。火焰式不適用面積表，另列第 124 條的資料要求。"""
    dets = [e for e in _of(eq, "detector") if "detector_type" in e.spec]
    flames = _of(eq, "flame_detector")
    if not dets and not flames:
        return [], []
    fp = _fireproof(floor, ctx)
    h = ctx.ceiling_height.get(floor.label or "")
    local_ids = {id(d) for d in dets}
    by_room: dict[int, list[Equipment]] = {}
    for d in dets + flames:
        r = floor.room_near(d.x, d.y, 0.3)
        if r is not None:
            by_room.setdefault(r.id, []).append(d)
    findings, exempt, projected = [], [], []
    for room in floor.rooms:
        if not floor.in_region(room):
            continue                                   # 屋突層屋頂碎塊、天溝等不算樓地板
        if room.kind in ("void", "outdoor", "elevator", "shaft", "stair"):
            continue                                   # 樓梯、昇降路、管道間依第 122 條第 6、7 款另計
        mine = by_room.get(room.id, [])
        local = [d for d in mine if id(d) in local_ids]
        flame = [d for d in mine if "flame_detector" in d.kinds]
        srcs = sorted({str(d.spec["projected_from"]) for d in mine if d.spec.get("projected_from")})
        src = f"（含上層挑空範圍內的探測器，圖號 {'、'.join(srcs)}）" if srcs else ""
        if srcs:
            projected.append(f"{room.name} {sum(1 for d in mine if d.spec.get('projected_from'))} 個（圖號 {'、'.join(srcs)}）")
        if room.kind == "corridor" and re.search(r"走廊|通道|走道", " ".join(room.labels)):
            if not any(d.spec["detector_type"] != "偵煙式" for d in local):
                continue                               # 走廊、通道的偵煙式依第 122 條第 5 款步行距離另計
        if room.kind == "toilet" and not room.conflict:
            exempt.append(room)
            continue
        if room.area < 2:
            continue
        if not local and flame:
            continue                                   # 只設火焰式：依第 124 條以監視距離涵蓋（另列資料要求）
        inside = local
        # 房內另有火焰式：局限型數量不足時，若火焰式監視範圍涵蓋全室可不計，降為需確認
        with_flame = (f"；房內另設火焰式探測器 {len(flame)} 個，若其標稱監視距離涵蓋全室（第 124 條），局限型數量可不計，請確認"
                      if flame else "")
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
        if h is not None and h >= 20 and dtype != "火焰式":
            continue                                   # 裝置面高度超過 20 m 得免設（第 116 條第 1 款）
        bands = [height_band(h)] if h is not None else ["lt4", "4to8"]
        fps = [fp] if fp is not None else [True, False]
        effs = [e for b in bands for f in fps if (e := _eff_area(dtype, dclass, b, f))]
        invalid = any(_eff_area(dtype, dclass, b, f) is None for b in bands for f in fps)
        if not effs:
            findings.append(Finding(
                "DET-120", ORANGE if flame else RED, "規格不符", floor.label or "", f"{room.name} 的{dtype}{dclass}種探測器不適用此裝置面高度",
                f"裝置面高度 {h} m 時，{dtype}局限型{dclass}種不得使用（第 114 條選用表）{src}{with_flame}",
                "改用適用該高度的探測器種類（8 m 以上用差動式分布型、光電式等；15 m 以上限偵煙式一種、光電式分離型或火焰式）",
                [law, "D0120029/114/1"], rooms=[room.name], area=room.area, geom=room.polygon))
            continue
        need_l, need_s = math.ceil(room.area / max(effs)), math.ceil(room.area / min(effs))
        have = len(inside)
        if have >= need_s and not invalid:
            continue
        hdesc = f"裝置面高度 {h} m" if h is not None else "裝置面高度未標示（以未滿 4 m 與 4～8 m 兩種情形計）"
        why = (f"{room.name} 約 {_fmt(room.area)} ㎡，{dtype}局限型{dclass}種，{hdesc}，"
               f"{'防火構造' if fp else ('非防火構造' if fp is False else '構造未知')}；"
               f"有效探測範圍 {_fmt(max(effs))}{'' if max(effs) == min(effs) else f'～{_fmt(min(effs))}'} ㎡，"
               f"需 {need_l}{'' if need_l == need_s else f'～{need_s}'} 個，現有 {have} 個{src}")
        if have < need_l:
            sev = _sev_for([room], RED)
            findings.append(Finding(
                "DET-120", ORANGE if flame and sev == RED else sev, "數量不足", floor.label or "",
                f"{room.name} 探測器不足（需 {need_l} 個，現有 {have} 個）", why + with_flame,
                f"在 {room.name} 增設 {need_l - have} 個探測器，並平均配置於探測區域", [law],
                rooms=[room.name], area=room.area, geom=room.polygon, metrics={"need": [need_l, need_s], "have": have}))
        else:
            missing = []
            if h is None:
                missing.append(f"{floor.label} 天花板（裝置面）高度")
            if fp is None:
                missing.append("建築物是否為防火構造")
            findings.append(Finding(
                "DET-120", YELLOW, "資料不足", floor.label or "", f"{room.name} 探測器數量需補資料才能判定", why + with_flame,
                f"補齊資料後重新檢核；若條件較嚴格，需增設至 {need_s} 個", [law], missing=missing,
                rooms=[room.name], area=room.area, geom=room.polygon, metrics={"need": [need_l, need_s], "have": have}))
    if flames:
        # 標示範圍：火焰式所在的房間（挑空裡的只標設備位置；挑空下方的房間由投影到下層的設備另列）
        rooms = [r for r in floor.rooms if r.kind not in ("void", "outdoor") and floor.in_region(r)
                 and any("flame_detector" in d.kinds for d in by_room.get(r.id, []))]
        srcs = sorted({str(d.spec["projected_from"]) for d in flames if d.spec.get("projected_from")})
        g = unary_union([r.polygon for r in rooms] + [Point(d.x, d.y).buffer(1.0) for d in flames])
        findings.append(Finding(
            "DET-124", BLUE, "需檢附資料", floor.label or "", f"火焰式探測器 {len(flames)} 個：請檢附標稱監視距離與視角",
            "火焰式探測器不適用局限型的有效探測面積表；依第 124 條第 2 款，距樓地板面 1.2 m 範圍內之空間應在探測器標稱監視距離範圍內，"
            "監視範圍無法由平面圖判定" + (f"（含上層挑空範圍內的火焰式探測器，圖號 {'、'.join(srcs)}）" if srcs else ""),
            f"檢附火焰式探測器型錄（標稱監視距離、視角），並在圖上繪出監視範圍，確認{'、'.join(_room_names(rooms)[:3]) or '設置處所'}"
            "距樓地板 1.2 m 內的空間都在監視範圍內", ["D0120029/124/1/2"],
            rooms=_room_names(rooms), area=None, geom=g, metrics={"count": len(flames)}))
    notes = []
    if exempt:
        notes.append(Note("DET-120", "免設探測器處所（廁所、浴室）：" + "、".join(r.name for r in exempt), ["D0120029/116/1/3"]))
    if projected:
        notes.append(Note("DET-120", f"下列房間計入上層挑空範圍內的探測器（依樓層對位投影）：{_list(projected)}", ["D0120029/120/1/2"]))
    return findings, notes


# ── 第 18 條附表：特定房間應選設水霧、泡沫、二氧化碳等滅火設備 ─────────────────
S18 = [   # (房名, 門檻 ㎡ 或依樓層, 附表項次, 可選設備, 得替代說明)
    (r"發電機|變壓器|變電|配電室|電氣室|受電", 200, 5, "水霧、二氧化碳或惰性氣體、鹵化烴或乾粉", None),
    (r"鍋爐|廚房", 200, 6, "二氧化碳或惰性氣體、鹵化烴或乾粉", "設有自動撒水設備且排油煙管及煙罩設簡易自動滅火裝置者不受限（附表註二）"),
    (r"電信機|電腦室|總機室|伺服器", 200, 7, "二氧化碳或惰性氣體、鹵化烴或乾粉", "得設置預動式自動撒水設備（附表註四）"),
    (r"停車|汽車修", "parking", 3, "水霧、泡沫、二氧化碳或惰性氣體、鹵化烴或乾粉", "得設置自動撒水設備（附表註四）"),
    (r"飛機修理|機庫", 200, 2, "泡沫或乾粉", None),
]


def special_suppression(floor: Floor, eq: list[Equipment], ctx: Context, grid=None):
    if not eq:
        return [], []                                  # 本層沒有任何消防設備（多半不是消防設備圖）
    from litian.review.required import _level
    kind, lv = _level(floor.label or "")
    findings = []
    for room in floor.rooms:
        names = " ".join(room.labels)
        for pat, th, item, systems, alt in S18:
            if not re.search(pat, names):
                continue
            if th == "parking":
                th = 500 if lv == 1 else (300 if kind == "roof" else 200)
            if room.area < th:
                break
            zone = room.polygon.buffer(0.3)
            inside = [e for e in eq if zone.covers(Point(e.x, e.y))]
            if any("special_suppression" in e.kinds for e in inside):
                break
            has_spk = any("sprinkler" in e.kinds for e in inside)
            has_simple = any("simple_suppression" in e.kinds for e in inside)
            if (item == 3 and has_spk) or (item == 6 and has_spk and has_simple):
                break
            sev = ORANGE if (item == 7 and has_spk) else _sev_for([room], RED)
            findings.append(Finding(
                "S18", sev, "未設置", floor.label or "", f"{room.name}（{_fmt(room.area)} ㎡）應選設{systems}滅火設備，圖上未見",
                f"依第 18 條附表第 {item} 項，此類場所樓地板面積達 {th} ㎡ 以上應選擇設置{systems}滅火設備"
                + (f"；{alt}" if alt else "") + ("；本室設有撒水頭，若為預動式得替代，請確認" if sev == ORANGE and item == 7 else ""),
                f"於 {room.name} 選設{systems}滅火設備並附設計計算；或依附表註記採替代方式並於圖上註明",
                ["D0120029/18/1"], rooms=[room.name], area=room.area, geom=room.polygon, metrics={"item": item}))
            break
    notes = []
    if ctx.occupancy == "甲-5":
        kitchens = [r for r in floor.rooms if re.search(r"廚房", " ".join(r.labels))]
        for r in kitchens:
            if not any("simple_suppression" in e.kinds or "special_suppression" in e.kinds
                       for e in eq if r.polygon.buffer(0.3).covers(Point(e.x, e.y))):
                findings.append(Finding(
                    "S18-2", ORANGE, "需確認", floor.label or "", f"{r.name} 未見簡易自動滅火設備",
                    "樓地板面積 300 ㎡ 以上之餐廳，其廚房排油煙管及煙罩應設簡易自動滅火設備（已依第 18 條第 1 項設滅火設備者得免設）",
                    "於排油煙管及煙罩設簡易自動滅火設備；若餐廳樓地板面積未達 300 ㎡ 請註明", ["D0120029/18/2"],
                    rooms=[r.name], area=r.area, geom=r.polygon))
    return findings, notes


RULES = [
    ("SPK-46", "撒水頭水平距離", sprinkler_distance),
    ("HYD-34", "室內消防栓水平距離", hydrant_distance),
    ("SPKR-133", "揚聲器水平距離", speaker_distance),
    ("EXT-31-3", "滅火器步行距離", extinguisher_walk),
    ("EXT-31-1", "滅火效能值", extinguisher_count),
    ("EXT-31-2", "電氣設備處所滅火器", extinguisher_electrical),
    ("DET-120", "探測器數量", detector_count),
    ("S18", "第 18 條特殊滅火設備", special_suppression),
]
