"""消防署審查及查驗作業基準＋附件三圖例。預期值來自 2026-10-01 對照官方網頁與附件三 PDF（第 1、7 頁逐一比對 32 個圖例）。"""

import json
import re
from pathlib import Path

import pytest

from litian.lawdb import nfa
from litian.lawdb import search as S
from litian.lawdb.sources import BY_PCODE

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SRC = BY_PCODE["FL019489"]


@pytest.fixture(scope="module")
def rule():
    p = RAW / "nfa_FL019489.html"
    if not p.exists():
        pytest.skip("先執行 python -m litian.lawdb.build 取得消防署資料")
    law, nodes, warns = nfa.parse_rule(p, SRC)
    assert warns == []
    return law, {n.node_id: n for n in nodes}


def test_rule_metadata(rule):
    law, _ = rule
    assert law.name == "消防機關辦理建築物消防安全設備審查及查驗作業基準"
    assert law.modified == "20200417"                       # 民國 109/04/17
    assert "內授消字第1090821936號" in law.effective_note
    assert law.article_count == 12


def test_rule_structure(rule):
    _, by = rule
    assert len(by["FL019489/2/1"].children) == 5             # 圖說審查程序（一）～（五）
    assert len(by["FL019489/6/1"].children) == 6             # 竣工查驗程序（一）～（六）
    assert by["FL019489/2/1/3"].citation == "審查及查驗作業基準第2點第3款"
    assert by["FL019489/2/1/3"].text.startswith("（三）消防圖說審查不合規定者，消防機關應製作審查紀錄表")
    # 硬斷行要接回去，不能留空白或把「（以下」「簡稱」拆開
    assert "（以下簡稱消防圖說）" in by["FL019489/1/1"].text
    # 行內的「附件一、二、三、四、五」不可被誤切成款
    assert by["FL019489/2/1/5"].text.endswith("如附件一、二、三、四、五。")


@pytest.fixture(scope="module")
def legend(tmp_path_factory):
    p = RAW / "nfa_FL019489_A3.odt"
    if not p.exists():
        pytest.skip("先執行 python -m litian.lawdb.build 取得附件三")
    img = tmp_path_factory.mktemp("legend")
    nodes, entries, warns = nfa.parse_legend(p, SRC, nfa.ATTACHMENTS["FL019489"][0], img)
    assert warns == []
    return nodes, entries, img


def test_legend_counts(legend):
    _, entries, _ = legend
    assert len(entries) == 284
    cats = {e.category for e in entries}
    assert len(cats) == 17 and {"滅火器", "自動撒水設備", "火警自動警報設備", "排煙設備"} <= cats
    assert all(e.symbols for e in entries)


def test_legend_pairs_match_official_pdf(legend):
    _, entries, img = legend
    by = {e.seq: e for e in entries}
    expected = {1: "乾粉滅火器", 2: "大型滅火器", 3: "懸掛式自動滅火器", 4: "室內消防栓", 13: "室外消防栓",
                27: "密閉式撒水頭（向下型）", 159: "偵煙式局限型探測器（3種、定址式）",
                166: "試驗器（光電式分離型探測器用）", 173: "火焰式探測器", 174: "熱煙複合式探測器"}
    for seq, name in expected.items():
        assert by[seq].name == name
        assert (img / by[seq].symbols[0]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert by[1].note_images                                  # 乾粉滅火器的備註附有加註範例圖


def test_legend_nodes(legend):
    nodes, entries, _ = legend
    att = nodes[0]
    assert att.node_id == "FL019489/A3" and att.level == "attachment" and len(att.children) == 284
    n = next(x for x in nodes if x.node_id == "FL019489/A3/27")
    assert n.citation == "審查及查驗作業基準附件三「自動撒水設備」密閉式撒水頭（向下型）"
    assert re.fullmatch(r"[A-Za-z0-9_]+\.png", entries[0].symbols[0])   # API 只放行這種檔名


def test_legend_intent_routing():
    assert S.LEGEND_INTENT.search("撒水頭的圖例")
    assert S.LEGEND_INTENT.search("消防栓符號怎麼畫")
    assert not S.LEGEND_INTENT.search("高架儲存倉庫自動撒水設備")


def test_legend_index_context_is_category_only():
    by = {"FL019489/A3": {"node_id": "FL019489/A3", "level": "attachment", "parent_id": None,
                           "text": "附件三：消防圖說圖示範例（284 個圖例，17 類：滅火器、自動撒水設備…）"}}
    n = {"node_id": "FL019489/A3/27", "pcode": "FL019489", "article": "A3", "level": "legend", "path": [27],
         "parent_id": "FL019489/A3", "text": "密閉式撒水頭（向下型）", "chapter": "附件三：消防圖說圖示範例 > 自動撒水設備",
         "citation": "x"}
    doc = S.index_document(n, by, {"FL019489": ("消防機關…作業基準", "審查及查驗作業基準")})
    assert doc["context"] == "自動撒水設備" and doc["level"] == "legend"


def test_built_legend_artifact():
    if not (ROOT / "data" / "lawdb" / "build_report.json").exists():
        pytest.skip("尚未建置")
    p = ROOT / "data" / "lawdb" / "legend.json"
    assert p.exists(), "建置完成卻沒有 legend.json（2026-10-01 曾因補丁靜默失敗而漏寫）"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert len(entries) == 284
    missing = [f for e in entries for f in e["symbols"] + e["note_images"]
               if not (ROOT / "data" / "lawdb" / "legend" / f).exists()]
    assert missing == []
