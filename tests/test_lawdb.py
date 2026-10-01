"""法規庫測試。預期值都來自 2026-09-30 研究階段以另一種方法（MOJ 網頁＋官方 API＋消防署網頁三方比對）查證過的事實。"""

import json
from pathlib import Path

import pytest

from litian.lawdb.numerals import to_cn, to_int
from litian.lawdb.occupancy import build_occupancy
from litian.lawdb.parse import parse_law
from litian.lawdb.xref import extract_xrefs

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"


@pytest.mark.parametrize("cn,n", [("一", 1), ("十", 10), ("十二", 12), ("二十一", 21), ("一百十", 110),
                                  ("一百九十二", 192), ("二百三十九", 239), ("一千", 1000), ("１８", 18), ("7", 7)])
def test_numerals(cn, n):
    assert to_int(cn) == n


@pytest.mark.parametrize("n", [1, 10, 12, 20, 101, 110, 192, 239, 1000, 1005])
def test_numerals_roundtrip(n):
    assert to_int(to_cn(n)) == n


@pytest.fixture(scope="module")
def corpus():
    if not (RAW / "D0120029.xml").exists():
        pytest.skip("先執行 python -m litian.lawdb.build 取得官方資料")
    laws, nodes = {}, []
    for p in sorted(RAW.glob("D*.xml")):
        law, ns, warns = parse_law(p)
        assert warns == [], warns
        laws[law.pcode] = law
        nodes += ns
    return laws, nodes, {n.node_id: n for n in nodes}


def test_article_counts(corpus):
    laws, _, _ = corpus
    std, act = laws["D0120029"], laws["D0120001"]
    assert (std.article_count, std.deleted_count, std.modified) == (266, 7, "20240424")
    assert (act.article_count, act.deleted_count) == (80, 1)


def test_level_counts_match_research(corpus):
    _, nodes, _ = corpus
    std = [n for n in nodes if n.pcode == "D0120029"]
    assert sum(n.level == "item" for n in std) == 836        # 研究：款「一、」836 行
    assert sum(n.level == "subitem" for n in std) == 310     # 研究：目「（一）」310 行


def test_article_12_structure(corpus):
    _, _, by = corpus
    art = by["D0120029/12"]
    assert len(art.children) == 1                            # 1 項
    items = by["D0120029/12/1"].children
    assert len(items) == 6                                   # 6 款
    assert sum(len(by[i].children) for i in items) == 28     # 28 目
    assert by["D0120029/12/1/1/3"].text.startswith("（三）觀光旅館、飯店、旅館、招待所")
    assert by["D0120029/12/1/1/3"].citation == "設置標準第12條第1款第3目"   # 單一項條文省略「第1項」
    assert by["D0120029/12"].chapter == "第二編 消防設計"


def test_article_17_paragraphs(corpus):
    _, _, by = corpus
    assert len(by["D0120029/17"].children) == 3
    assert len(by["D0120029/17/1"].children) == 9
    assert by["D0120029/17/3"].citation == "設置標準第17條第3項"
    assert by["D0120029/17/3"].text.startswith("第一項第九款所定場所")


def test_formula_and_table_lines_are_continuations(corpus):
    _, _, by = corpus
    assert len(by["D0120029/183/1"].children) == 9           # 「全揚程＝…」不是新的一項
    assert "H=h1+h2+h3+60m" in by["D0120029/183/1/1"].text
    assert "Q=8-6×a/A" in by["D0120029/83/1/2/2"].text
    assert any(by[c].has_table for c in by["D0120029/157"].children)


def test_pdf_only_tables(corpus):
    _, nodes, by = corpus
    assert sum(1 for n in nodes if n.pdf_table_url) == 20
    assert by["D0120029/18"].pdf_table_url.endswith("FileId=0000366988")


def test_chapter_path(corpus):
    _, _, by = corpus
    assert by["D0120029/45"].chapter == "第三編 消防安全設計 > 第一章 滅火設備 > 第三節 自動撒水設備"
    assert by["D0120029/97-1"].chapter.endswith("第六節之一 鹵化烴滅火設備")


def test_occupancy_codes(corpus):
    _, nodes, _ = corpus
    occ = {o.code: o for o in build_occupancy(nodes)}
    assert len(occ) == 29
    assert occ["甲-1"].text.startswith("電影片映演場所")
    assert occ["乙-12"].text == "幼兒園。"
    assert occ["丁-1"].text == "高度危險工作場所。"
    assert occ["戊-3"].node_id == "D0120029/12/1/5/3"
    assert occ["其他"].node_id == "D0120029/12/1/6"


def test_xrefs(corpus):
    _, nodes, _ = corpus
    xr = {}
    for x in extract_xrefs(nodes):
        xr.setdefault(x.src, []).append((x.raw, x.target))
    assert ("第六條第一項", "D0120001/6/1") in xr["D0120029/1/1"]                 # 「消防法（以下簡稱本法）第六條」
    assert xr["D0120029/17/1/1"] == [("第十二條第一款第一目", "D0120029/12/1/1/1"),
                                     ("同款", "D0120029/12/1/1"),
                                     ("第二款第一目", "D0120029/12/1/2/1")]
    assert ("第一項第九款", "D0120029/17/1/9") in xr["D0120029/17/3"]
    assert ("第二十二條", "D0120001/22") in xr["D0120002/11/1"]                  # 「本法第二十一條及第二十二條」
    assert ("前條", "D0120029/21") in xr["D0120029/22/1"]


def test_build_report_exists():
    p = RAW.parent / "lawdb" / "build_report.json"
    if not p.exists():
        pytest.skip("尚未建置")
    r = json.loads(p.read_text(encoding="utf-8"))
    assert r["warnings"] == []
    assert r["source_update"]


def test_failed_build_keeps_previous_legend(tmp_path, monkeypatch):
    """建置中途失敗時，不可先刪掉既有的圖例圖（2026-10-01 雲主機連不上消防署網站時發生）。"""
    from litian.lawdb import build as B
    out = tmp_path / "lawdb"
    (out / "legend").mkdir(parents=True)
    keep = out / "legend" / "FL019489_A3_001.png"
    keep.write_bytes(b"png")
    monkeypatch.setattr(B, "fetch_all", lambda raw, refresh: {"_update": {}})

    def unreachable(*a, **k):
        raise TimeoutError("連不上消防署網站")
    monkeypatch.setattr(B.nfa, "fetch", unreachable)
    with pytest.raises(Exception):
        B.build(tmp_path / "raw", out)
    assert keep.exists()
