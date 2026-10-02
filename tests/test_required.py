"""依場所判定應設設備（設置標準第 14～30-1 條）。"""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from shapely.geometry import box

from litian.plan.floor import Room
from litian.review import checks as K
from litian.review import engine as EN
from litian.review import required as RQ

from .test_plan_review import make_fire_dxf


def prof(occ, floors, stories=None, **kw):
    no = kw.pop("no_opening", [])
    fl = [RQ.FloorArea(lab, lv, a, lab in no) for lab, lv, a in floors]
    return RQ.Profile(occ, fl, stories if stories is not None else max((f.level for f in fl), default=0), **kw)


def status(reqs):
    return {r.key: r.status for r in reqs}


def by(reqs, key):
    return next(r for r in reqs if r.key == key)


SAMPLE = [("1F", 1, 9360), ("2F", 2, 2144), ("3F", 3, 5648)]          # 樣本廠房（面積計算表各層）


def test_factory_middle_hazard_three_storeys():
    reqs = RQ.evaluate(prof("丁-2", SAMPLE))
    s = status(reqs)
    assert s["14"] == s["15"] == s["16"] == s["19"] == s["20"] == s["22"] == s["24"] == RQ.REQUIRED
    assert s["17"] == s["23-1"] == s["23-2"] == s["26"] == s["29"] == RQ.NOT_REQUIRED
    assert by(reqs, "14").law == ["D0120029/14/1/2"]
    assert by(reqs, "15").law == ["D0120029/15/1/1"] and "第 15 條第 2 項" in by(reqs, "15").notes[0]
    assert by(reqs, "16").law == ["D0120029/16/1/2"] and "11,504" in by(reqs, "16").why       # 第一層＋第二層
    assert by(reqs, "24").law == ["D0120029/24/1/3", "D0120029/24/1/5"]
    assert by(reqs, "19").law == ["D0120029/19/1/1"] and any("第 19 條第 2 項" in n for n in by(reqs, "19").notes)


def test_low_hazard_factory_below_outdoor_hydrant_threshold():
    assert by(RQ.evaluate(prof("丁-3", SAMPLE)), "16").status == RQ.REQUIRED          # 11,504 ≥ 10,000
    small = RQ.evaluate(prof("丁-3", [("1F", 1, 4000), ("2F", 2, 3000)]))
    assert by(small, "16").status == RQ.NOT_REQUIRED


def test_karaoke_two_storeys():
    reqs = RQ.evaluate(prof("甲-1", [("1F", 1, 400), ("2F", 2, 400)]))
    s = status(reqs)
    assert all(s[k] == RQ.REQUIRED for k in ("14", "15", "17", "19", "22", "23-1", "23-2", "24", "28"))
    assert by(reqs, "17").floors == ["1F", "2F"] and by(reqs, "17").law == ["D0120029/17/1/1"]
    assert by(reqs, "15").law == ["D0120029/15/1/1"]                      # 甲-1 門檻 300 ㎡
    assert by(reqs, "19").law == ["D0120029/19/1/1", "D0120029/19/1/6"]
    assert not any("第 19 條第 2 項" in n for n in by(reqs, "19").notes)  # 甲類不適用撒水免設


def test_office_tower_twelve_storeys():
    floors = [(f"{i}F", i, 800) for i in range(1, 13)]
    reqs = RQ.evaluate(prof("乙-6", floors, height=45))
    assert by(reqs, "17").floors == ["11F", "12F"] and by(reqs, "17").law == ["D0120029/17/1/2"]
    assert by(reqs, "19").law == ["D0120029/19/1/3"]
    assert by(reqs, "23-1").floors == ["11F", "12F"]
    assert by(reqs, "26").status == RQ.REQUIRED and by(reqs, "26").floors[0] == "3F"
    assert by(reqs, "29").status == RQ.REQUIRED and by(reqs, "29").floors is None      # 十一層以上建築物之「各樓層」
    assert by(reqs, "15").law == ["D0120029/15/1/2"]


def test_basement_and_no_opening_floors():
    reqs = RQ.evaluate(prof("乙-6", [("B1", -1, 200), ("1F", 1, 300), ("2F", 2, 300)], stories=2, no_opening=["2F"]))
    assert by(reqs, "23-1").floors == ["B1", "2F"]
    assert "D0120029/14/1/3" in by(reqs, "14").law and "D0120029/15/1/4" in by(reqs, "15").law


def test_unknowns_are_not_guessed():
    reqs = RQ.evaluate(prof(None, SAMPLE))
    s = status(reqs)
    assert s["15"] == s["17"] == s["19"] == RQ.UNKNOWN and any("場所類別" in m for m in by(reqs, "15").missing)
    comp = RQ.evaluate(prof("戊-2", SAMPLE))
    assert by(comp, "15").status == RQ.UNKNOWN and "第 6 條" in by(comp, "15").why
    tall = RQ.evaluate(prof("乙-6", [(f"{i}F", i, 500) for i in range(1, 14)]))   # 13 層、高度未知 → 高層與否不明
    assert by(tall, "30-1").status == RQ.UNKNOWN and "建築物高度" in by(tall, "30-1").missing


def test_smoke_control_needs_ventilation_data_for_big_rooms():
    p = prof("丁-2", SAMPLE, rooms_over_100=["1F 作業廠房", "2F 辦公區"])
    r = by(RQ.evaluate(p), "28")
    assert r.status == RQ.UNKNOWN and "有效通風面積" in r.missing[0] and r.law == ["D0120029/28/1/2"]


def test_all_cited_nodes_exist():
    nodes = {json.loads(line)["node_id"] for line in Path("data/lawdb/nodes.jsonl").read_text(encoding="utf-8").splitlines()}
    cases = [prof(o, fl, **kw) for o, fl, kw in [
        ("丁-2", SAMPLE, {}), ("甲-1", [("1F", 1, 400), ("2F", 2, 400)], {}), (None, SAMPLE, {}),
        ("乙-6", [(f"{i}F", i, 800) for i in range(1, 13)], {"height": 45}), ("戊-3", [("B1", -1, 2000)], {}),
        ("乙-11", [("1F", 1, 900)], {"ceiling_height": {"1F": 12}}), ("戊-1", SAMPLE, {}), ("甲-3", SAMPLE, {"site_area": 30000}),
        ("丁-1", SAMPLE, {"no_opening": ["2F"], "rooms_over_100": ["x"]}), ("乙-3", SAMPLE, {}),
    ]]
    cited = {law for p in cases for r in RQ.evaluate(p) for law in r.law}
    assert len(cited) >= 30 and cited <= nodes, cited - nodes


def _fr(label, area, kinds=()):
    rooms = [Room(1, box(0, 0, 10, 10), ["電氣室"], k, False) for k in kinds]
    return NS(floor=NS(label=label, area=area, rooms=rooms), equipment=[], findings=[])


def test_build_profile_merges_mezzanine_and_roof():
    ctx = K.Context(occupancy="丁-2", floor_area={"2F": 2143.67})
    p = RQ.build_profile([_fr("1F", 9000, ["electrical"]), _fr("1MF", 100), _fr("2F", 3600), _fr("R1F", 130)], ctx)
    assert [(f.label, f.level, round(f.area, 2)) for f in p.floors] == [("1F", 1, 9100.0), ("2F", 2, 2143.67)]
    assert p.roof_area == 130 and p.stories == 2 and p.has_electrical
    assert any("地上層數以平面圖推定" in n for n in p.notes)


def test_engine_reports_missing_required_equipment(tmp_path):
    p = tmp_path / "fire.dxf"
    make_fire_dxf(p)                                          # 一層 453 ㎡，只有消防栓、滅火器
    res = EN.review_dxf(p, ctx=K.Context(occupancy="乙-6"))
    assert by(res.requirements, "24").status == RQ.REQUIRED  # 乙-6 居室應設緊急照明（第 24 條第 2 款）
    fr = res.floors[0]
    req = [f for f in fr.findings if f.rule.startswith("REQ-")]
    assert [(f.rule, f.severity) for f in req] == [("REQ-24", K.RED)] and "緊急照明" in req[0].title
    d = EN.to_dict(res)
    assert d["building"]["profile"]["occupancy"] == "乙-6" and d["building"]["requirements"]
    json.dumps(d, ensure_ascii=False)


def test_engine_summarises_when_no_fire_equipment_at_all(tmp_path):
    import ezdxf
    p = tmp_path / "arch.dxf"
    make_fire_dxf(p)
    doc = ezdxf.readfile(p)
    for e in list(doc.modelspace().query("INSERT")):
        if e.dxf.name in ("室內消防栓", "乾粉滅火器"):
            doc.modelspace().delete_entity(e)
    doc.saveas(p)
    res = EN.review_dxf(p, ctx=K.Context(occupancy="乙-6"))
    assert [f.rule for f in res.building_findings] == ["REQ"] and "未認出任何消防設備" in res.building_findings[0].title
    assert not any(f.rule.startswith("REQ-") for f in res.floors[0].findings)


# ── 2026-10-02 獨立稽核修正的回歸測試 ──

def test_school_classroom_exceptions_are_not_guessed():
    em = by(RQ.evaluate(prof("乙-3", SAMPLE)), "24")
    assert em.status == RQ.UNKNOWN and em.missing == ["是否為學校教室"]
    small = by(RQ.evaluate(prof("乙-3", [("1F", 1, 800), ("2F", 2, 800)])), "15")
    assert small.status == RQ.UNKNOWN and small.missing == ["是否為學校教室"]          # 補習班 500 ㎡、學校教室 1,400 ㎡
    assert by(RQ.evaluate(prof("乙-3", [("1F", 1, 1500)])), "15").status == RQ.REQUIRED


def test_height_unknown_from_eleven_storeys_is_not_low_rise():
    p = prof("乙-6", [(f"{i}F", i, 800) for i in range(1, 13)])
    assert p.high_rise is None
    reqs = RQ.evaluate(p)
    assert "建築物高度（是否為高層建築物）" in by(reqs, "17").missing
    assert not any("第 19 條第 2 項" in n for n in by(reqs, "19").notes)      # 高層與否未知 → 不提示撒水免設


def test_gas_leak_radio_and_broadcast():
    reqs = RQ.evaluate(prof("甲-5", [("B1", -1, 1200), ("1F", 1, 200)], stories=1))
    assert by(reqs, "21").status == RQ.REQUIRED and by(reqs, "21").floors == ["B1"]
    deep = RQ.evaluate(prof("乙-6", [(f"B{i}", -i, 800) for i in range(1, 5)] + [("1F", 1, 800)], stories=1, height=20))
    assert by(deep, "30").status == RQ.REQUIRED and by(deep, "30").law == ["D0120029/30/1/3"]
    assert by(deep, "18").status == RQ.UNKNOWN                               # 第 18 條按房間逐層判定


def test_high_rack_warehouse_needs_storey_height():
    r = by(RQ.evaluate(prof("乙-11", [("1F", 1, 900)], ceiling_height={"1F": 8})), "17")
    assert r.status == RQ.UNKNOWN and "樓層高度" in r.missing[0]
    r = by(RQ.evaluate(prof("乙-11", [("1F", 1, 900)], ceiling_height={"1F": 12})), "17")
    assert r.status == RQ.REQUIRED and r.law == ["D0120029/17/1/6"]


def test_composite_with_class_a_requires_extinguishers():
    r = by(RQ.evaluate(prof("戊-1", SAMPLE)), "14")
    assert r.status == RQ.REQUIRED and "D0120029/14/1/1" in r.law and r.floors is None


def test_basement_only_trigger_limits_scope_to_that_floor():
    reqs = RQ.evaluate(prof("乙-6", [("B1", -1, 200), ("1F", 1, 100), ("2F", 2, 100)], stories=2))
    assert by(reqs, "15").floors == ["B1"] and by(reqs, "15").law == ["D0120029/15/1/4"]
