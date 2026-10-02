"""避難照明規則（緊急照明、出口標示燈、避難方向指示燈）依竣工圖實測修正的回歸測試。
全部用程式畫的線（公尺，scale=1）；門＝門扇＋開門弧（門圖層），門的位置用圖形中心。"""

import math

from shapely.geometry import Point

from litian.plan import floor as F
from litian.review import checks as K
from litian.review import equipment as E
from litian.review import escape as ESC


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def door(p0, p1, n, k=12):
    """門洞 p0→p1，鉸鏈在 p0，門扇開向法線 n 那一側。回傳（門圖層的線, 圖形中心）。"""
    w = math.dist(p0, p1)
    a0, a1 = math.atan2(n[1], n[0]), math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    arc = [(p0[0] + w * math.cos(a0 + da * i / k), p0[1] + w * math.sin(a0 + da * i / k)) for i in range(k + 1)]
    xs, ys = [p0[0]] + [x for x, _ in arc], [p0[1]] + [y for _, y in arc]
    return [[p0, arc[0]], arc], ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def build(title, walls, doors=(), texts=(), points=(), columns=()):
    """doors：door() 的結果；points：只有位置、沒有圖形的門（畫在連續的牆線上）。"""
    layers = {"WALL": walls, "DOOR": [ln for ls, _ in doors for ln in ls], "COLUMN": list(columns)}
    return F.analyze(layers, [{"t": t, "x": x, "y": y} for t, x, y in texts], scale=1.0, title=title,
                     doors=[c for _, c in doors] + list(points))


def eq(legend, x, y, **spec):
    return E.Equipment("", legend, legend, E.kinds_of(legend), x, y, "F", {**E.specs(legend, {}), **spec})


def exit_sign(x, y, **kw):
    return eq("出口標示燈", x, y, **{"grade": "C", **kw})


def lamp(x, y):
    return eq("緊急照明燈（吸頂式）", x, y)


# ── 平面：30 m × 10 m 作業場，底邊外牆一扇門；右側樓梯與儲藏室；左上角辦公室的門貼近外牆（室內門） ──

def hall(title, *, in_wall=False):
    walls = [[(5, 0), (30, 0), (30, 10), (0, 10), (0, 0), (4, 0)],          # 外牆，底邊 x 4～5 是門洞
             [(20, 0), (20, 2)], [(20, 3), (20, 6)], [(20, 7), (20, 10)], [(20, 5), (30, 5)],
             [(0, 6), (4, 6), (4, 8.6)], [(4, 9.4), (4, 10)]]
    doors = [door((4, 0), (5, 0), (0, 1)),                    # 通往戶外（往內開）
             door((20, 3), (20, 2), (-1, 0)),                 # 儲藏室
             door((20, 7), (20, 6), (-1, 0)),                 # 樓梯
             door((4, 9.4), (4, 8.6), (1, 0))]                # 辦公室：離外框 1 m 內，但四周有牆
    texts = [("作業場", 10, 4), ("儲藏室", 25, 2.5), ("樓梯", 25, 7.5), ("辦公室", 2, 8)]
    return build(title, walls, doors, texts, points=[(12, 0)] if in_wall else [])


EXT_DOOR, STAIR_DOOR = (4.5, 0.5), (19.5, 6.5)


def test_door_to_outside_classification():
    fl = hall("壹層平面圖", in_wall=True)
    side = {(round(x, 1), round(y, 1)): ESC._door_to_outside(fl, (x, y)) for x, y in fl.exterior_doors()}
    assert side == {EXT_DOOR: "open", (4.4, 9.0): "inside", (12.0, 0.0): "wall"}


def test_refuge_exterior_door_without_sign_is_red():
    """反例：避難層通往戶外的門（牆上有門洞）真的沒有出口標示燈 → 仍為不符。"""
    fl = hall("壹層平面圖", in_wall=True)
    f, notes = ESC.exit_signs(fl, [exit_sign(19.5, 7.5)], K.Context())
    by = {x.severity: x for x in f}
    assert sorted(x.severity for x in f) == [K.ORANGE, K.RED]
    assert by[K.RED].law == ["D0120029/146-3/1/1"] and by[K.RED].geom.contains(Point(EXT_DOOR))
    assert by[K.ORANGE].geom.contains(Point(12, 0)) and "連續的牆線" in by[K.ORANGE].why    # 大型拉門、鐵捲門：需確認
    assert any("室內門" in n.text for n in notes)                                            # 辦公室門貼近外框，不當出口
    f, _ = ESC.exit_signs(fl, [exit_sign(19.5, 7.5), exit_sign(4.5, 1.0)], K.Context())
    assert [x.severity for x in f] == [K.ORANGE]


def test_exterior_doors_are_not_targets_above_refuge_floor():
    fl = hall("貳層平面圖", in_wall=True)
    f, notes = ESC.exit_signs(fl, [exit_sign(19.5, 7.5)], K.Context())
    assert f == [] and any("不是避難層" in n.text and "1 處" in n.text for n in notes)


def test_stair_door_serving_whole_floor_is_red():
    f, _ = ESC.exit_signs(hall("貳層平面圖"), [exit_sign(4.5, 1.0)], K.Context())
    assert [(x.severity, x.law) for x in f] == [(K.RED, ["D0120029/146-3/1/2"])] and f[0].geom.contains(Point(STAIR_DOOR))


def isolated_stair(title):
    """樓梯只通一間儲藏室（其餘是作業場，彼此不通）：經此樓梯門避難的範圍很小。"""
    walls = [rect(0, 0, 30, 10), [(20, 0), (20, 10)], [(20, 5), (24, 5)], [(25, 5), (30, 5)]]
    texts = [("作業場", 10, 5), ("儲藏室", 27, 2.5), ("樓梯", 25, 7.5)]
    return build(title, walls, [door((25, 5), (24, 5), (0, -1))], texts)


def test_stair_door_of_small_room_may_be_exempt_except_underground():
    f, _ = ESC.exit_signs(isolated_stair("貳層平面圖"), [exit_sign(10, 5)], K.Context())
    assert [x.severity for x in f] == [K.ORANGE] and "D0120029/146/1/1/1" in f[0].law and "易於觀察識別" in f[0].why
    f, _ = ESC.exit_signs(isolated_stair("地下一層平面圖"), [exit_sign(10, 5)], K.Context())   # 地下層不適用免設
    assert [x.severity for x in f] == [K.RED]
    f, _ = ESC.exit_signs(isolated_stair("貳層平面圖"), [exit_sign(10, 5)], K.Context(no_opening=["2F"]))
    assert [x.severity for x in f] == [K.RED]


def test_huge_room_named_stair_is_not_a_stair():
    """標示「樓梯」卻有 300 ㎡（樓梯沒圍成獨立房間）：它的門不當作通往直通樓梯之出入口。"""
    def plan(w):
        walls = [rect(0, 0, 20 + w, 15), [(20, 0), (20, 7)], [(20, 8), (20, 15)]]
        return build("貳層平面圖", walls, [door((20, 8), (20, 7), (-1, 0))], [("作業場", 10, 5), ("樓梯", 20 + w / 2, 5)])
    assert ESC.exit_signs(plan(20), [exit_sign(2, 2)], K.Context()) == ([], [])
    f, _ = ESC.exit_signs(plan(10), [exit_sign(2, 2)], K.Context())               # 150 ㎡ 仍當樓梯
    assert [x.severity for x in f] == [K.RED]


# ── 屋突層：外框是整片屋頂；樓地板只有梯間、電氣室 ──

def roof(door_out=False):
    stair = [[(12, 8), (10, 8), (10, 14), (16, 14), (16, 8), (13, 8)]] if door_out else [rect(10, 8, 16, 14)]
    walls = [rect(0, 0, 30, 20)] + stair + [[(16, 8), (19, 8), (19, 14), (16, 14)], rect(22, 8, 28, 14)]
    doors = [door((12, 8), (13, 8), (0, -1))] if door_out else []
    return build("屋突一層平面圖", walls, doors, [("梯間", 13, 11), ("電氣室", 25, 11)])


def test_roof_stair_door_to_roof_needs_confirmation_only():
    f, _ = ESC.exit_signs(roof(door_out=True), [exit_sign(25, 11)], K.Context())
    assert [x.severity for x in f] == [K.ORANGE] and "沒有樓地板" in f[0].why


def test_emergency_lights_on_roof_skip_roof_pieces_flag_landing_keep_real_rooms():
    fl = roof()
    assert {r.kind for r in fl.rooms if not fl.in_region(r)} == {"unknown"}
    f, notes = ESC.emergency_lights(fl, [lamp(13, 11)], K.Context())
    got = {(x.severity, x.rooms[0]) for x in f}
    assert (K.RED, "電氣室") in got                                    # 反例：屋突層有名稱的房間沒有燈仍報
    landing = [x for x in f if x.severity == K.YELLOW]                 # 緊鄰梯間的未命名小間：可能是梯廳、平台
    assert len(landing) == 1 and landing[0].geom.covers(Point(17.5, 11)) and landing[0].missing
    assert len(f) == 2                                                 # 屋頂（大片未命名範圍）不報
    assert any("不在本層樓地板範圍內" in n.text for n in notes)


# ── 緊急照明：燈的歸屬、衣帽間、名稱衝突的免設處所、避難層 30 m ──

def rooms_2f():
    walls = [rect(0, 0, 20, 10), [(10, 0), (10, 10)], [(10, 4), (20, 4)], [(14, 0), (14, 4)]]
    column = [rect(16.55, 1.55, 17.45, 2.45), [(16.55, 1.55), (17.45, 2.45)], [(16.55, 2.45), (17.45, 1.55)]]
    texts = [("辦公室", 5, 5), ("衣帽間", 12, 2), ("會議室", 15, 1), ("茶水間", 15, 7)]
    return build("貳層平面圖", walls, texts=texts, columns=column)


def test_light_ownership_wardrobe_and_habitable_room():
    fl = rooms_2f()
    assert fl.room_at(17, 2) is None                                   # 柱內（不是房間）
    lights = [lamp(9.6, 2), lamp(17, 2)]     # 辦公室內離衣帽間 0.4 m；會議室的燈圖形中心壓在柱上（離房間 0.48 m）
    f, _ = ESC.emergency_lights(fl, lights, K.Context())
    by = {x.rooms[0]: x for x in f}
    assert set(by) == {"衣帽間", "茶水間"}                              # 會議室算有燈；衣帽間不能拿隔壁房間的燈
    assert by["衣帽間"].severity == K.ORANGE and "兩種讀法" in by["衣帽間"].why and "不視為居室" in by["衣帽間"].why
    assert by["茶水間"].severity == K.RED                               # 反例：居室真的沒有緊急照明 → 不符


def test_conflict_room_with_only_exempt_names_is_exempt():
    walls = [rect(0, 0, 20, 10), [(10, 0), (10, 10)]]
    fl = build("貳層平面圖", walls, texts=[("客貨梯", 3, 5), ("機械室", 7, 5), ("辦公室", 13, 5), ("儲藏室", 17, 5)])
    assert {r.kind for r in fl.rooms} == {"mixed", "room"}
    f, notes = ESC.emergency_lights(fl, [lamp(25, 25)], K.Context())
    assert [(x.severity, x.rooms) for x in f] == [(K.RED, ["辦公室／儲藏室"])]   # 混了辦公室就不能整間免設
    assert any(n.law == ["D0120029/179/1/6"] and "客貨梯／機械室" in n.text for n in notes)


def test_refuge_exemption_ignores_interior_door_near_outline():
    """避難層 30 m 免設：只量到真正通往屋外的門；辦公室的門只是貼近外框，不能當屋外出口。"""
    walls = [[(46, 0), (50, 0), (50, 10), (0, 10), (0, 0), (45, 0)],
             [(0, 6), (4, 6), (4, 8.6)], [(4, 9.4), (4, 10)]]
    doors = [door((45, 0), (46, 0), (0, 1)), door((4, 9.4), (4, 8.6), (1, 0))]
    fl = build("壹層平面圖", walls, doors, [("作業場", 25, 5), ("辦公室", 2, 8)])
    f, notes = ESC.emergency_lights(fl, [lamp(25, 5)], K.Context())
    assert [(x.severity, x.rooms) for x in f] == [(K.RED, ["辦公室"])]


# ── 避難方向指示燈：出口標示燈有效範圍依解讀併入 ──

def corridor():
    return build("貳層平面圖", [rect(0, 0, 30, 2)], texts=[("走廊", 15, 1)])


def test_exit_sign_range_counts_for_direction_only_as_interpretation():
    fl = corridor()
    items = [eq("避難方向指示燈（單面單向）", 1, 1, grade="C"), exit_sign(29, 1)]
    f, _ = ESC.direction_lights(fl, items, K.Context())
    interp = [x for x in f if x.metrics.get("exit_sign_counted")]
    reds = [x for x in f if x.severity == K.RED]
    assert len(interp) == 1 and interp[0].severity == K.ORANGE and "依解讀" in interp[0].why
    assert "避難方向指示燈有效範圍" in interp[0].why and interp[0].geom.bounds[0] > 12.5   # 出口標示燈 C 級 15 m
    assert len(reds) == 1 and reds[0].geom.bounds[2] < 14.5            # 兩種燈都到不了的一段仍為不符
    f, _ = ESC.direction_lights(fl, items, K.Context(policy={"exit_sign_counts_for_direction": False}))
    assert not any(x.metrics.get("exit_sign_counted") for x in f)
    assert any(x.severity == K.RED and x.geom.bounds[2] > 29 for x in f)


def test_exit_sign_reach_by_grade_and_arrow():
    assert ESC._exit_reach(True)(exit_sign(0, 0, grade="B", arrow=True)) == 20.0
    assert ESC._exit_reach(True)(exit_sign(0, 0, grade="A")) == 60.0
    assert ESC._exit_reach(False)(exit_sign(0, 0, grade="A")) == 40.0          # 有無方向符號未標示：嚴格取有
    unknown = eq("出口標示燈", 0, 0)
    assert ESC._exit_reach(True)(unknown) == 60.0 and ESC._exit_reach(False)(unknown) == 15.0
