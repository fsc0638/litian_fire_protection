"""法規問答頁的排版純函式：條文 blocks 轉表格、AI 回答的 Markdown（表格、標題、分隔線）。
用 node 跑頁面裡標記段落的純函式；沒有 node 就略過。"""

import json
import re
import shutil
import subprocess

import pytest

from litian import api


def _run_js(tmp_path, data, body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，略過前端純函式測試")
    html = api.WEB_INDEX.read_text(encoding="utf-8")
    funcs = html[html.index("// ---------- 排版純函式（"):html.index("// ---------- 排版純函式結束")]
    js = tmp_path / "t.js"
    js.write_text(funcs + "\nconst D = " + json.dumps(data, ensure_ascii=False) + ";\n"
                  "const chip = (x) => '<c>' + Number(x) + '</c>';\n"
                  "process.stdout.write(JSON.stringify((() => {" + body + "})()));\n", encoding="utf-8")
    r = subprocess.run([node, str(js)], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8"))


def _md(tmp_path, text: str) -> str:
    return _run_js(tmp_path, text, "return mdHtml(D, chip);")


def _c(text, rowspan=1, colspan=1):
    return {"text": text, "rowspan": rowspan, "colspan": colspan}


# ---------- 條文 blocks ----------

def test_blocks_table_spans_and_escape(tmp_path):
    blocks = [{"type": "text", "text": "\n依下表規定：<注意>\n　　一、第一款\n"},
              {"type": "table", "header_rows": 2, "rows": [
                  [_c("離開距離（㎜）", colspan=2), _c("電壓（KV）", rowspan=2)],
                  [_c("最  低"), _c("標  準")],
                  [_c("150"), _c("250"), _c("7 以下")],
                  [_c("A&B", rowspan=2), _c("<x>"), _c("2.5")],
                  [_c("\"q\""), _c("'s'")]]},
              {"type": "pre", "text": "┌─┐\n│<│\n└─┘"},
              {"type": "text", "text": "   \n"}]
    h = _run_js(tmp_path, blocks, "return blocksHtml(D);")
    assert h.startswith('<div class="blocks">') and h.endswith("</div>")
    # 文字：跳脫、保留換行與全形縮排，頭尾空行拿掉；全空白的文字塊不輸出
    assert '<p class="text">依下表規定：&lt;注意&gt;\n　　一、第一款</p>' in h and h.count('<p class="text">') == 1
    # 表格：前兩列是表頭 th，跨欄跨列照抄，跨度 1 不寫屬性
    tbl = h[h.index('<div class="tbl"><table class="law flow">'):h.index("</table></div>")]
    assert tbl.count("<tr>") == 5 and tbl.count("<th") == 4 and tbl.count("<td") == 8
    assert '<th colspan="2">離開距離（㎜）</th><th rowspan="2">電壓（KV）</th>' in tbl
    assert "<tr><th>最  低</th><th>標  準</th></tr>" in tbl
    assert '<td rowspan="2">A&amp;B</td><td>&lt;x&gt;</td><td>2.5</td>' in tbl
    assert "<td>&quot;q&quot;</td><td>&#39;s&#39;</td>" in tbl
    assert 'rowspan="1"' not in tbl and 'colspan="1"' not in tbl and "<x>" not in h
    # 拆不了的方框表格：等寬原樣、跳脫
    assert '<pre class="box">┌─┐\n│&lt;│\n└─┘</pre>' in h


def test_blocks_bad_spans_and_empty(tmp_path):
    got = _run_js(tmp_path, {"t": [{"type": "table", "header_rows": 0, "rows": [[
        {"text": "a", "rowspan": "2\" onclick=\"x", "colspan": -3}, {"text": None}]]}]},
        "return { t: blocksHtml(D.t), empty: blocksHtml([]), none: blocksHtml(null) };")
    # 跨度只吃整數，怪值當 1；沒有表頭列就全是 td
    assert got["t"] == '<div class="blocks"><div class="tbl"><table class="law flow"><tbody><tr><td>a</td><td></td></tr></tbody></table></div></div>'
    assert got["empty"] == got["none"] == '<div class="blocks"></div>'


# ---------- AI 回答的 Markdown ----------

def test_md_table_with_cites_and_align(tmp_path):
    h = _md(tmp_path, "比較如下：\n| 項目 | 距離 | 依據 |\n|:---|---:|:--:|\n| 撒水頭 | 2.5 公尺 | [1] |\n"
                      "| **滅火器** | 20 公尺 | [2, 3] |\n\n以上。")
    assert h.startswith("<p>比較如下：</p>") and h.endswith("<p>以上。</p>")
    assert ('<thead><tr><th>項目</th><th style="text-align:right">距離</th><th style="text-align:center">依據</th></tr></thead>'
            in h)
    assert ('<tr><td>撒水頭</td><td style="text-align:right">2.5 公尺</td><td style="text-align:center"><c>1</c></td></tr>'
            in h)
    assert "<td><strong>滅火器</strong></td>" in h and "<c>2</c><c>3</c>" in h
    assert "|" not in h and "---" not in h


def test_md_table_loose_pipes_and_ragged_rows(tmp_path):
    h = _md(tmp_path, "項目 | 數值\n--- | ---\n甲 | 1 | 多一格\n乙 \\| 丙 | 2\n之後的文字")
    # 頭尾沒有 |、欄數參差：少的補空格，多的照樣顯示不丟內容；\| 是字面上的 |
    assert "<thead><tr><th>項目</th><th>數值</th><th></th></tr></thead>" in h
    assert "<tbody><tr><td>甲</td><td>1</td><td>多一格</td></tr><tr><td>乙 | 丙</td><td>2</td><td></td></tr></tbody>" in h
    # 沒有 | 的一行就結束表格
    assert h.endswith("</table></div><p>之後的文字</p>")
    h2 = _md(tmp_path, "| 項目 |\n| --- |\n只有表頭")
    assert h2 == ('<div class="tbl"><table class="law flow"><thead><tr><th>項目</th></tr></thead></table></div>'
                  "<p>只有表頭</p>")


def test_md_partial_table_while_streaming(tmp_path):
    full = "下表：\n| 場所 | 樓地板面積 |\n|---|---:|\n| KTV | 150 平方公尺以上 [1] |\n| 旅館 | 全部 |\n\n說明。"
    got = _run_js(tmp_path, full, """
        const out = [];
        for (let i = 0; i <= D.length; i++) out.push(mdHtml(D.slice(0, i), chip));
        return out;""")
    assert len(got) == len(full) + 1                      # 每一段前綴都算得出來，不會丟例外
    # 分隔列還沒到：表頭先當一般段落
    head = full.index("|---")
    assert got[head] == "<p>下表：<br>| 場所 | 樓地板面積 |</p>" and "<table" not in got[head]
    assert "<table" not in got[head + 1]                  # 只有「|」還不是分隔列
    assert "<table" in got[head + 2]                      # 「|-」就成表
    assert "<thead><tr><th>場所</th><th>樓地板面積</th></tr></thead>" in got[head + 2]
    # 正在寫的一列：有幾格畫幾格
    mid = full.index("150") + 2
    assert '<tr><td>KTV</td><td style="text-align:right">15</td></tr>' in got[mid]
    assert got[-1].endswith("</table></div><p>說明。</p>") and "<c>1</c>" in got[-1]


def test_md_headings_and_rules(tmp_path):
    h = _md(tmp_path, "# 結論\n應設置。\n## 依據 ##\n###### 小標\n#不是標題\n---\n* * *\n- 清單")
    assert h == ("<h3>結論</h3><p>應設置。</p><h4>依據</h4><h6>小標</h6><p>#不是標題</p><hr><hr>"
                 "<ul><li>清單</li></ul>")
    assert _md(tmp_path, "### 第 [2] 條 **重點**") == "<h5>第 <c>2</c> 條 <strong>重點</strong></h5>"


def test_md_numbers_never_rewritten(tmp_path):
    h = _md(tmp_path, "2.5 公尺以上\n1. 第一項\n2) 第二項\n3、第三項\n\n10. 第十項\n| 間距 |\n|---|\n| 2.5 公尺 |")
    assert h.startswith("<p>2.5 公尺以上</p><ol><li>第一項</li><li>第二項</li><li>第三項</li></ol>")
    assert '<ol start="10"><li>第十項</li></ol>' in h and "<td>2.5 公尺</td>" in h


def test_md_pipe_without_separator_stays_paragraph(tmp_path):
    h = _md(tmp_path, "A | B 都可以\n下一行\n\n全形｜不算\n|---|")
    assert h == "<p>A | B 都可以<br>下一行</p><p>全形｜不算<br>|---|</p>" and "<table" not in h


def test_source_card_uses_blocks():
    html = api.WEB_INDEX.read_text(encoding="utf-8")
    card = html[html.index("function sourceCard("):html.index("function tableHtml(")]
    assert "blocksHtml(s.blocks)" in card and "blocksHtml(s.article_blocks)" in card
    # 原文排版留著當次要按鈕；看表格（結構化表格）不變
    assert re.search(r'class="sub" data-act="raw">看原文排版<', card) and 'data-act="table">看表格<' in card
    assert "mdHtml(s, chip)" in html
