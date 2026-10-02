"""平面理解（認房間）與逐項檢核（距離涵蓋、數量）。全部用程式畫的簡單平面，不含真實圖說。"""

import json
import math
import re
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import Point, box

from litian.plan import floor as F
from litian.plan import geometry as G
from litian.review import checks as K
from litian.review import coverage as C
from litian.review import engine as EN
from litian.review import equipment as E
from litian.review import render as R

from . import _plans as P

LEGEND = E.load_legend()
DICT = E.Dictionary(LEGEND)


def plan(extra_texts=(), extra_walls=()):
    layers = P.layers()
    layers["WALL"] = layers["WALL"] + [list(w) for w in extra_walls]
    return F.analyze(layers, P.texts() + [{"t": t, "x": x, "y": y} for t, x, y in extra_texts], scale=1.0, title="壹層平面圖")


def eq(legend, x, y, **spec):
    base = E.specs(legend, {})
    return E.Equipment("", legend, legend, E.kinds_of(legend), x, y, "F", {**base, **spec})


def room(fl, name):
    return next(r for r in fl.rooms if name in r.labels)


def by_rule(findings, rule):
    return [f for f in findings if f.rule == rule]


# ── 平面理解 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,label", [
    ("壹層平面圖", "1F"), ("壹層夾層平面圖", "1MF"), ("地下二層平面圖", "B2"), ("屋突壹層平面圖", "R1F"),
    ("屋頂層平面圖", "RF"), ("十一層消防設備平面圖", "11F"), ("3樓平面圖", "3F"), ("二十三層平面圖", "23F"),
    ("全區配置圖", None), ("天花板平面圖", None), ("筏基平面圖", None), ("立面圖", None),
])
def test_floor_label(title, label):
    assert F.floor_label(title) == label


def test_unit_scale():
    assert F.unit_scale({"單位": "cm"}, 0) == 0.01
    assert F.unit_scale({"單位": "MM"}, 5) == 0.001          # 圖框欄位優先
    assert F.unit_scale({}, 4) == 0.001 and F.unit_scale({}, 0) is None


@pytest.mark.parametrize("labels,kind,conflict", [
    (["女廁"], "toilet", False), (["客貨梯"], "elevator", False), (["(E梯_直通樓梯)"], "stair", False),
    (["機械室", "(電氣室2)"], "electrical", False), (["作業廠房C1", "(挑空)"], "void", False),
    (["機械室", "(發電機室)", "(D梯_安全梯)"], "mixed", True), (["配電盤", "辦公室"], "room", False),
    (["屋頂平台"], "outdoor", False), ([], "unknown", False),
])
def test_room_kind(labels, kind, conflict):
    assert F.room_kind(labels) == (kind, conflict)


def test_analyze_finds_rooms_names_and_areas():
    fl = plan()
    assert fl.label == "1F" and fl.fireproof is True and not fl.warnings
    got = {r.name: (r.kind, r.area) for r in fl.rooms}
    assert set(got) == {"辦公室", "會議室", "男廁"}                      # 門扇扇形、牆縫、格線都不算房間
    assert got["辦公室"][0] == "room" and got["男廁"][0] == "toilet"
    assert got["辦公室"][1] == pytest.approx(14.7 * 14.6, rel=0.01)
    assert got["男廁"][1] == pytest.approx(14.7 * 4.7, rel=0.01)
    assert fl.area == pytest.approx(30 * 15, rel=0.01)
    assert fl.walkable.area < fl.area and fl.walkable.contains(box(15, 6.2, 15.05, 6.8))   # 門洞可走


def test_analyze_same_result_in_centimetres():
    a = {r.name: round(r.area, 1) for r in plan().rooms}
    fl = F.analyze(P.layers(0.01), P.texts(0.01), scale=0.01, title="壹層平面圖")
    assert {r.name: round(r.area, 1) for r in fl.rooms} == a


def test_void_room_excluded_from_floor_region():
    fl = plan(extra_texts=[("(挑空)", 22, 4)])
    assert room(fl, "會議室").kind == "void"
    assert fl.area == pytest.approx(30 * 15 - 14.7 * 9.7, rel=0.02)


def test_analyze_requires_wall_layer():
    with pytest.raises(ValueError, match="牆圖層"):
        F.analyze({"A-OTHER": [[(0, 0), (1, 1)]]}, [], scale=1.0)


def test_explode_inherits_insert_layer_and_flattens_arcs():
    doc = ezdxf.new("R2018")
    blk = doc.blocks.new("DOOR1")
    blk.add_line((0, 0), (100, 0))                                  # 0 層 → 沿用插入圖層
    blk.add_arc((0, 0), 100, 0, 90)
    blk.add_line((0, 0), (0, 10), dxfattribs={"layer": "FRAME"})    # 自有圖層保留
    outer = doc.blocks.new("NEST")
    outer.add_blockref("DOOR1", (0, 0))
    doc.modelspace().add_blockref("NEST", (1000, 0), dxfattribs={"layer": "A-DOOR"})
    prims = G.explode(doc)
    by = G.by_bbox(prims, None)
    assert len(by["A-DOOR"]) == 2 and len(by["FRAME"]) == 1
    arc = max(by["A-DOOR"], key=len)
    assert len(arc) >= 5 and all(math.hypot(x - 1000, y) == pytest.approx(100, abs=2.5) for x, y in arc)
    assert G.by_bbox(prims, [0, -10, 500, 500]) == {}


# ── 距離計算 ─────────────────────────────────────────────────────────────

def test_walk_grid_error_within_tolerance():
    import numpy as np
    g = C.WalkGrid(box(0, 0, 40, 30), 0.2)
    d = g.distances([(1, 1)])
    true = np.hypot(g.gx - 1, g.gy - 1)
    m = g.free & (true > 3)
    assert ((d[m] - true[m]) / true[m]).max() < C.WALK_TOL
    L = box(0, 0, 30, 2).union(box(28, 0, 30, 30))                 # L 型走廊：要繞內角
    g2 = C.WalkGrid(L, 0.2)
    d2 = g2.distances([(1, 1)])
    exact = math.dist((1, 1), (28, 2)) + math.dist((28, 2), (29, 29))
    r, c = int((29 - g2.y0) / 0.2), int((29 - g2.x0) / 0.2)
    assert d2[r, c] == pytest.approx(exact, rel=C.WALK_TOL)


def test_walk_grid_unreachable_is_inf_and_wall_mounted_points_snap():
    import numpy as np
    two = box(0, 0, 10, 10).union(box(20, 0, 30, 10))
    g = C.WalkGrid(two, 0.5)
    d = g.distances([(10.3, 5)])                                    # 掛在牆上（區域外 0.3 m）→ 吸附到最近可走格
    assert np.isfinite(d[g.region_cells(box(0, 0, 10, 10))]).all()
    assert np.isinf(d[g.region_cells(box(20, 0, 30, 10))]).all()


def test_uncovered_drops_slivers():
    assert C.uncovered(box(0, 0, 10, 10), [(5, 5)], 7.5) == []          # 圓包住整個方塊
    got = C.uncovered(box(0, 0, 10, 10), [(0, 0)], 10)
    assert len(got) == 1 and got[0].area == pytest.approx(100 - math.pi * 25, rel=0.01)


# ── 設備辨識 ─────────────────────────────────────────────────────────────

def test_dictionary_lookup_and_kinds():
    assert DICT.lookup("密閉式撒水頭(向下型)") == "密閉式撒水頭（向下型）"     # 半形括號
    assert DICT.lookup(" 乾粉滅火器 ") == "乾粉滅火器"
    firm = E.Dictionary(LEGEND, blocks=[(r"^SPK-PEND", "密閉式撒水頭（向下型）")])
    assert firm.lookup("SPK-PEND-15") == "密閉式撒水頭（向下型）" and DICT.lookup("SPK-PEND-15") is None
    assert E.kinds_of("綜合消防栓箱（含連結送水管出水口）") == ("hydrant", "standpipe_outlet")
    assert E.kinds_of("逆止閥") == ()


def test_specs_from_name_and_attributes():
    assert E.specs("差動式局限型探測器（1種）", {}) == {"detector_type": "差動式", "detector_class": "1"}
    assert E.specs("定溫式局限型探測器（特種、防水型）", {})["detector_class"] == "特種"
    assert E.specs("密閉式撒水頭（向下型）", {"型式": "快速反應型"}) == {"response": "quick"}
    assert E.specs("乾粉滅火器", {"效能值": "A-3,B-10,C"}) == {"a_value": 3}


def test_recognize_counts_unknown_and_ignores_non_check_legend():
    ins = [{"name": "乾粉滅火器", "x": 100, "y": 200, "attribs": {}}, {"name": "逆止閥", "x": 0, "y": 0},
           {"name": "XYZ", "x": 0, "y": 0}, {"name": "XYZ", "x": 1, "y": 0}]
    found, unknown = E.recognize(ins, 0.01, DICT)
    assert [(e.legend, e.x, e.y) for e in found] == [("乾粉滅火器", 1.0, 2.0)]
    assert unknown == {"XYZ": 2}


# ── 檢核規則 ─────────────────────────────────────────────────────────────

def grid_pts(poly, step):
    x0, y0, x1, y1 = poly.bounds
    out = []
    y = y0 + step / 2
    while y < y1:
        x = x0 + step / 2
        while x < x1:
            out.append((x, y))
            x += step
        y += step
    return out


def sprinklers(fl, step, response="standard", skip=None):
    pts = []
    for n in ("辦公室", "會議室"):
        pts += [p for p in grid_pts(room(fl, n).polygon, step) if not (skip and skip.contains(Point(p)))]
    spec = {"response": response} if response else {"response": None}
    return [eq("密閉式撒水頭（向下型）", x, y, **spec) for x, y in pts]


def test_sprinkler_full_grid_passes_and_toilet_exempt():
    fl = plan()
    f, notes = K.sprinkler_distance(fl, sprinklers(fl, 3.2), K.Context())
    assert f == [] and "男廁" in notes[0].text and notes[0].law == ["D0120029/49/1/1"]


def test_sprinkler_gap_is_red_with_room_and_law():
    fl = plan()
    f, _ = K.sprinkler_distance(fl, sprinklers(fl, 3.2, skip=box(3, 3, 10, 10)), K.Context())
    assert len(f) == 1 and f[0].severity == K.RED and f[0].rooms == ["辦公室"]
    assert f[0].law == ["D0120029/46/1/3/1"] and f[0].metrics["radius"] == 2.3 and f[0].metrics["add"] >= 1


def test_sprinkler_unknown_response_is_yellow_when_only_strict_fails():
    fl = plan()
    f, _ = K.sprinkler_distance(fl, sprinklers(fl, 3.6, response=None), K.Context())
    assert f and all(x.severity == K.YELLOW for x in f)
    assert any("感度" in m for m in f[0].missing)
    f2, _ = K.sprinkler_distance(fl, sprinklers(fl, 3.6, response="quick"), K.Context())
    assert f2 == []                                               # 快速反應型、防火構造 2.6 m → 3.6 m 方格夠


def test_hydrant_distance():
    fl = plan()
    assert K.hydrant_distance(fl, [eq("室內消防栓", 15, 7.5)], K.Context())[0] == []
    f, _ = K.hydrant_distance(fl, [eq("室內消防栓", 1, 1)], K.Context())
    assert len(f) == 1 and f[0].severity == K.RED and "男廁" in f[0].rooms
    assert f[0].law == ["D0120029/34/1/1/1", "D0120029/34/1/2/1"]


def test_speaker_distance():
    fl = plan()
    f, _ = K.speaker_distance(fl, [eq("揚聲器（嵌頂式）", 7, 7)], K.Context())
    assert f and all(x.severity == K.RED for x in f) and any("會議室" in x.rooms for x in f)
    assert K.speaker_distance(fl, [eq("揚聲器（嵌頂式）", x, y) for x in (5, 15, 25) for y in (4, 11)], K.Context())[0] == []


def test_extinguisher_walking_distance():
    fl = plan()
    assert K.extinguisher_walk(fl, [eq("乾粉滅火器", 15.5, 6.5)], K.Context())[0] == []
    f, _ = K.extinguisher_walk(fl, [eq("乾粉滅火器", 1, 1)], K.Context())
    red = [x for x in f if x.severity == K.RED]
    assert red and "男廁" in red[0].rooms and red[0].metrics["max_walk"] > 20
    assert red[0].law == ["D0120029/31/1/3"]


def test_extinguisher_unreachable_room_is_yellow():
    closed = [[(3, 3), (6, 3), (6, 6), (3, 6), (3, 3)], [(3.2, 3.2), (5.8, 3.2), (5.8, 5.8), (3.2, 5.8), (3.2, 3.2)]]
    fl = plan(extra_texts=[("儲藏室", 4.5, 4.5)], extra_walls=closed)
    f, _ = K.extinguisher_walk(fl, [eq("乾粉滅火器", 15.5, 6.5)], K.Context())
    assert [x.severity for x in f] == [K.YELLOW] and "儲藏室" in f[0].rooms


def test_extinguisher_capacity():
    fl = plan()                                                   # 約 453 ㎡
    one = [eq("乾粉滅火器", 15.5, 6.5, a_value=3)]
    f, _ = K.extinguisher_count(fl, one, K.Context())
    assert f[0].severity == K.YELLOW and any("場所類別" in m for m in f[0].missing)
    assert K.extinguisher_count(fl, one, K.Context(occupancy_group="2-4"))[0] == []     # 每 200 ㎡ → 需 3
    f, _ = K.extinguisher_count(fl, one, K.Context(occupancy_group="1-5"))           # 每 100 ㎡ → 需 5
    assert f[0].severity == K.RED and f[0].law == ["D0120029/31/1/1/1"]
    f, _ = K.extinguisher_count(fl, [eq("乾粉滅火器", 15.5, 6.5)], K.Context(occupancy_group="2-4"))
    assert f[0].severity == K.YELLOW and any("效能值" in m for m in f[0].missing)


def test_extinguisher_for_electrical_room():
    layers = P.layers()
    texts = [t if t["t"] != "會議室" else {**t, "t": "電氣室"} for t in P.texts()]
    fl = F.analyze(layers, texts, scale=1.0, title="壹層平面圖")
    f, _ = K.extinguisher_electrical(fl, [eq("乾粉滅火器", 22, 5)], K.Context())
    assert len(f) == 1 and f[0].metrics == {"need": 2, "have": 1} and f[0].law == ["D0120029/31/1/2"]
    assert K.extinguisher_electrical(fl, [eq("乾粉滅火器", 22, 5), eq("乾粉滅火器", 23, 5)], K.Context())[0] == []


def detectors(n, x0=3, y=7):
    return [eq("差動式局限型探測器（1種）", x0 + 4 * i, y) for i in range(n)]


def test_detector_count_rules():
    fl = plan()
    ctx = K.Context(ceiling_height={"1F": 3.5})
    meet = [eq("差動式局限型探測器（1種）", 22, 5), eq("差動式局限型探測器（1種）", 25, 5)]
    f, notes = K.detector_count(fl, detectors(3) + meet, ctx)          # 辦公室 214 ㎡ ÷ 90 → 3 個
    assert f == [] and "男廁" in notes[0].text
    f, _ = K.detector_count(fl, detectors(2) + meet, ctx)
    assert [(x.severity, x.category, x.metrics["need"]) for x in f] == [(K.RED, "數量不足", [3, 3])]
    f, _ = K.detector_count(fl, detectors(3), ctx)
    assert [(x.severity, x.category, x.rooms) for x in f] == [(K.RED, "未設置", ["會議室"])]


def test_detector_unknown_height_is_yellow():
    fl = plan()
    meet = [eq("差動式局限型探測器（1種）", 22 + i, 5) for i in range(4)]
    f, _ = K.detector_count(fl, detectors(3) + meet, K.Context())      # 未滿 4 m 需 3；4～8 m 需 5
    assert [(x.severity, x.rooms) for x in f] == [(K.YELLOW, ["辦公室"])] and any("天花板" in m for m in f[0].missing)


def test_every_cited_law_node_exists():
    nodes = {json.loads(line)["node_id"] for line in Path("data/lawdb/nodes.jsonl").read_text(encoding="utf-8").splitlines()}
    src = Path(K.__file__).read_text(encoding="utf-8")
    cited = set(re.findall(r"D0120029(?:/[0-9-]+)+", src))
    assert len(cited) >= 15 and cited <= nodes, cited - nodes


# ── 整條流程：DXF → 檢核 ─────────────────────────────────────────────────

def make_fire_dxf(path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    frame = doc.blocks.new("TITLE")
    for i in range(45):
        frame.add_line((i * 20, 0), (i * 20, 594))
    for i in range(10):
        frame.add_text(chr(65 + i), dxfattribs={"insert": (i * 80, 600), "height": 5})
    meta = doc.blocks.new("META")
    for tag in ("圖號", "中文圖名", "單位"):
        meta.add_attdef(tag, (0, 0))
    msp.add_blockref("TITLE", (-500, -800), dxfattribs={"xscale": 5, "yscale": 5})
    ref = msp.add_blockref("META", (3500, -700))
    ref.add_auto_attribs({"圖號": "F-101", "中文圖名": "壹層消防設備平面圖", "單位": "cm"})
    for layer, polys in P.layers(0.01).items():
        for pts in polys:
            msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
    for t in P.texts(0.01):
        msp.add_text(t["t"], dxfattribs={"insert": (t["x"], t["y"]), "height": 30})
    for name in ("室內消防栓", "乾粉滅火器"):
        doc.blocks.new(name).add_circle((0, 0), 15)
    msp.add_blockref("室內消防栓", (100, 100))                     # 角落 → 遠端超過 25 m
    msp.add_blockref("乾粉滅火器", (1550, 650))
    msp.add_blockref("乾粉滅火器", (3600, 1800))                   # 圖例表裡的符號（建物外）不算
    doc.saveas(path)


def test_review_dxf_end_to_end(tmp_path):
    p = tmp_path / "fire.dxf"
    make_fire_dxf(p)
    res = EN.review_dxf(p)
    assert len(res.floors) == 1
    fr = res.floors[0]
    assert fr.floor.label == "1F" and fr.number == "F-101" and fr.outside == 1
    assert sorted(e.legend for e in fr.equipment) == ["乾粉滅火器", "室內消防栓"]
    rules = {(f.rule, f.severity) for f in fr.findings}
    assert ("HYD-34", K.RED) in rules and not by_rule(fr.findings, "EXT-31-3")
    d = EN.to_dict(res)
    json.dumps(d, ensure_ascii=False)
    hyd = next(f for f in d["floors"][0]["findings"] if f["rule"] == "HYD-34")
    assert hyd["geom"]["type"] in ("Polygon", "MultiPolygon") and hyd["law"]
    svg = R.floor_svg(fr)
    assert svg.startswith("<svg") and svg.count("<path") > 3


def test_worker_runs_review_after_extraction(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    from tests.test_drawing_pipeline import FakeConn
    dxf = tmp_path / "001_F-101.dxf"
    make_fire_dxf(dxf)
    saved, reviews, failed = [], [], []
    monkeypatch.setattr(W.ST, "claim", lambda conn: {"id": 7, "name": "F-101.dxf", "kind": "dxf", "path": str(dxf), "attempts": 1})
    monkeypatch.setattr(W.ST, "save_result", lambda conn, fid, ir, stats: saved.append(fid))
    monkeypatch.setattr(W.ST, "save_review", lambda conn, fid, status, result, error, svg_dir: reviews.append((fid, status, result, error, svg_dir)))
    monkeypatch.setattr(W.ST, "save_failure", lambda conn, fid, err, retry: failed.append(err))
    assert W.run_once(FakeConn(), tmp_path) is True
    assert failed == [] and saved == [7]
    fid, status, result, error, svg_dir = reviews[0]
    assert (fid, status, error) == (7, "done", None)
    fl = result["floors"][0]
    assert fl["label"] == "1F" and fl["equipment"] == {"hydrant": 1, "extinguisher": 1}
    hyd = next(f for f in fl["findings"] if f["rule"] == "HYD-34")
    assert "geom" not in hyd and len(hyd["bbox"]) == 4 and hyd["no"] >= 1
    assert (Path(svg_dir) / "1F.svg").read_text(encoding="utf-8").startswith("<svg")


def test_room_display_name_is_short():
    r = F.Room(1, box(0, 0, 1, 1), ["作業廠房C1", "buffer區", "配電盤", "配電盤", "包裝區", "碼頭區"], "room", False)
    assert r.name == "作業廠房C1／buffer區 等 5 個標示"
    assert F.Room(2, box(0, 0, 1, 1), ["天車", "天車"], "unknown", False).name == "天車"


def test_farthest_point_is_found_inside_the_gap():
    fl = plan()
    heads = sprinklers(fl, 3.2, skip=box(3, 3, 10, 10))
    f, _ = K.sprinkler_distance(fl, heads, K.Context())
    assert f[0].metrics["farthest"] == pytest.approx(5.0, abs=0.3)            # 7 m 見方的洞中間，不是邊界上的 2.3 m
