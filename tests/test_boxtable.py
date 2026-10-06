"""條文方框字元表格 → 區塊（文字／表格／原樣）：全部法規庫表格都要解析成功、字元不漏、格子拼得成長方形；
API 的條文、檢索、問答來源、工作台引用條文都要帶上區塊。不連資料庫：條文用 data/lawdb/nodes.jsonl 假造。"""

import json
import random
from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from litian import api
from litian.lawdb import boxtable as BT

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _nodes() -> dict[str, dict]:
    rows = [json.loads(l) for l in (ROOT / "data" / "lawdb" / "nodes.jsonl").read_text(encoding="utf-8").splitlines() if l]
    return {r["node_id"]: r for r in rows}


def _text(node_id: str) -> str:
    return _nodes()[node_id]["text"]


TABLE_NODES = sorted(nid for nid, n in _nodes().items() if BT.has_table(n["text"]))


def _chars(s: str) -> list[str]:
    return [ch for ch in s if not ch.isspace() and ch not in BT.BOX]


def _runs(text: str) -> tuple[list[str], list[list[str]]]:
    """（表格以外的字元, 各表格的字元）——測試自己切，不用被測的程式。連續兩行以上的方框行才算表格。"""
    lines = text.split("\n")
    tab = [bool(l.strip()) and l.lstrip()[0] in BT.BOX for l in lines]
    other, tables, i = [], [], 0
    while i < len(lines):
        j = i
        while j < len(lines) and tab[j] == tab[i]:
            j += 1
        chars = [c for l in lines[i:j] for c in _chars(l)]
        if tab[i] and j - i >= 2:
            tables.append(chars)
        else:
            other += chars
        i = j
    return other, tables


def _subsequence(sub: list[str], seq: list[str]) -> bool:
    it = iter(seq)
    return all(ch in it for ch in sub)


def _check_grid(t: dict) -> int:
    """依 HTML 表格的排法放格子（跳過上面跨列佔掉的位置）：不能重疊、不能超出底部、每一列欄數相同。回欄數。"""
    rows, occ = t["rows"], set()
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            assert cell["rowspan"] >= 1 and cell["colspan"] >= 1 and isinstance(cell["text"], str)
            while (r, c) in occ:
                c += 1
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    assert (r + dr, c + dc) not in occ, f"格子重疊：第 {r} 列"
                    occ.add((r + dr, c + dc))
            c += cell["colspan"]
    assert all(r < len(rows) for r, _ in occ), "跨列超出表格底部"
    ncol = max(c for _, c in occ) + 1
    for r in range(len(rows)):
        assert {c for rr, c in occ if rr == r} == set(range(ncol)), f"第 {r} 列欄數不齊"
    assert 1 <= t["header_rows"] <= len(rows)
    return ncol


def _texts(row: list[dict]) -> list[str]:
    return [c["text"] for c in row]


# ---------------- 法規庫全部表格 ----------------

def test_all_law_tables_parse_without_fallback():
    assert len(TABLE_NODES) >= 60
    for nid in TABLE_NODES:
        bs = BT.blocks(_text(nid))
        assert [b for b in bs if b["type"] == "pre"] == [], nid
        assert any(b["type"] == "table" for b in bs), nid


def test_all_law_tables_keep_every_character_in_order():
    for nid in TABLE_NODES:
        text = _text(nid)
        other, tables = _runs(text)
        bs = BT.blocks(text)
        assert [c for b in bs if b["type"] == "text" for c in _chars(b["text"])] == other, nid
        out = [b for b in bs if b["type"] == "table"]
        assert len(out) == len(tables), nid
        for t, src in zip(out, tables):
            cells = [cell["text"] for row in t["rows"] for cell in row]
            assert Counter(c for s in cells for c in _chars(s)) == Counter(src), nid
            for s in cells:
                assert _subsequence(_chars(s), src), (nid, s)


def test_all_law_tables_fill_a_rectangle():
    for nid in TABLE_NODES:
        for b in BT.blocks(_text(nid)):
            if b["type"] == "table":
                _check_grid(b)


def test_text_blocks_keep_lines_and_drop_separator_blank_lines():
    bs = BT.blocks("前言\n  縮排第二行\n\n┌─┐\n│甲│\n└─┘\n\n註：後記\n")
    assert bs[0] == {"type": "text", "text": "前言\n  縮排第二行"}
    assert bs[1]["type"] == "table" and bs[1]["rows"] == [[{"text": "甲", "rowspan": 1, "colspan": 1}]]
    assert bs[2] == {"type": "text", "text": "註：後記"}


# ---------------- 個別表格的確切結構 ----------------

def test_detector_table_120():
    bs = BT.blocks(_text("D0120029/120/1/2"))
    assert [b["type"] for b in bs] == ["text", "table"]
    assert bs[0]["text"].startswith("二、各探測區域應設探測器數")
    t = bs[1]
    assert _check_grid(t) == 7 and len(t["rows"]) == 9
    assert t["header_rows"] == 2                                     # 裝置面高度＋建築物構造兩列表頭
    assert [(c["text"], c["rowspan"], c["colspan"]) for c in t["rows"][0]] == [
        ("裝置面高度", 1, 3), ("未滿四公尺", 1, 2), ("四公尺以上未滿八公尺", 1, 2)]
    assert _texts(t["rows"][1]) == ["建築物構造", "防火構造建築物", "其他建築物", "防火構造建築物", "其他建築物"]
    assert t["rows"][2][0] == {"text": "探測器種類及有效探測範圍（平方公尺）", "rowspan": 7, "colspan": 1}
    assert t["rows"][2][1] == {"text": "差動式局限型", "rowspan": 2, "colspan": 1}
    assert _texts(t["rows"][2])[2:] == ["一種", "90", "50", "45", "30"]
    assert _texts(t["rows"][3]) == ["二種", "70", "40", "35", "25"]
    assert t["rows"][4][0] == {"text": "補償式局限型", "rowspan": 2, "colspan": 1}
    assert t["rows"][6][0] == {"text": "定溫式局限型", "rowspan": 3, "colspan": 1}   # 「定溫式」「局限型」跨過兩條部分分隔線
    assert _texts(t["rows"][8]) == ["二種", "20", "15", "–", "–"]
    for row in t["rows"][2:]:                                        # 每列最後 4 格是數值欄
        assert all(c["colspan"] == 1 for c in row[-4:])


def test_exit_light_table_146_2():
    t = BT.blocks(_text("D0120029/146-2/1/1"))[1]
    shape = [[(c["text"], c["rowspan"], c["colspan"]) for c in row] for row in t["rows"]]
    assert shape == [
        [("區分", 1, 3), ("步行距離（公尺）", 1, 1)],
        [("出口標示燈", 5, 1), ("A 級", 2, 1), ("未顯示避難方向符號者", 1, 1), ("六十", 1, 1)],
        [("顯示避難方向符號者", 1, 1), ("四十", 1, 1)],
        [("B 級", 2, 1), ("未顯示避難方向符號者", 1, 1), ("三十", 1, 1)],
        [("顯示避難方向符號者", 1, 1), ("二十", 1, 1)],
        [("C 級", 1, 2), ("十五", 1, 1)],
        [("避難方向指示燈", 3, 1), ("A 級", 1, 2), ("二十", 1, 1)],
        [("B 級", 1, 2), ("十五", 1, 1)],
        [("C 級", 1, 2), ("十", 1, 1)],
    ]
    assert t["header_rows"] == 1                                     # 「區分」跨欄但底下是資料列


def test_simple_table_70():
    one = lambda s: {"text": s, "rowspan": 1, "colspan": 1}
    assert BT.blocks(_text("D0120029/70/1")) == [
        {"type": "text", "text": "固定式泡沫滅火設備之泡沫放出口，依泡沫膨脹比，就下表選擇設置之："},
        {"type": "table", "header_rows": 1, "rows": [
            [one("膨脹比種類"), one("泡沫放出口種類")],
            [one("膨脹比二十以下（低發泡）"), one("泡沫噴頭或泡水噴頭")],
            [one("膨脹比八十以上一千以下（高發泡）"), one("高發泡放出口")]]},
    ]


def test_multiline_header_cells_99():
    t = BT.blocks(_text("D0120029/99/1/1"))[1]
    assert _texts(t["rows"][0]) == ["乾粉藥劑種類", "第一種乾粉（主成份碳酸氫鈉", "第二種乾粉（主成份碳酸氫鉀",
                                    "第三種乾粉（主成份磷酸二氫銨）", "第四種乾粉（主成份碳酸氫鉀及尿素化合物）"]
    assert _texts(t["rows"][2]) == ["每平方公尺開口部所需追加滅火藥劑量（㎏／㎡）", "4.5", "2.7", "2.7", "1.8"]
    assert _texts(t["rows"][1])[0] == "每立方公尺防護區域所需滅火藥劑量（㎏／m³）"   # ³ 佔 1 格，欄位才對得齊


def test_header_rows_follow_spans():
    heads = lambda nid: [b["header_rows"] for b in BT.blocks(_text(nid)) if b["type"] == "table"]
    assert heads("D0120029/213/1/1") == [3]                          # 左上角格子跨 3 列
    assert heads("D0120029/122/1/4") == [2]
    assert heads("D0120029/157/1") == [1]                            # 第一欄跨欄的「設置場所應設數量」底下是資料
    assert heads("D0120029/213/1/3") == [2]                          # 「Ⅰ型」下分「泡沫水溶液量／放出率」


def test_dash_inside_cell_is_text_not_rule():
    t = BT.blocks(_text("D0120029/213/1/3"))[1]
    assert _texts(t["rows"][2]) == ["一六○", "八", "二四○", "八", "─", "─", "─", "─", "二四○", "八"]


def test_wrapped_ascii_words_keep_a_space():
    t = BT.blocks("┌────┬──┐\n│（Double│中文│\n│deck）  │交換│\n└────┴──┘")[0]
    assert _texts(t["rows"][0]) == ["（Double deck）", "中文交換"]


# ---------------- 沒有表格、壞掉的表格 ----------------

def test_has_table_false_for_plain_text():
    assert not BT.has_table("一、十層以下建築物之樓層。\n二、地下層。")
    assert not BT.has_table("")
    assert not BT.has_table(None)
    formula = "在下列公式求得值以下者。\n                 ┌──────────\n      ｒ＝３／４ │ＱＳα／π\n      ｒ值：距離"
    assert not BT.has_table(formula)                                 # 只有一行方框字元（根號上橫線）不是表格
    assert BT.blocks(formula) == [{"type": "text", "text": formula}]
    assert BT.has_table(_text("D0120029/70/1"))


@pytest.mark.parametrize("raw", [
    "┌──┬──┐\n│甲甲│乙乙│\n│    └──┤\n│丙丙丙丙丙│\n└──┴──┘",            # L 形儲存格
    "┌─┐\n│甲│ 註\n└─┘",                                               # 右框外有文字
    "┌──┐\n│甲│\n└──┘",                                                 # 右框錯位（框寬 4、字寬 2）
    "┌──┬──┐\n│甲甲│乙 │\n└──┴──┘",                                     # 中間直線錯位（半形字少一格）
    "┌──┐\n└──┘",                                                       # 沒有任何儲存格
    "│││\n││",                                                           # 只有直線
])
def test_malformed_table_falls_back_to_pre(raw):
    bs = BT.blocks("前言\n" + raw + "\n後記")
    assert bs == [{"type": "text", "text": "前言"}, {"type": "pre", "text": raw}, {"type": "text", "text": "後記"}]


def test_mutated_law_tables_never_raise_and_keep_characters():
    """把真的法規表格隨機改壞幾個字（換字、刪字、插字）：不能丟錯、字元不能少，解析得出的表格仍要拼得成長方形。"""
    rnd = random.Random(20261006)
    alphabet = BT.BOX * 2 + "  甲a1–³"
    kinds = Counter()
    for _ in range(300):
        chars = list(_text(rnd.choice(TABLE_NODES)))
        box_at = [i for i, ch in enumerate(chars) if ch in BT.BOX]
        for _ in range(rnd.randint(1, 3)):
            i = rnd.choice(box_at)
            op = rnd.randrange(3)
            if op == 0:
                chars[i] = rnd.choice(alphabet)
            elif op == 1:
                chars[i] = ""
            else:
                chars[i] += rnd.choice(alphabet)
        text = "".join(chars)
        out = []
        for b in BT.blocks(text):
            kinds[b["type"]] += 1
            if b["type"] == "table":
                _check_grid(b)
                out += [c for row in b["rows"] for cell in row for c in _chars(cell["text"])]
            else:
                out += _chars(b["text"])
        assert Counter(out) == Counter(_chars(text)), text
    assert kinds["table"] > 0 and kinds["pre"] > 0                   # 兩條路都有走到


# ---------------- API：條文、檢索、問答來源、工作台引用條文 ----------------

LAWS = {"D0120029": {"name": "各類場所消防安全設備設置標準", "modified": "20240424"}}
USER = {"id": 1, "username": "amy", "role": "reviewer"}


@pytest.fixture
def law_client(monkeypatch):
    nodes = _nodes()

    def fake_all(sql, *a):
        if "WHERE parent_id = %s" in sql:
            return [n for n in nodes.values() if n["parent_id"] == a[0]]
        return []
    monkeypatch.setattr(api, "_node", lambda nid: nodes.get(nid))
    monkeypatch.setattr(api, "_one", lambda sql, *a: None)
    monkeypatch.setattr(api, "_all", fake_all)
    monkeypatch.setattr(api, "_law_names", lambda: LAWS)
    # 檢索路線：只讓關鍵字路線回第 120 條第 1 項第 2 款
    monkeypatch.setenv("MEILI_URL", "http://example.invalid")
    monkeypatch.setenv("MEILI_MASTER_KEY", "k")
    for k in ("OPENAI_API_KEY", "ASK_ACCESS_CODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(api.S, "keyword", lambda *a, **k: ["D0120029/120/1/2"])
    monkeypatch.setattr(api.S, "occupancy_route", lambda *a, **k: [])
    monkeypatch.setattr(api.S, "legend_route", lambda *a, **k: [])
    monkeypatch.setattr(api, "_known_bigrams", lambda: frozenset())
    monkeypatch.setattr(api, "_table_index", lambda: ())
    monkeypatch.setattr(api, "_save_log", lambda rec: None)
    return TestClient(api.app)


def test_node_api_adds_blocks_for_node_article_and_children(law_client):
    d = law_client.get("/api/law/nodes/D0120029/120/1/2").json()
    assert d["text"] == _text("D0120029/120/1/2")                    # 原文照舊
    assert d["blocks"] == BT.blocks(_text("D0120029/120/1/2"))
    assert d["article_text"] == _text("D0120029/120")
    assert d["article_blocks"] == BT.blocks(_text("D0120029/120"))
    art = law_client.get("/api/law/nodes/D0120029/120").json()
    assert art["blocks"][1]["type"] == "table" and "article_text" not in art and "article_blocks" not in art
    para = law_client.get("/api/law/nodes/D0120029/120/1").json()
    assert "blocks" not in para and para["article_blocks"] == BT.blocks(_text("D0120029/120"))
    kids = {c["node_id"]: c for c in para["children"]}
    assert kids["D0120029/120/1/2"]["blocks"] == d["blocks"] and "article_blocks" not in kids["D0120029/120/1/2"]
    assert "blocks" not in kids["D0120029/120/1/1"]


def test_node_api_plain_text_has_no_blocks(law_client):
    d = law_client.get("/api/law/nodes/D0120029/17/1/1").json()
    assert "blocks" not in d and "article_blocks" not in d and d["text"] == _text("D0120029/17/1/1")


def test_search_results_carry_blocks(law_client):
    d = law_client.get("/api/law/search", params={"q": "探測器數量"}).json()
    r = d["results"][0]
    assert r["node_id"] == "D0120029/120/1/2"
    assert r["blocks"] == BT.blocks(_text("D0120029/120/1/2")) and r["article_blocks"][1]["type"] == "table"


def test_ask_sources_event_carries_blocks(law_client):
    r = law_client.post("/api/law/ask", json={"question": "探測器數量"})
    events = [(e.split("\n")[0][7:], json.loads(e.split("\n")[1][6:])) for e in r.text.strip().split("\n\n")]
    assert events[0][0] == "sources"
    src = events[0][1]["sources"][0]
    assert src["node_id"] == "D0120029/120/1/2" and src["blocks"][1]["type"] == "table"
    assert src["article_blocks"] == BT.blocks(_text("D0120029/120"))


def test_blocks_are_memoized():
    t = _text("D0120029/160/1")
    assert api._blocks(t) is api._blocks(t)
    assert api._blocks("沒有表格的條文") is None and api._blocks(None) is None


def test_review_bundle_sends_full_text_and_blocks(monkeypatch):
    @contextmanager
    def conn():
        yield NS()
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    monkeypatch.setattr(api.AU, "session_user", lambda c, token: USER if token == "good-token" else None)
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"id": a[0], "name": "案", "created_by": "amy", "created_at": "t"}
                        if "FROM review_case" in sql else None)
    long_id, plain_id = "D0120029/160/1", "D0120029/17/1/1"
    result = {"building": {"findings": [{"law": [long_id]}], "requirements": [], "notes": [{"law": [plain_id]}]}}

    def fake_all(sql, *a):
        if "FROM file_review" in sql:
            return [{"file_id": 7, "name": "F-101.dxf", "status": "done", "error": None, "result": result,
                     "svg_dir": None, "created_at": "t", "cad_state": None}]
        assert "law_node" in sql and a[0] == sorted([long_id, plain_id])
        return [{"node_id": i, "citation": _nodes()[i]["citation"], "text": _text(i)} for i in a[0]]
    monkeypatch.setattr(api, "_all", fake_all)
    monkeypatch.setattr(api.DS, "get_context", lambda c, cid: {})
    monkeypatch.setattr(api.DS, "decisions", lambda c, cid: {})
    client = TestClient(api.app)
    client.cookies.set("__Host-fr_session", "good-token")
    laws = client.get("/api/cases/3/reviews").json()["laws"]
    assert len(_text(long_id)) > 600 and laws[long_id]["text"] == _text(long_id)    # 不再截斷成 600 字
    assert laws[long_id]["blocks"] == BT.blocks(_text(long_id))
    assert laws[plain_id] == {"citation": _nodes()[plain_id]["citation"], "text": _text(plain_id)}
