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
