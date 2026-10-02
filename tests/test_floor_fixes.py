"""平面理解的修正（大型竣工圖實測後）：挑空裡的柱與樓梯核、牆線畫穿門洞、房名壓在牆線上、衛生器具推定廁所、
隔間編號標籤圖層、牆縫細長條。全部用程式畫的線（公尺，scale=1）。"""

import math

import pytest
from shapely.geometry import Point

from litian.plan import floor as F


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def T(t, x, y):
    return {"t": t, "x": x, "y": y}


def test_void_removes_columns_inside_and_keeps_named_core():
    """挑空內的獨立柱不能變成一塊塊「樓地板」；挑空裡圍成樓梯間的樓梯核要留在樓地板內。"""
    layers = {"WALL": [rect(0, 0, 40, 20), [(20, 0), (20, 20)], rect(28, 13, 32, 17)],
              "COLUMN": [rect(25, 9.7, 25.6, 10.3), rect(35, 9.7, 35.6, 10.3)]}
    texts = [T("辦公室", 10, 10), T("(挑空)", 30, 5), T("樓梯", 30, 15)]
    fl = F.analyze(layers, texts, scale=1.0, title="二層平面圖")
    kinds = {r.name: r.kind for r in fl.rooms}
    assert kinds == {"辦公室": "room", "(挑空)": "void", "樓梯": "stair"}
    assert fl.region.distance(Point(25.3, 10)) > 1 and fl.region.distance(Point(35.3, 10)) > 1   # 柱不在樓地板
    assert fl.region.covers(Point(30, 15))                                                 # 樓梯核補回
    assert fl.area == pytest.approx(20 * 20 + 4 * 4, rel=0.02)


def test_wall_line_drawn_through_door_is_cut_for_walking():
    """庫板牆線連續畫過拉門、門洞：算步行距離時在門圖塊範圍切開，兩間房走得通。"""
    arc = [(10 + 0.9 * math.cos(a / 10 * math.pi / 2), 4 + 0.9 * math.sin(a / 10 * math.pi / 2)) for a in range(11)]
    layers = {"WALL": [rect(0, 0, 20, 10), [(10, 0), (10, 10)]], "DOOR": [[(10, 4), (10.9, 4)], arc]}
    fl = F.analyze(layers, [T("倉庫", 5, 5), T("辦公室", 15, 5)], scale=1.0, title="一層平面圖")
    assert sorted(r.name for r in fl.rooms) == ["倉庫", "辦公室"]                       # 房間切分不受影響
    assert len(F._parts(fl.walkable)) == 1


def test_room_name_on_wall_line_and_fixture_toilet():
    """「女廁」文字壓在牆線上（不在任何房間內）→ 分給最近的未命名房間；沒房名但有洗手台 → 推定廁所。"""
    layers = {"WALL": [rect(0, 0, 12, 6), [(4, 0), (4, 6)], [(8, 0), (8, 6)], [(0, 3), (4, 3)]]}
    texts = [T("辦公室", 10, 3), T("女廁", 2, 2.99), T("走廊", 6, 3)]          # 女廁文字壓在 y=3 的牆線上
    fl = F.analyze(layers, texts, scale=1.0, title="一層平面圖", fixtures=[(2, 4.5)])
    by = {r.name: r for r in fl.rooms}
    assert by["女廁"].kind == "toilet" and by["女廁"].polygon.covers(Point(2, 1))
    guessed = [r for r in fl.rooms if "依衛生器具推定" in r.name]
    assert len(guessed) == 1 and guessed[0].kind == "toilet" and guessed[0].polygon.covers(Point(2, 4.5))
    assert any("推定為廁所" in w for w in fl.warnings)
    assert F.is_fixture("Area_2F$0$WASH26", "Area_2F$0$toilet") and not F.is_fixture("D1", "door")


def test_room_near_and_in_region():
    layers = {"WALL": [rect(0, 0, 10, 10), [(5, 0), (5, 10)]]}
    fl = F.analyze(layers, [T("辦公室", 2, 5), T("會議室", 8, 5)], scale=1.0, title="一層平面圖")
    assert fl.room_near(5.0 + 0.2, 5).name == "會議室"                 # 壓在牆線上（離房間 0.2 m 內）
    assert fl.room_near(-1, 5) is None
    assert all(fl.in_region(r) for r in fl.rooms)


def test_label_tag_layer_is_not_wall_and_thin_strip_is_not_room():
    prof = F.LayerProfile()
    assert prof.role("Area_2F$0$WALL-NO") is None and prof.role("Area_2F$0$WALL-NO2") is None
    assert prof.role("Area_2F$0$WALL") == "wall"
    # 外牆雙線之間 0.5 m 寬、20 m 長的夾縫：不是房間
    layers = {"WALL": [rect(0, 0, 20, 10), rect(0, 10, 20, 10.5)]}
    fl = F.analyze(layers, [T("倉庫", 10, 5)], scale=1.0, title="一層平面圖")
    assert [r.name for r in fl.rooms] == ["倉庫"]


def test_big_hall_with_stair_label_is_still_checked():
    """樓梯沒圍成獨立房間、樓梯名寫在大廠房裡：不能整片當樓梯跳過檢核，改當一般房間（待確認）。"""
    layers = {"WALL": [rect(0, 0, 30, 20)]}
    fl = F.analyze(layers, [T("作業廠房", 10, 10), T("(安全梯)", 25, 15)], scale=1.0, title="一層平面圖")
    r = fl.rooms[0]
    assert r.kind == "room" and r.conflict
