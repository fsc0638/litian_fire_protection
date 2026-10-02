"""平面理解：一張平面圖 → 樓層外框、房間（範圍、名稱、種類、面積）、可走區域。

做法（全部用 shapely，座標先換成公尺）：
1. 牆、柱、門、窗圖層的線各加粗 3 cm 後聯集。門圖塊的門扇＋開門弧會把門洞封起來，
   所以聯集圖形裡被圍住的「洞」就是一間間房間。
2. 樓層外框＝聯集圖形做「閉運算」（先外擴 3 m 再內縮 3 m，把 6 m 以下的缺口補起來）後的最大外輪廓。
3. 房間名稱＝落在洞裡的文字；依關鍵字判斷種類（廁所、樓梯、管道間、走廊、機電室…），
   檢核時用來套免設規定。一個洞裡出現互相衝突的種類（門沒畫、兩間連在一起）就標記待確認。
4. 樓地板範圍＝外框扣掉挑空、室外平台（挑空內的獨立柱、隔線一併扣掉；挑空裡圍成房間的樓梯核等補回）。
5. 可走區域＝樓地板扣掉牆、柱、窗（門不算障礙；牆線畫穿門洞時，門圖塊範圍內的牆線切開），算步行距離用。

圖層判斷用名稱規則（各事務所命名不同，可依案件覆寫）；判斷不了的不硬猜，回報在 warnings。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

SEAL = 0.03          # 牆線加粗半徑（m）：封住 6 cm 以下的接縫
CLOSE_R = 3.0        # 外框閉運算半徑（m）：補起 6 m 以下的外牆缺口（大門、鐵捲門）
WALK_R = 0.12        # 算步行距離時牆的加粗半徑（m）：讓細線在格點上也擋得住
MIN_ROOM = 0.5       # 小於這個面積（㎡）的洞不算房間（柱內空隙）
CAVITY = 0.2         # 寬度不到 2×20 cm 的洞是雙線牆的夾縫，不算房間
DOOR_SHARE = 0.5     # 邊界一半以上貼著門圖塊的洞是門扇與開門弧圍出的扇形，不算房間
STRIP_W = 0.6        # 平均寬度（2×面積／周長）不到 60 cm 的長條（周長 > STRIP_LEN）是牆縫、女兒牆夾縫、天溝，不算房間
STRIP_LEN = 10.0
NAME_TOL = 0.3       # 房名文字壓在牆線上（落在房間外 30 cm 內）時，分給最近的未命名房間
BIG_CONFLICT = 200.0  # 標示有樓梯、廁所等卻大於這個面積（㎡）：樓梯沒圍成獨立房間，整片當一般房間（需確認）
DOOR_GAP_MAX = 6.0   # 門圖塊（含拉門、雙開門）最長邊；超過的不當門洞切牆

UNIT_SCALE = {"mm": 0.001, "cm": 0.01, "m": 1.0, "公分": 0.01, "公釐": 0.001, "公尺": 1.0}
INSUNITS = {4: 0.001, 5: 0.01, 6: 1.0}


@dataclass
class LayerProfile:
    """圖層名稱 → 角色。預設值是常見命名；個案可覆寫。"""
    wall: str = r"WALL|牆|隔間|庫板|^RC$|^C-ST$"
    column: str = r"COLUMN|柱"
    door: str = r"DOOR|DOR|門"
    window: str = r"WINDOW|^WIN|窗"
    exclude: str = r"TXT|TEXT|DIM|ANNO|HATCH|標註|-NO\d*$"       # 「WALL-NO」等是隔間編號標籤，不是牆

    def role(self, layer: str) -> str | None:
        layer = layer.split("$0$")[-1]          # 綁定外部參考後的圖層名稱帶「參考名$0$」前綴
        if re.search(self.exclude, layer, re.I):
            return None
        for name in ("wall", "column", "door", "window"):
            if re.search(getattr(self, name), layer, re.I):
                return name
        return None


# 房間種類：依序比對，先中先贏（「電梯」要排在「梯」前面）
ROOM_KINDS = [
    ("void", r"挑空|挑高|天井|中庭|開口部"),
    ("outdoor", r"屋頂平[台臺]|陽[台臺]|露[台臺]|平[台臺]$"),
    ("machine", r"(電梯|昇降機|升降機)(機房|機械室)"),          # 電梯機房是機械室，不是昇降機道
    ("corridor", r"電梯廳|電梯前室|[昇升]降機間"),                 # 電梯廳、前室是有人停留的通道
    ("elevator", r"電梯|客梯|貨梯|客貨梯|昇降機道|升降機道|昇降路"),
    ("stair", r"安全梯|直通樓梯|樓梯|梯間|[A-Z]梯"),
    ("toilet", r"廁|洗手間|浴室|盥洗"),
    ("shaft", r"管道間|^PS$|^DS$"),
    ("electrical", r"電氣室|變電室|配電室|發電機|電信室|電腦室|受電室"),
    ("machine", r"機械室|機房|泵浦|幫浦|空調|水箱|水池"),
    ("corridor", r"走廊|走道|通道|門廳|梯廳|穿堂|玄關"),
    ("kitchen", r"廚房|鍋爐"),
    ("room", r"室|廳|房|間|區|場|庫|廠|店|部|中心|辦公"),
]
FIXTURE = re.compile(r"WASH|^T0\d\d|馬桶|小便|洗手|面盆|TOILET|廁", re.I)


def is_fixture(name: str, layer: str) -> bool:
    """衛生器具圖塊（洗手台、馬桶）：沒有房名的小房間裡有這些 → 推定為廁所。"""
    return bool(FIXTURE.search(name.split("$0$")[-1]) or FIXTURE.search(layer.split("$0$")[-1]))


# 不是房間名稱的註記（防火、尺寸、材料等）
NOT_NAME = re.compile(r"PIT|^\d+(\.\d+)?T$|荷重|F\d+A|防火|遮煙|阻熱|投影|伸縮縫|護欄|坡度|天溝|H=|OH:|㎡|cm|mm|\d{2,}")


def unit_scale(meta: dict | None, insunits: int | None) -> float | None:
    """圖面單位 → 公尺的倍率。先看圖框的「單位」欄，再看 DXF 標頭；都沒有回 None。"""
    for k, v in (meta or {}).items():
        if "單位" in k or k.upper() in ("UNIT", "UNITS"):
            s = str(v).strip().lower()
            if s in UNIT_SCALE:
                return UNIT_SCALE[s]
    return INSUNITS.get(insunits or 0)


_NUM = {"一": 1, "壹": 1, "二": 2, "貳": 2, "兩": 2, "三": 3, "參": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
        "六": 6, "陸": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
_CN = "0-9一二三四五六七八九十壹貳參叁肆伍陸柒捌玖拾"


def _cn_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if not s:
        return None
    if s[0] in "十拾":
        return 10 + (_NUM.get(s[1], 0) if len(s) > 1 else 0)
    if len(s) >= 2 and s[1] in "十拾":
        return _NUM.get(s[0], 0) * 10 + (_NUM.get(s[2], 0) if len(s) > 2 else 0)
    return _NUM.get(s) if len(s) == 1 else None


NOT_PLAN = re.compile(r"配置|位置|天花|裝修|筏基|基礎|基地|景觀|植栽|地籍|昇位|升位|系統圖|剖面|立面|詳圖|大樣|水力|計算|索引|數量|涵蓋|檢討表|門窗")


def floor_label(title: str) -> str | None:
    """圖名 → 樓層代號（1F、1MF、B2、R1F、RF）。不是平面圖或認不出樓層回 None。
    消防圖常寫「一層消防排煙系統」「一層室內栓、火警設備平面圖」：有樓層且是消防設備配置的也算平面圖。"""
    t = (title or "").replace(" ", "")
    if NOT_PLAN.search(t) or not ("平面" in t or re.search(r"消防|設備|系統", t)):
        return None
    if "屋頂層" in t or re.search(r"(?<![A-Za-z0-9])RF(層|樓)", t):
        return "RF"
    m = re.search(r"(?<![A-Za-z0-9])R(\d+)F(層|樓)", t)
    if m:
        return f"R{int(m.group(1))}F"
    m = re.search(rf"屋突([{_CN}]+)層", t)
    if m and (n := _cn_int(m.group(1))):
        return f"R{n}F"
    m = re.search(rf"地下([{_CN}]+)(層|樓)", t)
    if m and (n := _cn_int(m.group(1))):
        return f"B{n}"
    m = re.search(rf"([{_CN}]+)(層|樓)(夾層)?", t)
    if m and (n := _cn_int(m.group(1))):
        return f"{n}MF" if m.group(3) else f"{n}F"
    return None


def room_kind(labels: list[str]) -> tuple[str, bool]:
    """回傳（種類, 是否有衝突）。衝突＝同一範圍出現兩種以上非一般房間種類，或特殊種類與一般房間混在一起。"""
    kinds = []
    for s in labels:
        for k, pat in ROOM_KINDS:
            if re.search(pat, s):
                kinds.append(k)
                break
    special = {k for k in kinds if k != "room"}
    if not kinds:
        return "unknown", False
    for k in ("void", "outdoor"):              # 「(挑空)」等註記優先：下層的房名會透過挑空出現在這層
        if k in special:
            return k, False
    if special == {"machine", "electrical"}:   # 「機械室（電氣室1）」＝較具體的電氣室
        return "electrical", False
    if len(special) > 1:
        return "mixed", True
    if special and "room" in kinds:
        return special.pop(), True          # 例：「機械室」＋「辦公室」連在一起
    return (special.pop() if special else "room"), False


@dataclass
class Room:
    id: int
    polygon: Polygon
    labels: list[str]
    kind: str
    conflict: bool
    hole: Polygon | None = None         # 未扣掉內部島（柱、樓梯核）的原始範圍

    @property
    def name(self) -> str:
        """顯示用名稱：優先取能判斷種類的標示（房名），去重後最多兩個；大空間常有一堆區域、設備註記。"""
        if not self.labels:
            return "（未標示）"
        uniq = list(dict.fromkeys(self.labels))
        named = [s for s in uniq if any(re.search(p, s) for _, p in ROOM_KINDS)]
        pick = (named or uniq)[:2]
        rest = len(uniq) - len(pick)
        return "／".join(pick) + (f" 等 {len(uniq)} 個標示" if rest > 0 else "")

    @property
    def area(self) -> float:
        return self.polygon.area


@dataclass
class Floor:
    label: str | None
    title: str
    scale: float
    outline: Polygon
    rooms: list[Room]
    region: shapely.Geometry            # 樓地板範圍＝外框扣掉挑空、室外平台（屋突層只取有名稱的室內房間）
    walls: shapely.Geometry             # 牆＋柱＋窗（門不算），已加粗 WALK_R
    walkable: shapely.Geometry
    fireproof: bool | None              # 圖上註記「防火構造」→ True；查無 → None（未知）
    warnings: list[str] = field(default_factory=list)
    doors: list[tuple[float, float]] = field(default_factory=list)   # 門圖塊插入點（公尺）
    _share: dict = field(default_factory=dict, repr=False)

    def exterior_doors(self, tol: float = 1.5) -> list[tuple[float, float]]:
        """通往戶外的門：貼著樓層外框。"""
        ring = self.outline.exterior
        return [d for d in self.doors if ring.distance(Point(d)) <= tol]

    def stair_doors(self, tol: float = 1.5) -> list[tuple[float, float]]:
        """通往樓梯的門：貼著樓梯間的邊界。"""
        stairs = [r.polygon for r in self.rooms if r.kind == "stair" and not r.conflict]
        return [d for d in self.doors if any(s.exterior.distance(Point(d)) <= tol for s in stairs)]

    @property
    def area(self) -> float:
        return self.region.area

    def room_at(self, x: float, y: float) -> Room | None:
        p = Point(x, y)
        return next((r for r in self.rooms if r.polygon.covers(p)), None)

    def room_near(self, x: float, y: float, tol: float = 0.3) -> Room | None:
        """設備所在的房間：落在房間外 tol 內也算（插入點常壓在牆線、門弧或標籤缺口上）。"""
        r = self.room_at(x, y)
        if r is not None or tol <= 0:
            return r
        p = Point(x, y)
        d, r = min(((r.polygon.distance(p), r) for r in self.rooms), default=(None, None), key=lambda t: t[0] or 0)
        return r if d is not None and d <= tol else None

    def in_region(self, room: Room, share: float = 0.5) -> bool:
        """房間是否算樓地板（逐房規則只檢核這些）：屋突層未標示的範圍（屋頂、天溝）等不在樓地板內。"""
        if room.id not in self._share:
            a = room.polygon.area
            self._share[room.id] = room.polygon.intersection(self.region).area / a if a > 0 else 0.0
        return self._share[room.id] >= share


def _lines(polys, s: float):
    return [LineString([(x * s, y * s) for x, y in pts]) for pts in polys if len(pts) >= 2]


def _filled(p: Polygon) -> Polygon:
    return Polygon(p.exterior)


def _parts(g) -> list[Polygon]:
    if g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]


def is_room_name(s: str) -> bool:
    s = s.strip()
    return bool(re.search(r"[一-鿿]", s)) and len(s) <= 20 and not NOT_NAME.search(s)


def _door_gaps(door_polys, scale: float):
    """門圖層的每一群線（門扇＋開門弧）的外接矩形：牆線畫穿門洞時，算步行距離要在這裡切開。"""
    if not door_polys:
        return None
    dz = unary_union(shapely.buffer(_lines(door_polys, scale), 0.05))
    gaps = []
    for g in _parts(dz):
        rect = g.minimum_rotated_rectangle
        if not isinstance(rect, Polygon):
            continue
        xs = list(rect.exterior.coords)
        sides = [Point(xs[i]).distance(Point(xs[i + 1])) for i in range(min(4, len(xs) - 1))]
        if sides and max(sides) <= DOOR_GAP_MAX:
            gaps.append(rect.buffer(WALK_R + 0.05, join_style="mitre"))
    return unary_union(gaps) if gaps else None


def analyze(layers: dict[str, list], texts: list[dict], *, scale: float, title: str = "",
            profile: LayerProfile | None = None, doors: list[tuple[float, float]] = (),
            fixtures: list[tuple[float, float]] = ()) -> Floor:
    """layers：geometry.by_bbox 的輸出（圖面單位）；texts：IR 文字（圖面單位）；scale：圖面單位 → 公尺；
    doors：門圖塊插入點（圖面單位）；fixtures：衛生器具圖塊插入點（圖面單位，推定未命名的廁所）。"""
    profile = profile or LayerProfile()
    warnings: list[str] = []
    roles: dict[str, list] = {"wall": [], "column": [], "door": [], "window": []}
    for layer, polys in layers.items():
        r = profile.role(layer)
        if r:
            roles[r].extend(polys)
    if not roles["wall"]:
        raise ValueError("找不到牆圖層（圖層名稱對不上預設規則，需設定圖層對應）")
    if not roles["door"]:
        warnings.append("找不到門圖層：房間會和走廊連成一片")

    enclose = _lines(roles["wall"] + roles["column"] + roles["door"] + roles["window"], scale)
    door_zone = unary_union(shapely.buffer(_lines(roles["door"], scale), 2 * SEAL)) if roles["door"] else None
    U = unary_union(shapely.buffer(enclose, SEAL, cap_style="square", join_style="mitre"))
    parts = sorted(_parts(U), key=lambda p: -_filled(p).area)
    if not parts:
        raise ValueError("牆線無法圍出任何範圍")

    closed = U.buffer(CLOSE_R, join_style="mitre").buffer(-CLOSE_R, join_style="mitre")
    outline0 = max((_filled(p) for p in _parts(closed)), key=lambda p: p.area)

    # 房間：外框附近每個聯集部分的洞；洞裡若有另一個部分（獨立的柱、隔間），扣掉它的外輪廓
    zone = outline0.buffer(CLOSE_R)
    inner = [p for p in parts if zone.contains(p.representative_point())]
    islands = [_filled(p) for p in inner]
    name_pts = [(Point(t["x"] * scale, t["y"] * scale), t["t"].strip()) for t in texts if is_room_name(t["t"])]
    rooms: list[Room] = []
    for p in inner:
        for ring in p.interiors:
            hole = Polygon(ring)
            if hole.area < MIN_ROOM or hole.buffer(-CAVITY).is_empty:
                continue
            if door_zone is not None and hole.exterior.intersection(door_zone).length >= DOOR_SHARE * hole.exterior.length:
                continue
            inside = [isl for isl in islands if isl.area < hole.area and hole.contains(isl.representative_point())]
            poly = hole.difference(unary_union(inside)) if inside else hole
            if poly.area < MIN_ROOM or poly.buffer(-CAVITY).is_empty:      # 扣掉內部後只剩牆縫（例：外牆雙線之間）
                continue
            if poly.length > STRIP_LEN and 2 * poly.area / poly.length < STRIP_W:
                continue                                                  # 細長條：牆縫、女兒牆夾縫、天溝
            labels = [s for pt, s in name_pts if poly.covers(pt)]
            kind, conflict = room_kind(labels)
            rooms.append(Room(0, poly, labels, kind, conflict, hole))
    # 房名文字壓在牆線上（插入點落在房間外一點點）：分給最近的未命名房間
    loose = [(pt, s) for pt, s in name_pts if not any(r.polygon.covers(pt) for r in rooms)]
    for pt, s in loose:
        cands = [(r.polygon.distance(pt), i) for i, r in enumerate(rooms) if not r.labels]
        if cands and (best := min(cands))[0] <= NAME_TOL:
            r = rooms[best[1]]
            r.labels = [s]
            r.kind, r.conflict = room_kind(r.labels)
    # 沒有房名但有洗手台、馬桶 → 推定為廁所
    fx = [Point(x * scale, y * scale) for x, y in fixtures]
    guessed = []
    for r in rooms:
        if not r.labels and r.area < 30 and any(r.polygon.covers(p) for p in fx):
            r.labels = ["廁所（依衛生器具推定）"]
            r.kind, r.conflict = room_kind(r.labels)
            guessed.append(r)
    for r in rooms:
        if r.conflict and r.kind in ("stair", "toilet", "elevator", "shaft") and r.area > BIG_CONFLICT:
            r.kind = "room"                       # 大空間裡只是寫了樓梯名（樓梯沒圍成獨立房間）：仍要檢核，標待確認
    rooms.sort(key=lambda r: -r.area)
    for i, r in enumerate(rooms, 1):
        r.id = i
    # 外框輪廓若在某處只以一點相接，會「凹」進房間把整間排除；圍得出來的房間一定是樓地板，併回外框
    merged = unary_union([outline0] + [r.polygon.buffer(2 * SEAL, join_style="mitre") for r in rooms])
    outline = max((_filled(p) for p in _parts(merged)), key=lambda p: p.area)
    conflicts = [r.name for r in rooms if r.conflict]
    if conflicts:
        warnings.append(f"{len(conflicts)} 個範圍內有不同種類的房間名稱（可能門沒畫、房間連在一起）：" + "、".join(conflicts[:5]))
    if guessed:
        warnings.append(f"{len(guessed)} 個未命名的小房間內有洗手台、馬桶，推定為廁所：" + "、".join(f"#{r.id}" for r in guessed[:5]))
    stair_re = dict(ROOM_KINDS)["stair"]
    void_stairs = sorted({s for r in rooms if r.kind == "void" for s in r.labels if re.search(stair_re, s)})
    if void_stairs:
        warnings.append("挑空範圍內有樓梯標示（樓梯可能沒有圍成獨立房間，未檢核）：" + "、".join(void_stairs[:5]))

    label = floor_label(title)
    if label and label.startswith("R"):
        # 屋突層的外框是整片屋頂；樓地板只有有名稱的室內房間（機械室、樓梯、電梯等）
        kept = [r.polygon for r in rooms if r.kind not in ("void", "outdoor", "unknown")]
        region = unary_union(kept) if kept else Polygon()
        warnings.append("屋突層只檢核有名稱的室內房間；未標示名稱的範圍視為屋頂")
    else:
        # 挑空連同裡面的獨立柱、隔線整片扣掉；挑空裡圍成有名稱房間的島（樓梯核等）補回
        voids = [r.hole or r.polygon for r in rooms if r.kind in ("void", "outdoor")]
        region = outline
        if voids:
            vfill = unary_union(voids)
            named = [r for r in rooms if r.kind not in ("void", "outdoor", "unknown")]
            cores = [_filled(p) for p in inner if vfill.contains(p.representative_point())
                     and any(_filled(p).contains(r.polygon.representative_point()) for r in named)]
            region = outline.difference(vfill)
            if cores:
                region = region.union(unary_union(cores))
    region = unary_union([g for g in _parts(region) if g.area >= MIN_ROOM]) if not region.is_empty else region
    unknown_big = [r for r in rooms if r.kind == "unknown" and r.area >= 100]
    if unknown_big:
        warnings.append(f"{len(unknown_big)} 個 100 ㎡ 以上的範圍沒有房間名稱（可能是挑空，請確認）："
                        + "、".join(f"#{r.id}（{r.area:.0f} ㎡）" for r in unknown_big[:5]))
    walls = unary_union(shapely.buffer(_lines(roles["wall"] + roles["column"] + roles["window"], scale),
                                       WALK_R, cap_style="square", join_style="mitre"))
    gaps = _door_gaps(roles["door"], scale)
    if gaps is not None:
        walls = walls.difference(gaps)                 # 牆線畫穿門洞（庫板、拉門）時切開
    walkable = region.difference(walls)
    fireproof = True if any("防火構造" in t["t"] for t in texts) else None
    return Floor(label, title, scale, outline, rooms, region, walls, walkable, fireproof, warnings,
                 [(x * scale, y * scale) for x, y in doors])
