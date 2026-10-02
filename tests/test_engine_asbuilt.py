"""竣工圖交叉檢查後的整份圖流程修正：樓層對位與挑空投影、依應設判定降級標示設備缺失、樓梯間廣播垂直距離。
全部用程式畫的圖（兩層樓的合成圖，座標公尺）。"""

import json
import random
import re
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import ezdxf
import pytest
from shapely.geometry import Point, box

from litian.plan import floor as F
from litian.review import checks as K
from litian.review import engine as EN
from litian.review import equipment as E
from litian.review import required as RQ
from litian.review import stack as ST

SMOKE = "偵煙式局限型探測器（2種）"
SPEAKER = "揚聲器（壁掛式）"
EXIT = "出口標示燈"


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def plan(hall: str, core: bool = True, roof: bool = False):
    """40 m × 20 m：左邊 30 m 是大空間（名稱 hall），右邊 10 m 是核心（客梯、管道間、A梯、管道間(電)、辦公室）。
    core=False：核心只剩客梯（對位錨點不足）；roof=True：屋突層，只畫核心（大空間位置是屋頂，不在任何房間內）。"""
    walls = [rect(30, 0, 40, 20)] if roof else [rect(0, 0, 40, 20), [(30, 0), (30, 20)]]
    texts = [] if roof else [(hall, 15, 10)]
    texts.append(("辦公室", 37, 8))
    walls += [[(30, 3), (33, 3)], [(33, 0), (33, 3)]]
    texts.append(("客梯", 31.5, 1.5))
    if core:
        walls += [[(33, 2), (35, 2)], [(35, 0), (35, 2)], [(35, 15), (40, 15)], [(35, 15), (35, 20)],
                  [(30, 18), (32, 18)], [(32, 18), (32, 20)]]
        texts += [("管道間", 34, 1), ("A梯", 37.5, 17.5), ("管道間(電)", 31, 19)]
    return walls, texts


def analyze(title, walls, texts, ox=0.0, oy=0.0):
    move = lambda pts: [(x + ox, y + oy) for x, y in pts]  # noqa: E731
    return F.analyze({"WALL": [move(p) for p in walls]}, [{"t": t, "x": x + ox, "y": y + oy} for t, x, y in texts],
                     scale=1.0, title=title)


def det(x, y, legend=SMOKE):
    return E.Equipment("", legend, legend, E.kinds_of(legend), x, y, "0", E.specs(legend, {}))


# ── 對位 ────────────────────────────────────────────────────────────────

def test_align_uses_several_anchors_and_refuses_when_too_few():
    walls, texts = plan("作業廠房")
    lo = analyze("一層消防設備平面圖", walls, texts)
    hi = analyze("二層消防設備平面圖", *plan("(挑空)"), ox=12.0, oy=-60.0)
    sh = ST.align(hi, lo)
    assert sh is not None and sh.anchors >= 3 and sh.resid < ST.MAX_RESID
    assert (sh.dx, sh.dy) == pytest.approx((-12.0, 60.0), abs=0.05)
    only_lift = analyze("二層消防設備平面圖", *plan("(挑空)", core=False), ox=12.0, oy=-60.0)
    assert ST.align(only_lift, lo) is None                                  # 只有一個共同錨點：不採用


def test_roof_detectors_outside_rooms_go_to_the_floor_below():
    f1 = analyze("一層消防設備平面圖", *plan("作業廠房"))
    rf = analyze("屋頂層消防設備平面圖", *plan("", roof=True), oy=-30.0)
    assert rf.label == "RF" and rf.room_near(10, -20) is None
    eq1 = [det(37, 8), det(10, 10)]                                         # 1F 已畫了 (10, 10) 那一個
    eqr = [det(10, -20), det(20, -20), det(37.5, -12.5)]                    # 屋頂板下兩個＋A梯內一個（屬屋突層）
    projs, warns = ST.project_detectors([f1, rf], ["F-101", "F-1R1"], [eq1, eqr])
    assert warns == [] and len(projs) == 1
    p = projs[0]
    assert (p.target, p.source, p.roof) == (0, 1, True)
    assert [(round(e.x, 1), round(e.y, 1)) for e in p.equipment] == [(20.0, 10.0)]   # 重複的不再加
    assert p.equipment[0].spec["projected_from"] == "F-1R1" and "projected_from" not in eqr[1].spec
    projs, _ = ST.project_detectors([f1, rf], ["F-101", "F-1R1"], [eq1, eqr], stories=5)
    assert projs == []                                                      # 圖上只有 1F，填寫 5 層：屋突層不知接在哪層


def test_projection_passes_only_through_floor_openings():
    """挑空（樓板開口）一路往下；挑高、中庭、天井可能有樓板（例：一樓挑高大廳）→ 停止並警告，不落到更下層。"""
    office = det(37, 8)
    grid = [(x, y) for x in (5, 15, 25) for y in (5, 15)]
    lo = analyze("一層消防設備平面圖", *plan("作業廠房"))
    f2 = analyze("二層消防設備平面圖", *plan("(挑空)"), oy=-60)
    f3 = analyze("三層消防設備平面圖", *plan("(挑空)"), oy=-120)
    eq = [[office], [det(37, 8 - 60)], [det(10, 10 - 120)]]
    projs, warns = ST.project_detectors([lo, f2, f3], ["F-101", "F-102", "F-103"], eq)
    assert warns == [] and [(p.target, p.source, p.levels, len(p.equipment)) for p in projs] == [(0, 2, 3, 1)]
    assert projs[0].equipment[0].spec["projected_levels"] == 3                # 裝在 3F 樓板下：自 1F 起約 3 層樓高
    b1 = analyze("地下一層消防設備平面圖", *plan("停車場"))
    lobby = analyze("一層消防設備平面圖", *plan("挑高大廳"), oy=-30)
    eq = [[office], [det(37, 8 - 30)], [det(x, y - 60) for x, y in grid]]
    projs, warns = ST.project_detectors([b1, lobby, f2], ["B-101", "F-101", "F-102"], eq)
    assert projs == []                                                      # 不穿過有樓板的挑高大廳灌進地下停車場
    assert len(warns) == 1 and "F-102" in warns[0] and "6 個探測器" in warns[0] and "「挑高大廳」" in warns[0]
    tall = analyze("二層消防設備平面圖", *plan("挑高大廳"), oy=-60)          # 上層的「挑高大廳」：視為本層設備
    projs, warns = ST.project_detectors([lo, tall], ["F-101", "F-102"], [[office], eq[2]])
    assert projs == [] and warns == []


def test_same_detectors_on_two_upper_sheets_are_added_once():
    """同層兩張圖（火警、排煙連動）都畫了挑空內同一批探測器：下層只加一次。"""
    grid = [(x, y) for x in (5, 15, 25) for y in (5, 15)]
    lo = analyze("一層消防設備平面圖", *plan("作業廠房"))
    fire = analyze("二層消防設備平面圖", *plan("(挑空)"), oy=-60)
    smoke = analyze("二層消防排煙系統", *plan("(挑空)"), ox=60, oy=-60)
    eq = [[det(37, 8)], [det(x, y - 60) for x, y in grid], [det(x + 60, y - 60) for x, y in grid]]
    projs, warns = ST.project_detectors([lo, fire, smoke], ["F-101", "F-102", "S-102"], eq)
    assert warns == [] and [(p.target, p.source, len(p.equipment)) for p in projs] == [(0, 1, 6)]


def _shafts(n, ox, oy, seed, jitter=0.2):
    """n 個同尺寸管道間「PS」排成格子（間距 6 m），位置有 ±jitter 的誤差。"""
    rnd = random.Random(seed)
    side = int(n ** 0.5) + 1
    rooms = []
    for k in range(n):
        i, j = divmod(k, side)
        x, y = i * 6.0 + ox + rnd.uniform(-jitter, jitter), j * 6.0 + oy + rnd.uniform(-jitter, jitter)
        rooms.append(F.Room(k + 1, box(x, y, x + 1.2, y + 0.8), ["PS"], "shaft", False))
    return NS(rooms=rooms)


def test_align_scales_to_many_anchors():
    a, b = _shafts(120, 0, 0, 1), _shafts(120, 3.0, -164.2, 2)
    t = time.time()
    sh = ST.align(a, b)
    assert time.time() - t < 2.0
    assert sh is not None and sh.anchors >= 100 and (sh.dx, sh.dy) == pytest.approx((3.0, -164.2), abs=0.1)


def test_aligner_caches_and_reuses_the_reverse(monkeypatch):
    lo = analyze("一層消防設備平面圖", *plan("作業廠房"))
    hi = analyze("二層消防設備平面圖", *plan("(挑空)"), ox=12.0, oy=-60.0)
    calls, real = [], ST.align
    monkeypatch.setattr(ST, "align", lambda *a: calls.append(1) or real(*a))
    al = ST.Aligner([hi, lo])
    s1, s2, s3 = al(0, 1), al(1, 0), al(0, 1)
    assert len(calls) == 1 and s3 is s1 and (s2.dx, s2.dy) == (-s1.dx, -s1.dy) and s2.anchors == s1.anchors


# ── 整份圖：兩層樓，2F 大空間是挑空 ─────────────────────────────────────────

def _sheet(msp, number, title, ox, oy, walls, texts, equip):
    """一張圖（單位公分）：圖框左下角在 (ox-2, oy-5) m，平面與設備平移 (ox, oy) m。"""
    msp.add_blockref("TITLE", ((ox - 2) * 100, (oy - 5) * 100), dxfattribs={"xscale": 5, "yscale": 5})
    ref = msp.add_blockref("META", ((ox + 38) * 100, (oy - 4) * 100))
    ref.add_auto_attribs({"圖號": number, "中文圖名": title, "單位": "cm"})
    for pts in walls:
        msp.add_lwpolyline([((x + ox) * 100, (y + oy) * 100) for x, y in pts], dxfattribs={"layer": "WALL"})
    for t, x, y in texts:
        msp.add_text(t, dxfattribs={"insert": ((x + ox) * 100, (y + oy) * 100), "height": 30})
    for name, x, y in equip:
        msp.add_blockref(name, ((x + ox) * 100, (y + oy) * 100), dxfattribs={"layer": "DOOR" if name == "D1" else "0"})


def make_dxf(path, *, upper_core=True, exit_sign=False, stair_speaker=True, hall_stair=False):
    """1F（F-101）：大空間「作業廠房」沒有探測器，辦公室 2 個；2F（F-102，畫在 (10, -60) m）：大空間是挑空，
    挑空範圍內 6 個探測器（裝在 2F 樓板下、保護 1F 大空間），辦公室 2 個。
    hall_stair：1F 大空間裡另寫「B梯」（樓梯沒有圍成獨立房間）。"""
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
    for name in (SMOKE, SPEAKER, EXIT):
        doc.blocks.new(name).add_circle((0, 0), 15)
    doc.blocks.new("D1").add_point((0, 0))
    office = [(SMOKE, 37, 8), (SMOKE, 37, 11), (SPEAKER, 36, 10)]
    eq1 = office + ([(SPEAKER, 37.5, 17.5)] if stair_speaker else [])
    eq1 += [("D1", 20, 0), (EXIT, 36, 6)] if exit_sign else []
    eq2 = office + [(SMOKE, x, y) for x in (5, 15, 25) for y in (5, 15)]
    walls, texts = plan("作業廠房")
    _sheet(msp, "F-101", "壹層消防設備平面圖", 0, 0, walls, texts + ([("B梯", 20, 12)] if hall_stair else []), eq1)
    _sheet(msp, "F-102", "貳層消防設備平面圖", 10, -60, *plan("(挑空)", core=upper_core), eq2)
    doc.saveas(path)


def _hall_det(fr):
    return [f for f in fr.findings if f.rule == "DET-120" and any("作業廠房" in r for r in f.rooms)]


def test_void_detectors_on_upper_sheet_protect_the_hall_below(tmp_path):
    p = tmp_path / "fire.dxf"
    make_dxf(p)
    res = EN.review_dxf(p, ctx=K.Context(ceiling_height={"1F": 3.0, "2F": 3.0}))
    f1, f2 = res.floors
    assert (f1.floor.label, f2.floor.label) == ("1F", "2F")
    assert _hall_det(f1) == []                                   # 以本層 3 m 計：600 ㎡ ÷ 150 ㎡ → 4 個，挑空投影來 6 個
    assert len(f1.projected) == 6 and {e.spec["projected_from"] for e in f1.projected} == {"F-102"}
    hall = next(r for r in f1.floor.rooms if r.name == "作業廠房")
    assert all(hall.polygon.covers(Point(e.x, e.y)) for e in f1.projected)
    assert sum("detector" in e.kinds for e in f1.equipment) == 2                # 設備統計不含投影來的
    assert EN.to_dict(res)["floors"][0]["equipment"]["detector"] == 2
    note = next(n for n in f1.notes if "F-102" in n.text)
    assert "挑空" in note.text and "6 個探測器" in note.text and "不計入本圖設備數量" in note.text
    assert "約 2 層樓高" in note.text and note.law == ["D0120029/114/1"]      # 高度前提寫在說明裡
    assert f2.projected == [] and not res.warnings
    # 實際裝在 2F 樓板下（兩層樓高）：以本層 3 m 檢討會藏掉不足 → 需確認，以 4 m 以上（75 ㎡）試算需 8 個
    h = [f for f in f1.findings if f.rule == "DET-114"]
    assert [(f.severity, f.category, f.rooms) for f in h] == [(K.ORANGE, "需確認", ["作業廠房"])]
    assert h[0].metrics["need_est"] == 8 and h[0].metrics["have"] == 6 and h[0].law[0] == "D0120029/114/1"
    assert "F-102 圖 6 個，約 2 層樓高" in h[0].why and "高於本層設定的天花板高度 3 m" in h[0].why


def test_projected_detectors_keep_the_mounting_height_in_view(tmp_path):
    p = tmp_path / "fire.dxf"
    make_dxf(p)
    red = EN.review_dxf(p, ctx=K.Context(ceiling_height={"1F": 6.0}))
    f = _hall_det(red.floors[0])
    assert [(x.severity, x.category) for x in f] == [(K.RED, "數量不足")]
    assert "取自上層圖的挑空範圍" in f[0].why and "D0120029/114/1" in f[0].law  # 已列不符：補上高度前提，不重複列
    assert not [x for x in red.floors[0].findings if x.rule == "DET-114"]
    unknown = EN.review_dxf(p)                                   # 樓高未知：6 個，以 4 m 以上試算需 8 個
    assert [x.severity for x in unknown.floors[0].findings if x.rule == "DET-114"] == [K.ORANGE]
    low = EN.review_dxf(p, ctx=K.Context(height=8.0))           # 估算裝置面 8 m：逐房規則只列資料不足（需 4～8 個）
    f = [x for x in low.floors[0].findings if x.rule == "DET-114"]
    assert [x.severity for x in _hall_det(low.floors[0])] == [K.YELLOW]
    assert len(f) == 1 and "可能不足" in f[0].title and "估算裝置面約 8 m" in f[0].why


HEAT, SMOKE1 = "差動式局限型探測器（2種）", "偵煙式局限型探測器（1種）"


def _void_room(legend, n, height=None):
    """1F 大空間（約 600 ㎡）有 n 個從 2F 挑空投影來的探測器（約 2 層樓高）；回傳 DET-114。"""
    fl = analyze("一層消防設備平面圖", *plan("作業廠房"))
    pts = [(x, y) for x in (3, 9, 15, 21, 27) for y in (2, 6, 10, 14)][:n]
    pe = [replace(e, spec={**e.spec, "projected_from": "F-102", "projected_levels": 2}) for e in (det(x, y, legend) for x, y in pts)]
    fr = EN.FloorResult(0, "F-101", "壹層消防設備平面圖", fl, [det(37, 8, legend)], [], [], projected=pe)
    EN.void_heights(NS(floors=[fr], profile=NS(stories=2)), K.Context(height=height))
    return [x for x in fr.findings if x.rule == "DET-114"]


def test_projected_detector_height_check_cases():
    f = _void_room(HEAT, 8, height=8.0)                          # 熱式：裝置面 8 m 以上不得使用
    assert [(x.severity, x.rooms) for x in f] == [(K.ORANGE, ["作業廠房"])]
    assert "估算裝置面約 8 m" in f[0].why and "差動式局限型2種在此高度不得使用" in f[0].why
    assert f[0].law == ["D0120029/114/1", "D0120029/120/1/2"]
    assert [x.severity for x in _void_room(SMOKE, 8)] == [K.ORANGE]          # 偵煙式二種、高度未知：可能達 15 m 以上
    assert "可能不足" in _void_room(SMOKE1, 6)[0].title                     # 數量未達 4 m 以上試算的需設數
    # 高度再高也藏不住缺失才不列：偵煙式一種（4～20 m 有效範圍相同）、或二種且估算未達 15 m，數量也夠
    assert _void_room(SMOKE1, 8) == [] and _void_room(SMOKE, 8, height=8.0) == []


def test_no_projection_without_alignment(tmp_path):
    p = tmp_path / "fire.dxf"
    make_dxf(p, upper_core=False)
    res = EN.review_dxf(p, ctx=K.Context(ceiling_height={"1F": 3.0, "2F": 3.0}))
    f1 = res.floors[0]
    assert f1.projected == [] and not any("F-102" in n.text for n in f1.notes)
    assert any("F-102" in w and "無法對位" in w for w in res.warnings)
    assert [(f.severity, f.category) for f in _hall_det(f1)] == [(K.RED, "未設置")]


# ── 依第 23 條非應設的標示設備：缺失改為建議 ─────────────────────────────────

def test_exit_sign_findings_become_advice_when_not_required(tmp_path):
    p = tmp_path / "fire.dxf"
    make_dxf(p, exit_sign=True)
    res = EN.review_dxf(p, ctx=K.Context(occupancy="丁-2"))
    assert next(r for r in res.requirements if r.key == "23-1").status == RQ.NOT_REQUIRED
    ex = [f for f in res.floors[0].findings if f.rule == "EXIT-146-3"]
    assert ex and all(f.severity == K.BLUE for f in ex)
    assert "依第 23 條本建物非應設出口標示燈（自主設置），檢討結果僅供參考" in ex[0].why
    assert "未勾選無開口樓層，以全部樓層皆非無開口樓層判定；若本層屬無開口樓層即為應設" in ex[0].why   # 判定前提
    assert "另一種讀法：若認定自主設置者亦應符合第 146 條之 3 的位置規定，本項仍為缺失" in ex[0].why
    assert "D0120029/23/1/1" in ex[0].law and "避難指標" not in ex[0].why
    assert res.floors[0].findings[-len(ex):] == ex                          # 重新排序：建議排在最後
    ticked = EN.review_dxf(p, ctx=K.Context(occupancy="丁-2", no_opening=["5F"]))   # 已檢討過無開口樓層：不再寫前提
    ex = [f for f in ticked.floors[0].findings if f.rule == "EXIT-146-3"]
    assert ex and all(f.severity == K.BLUE and "未勾選無開口樓層" not in f.why for f in ex)
    off = EN.review_dxf(p, ctx=K.Context(occupancy="丁-2", policy={"voluntary_signs_note": False}))
    assert {f.severity for f in off.floors[0].findings if f.rule == "EXIT-146-3"} == {K.RED}
    req = EN.review_dxf(p, ctx=K.Context(occupancy="甲-1"))               # 甲類應設：不降
    assert {f.severity for f in req.floors[0].findings if f.rule == "EXIT-146-3"} == {K.RED}


def _finding(rule):
    return K.Finding(rule, K.RED, "距離超過", "", "t", "w", "f", ["D0120029/146-3/2/3"])


def test_direction_light_advice_only_on_floors_not_required():
    """23-2 只有部分樓層應設（例：十一層以上）：其他地上樓層的方向指示燈缺失改建議，應設樓層照舊。"""
    floors = [NS(floor=NS(label=lab), findings=[_finding("DIR-146-3"), _finding("HYD-34")]) for lab in ("3F", "11F")]
    res = NS(floors=floors, requirements=[RQ.Requirement("23-2", "避難方向指示燈", ("direction_light",), RQ.REQUIRED, "",
                                                         ["D0120029/23/1/2"], floors=["11F"])])
    EN.voluntary_signs(res, K.Context())
    low, high = floors
    assert [(f.rule, f.severity) for f in low.findings] == [("HYD-34", K.RED), ("DIR-146-3", K.BLUE)]
    why, law = low.findings[1].why, low.findings[1].law
    assert "本層（應設樓層：11F）非應設避難方向指示燈" in why
    # 第 23 條第 4 款：指示燈有效範圍外的走廊仍應設避難指標（系統不辨識避難指標）
    assert "依第 23 條第 4 款仍應設避難指標" in why and "圖上未辨識避難指標，請確認" in why
    assert "另一種讀法" in why and "D0120029/23/1/4" in law and "D0120029/153/1/2" in law
    assert [f.severity for f in high.findings] == [K.RED, K.RED]


def test_sign_advice_does_not_touch_shared_law_lists():
    shared = ["D0120029/146-3/2/3"]
    a, b = (K.Finding("DIR-146-3", K.RED, "距離超過", "", "t", "w", "f", shared) for _ in range(2))
    res = NS(floors=[NS(floor=NS(label="3F"), findings=[a]), NS(floor=NS(label="11F"), findings=[b])],
             requirements=[RQ.Requirement("23-2", "避難方向指示燈", ("direction_light",), RQ.REQUIRED, "",
                                          ["D0120029/23/1/2"], floors=["11F"])])
    EN.voluntary_signs(res, K.Context())
    assert a.severity == K.BLUE and "D0120029/23/1/2" in a.law
    assert shared == ["D0120029/146-3/2/3"] and b.law is shared             # 共用的法條清單沒被改到


# ── 樓梯間廣播：垂直每 15 m 一個（第 133 條第 2 款第 5 目）────────────────────

def _stair_findings(res):
    return [f for f in res.building_findings if f.rule == "SPKR-133-5"]


def test_stair_speakers_need_storey_height(tmp_path):
    p = tmp_path / "fire.dxf"
    make_dxf(p, hall_stair=True)
    res = EN.review_dxf(p)
    f = _stair_findings(res)
    assert [(x.severity, x.category, x.floor) for x in f] == [(K.YELLOW, "資料不足", "全棟")]
    assert "A梯：1F 1 個、2F 無" in f[0].why and f[0].missing == ["各層樓高（或建築物高度）"]
    assert "另 B梯 沒有圍成樓梯間，未列入" in f[0].why                     # 只寫在大空間裡的樓梯：明白列出，不默默略過
    assert any(n.rule == "SPKR-133-5" and "B梯" in n.text for n in res.building_notes)
    assert f[0].law == ["D0120029/133/1/2/5"] and f[0].metrics["stairs"] == {"A梯": {"1F": 1, "2F": 0}}
    assert _stair_findings(EN.review_dxf(p, ctx=K.Context(height=8.0))) == []      # 2 層、垂直 4 m：1 個即可
    off = EN.review_dxf(p, ctx=K.Context(policy={"stair_speaker_vertical": False}))
    assert _stair_findings(off) == []


def test_stair_without_speaker_is_red_when_height_known(tmp_path):
    p = tmp_path / "fire.dxf"
    make_dxf(p, stair_speaker=False)
    f = _stair_findings(EN.review_dxf(p, ctx=K.Context(height=8.0)))
    assert [(x.severity, x.title) for x in f] == [(K.RED, "A梯 樓梯間揚聲器不足（需 1 個，現有 0 個）")]
    assert "以建築物高度 8 m ÷ 2 層估算每層約 4 m" in f[0].why
    q = tmp_path / "partial.dxf"
    make_dxf(q, upper_core=False, stair_speaker=False)                      # 2F 沒有圍成 A梯：整座樓梯沒看全
    f = _stair_findings(EN.review_dxf(q, ctx=K.Context(height=8.0)))
    assert [(x.severity, x.category) for x in f] == [(K.ORANGE, "需確認")] and "只在 1F 認得出樓梯間" in f[0].why


def test_partial_stair_is_checked_by_hand_even_when_count_looks_enough(tmp_path):
    """樓梯只在 1F 圍成樓梯間（有 1 個揚聲器）：只算認得的樓層數量「夠」，但整座樓梯可能更高 → 一律需確認。"""
    q = tmp_path / "partial.dxf"
    make_dxf(q, upper_core=False)
    for height in (8.0, 40.0):
        f = _stair_findings(EN.review_dxf(q, ctx=K.Context(height=height)))
        assert [(x.severity, x.category) for x in f] == [(K.ORANGE, "需確認")]
    assert "依認得出的樓層估算需 1 個、現有 1 個" in f[0].why
    assert "若這座樓梯通達圖上 1F～2F（約 20 m）則需 2 個" in f[0].why and f[0].metrics["whole"] is False


def test_cited_law_nodes_exist():
    nodes = {json.loads(line)["node_id"] for line in Path("data/lawdb/nodes.jsonl").read_text(encoding="utf-8").splitlines()}
    src = "".join(Path(m.__file__).read_text(encoding="utf-8") for m in (EN, ST))
    cited = set(re.findall(r"D0120029(?:/[0-9-]+)+", src))
    assert cited and cited <= nodes, cited - nodes
