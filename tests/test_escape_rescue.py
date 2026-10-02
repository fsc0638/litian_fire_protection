"""第二批規則：出口標示燈、避難方向指示燈、緊急照明、排煙口、連結送水管出水口。"""

import pytest

from litian.plan import floor as F
from litian.review import checks as K
from litian.review import escape as ESC
from litian.review import rescue as RES
from litian.review import equipment as E

from . import _plans as P

DOORS = [(15.0, 6.0), (20.0, 10.0), (5.0, 0.25)]           # 內門兩扇＋外牆一扇（貼外框）


def plan(rename=None, title="壹層平面圖", doors=DOORS):
    texts = [dict(t) for t in P.texts()]
    for t in texts:
        if rename and t["t"] in rename:
            t["t"] = rename[t["t"]]
    return F.analyze(P.layers(), texts, scale=1.0, title=title, doors=doors)


def eq(legend, x, y, **spec):
    return E.Equipment("", legend, legend, E.kinds_of(legend), x, y, "F", {**E.specs(legend, {}), **spec})


def test_doors_classified_exterior_and_stair():
    fl = plan(rename={"男廁": "安全梯"})
    assert fl.exterior_doors() == [(5.0, 0.25)]
    assert fl.stair_doors() == [(20.0, 10.0)]


def test_exit_sign_required_at_exterior_and_stair_doors():
    fl = plan(rename={"男廁": "安全梯"})
    f, notes = ESC.exit_signs(fl, [eq("出口標示燈", 5, 1)], K.Context())
    assert [(x.severity, x.law) for x in f] == [(K.RED, ["D0120029/146-3/1/2"])] and "樓梯" in f[0].title
    assert "2 處" in notes[0].text
    both = [eq("出口標示燈", 5, 1), eq("出口標示燈", 20.5, 9.5)]
    assert ESC.exit_signs(fl, both, K.Context())[0] == []
    assert ESC.exit_signs(fl, [], K.Context()) == ([], [])                 # 圖上沒有出口標示燈 → 由應設設備規則處理


def test_direction_light_ranges_by_grade():
    fl = plan(rename={"會議室": "走廊"})
    light = lambda **kw: [eq("避難方向指示燈（單面單向）", 16, 1, **kw)]   # noqa: E731
    f, _ = ESC.direction_lights(fl, light(grade="C"), K.Context())         # 10 m：走廊遠端超出
    assert f and f[0].severity == K.RED and f[0].law == ["D0120029/146-3/2/3", "D0120029/146-2"]
    assert ESC.direction_lights(fl, light(grade="A"), K.Context())[0] == []   # 20 m：全涵蓋
    f, _ = ESC.direction_lights(fl, light(), K.Context())                  # 等級未標示 → 只有 C 級時不符
    assert f and all(x.severity == K.YELLOW for x in f) and "等級" in f[0].missing[0]


def test_direction_lights_skip_when_no_corridor_named():
    f, notes = ESC.direction_lights(plan(), [eq("避難方向指示燈（單面單向）", 16, 1)], K.Context())
    assert f == [] and "未辨識出走廊" in notes[0].text


def test_emergency_lights_rooms_and_exemptions():
    two = plan(title="貳層平面圖")
    f, notes = ESC.emergency_lights(two, [eq("緊急照明燈（吸頂式）", 7, 7)], K.Context())
    assert [(x.rooms, x.severity) for x in f] == [(["會議室"], K.RED)]
    assert any("男廁" in n.text and n.law == ["D0120029/179/1/6"] for n in notes)
    assert any(n.law == ["D0120029/178/1"] for n in notes)                 # 照度需另附計算
    one = plan()                                                           # 避難層：會議室 30 m 內可達外門 → 免設
    f, notes = ESC.emergency_lights(one, [eq("緊急照明燈（吸頂式）", 7, 7)], K.Context())
    assert f == [] and any(n.law == ["D0120029/179/1/1"] and "會議室" in n.text for n in notes)


def test_smoke_vents_missing_room_area_and_opening():
    fl = plan()
    vent = eq("排煙口（天花板型）", 7.5, 7.5, open_area=0.36)
    f, notes = RES.smoke_vents(fl, [vent], K.Context())
    got = {(x.rooms[0], x.category) for x in f}
    assert ("會議室", "未設置") in got and ("辦公室", "規格不符") in got
    short = next(x for x in f if x.category == "規格不符")
    assert short.metrics["need"] == pytest.approx(213.7 * 0.02, abs=0.05) and short.law == ["D0120029/188/1/7"]
    assert "男廁" in notes[0].text
    f, _ = RES.smoke_vents(fl, [eq("排煙口（天花板型）", 7.5, 7.5)], K.Context())
    assert any(x.severity == K.YELLOW and "開口" in x.missing[0] for x in f)


def test_standpipe_outlets_only_from_third_floor():
    three = plan(rename={"男廁": "安全梯"}, title="參層平面圖")
    good = [eq("連結送水管出水口", 22, 10.5)]
    assert RES.standpipe_outlets(three, good, K.Context())[0] == []
    f, _ = RES.standpipe_outlets(three, [eq("連結送水管出水口", 2, 2)], K.Context())
    assert [(x.severity, x.law) for x in f] == [(K.ORANGE, ["D0120029/180/1/1"])]
    assert RES.standpipe_outlets(plan(title="貳層平面圖"), [eq("連結送水管出水口", 2, 2)], K.Context())[0] == []
