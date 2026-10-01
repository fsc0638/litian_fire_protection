"""法定表格（data/tables/*.yaml）。第 18 條預期值＝開發者與獨立轉錄者各自轉錄後逐格一致的結果；第 157 條由條文解析。"""

import copy
import json
from pathlib import Path

import pytest

from litian.lawdb import tables as T

ROOT = Path(__file__).resolve().parents[1]
TDIR = ROOT / "data" / "tables"


@pytest.fixture(scope="module")
def tbl():
    return {t["node_id"]: t for t in T.load_tables(TDIR)}


def test_box_rows_splits_cells_by_bar_not_position():
    text = ("┌─┬──┐\n│甲│乙乙│\n├─┼──┤\n│1 │避難│\n│  │梯、│\n│  │滑臺│\n└─┴──┘")
    assert T.box_rows(text) == [[["甲"], ["乙乙"]], [["1"], ["避難", "梯、", "滑臺"]]]


def test_article_18_table(tbl):
    t = tbl["D0120029/18"]
    assert t["columns"] == ["水霧", "泡沫", "二氧化碳或惰性氣體", "鹵化烴", "乾粉"]   # 二氧化碳與惰性氣體同一欄
    marks = {r["no"]: r["marks"] for r in t["rows"]}
    assert marks[1] == ["泡沫", "乾粉"]                                   # 屋頂直昇機停機場
    assert marks[2] == ["泡沫", "乾粉"]                                   # 飛機修理廠、飛機庫
    assert marks[3] == marks[4] == t["columns"]                          # 汽車修理廠、停車空間；機械式停車
    assert marks[5] == ["水霧", "二氧化碳或惰性氣體", "鹵化烴", "乾粉"]       # 發電機室、變壓器室（無泡沫）
    assert marks[6] == marks[7] == ["二氧化碳或惰性氣體", "鹵化烴", "乾粉"]   # 鍋爐房廚房；電信機械室
    assert len(t["notes"]) == 5 and t["notes"][4].endswith("不得設置二氧化碳滅火設備。")
    assert t["status"] == "draft" and t["source"]["sha256"].startswith("da838a8c")


def test_article_157_yaml_matches_current_law_text(tbl):
    nodes = {json.loads(l)["node_id"]: json.loads(l)
             for l in (ROOT / "data" / "lawdb" / "nodes.jsonl").read_text(encoding="utf-8").splitlines() if l}
    parsed = T.parse_157(nodes["D0120029/157"]["text"])
    t = tbl["D0120029/157"]
    assert parsed["columns"] == t["columns"] and parsed["rows"] == t["rows"]


def test_article_157_cells(tbl):
    t = tbl["D0120029/157"]
    assert t["columns"] == ["地下層", "第二層", "第三層、第四層或第五層", "第六層以上之樓層"]
    r = {x["no"]: x["cells"] for x in t["rows"]}
    assert r[1]["第二層"]["devices"] == ["避難梯", "避難橋", "緩降機", "救助袋", "滑臺"]
    assert r[2]["第二層"]["devices"] == ["避難梯", "避難橋", "避難繩索", "緩降機", "救助袋", "滑臺", "滑杆"]
    assert r[3]["第二層"]["raw"] == "同上" and r[3]["第二層"]["same_as_row"] == 2
    assert r[4]["第二層"].get("blank") and r[5]["地下層"].get("blank")
    flagged = [(no, c) for no, cells in r.items() for c, v in cells.items() if "review" in v]
    assert flagged == [(5, "第二層")]                   # 唯一「正上方是空白格」的「同上」


def test_validate_rejects_verified_without_signer(tbl):
    t = copy.deepcopy(tbl["D0120029/18"])
    t["status"] = "verified"
    with pytest.raises(ValueError):
        T.validate(t)
    t["verified_by"], t["verified_at"] = "某消防設備師（證號）", "2026-10-15"
    T.validate(t)


def test_validate_rejects_unknown_column(tbl):
    t = copy.deepcopy(tbl["D0120029/18"])
    t["rows"][0]["marks"] = ["二氧化碳"]                 # 不是表頭的欄名
    with pytest.raises(ValueError):
        T.validate(t)


def test_rows_text_for_search(tbl):
    s = T.rows_text(tbl["D0120029/18"])
    assert "室內停車空間" in s and "可選設：水霧、泡沫、二氧化碳或惰性氣體、鹵化烴、乾粉" in s


# ---- 多部分格式（parts）：表格＋公式＋配線圖，2026-10-01 新增 ----

MULTI = {
    "node_id": "D0120029/83-2", "citation": "設置標準第83條之2附表", "law_version": "20240424",
    "source": {"kind": "pdf"}, "status": "draft",
    "parts": [
        {"title": "所需滅火藥劑量", "kind": "formula", "formula": "W = V / S × ln(100 / (100 − C))",
         "variables": {"W": "防護空間所需藥劑量（kg）", "C": "設計濃度百分比"}},
        {"title": "比容積公式", "kind": "table", "columns": ["比容積公式"],
         "rows": [{"no": 1, "place": "IG-100", "cells": {"比容積公式": {"raw": "s=0.7997+0.00293t"}}}],
         "notes": ["本表為示意"]},
        {"title": "配線", "kind": "diagram", "columns": ["耐燃保護", "耐熱保護"],
         "rows": [{"no": 1, "place": "7.緊急廣播設備", "cells": {"耐燃保護": {"raw": "緊急電源—擴音機"}}}]},
    ],
    "notes": ["整條備註"],
}


def test_validate_multi_part_ok():
    T.validate(MULTI)


def test_validate_multi_part_errors():
    import copy
    bad = copy.deepcopy(MULTI)
    bad["parts"][0]["formula"] = ""
    with pytest.raises(ValueError, match="缺 formula"):
        T.validate(bad)
    bad = copy.deepcopy(MULTI)
    bad["parts"][1]["rows"][0]["cells"]["不存在"] = {"raw": "x"}
    with pytest.raises(ValueError, match="第 2 部分第 1 列有不存在的欄"):
        T.validate(bad)
    bad = copy.deepcopy(MULTI)
    bad["parts"][2]["kind"] = "picture"
    with pytest.raises(ValueError, match="kind 只能是"):
        T.validate(bad)
    bad = copy.deepcopy(MULTI)
    del bad["parts"]
    with pytest.raises(ValueError, match="columns／rows"):
        T.validate(bad)


def test_rows_text_multi_part():
    s = T.rows_text(MULTI)
    assert s.splitlines() == [
        "【所需滅火藥劑量】", "公式：W = V / S × ln(100 / (100 − C))", "W：防護空間所需藥劑量（kg）", "C：設計濃度百分比",
        "【比容積公式】", "IG-100 比容積公式：s=0.7997+0.00293t", "本表為示意",
        "【配線】", "7.緊急廣播設備 耐燃保護：緊急電源—擴音機",
        "整條備註"]


# ---- 2026-10-01 第二批：其餘 PDF 表格（兩輪獨立轉錄＋裁決）----

NEW = ["47", "57", "83", "83-2", "84", "97-2", "97-3", "97-5", "117", "133", "163", "164", "165", "183",
       "198", "201", "222", "236"]


def test_all_tables_load_and_are_drafts():
    ts = {t["node_id"]: t for t in T.load_tables()}
    assert len(ts) == 20
    for art in NEW:
        t = ts[f"D0120029/{art}"]
        assert t["status"] == "draft" and t["verified_by"] is None
        assert len(t["transcription"]) >= 3 and t["cross_check"].startswith("2026-10-01")
        assert t["source"]["kind"] == "pdf" and len(t["source"]["sha256"]) == 64
        assert T.rows_text(t).strip()


def test_article_198_symbols():
    t = {x["node_id"]: x for x in T.load_tables()}["D0120029/198"]
    part = t["parts"][0]
    cells = [(r["place"], c, v["raw"]) for r in part["rows"] for c, v in r["cells"].items()]
    assert len(part["rows"]) == 13 and len(part["columns"]) == 31
    assert sum(1 for *_, v in cells if v == "○") == 216
    assert [(p, c) for p, c, v in cells if v == "Δ"] == [("第四類公共危險物品", "第二種／自動撒水設備")]


def test_formulas_transcribed_without_added_multiplication():
    ts = {t["node_id"]: t for t in T.load_tables()}
    f = lambda nid: [p["formula"] for p in ts[nid]["parts"] if p.get("kind") == "formula"]
    assert f("D0120029/83-2") == ["W = (V / S) ln(100 / (100 − C))"]
    assert f("D0120029/97-3") == ["W = (V / S)(C / (100 − C))"]
