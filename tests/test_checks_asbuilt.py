"""檢核規則的修正（大型竣工圖交叉檢查後）：水平距離不量牆厚帶、管道間／樓梯間／樓梯核的解讀、揚聲器小房間但書實算、
邊際超出、滅火器逐房與屋外連接、非居室、探測器歸房容差、屋頂碎塊、火焰式、上層投影。
全部用程式畫的線（公尺，scale=1）；每個放寬都配一個「真的違規仍要報」的反例。"""

import math

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from litian.plan import floor as F
from litian.review import checks as K
from litian.review import coverage as C
from litian.review import equipment as E


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def T(t, x, y):
    return {"t": t, "x": x, "y": y}


def eq(legend, x, y, **spec):
    return E.Equipment("", legend, legend, E.kinds_of(legend), x, y, "F", {**E.specs(legend, {}), **spec})


def door(hx, hy, ang, w=1.0, n=12):
    a = math.radians(ang)
    arc = [(hx + w * math.cos(a + math.pi / 2 * i / n), hy + w * math.sin(a + math.pi / 2 * i / n)) for i in range(n + 1)]
    return [[(hx, hy), (hx + w * math.cos(a), hy + w * math.sin(a))], arc]


def floor_of(rooms, label="1F"):
    """直接用房間多邊形組一層（樓地板＝房間聯集），省去畫牆。rooms：[(多邊形, [房名], 種類)]。"""
    rs = [F.Room(i, p, labels, kind, False) for i, (p, labels, kind) in enumerate(rooms, 1)]
    region = unary_union([r.polygon for r in rs])
    return F.Floor(label, "", 1.0, region.envelope, rs, region, box(0, 0, 0, 0), region, True)


def notes_of(notes, rule):
    return " ".join(n.text for n in notes if n.rule == rule)


HYD, SPKR = "室內消防栓", "揚聲器（嵌頂式）"


# ── 水平距離：量測範圍 ─────────────────────────────────────────────────────

def test_wall_band_is_not_measured_but_rooms_are():
    """外牆 0.5 m 厚：牆厚帶離消防栓超過 25 m 不是缺失；房間內部都在 25 m 內就沒有缺失。"""
    fl = F.analyze({"WALL": [rect(0, 0, 50, 10), rect(0.5, 0.5, 49.5, 9.5)]}, [T("倉庫", 25, 5)], scale=1.0, title="一層平面圖")
    pts = [(25, 5)]
    assert C.uncovered(fl.region, pts, 25.0)                        # 整片樓地板量：牆厚帶會冒出來
    assert K.hydrant_distance(fl, [eq(HYD, *pts[0])], K.Context()) == ([], [])


def test_open_floor_not_enclosed_as_room_is_still_checked():
    """外牆有 4 m 開口、沒圍成房間的大空間：仍要檢核（不能因為不是房間就漏掉），標需確認並說明。"""
    walls = [rect(0, 0, 20, 20), [(20, 0), (60, 0), (60, 8)], [(60, 12), (60, 20), (20, 20)]]
    fl = F.analyze({"WALL": walls}, [T("辦公室", 10, 10)], scale=1.0, title="一層平面圖")
    assert [r.name for r in fl.rooms] == ["辦公室"]
    f, _ = K.hydrant_distance(fl, [eq(HYD, 10, 10)], K.Context())
    assert len(f) == 1 and f[0].severity == K.ORANGE and f[0].rooms == [] and "沒有圍成房間" in f[0].why
    assert f[0].area > 100 and f[0].geom.bounds[2] > 50


def long_plan(right_label, split=48, length=52, small=None):
    """左邊大房間、右邊一間（樓梯、管道間…）；small＝在右邊再隔出的小房間 (x0, y0, x1, y1, 房名)。"""
    walls = [rect(0, 0, length, 10), [(split, 0), (split, 10)]]
    texts = [T("辦公室", 10, 5), T(right_label, (split + length) / 2, 8)]
    if small:
        x0, y0, x1, y1, name = small
        walls.append(rect(x0, y0, x1, y1))
        texts.append(T(name, (x0 + x1) / 2, (y0 + y1) / 2))
    return F.analyze({"WALL": walls}, texts, scale=1.0, title="一層平面圖")


def test_shaft_beyond_reach_is_note_by_default_and_finding_when_policy_says_so():
    fl = long_plan("管道間")
    h = [eq(HYD, 24, 5)]
    f, notes = K.hydrant_distance(fl, h, K.Context())
    assert f == [] and "管道間" in notes_of(notes, "HYD-34") and "另一種讀法" in notes_of(notes, "HYD-34")
    f, _ = K.hydrant_distance(fl, h, K.Context(policy={"shaft_in_coverage": True}))
    assert [(x.severity, x.rooms) for x in f] == [(K.RED, ["管道間"])]
    sp = [eq(SPKR, x, 5) for x in (5, 15, 25, 35, 40)]                 # 辦公室都在 10 m 內，管道間不在
    f, notes = K.speaker_distance(fl, sp, K.Context())
    assert f == [] and "管道間" in notes_of(notes, "SPKR-133")


def test_speaker_stair_follows_vertical_rule_and_policy_can_switch_back():
    fl = long_plan("樓梯", split=30, length=40)
    sp = [eq(SPKR, x, 5) for x in (5, 15, 25)]
    f, notes = K.speaker_distance(fl, sp, K.Context())
    assert f == [] and "SPKR-133-5" in notes_of(notes, "SPKR-133") and notes[0].law == ["D0120029/133/1/2/5"]
    f, _ = K.speaker_distance(fl, sp, K.Context(policy={"stair_speaker_vertical": False}))
    assert [(x.severity, x.rooms) for x in f] == [(K.RED, ["樓梯"])]


def test_hydrant_stair_core_is_orange_with_both_readings_but_other_rooms_stay_red():
    """樓梯間與緊鄰的小儲藏室（< 10 ㎡）超出 25 m → 需確認（兩種讀法）；同樣超出的一般房間仍是不符。"""
    walls = [rect(0, 0, 56, 10), [(40, 0), (40, 10)], [(50, 0), (50, 10)], rect(50, 0, 53, 3)]
    texts = [T("辦公室", 20, 5), T("直通樓梯", 45, 5), T("儲藏室", 51.5, 1.5), T("機房", 53, 6)]
    fl = F.analyze({"WALL": walls}, texts, scale=1.0, title="二層平面圖")
    f, _ = K.hydrant_distance(fl, [eq(HYD, 20, 5)], K.Context())
    by = {x.rooms[0]: x for x in f}
    assert by["直通樓梯"].severity == K.ORANGE and "實務" in by["直通樓梯"].why and "嚴格" in by["直通樓梯"].why
    assert by["儲藏室"].severity == K.ORANGE
    assert by["機房"].severity == K.RED                                  # 21 ㎡：不是附屬小房間


# ── 揚聲器：小房間但書實算、邊際超出 ───────────────────────────────────────

def test_small_room_proviso_needs_speaker_within_8_m():
    room = [(box(0, 0, 6, 1), ["茶水間"], "room")]                       # 6 ㎡ 居室
    f, notes = K.speaker_distance(floor_of(room), [eq(SPKR, 14, 0.5)], K.Context())
    assert f == [] and "但書" in notes_of(notes, "SPKR-133") and "8 m" in notes_of(notes, "SPKR-133")
    f, notes = K.speaker_distance(floor_of(room), [eq(SPKR, 15, 0.5)], K.Context())   # 9 m：不符但書
    assert [x.severity for x in f] == [K.RED] and not notes


def test_small_room_proviso_area_limit_by_room_type():
    store = [(box(0, 0, 4, 5), ["儲藏室"], "room")]                      # 20 ㎡ 非居室（上限 30 ㎡）
    f, _ = K.speaker_distance(floor_of(store), [eq(SPKR, 12, 2.5)], K.Context())
    assert f == []
    office = [(box(0, 0, 4, 5), ["辦公室"], "room")]                     # 20 ㎡ 居室（上限 6 ㎡）
    f, _ = K.speaker_distance(floor_of(office), [eq(SPKR, 12, 2.5)], K.Context())
    assert [x.severity for x in f] == [K.RED]


def test_marginal_overshoot_is_kept_red_and_labelled():
    """四顆揚聲器排成 11 × 17.3 m：中間留下最遠約 10.25 m 的小空隙——真的超出，嚴重度不變，標邊際超出。"""
    fl = floor_of([(box(0, 0, 11, 17.3), ["廠房"], "room")])
    sp = [eq(SPKR, x, y) for x in (0, 11) for y in (0, 17.3)]
    f, _ = K.speaker_distance(fl, sp, K.Context())
    assert len(f) == 1 and f[0].severity == K.RED and "邊際超出" in f[0].title
    assert f[0].area < 1 and f[0].metrics["farthest"] == pytest.approx(10.25, abs=0.05)
    assert "0.3 m" in f[0].fix and "第 133 條第 3 款" in f[0].fix and "D0120029/133/1/3" in f[0].law
    big = floor_of([(box(0, 0, 12, 18), ["廠房"], "room")])        # 最遠 10.8 m：不是邊際
    f, _ = K.speaker_distance(big, [eq(SPKR, x, y) for x in (0, 12) for y in (0, 18)], K.Context())
    assert f and all("邊際" not in x.title for x in f) and f[0].severity == K.RED


# ── 滅火器步行距離 ─────────────────────────────────────────────────────────

def stair_plan(title, main="廠房"):
    """主房間與樓梯間；樓梯間唯一的門開向屋外（南牆 x 26～27），主房間的門在南牆 x 23～24。"""
    walls = [[(0, 0), (23, 0)], [(24, 0), (26, 0)], [(27, 0), (30, 0)], [(30, 0), (30, 10), (0, 10), (0, 0)], [(25, 0), (25, 10)]]
    doors = door(26, 0, 0) + door(23, 0, 0)
    return F.analyze({"WALL": walls, "DOOR": doors}, [T(main, 12, 5), T("樓梯", 28, 6)], scale=1.0, title=title)


def test_stair_reachable_only_from_outside_on_ground_floor():
    """避難層：只能從屋外進出的樓梯間經屋外接到滅火器（不再是走不進去）；居室真的超過 20 m 仍報。"""
    fl = stair_plan("一層平面圖")
    ext = [eq("乾粉滅火器", 22, 1), eq("乾粉滅火器", 5, 5)]
    assert K.extinguisher_walk(fl, ext, K.Context()) == ([], [])
    assert K.extinguisher_walk(fl, ext, K.Context(policy={"habitable_only_extinguisher": False})) == ([], [])  # 樓梯經屋外 20 m 內
    f, _ = K.extinguisher_walk(fl, ext[:1], K.Context())                 # 拿掉一具：主房間西側超過 20 m
    assert {x.rooms[0] for x in f} == {"廠房"} and K.RED in {x.severity for x in f}
    # 屋外路徑只補室內走不到的範圍：主房間的距離不會因為屋外繞行而變短
    assert not any(x.metrics.get("via_outdoor") for x in f)


def test_stair_reachable_only_from_outside_on_upper_floor_is_reference():
    fl = stair_plan("二層平面圖")
    ext = [eq("乾粉滅火器", 22, 1), eq("乾粉滅火器", 5, 5)]
    f, notes = K.extinguisher_walk(fl, ext, K.Context())
    assert f == [] and "樓梯" in notes_of(notes, "EXT-31-3") and "走不進去" in notes_of(notes, "EXT-31-3")
    f, _ = K.extinguisher_walk(fl, ext, K.Context(policy={"habitable_only_extinguisher": False}))
    assert [(x.severity, x.rooms) for x in f] == [(K.YELLOW, ["樓梯"])] and "樓梯間" in f[0].why


def test_unreachable_habitable_room_is_still_reported_per_room():
    """門沒畫的居室：每間各列一條、各自的範圍（不合併成跨整層的大框）；非居室只列說明。"""
    walls = [rect(0, 0, 60, 10), [(20, 0), (20, 10)], [(40, 0), (40, 10)], rect(50, 6, 54, 9.5)]
    texts = [T("辦公室", 30, 5), T("會議室", 10, 5), T("檔案室", 45, 3), T("儲藏室", 52, 8)]
    fl = F.analyze({"WALL": walls}, texts, scale=1.0, title="二層平面圖")
    f, notes = K.extinguisher_walk(fl, [eq("乾粉滅火器", 30, 5)], K.Context())
    got = {x.rooms[0]: x for x in f}
    assert set(got) == {"會議室", "檔案室"} and all(x.severity == K.YELLOW for x in f)
    assert got["會議室"].geom.bounds[2] <= 20.1 and got["檔案室"].geom.bounds[0] >= 39.9
    assert "儲藏室" in notes_of(notes, "EXT-31-3")


def test_wall_gaps_are_not_unreachable_rooms():
    """房間裡 0.5 m 寬的雙線牆夾縫、不到 1 ㎡ 的小封閉格：牆縫，不列走不進去。"""
    walls = [rect(0, 0, 20, 10), rect(3, 3, 10, 3.5), rect(14, 5, 14.9, 5.9)]
    fl = F.analyze({"WALL": walls}, [T("辦公室", 5, 7)], scale=1.0, title="二層平面圖")
    assert K.extinguisher_walk(fl, [eq("乾粉滅火器", 10, 6)], K.Context()) == ([], [])


def test_non_habitable_rooms():
    room = lambda labels, kind: F.Room(1, box(0, 0, 1, 1), labels, kind, False)  # noqa: E731
    assert K._non_habitable(room(["儲藏室"], "room")) and K._non_habitable(room(["女廁"], "toilet"))
    assert not K._non_habitable(room(["倉庫"], "room")) and not K._non_habitable(room(["機械室"], "machine"))
    assert K._non_habitable(room(["儲藏室", "儲藏櫃", "儲藏室"], "room"))
    for labels in (["辦公室", "儲藏室"], ["辦公室", "儲藏櫃"], ["倉庫", "儲藏室"], ["生產區", "成品儲藏區"], ["儲藏櫃"]):
        assert not K._non_habitable(room(labels, F.room_kind(labels)[0])), labels


def floor_named(rooms, label="1F"):
    """同 floor_of，但種類與名稱衝突由房名判讀（F.room_kind）。rooms：[(多邊形, [房名])]。"""
    rs = [F.Room(i, p, labels, *F.room_kind(labels)) for i, (p, labels) in enumerate(rooms, 1)]
    region = unary_union([r.polygon for r in rs])
    return F.Floor(label, "", 1.0, region.envelope, rs, region, box(0, 0, 0, 0), region, True)


@pytest.mark.parametrize("labels, size", [
    (["辦公室", "儲藏室"], (40, 5)),                       # 門沒畫、兩間連成一間
    (["辦公室", "儲藏櫃"], (40, 5)),                       # 家具註記
    (["倉庫", "儲藏室"], (40, 5)),
    (["生產區", "成品儲藏區", "包裝區"], (60, 20)),         # 大廠房裡的分區標示
])
def test_room_with_storage_among_several_labels_is_still_habitable(labels, size):
    """房間有多個標示、只有其中一個是儲藏：不能當儲藏室把步行距離的不符藏成說明。"""
    fl = floor_named([(box(0, 0, *size), labels)], label="2F")
    f, notes = K.extinguisher_walk(fl, [eq("乾粉滅火器", 0.5, 0.5)], K.Context())
    assert K.RED in {x.severity for x in f} and not notes_of(notes, "EXT-31-3")
    assert max(x.metrics["max_walk"] for x in f) > 40


def test_speaker_proviso_not_for_multi_label_or_conflicting_rooms():
    """但書面積上限：多標示的居室仍是 6 ㎡；名稱衝突的房間（男廁＋辦公室）以 6 ㎡ 計，照列缺失（需確認）。"""
    office = floor_named([(box(0, 0, 4, 5), ["辦公室", "儲藏櫃"])])               # 20 ㎡、揚聲器在 8 m 處
    f, notes = K.speaker_distance(office, [eq(SPKR, 12, 2.5)], K.Context())
    assert [x.severity for x in f] == [K.RED] and not notes
    mixed = floor_named([(box(0, 0, 5, 5), ["男廁", "辦公室"])])                  # 25 ㎡、kind toilet、衝突
    assert mixed.rooms[0].kind == "toilet" and mixed.rooms[0].conflict
    f, notes = K.speaker_distance(mixed, [eq(SPKR, 13, 2.5)], K.Context())
    assert [x.severity for x in f] == [K.ORANGE] and not notes
    store = floor_named([(box(0, 0, 4, 5), ["儲藏室", "儲藏櫃"])])                 # 真的儲藏室：30 ㎡ 上限、但書成立
    f, notes = K.speaker_distance(store, [eq(SPKR, 12, 2.5)], K.Context())
    assert f == [] and "但書" in notes_of(notes, "SPKR-133")


# ── 電梯廳、電梯前室、電梯機房：房名分類歸為 elevator，但不是昇降機道 ─────────────────

def test_hoistway_vs_elevator_lobby():
    room = lambda labels: F.Room(1, box(0, 0, 1, 1), labels, *F.room_kind(labels))  # noqa: E731
    for labels in (["客梯"], ["3T客貨梯", "(無障礙電梯)"], ["昇降機道"], ["管道間"], ["管道間(水)", "人孔"]):
        assert K._hoistway(room(labels)), labels
    for labels in (["電梯廳"], ["電梯前室"], ["電梯間"], ["電梯機房"], ["辦公室"], ["管道間", "辦公室"]):
        assert not K._hoistway(room(labels)), labels
    assert K._sprinkler_exempt(room(["客梯"]), True) == "D0120029/49/1/3"
    assert K._sprinkler_exempt(room(["電梯機房"]), True) == "D0120029/49/1/4"
    assert K._sprinkler_exempt(room(["電梯廳"]), True) is None


@pytest.mark.parametrize("name", ["電梯廳", "電梯前室"])
def test_elevator_lobby_beyond_reach_is_reported_not_noted(name):
    """電梯廳是有人停留的樓地板：超出消防栓 25 m、揚聲器 10 m 仍列不符，不能當昇降機道改成說明。"""
    fl = long_plan(name)
    assert next(r for r in fl.rooms if r.name == name).kind == "corridor"         # 房間種類表把電梯廳、前室歸通道
    f, notes = K.hydrant_distance(fl, [eq(HYD, 24, 5)], K.Context())
    assert [(x.severity, x.rooms) for x in f] == [(K.RED, [name])] and not notes_of(notes, "HYD-34")
    f, notes = K.speaker_distance(fl, [eq(SPKR, x, 5) for x in (5, 15, 25, 35, 40)], K.Context())
    assert [(x.severity, x.rooms) for x in f] == [(K.RED, [name])] and not notes_of(notes, "SPKR-133")
    ext = [eq("乾粉滅火器", 12, 5), eq("乾粉滅火器", 36, 5)]
    f, _ = K.extinguisher_walk(fl, ext, K.Context())                           # 門沒畫：走不進去照列
    assert [(x.severity, x.rooms) for x in f] == [(K.YELLOW, [name])]
    hoist = long_plan("客梯")                                                   # 對照：昇降機道仍是說明、不檢核步行距離
    f, notes = K.hydrant_distance(hoist, [eq(HYD, 24, 5)], K.Context())
    assert f == [] and "昇降機道" in notes_of(notes, "HYD-34")
    assert K.extinguisher_walk(hoist, ext, K.Context()) == ([], [])


# ── 滅火器步行距離：逐房子遮罩（記憶體）──────────────────────────────────────

def test_sub_mask_polygons_match_whole_grid_version():
    grid = C.WalkGrid(box(0, 0, 12, 8))
    rng = __import__("numpy").random.default_rng(7)
    full = grid.free & (rng.random(grid.free.shape) < 0.5)
    sl = (slice(5, 31), slice(9, 47))
    full[:5], full[31:], full[:, :9], full[:, 47:] = False, False, False, False
    got = unary_union(K._sub_polygons(grid, sl, full[sl]))
    want = unary_union(grid.cells_to_polygons(full))
    assert got.symmetric_difference(want).area < 1e-9 and want.area > 1


def test_extinguisher_walk_memory_does_not_scale_with_room_count():
    """384 間房、24 萬格：逐房只存外接矩形那塊遮罩，規則本身的記憶體峰值在數十 MB 內（原本每間房一張整層遮罩約 180 MB）。"""
    import tracemalloc
    rooms = [(box(i * 5, j * 5, i * 5 + 5, j * 5 + 5), ["辦公室"], "room") for i in range(24) for j in range(16)]
    fl = floor_of(rooms, label="2F")
    grid = C.WalkGrid(fl.walkable)
    ext = [eq("乾粉滅火器", x + 0.5, y + 0.5) for x in range(0, 120, 32) for y in range(0, 80, 32)]
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        f, _ = K.extinguisher_walk(fl, ext, K.Context(), grid=grid)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert peak < 60 * 2**20
    assert K.RED in {x.severity for x in f} and all(len(x.rooms) == 1 for x in f)


def test_warehouse_is_habitable_and_storage_within_tolerance_is_note():
    fl = stair_plan("二層平面圖", main="倉庫")
    f, _ = K.extinguisher_walk(fl, [eq("乾粉滅火器", 22, 1)], K.Context())
    assert {x.rooms[0] for x in f} == {"倉庫"} and K.RED in {x.severity for x in f}     # 倉庫是居室：照報
    fl = floor_of([(box(0, 0, 20.4, 1), ["儲藏室"], "room")], label="2F")
    f, notes = K.extinguisher_walk(fl, [eq("乾粉滅火器", 0.1, 0.5)], K.Context())
    assert f == [] and "誤差內" in notes_of(notes, "EXT-31-3")


# ── 探測器 ──────────────────────────────────────────────────────────────────

def two_rooms(title="一層平面圖", right="會議室"):
    walls = [rect(0, 0, 30, 10), [(14.9, 0), (14.9, 10)], [(15.1, 0), (15.1, 10)]]
    return F.analyze({"WALL": walls}, [T("辦公室", 7, 5), T(right, 22, 5)], scale=1.0, title=title)


HEAT = "差動式局限型探測器（1種）"
CTX = K.Context(ceiling_height={"1F": 3.5, "R1F": 3.5}, fireproof=True)


def test_detector_on_wall_line_counts_for_nearest_room_once():
    """探測器壓在牆線上（房間外 0.1 m）：歸給最近的房間，只算一次。"""
    fl = two_rooms()
    left = [eq(HEAT, 4, 5), eq(HEAT, 10, 5)]                            # 辦公室 ~146 ㎡ ÷ 90 → 2 個
    assert K.detector_count(fl, left + [eq(HEAT, 15.08, 5), eq(HEAT, 25, 5)], CTX) == ([], [])
    f, _ = K.detector_count(fl, left + [eq(HEAT, 25, 5)], CTX)          # 真的少一個：照報
    assert [(x.severity, x.category, x.rooms) for x in f] == [(K.RED, "數量不足", ["會議室"])]


def test_room_without_detector_is_reported_but_roof_fragment_is_not():
    fl = two_rooms()
    f, _ = K.detector_count(fl, [eq(HEAT, 4, 5), eq(HEAT, 10, 5)], CTX)
    assert [(x.severity, x.category, x.rooms) for x in f] == [(K.RED, "未設置", ["會議室"])]
    roof = F.analyze({"WALL": [rect(0, 0, 30, 10), [(14.9, 0), (14.9, 10)], [(15.1, 0), (15.1, 10)]]},
                     [T("機械室", 7, 5)], scale=1.0, title="屋突一層平面圖")    # 右半未標示＝屋頂
    assert not roof.in_region(next(r for r in roof.rooms if not r.labels))
    assert K.detector_count(roof, [eq(HEAT, 4, 5), eq(HEAT, 10, 5)], CTX) == ([], [])


def test_flame_detector_room_and_data_request():
    fl = two_rooms(right="卸貨區")
    left = [eq(HEAT, 4, 5), eq(HEAT, 10, 5)]
    flames = [eq("火焰式探測器", 16, 1), eq("火焰式探測器", 29, 9)]
    f, _ = K.detector_count(fl, left + flames, CTX)
    assert [(x.rule, x.severity) for x in f] == [("DET-124", K.BLUE)]
    assert f[0].law == ["D0120029/124/1/2"] and "監視距離" in f[0].title and f[0].rooms == ["卸貨區"]
    f, _ = K.detector_count(fl, left + flames[:1] + [eq(HEAT, 25, 5)], CTX)   # 火焰式＋局限型不足：需確認
    det = [x for x in f if x.rule == "DET-120"]
    assert [(x.severity, x.category) for x in det] == [(K.ORANGE, "數量不足")] and "火焰式" in det[0].why
    f, _ = K.detector_count(fl, flames[:1], CTX)                             # 辦公室什麼都沒有：照報
    assert ("DET-120", K.RED, ["辦公室"]) in [(x.rule, x.severity, x.rooms) for x in f]


def test_projected_detectors_count_and_are_named():
    fl = two_rooms()
    up = lambda x, y: eq(HEAT, x, y, projected_from="F-301")  # noqa: E731
    f, notes = K.detector_count(fl, [eq(HEAT, 4, 5), eq(HEAT, 10, 5), up(20, 5), up(26, 5)], CTX)
    assert f == [] and "F-301" in notes_of(notes, "DET-120")
    f, _ = K.detector_count(fl, [eq(HEAT, 4, 5), eq(HEAT, 10, 5), up(20, 5)], CTX)
    assert [x.rooms for x in f] == [["會議室"]] and "含上層挑空範圍內的探測器，圖號 F-301" in f[0].why


def test_elevator_lobby_without_detector_is_reported():
    left = [eq(HEAT, 4, 5), eq(HEAT, 10, 5)]
    f, _ = K.detector_count(two_rooms(right="電梯廳"), left, CTX)
    assert [(x.severity, x.category, x.rooms) for x in f] == [(K.RED, "未設置", ["電梯廳"])]
    assert K.detector_count(two_rooms(right="客梯"), left, CTX) == ([], [])    # 昇降機道依第 122 條第 7 款另計
