"""檢核報告匯出：可列印（存成 PDF）的 HTML，以及 Excel 可開的 CSV。

報告內容：檢核條件、應設設備判定、各樓層標示圖與缺失（含審核結果：接受／退回／未處理）、要補的資料彙總。
「退回」的缺失不列入缺失表，只在統計中註明筆數；未處理的照列並標「未審核」。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from html import escape

SEV = {"RED": "不符", "ORANGE": "需確認", "YELLOW": "資料不足", "BLUE": "建議"}
SEV_COLOR = {"RED": "#c5221f", "ORANGE": "#c25d00", "YELLOW": "#8a6d00", "BLUE": "#1a56c4"}
STATUS = {"REQUIRED": "應設", "NOT_REQUIRED": "未達門檻", "UNKNOWN": "無法判定"}
DECISION = {"accept": "接受", "reject": "退回", None: "未審核"}

CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font: 11pt/1.55 "Microsoft JhengHei", "Noto Sans TC", "PingFang TC", sans-serif; color: #1c1b19; margin: 0; background: #fff; }
.wrap { max-width: 900px; margin: 0 auto; padding: 20px; }
h1 { font-size: 18pt; margin: 0 0 4px; } h2 { font-size: 13pt; margin: 22px 0 8px; border-bottom: 2px solid #1c1b19; padding-bottom: 3px; }
h3 { font-size: 11.5pt; margin: 16px 0 6px; }
.muted { color: #5f5b53; } .small { font-size: 9pt; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; margin: 6px 0; }
th, td { border: 1px solid #c9c4b8; padding: 4px 6px; vertical-align: top; text-align: left; }
th { background: #f1efea; font-weight: 600; }
.sev { font-weight: 700; white-space: nowrap; }
.plan { width: 100%; border: 1px solid #c9c4b8; margin: 6px 0; }
.plan svg { width: 100%; height: auto; display: block; }
.box { border: 1px solid #c9c4b8; border-radius: 6px; padding: 8px 10px; margin: 8px 0; background: #faf9f6; }
.floor { break-before: page; }
.nobreak { break-inside: avoid; }
.toolbar { position: sticky; top: 0; background: #fff; padding: 8px 0; border-bottom: 1px solid #e2ded5; margin-bottom: 10px; }
@media print { .toolbar { display: none; } .wrap { padding: 0; } }
"""


def _law(ids, laws) -> str:
    return "、".join(escape(laws.get(i, {}).get("citation", i)) for i in ids)


def _decided(decisions: dict, file_id, key):
    return decisions.get(str(file_id), {}).get(key) or {}


def _rows(reviews: list[dict], decisions: dict):
    """攤平成（檔案, 樓層, 缺失, 審核, 是否全棟）清單；全棟缺失的樓層欄放「全棟」或圖號。"""
    for r in reviews:
        res = r.get("result") or {}
        b = res.get("building") or {}
        for f in b.get("findings", []):
            yield r, f.get("floor") or "全棟", f, _decided(decisions, r["file_id"], f.get("key")), True
        for fl in res.get("floors", []):
            for f in fl["findings"]:
                yield r, fl["label"], f, _decided(decisions, r["file_id"], f.get("key")), False


def build_html(case: dict, context: dict, occupancy: dict, reviews: list[dict], decisions: dict, laws: dict,
               svgs: dict, user: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    rows = list(_rows(reviews, decisions))
    kept = [x for x in rows if x[3].get("decision") != "reject"]
    rejected = len(rows) - len(kept)
    counts = {k: sum(1 for x in kept if x[2]["severity"] == k) for k in SEV}
    undecided = sum(1 for x in kept if not x[3].get("decision"))
    o = [f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
         f"<title>消防設備自審報告｜{escape(case['name'])}</title><style>{CSS}</style></head><body><div class='wrap'>"]
    o.append("<div class='toolbar'><button onclick='window.print()'>列印／另存 PDF</button></div>")
    o.append(f"<h1>消防安全設備圖說自審報告</h1><div class='muted'>案件 {case['id']}｜{escape(case['name'])}｜"
             f"產出 {now:%Y-%m-%d %H:%M}｜{escape(user)}</div>")
    o.append("<div class='box small'>本報告由系統依圖面自動檢核產生，判定依據為《各類場所消防安全設備設置標準》條文；"
             "圖面判讀（房間範圍、設備辨識）可能有誤，所有缺失須經消防設備師逐條確認後始得作為審查意見。</div>")

    # 檢核條件
    occ = context.get("occupancy")
    o.append("<h2>一、檢核條件</h2><table>")
    items = [
        ("場所類別（第 12 條）", f"{occ}　{occupancy.get(occ, '')}" if occ else "未填寫"),
        ("防火構造", {True: "是", False: "否"}.get(context.get("fireproof"), "依圖面註記判讀")),
        ("地上層數", context.get("stories") or "依平面圖推定"),
        ("建築物高度", f"{context['height']} m" if context.get("height") else "未填寫"),
        ("基地面積", f"{context['site_area']:,} ㎡" if context.get("site_area") else "未填寫"),
        ("無開口樓層", "、".join(context.get("no_opening") or []) or "無（未勾選）"),
        ("天花板（裝置面）高度", "、".join(f"{k} {v} m" for k, v in (context.get("ceiling_height") or {}).items()) or "未填寫"),
        ("樓地板面積覆寫", "、".join(f"{k} {v:,} ㎡" for k, v in (context.get("floor_area") or {}).items()) or "無（使用圖面判讀值）"),
    ]
    o += [f"<tr><th style='width:30%'>{escape(k)}</th><td>{escape(str(v))}</td></tr>" for k, v in items]
    o.append("</table>")

    # 應設設備
    for r in reviews:
        b = (r.get("result") or {}).get("building") or {}
        req = b.get("requirements") or []
        if not req:
            continue
        p = b.get("profile") or {}
        o.append(f"<h2>二、應設設備判定（{escape(r['name'])}）</h2>")
        if p:
            fls = "、".join(f"{f['label']} {f['area']:,.0f} ㎡" for f in p.get("floors", []))
            o.append(f"<div class='small muted'>地上 {p.get('stories') or '?'} 層｜總樓地板面積約 {p.get('total_area', 0):,.0f} ㎡｜{escape(fls)}</div>")
        o.append("<table><tr><th>設備</th><th>判定</th><th>理由</th><th>依據</th><th>需補資料</th></tr>")
        for q in req:
            o.append(f"<tr class='nobreak'><td>{escape(q['equipment'])}</td><td class='sev'>{STATUS[q['status']]}"
                     f"{'（' + '、'.join(q['floors']) + '）' if q.get('floors') else ''}</td><td>{escape(q['why'])}"
                     f"{''.join('<div class=small>※ ' + escape(n) + '</div>' for n in q.get('notes', []))}</td>"
                     f"<td class='small'>{_law(q['law'], laws)}</td><td class='small'>{escape('；'.join(q.get('missing', [])))}</td></tr>")
        o.append("</table>")
        break

    # 統計
    o.append("<h2>三、缺失統計</h2><table><tr>" + "".join(f"<th>{v}</th>" for v in SEV.values())
             + "<th>未審核</th><th>已退回（不列入）</th></tr><tr>"
             + "".join(f"<td>{counts[k]}</td>" for k in SEV) + f"<td>{undecided}</td><td>{rejected}</td></tr></table>")

    def table(items):
        out = ["<table><tr><th>#</th><th>嚴重度</th><th>缺失與判定理由</th><th>改善建議／要補的資料</th><th>依據</th><th>審核</th></tr>"]
        for _r, _fl, f, d, _b in items:
            miss = "<div><b>要補：</b>" + escape("；".join(f["missing"])) + "</div>" if f.get("missing") else ""
            note = f"<div class='small'>{escape(d.get('note') or '')}</div>" if d.get("note") else ""
            out.append(f"<tr class='nobreak'><td>{f['no']}</td><td class='sev' style='color:{SEV_COLOR[f['severity']]}'>{SEV[f['severity']]}</td>"
                       f"<td><b>{escape(f['title'])}</b><div class='small'>{escape(f['why'])}</div></td>"
                       f"<td class='small'>{escape(f['fix'])}{miss}</td><td class='small'>{_law(f['law'], laws)}</td>"
                       f"<td class='small'>{DECISION[d.get('decision')]}{note}</td></tr>")
        out.append("</table>")
        return "".join(out)

    o.append("<h2>四、全棟與系統圖缺失</h2>")
    bitems = [x for x in kept if x[4]]
    o.append(table(bitems) if bitems else "<p class='muted'>無。</p>")
    for r in reviews:
        for fl in (r.get("result") or {}).get("floors", []):
            items = [x for x in kept if x[0] is r and not x[4] and x[1] == fl["label"]]
            if not fl["findings"] and not fl["equipment"]:
                continue
            o.append(f"<section class='floor'><h2>{escape(fl['label'])}｜{escape(fl.get('number') or '')} {escape(fl['title'])}</h2>"
                     f"<div class='small muted'>{escape(r['name'])}｜樓地板約 {fl['area']:,.0f} ㎡</div>")
            svg = svgs.get((r["file_id"], fl["label"]))
            if svg:
                o.append(f"<div class='plan'>{svg}</div>")
            o.append(table(items) if items else "<p class='muted'>本層未列缺失。</p>")
            notes = fl.get("notes") or []
            if notes:
                o.append("<div class='small muted'>" + "".join(f"<div>※ {escape(n['text'])}（{_law(n['law'], laws)}）</div>" for n in notes) + "</div>")
            o.append("</section>")

    missing = sorted({m for x in kept for m in x[2].get("missing", [])})
    o.append("<h2>五、要補的資料</h2>" + ("<ul>" + "".join(f"<li>{escape(m)}</li>" for m in missing) + "</ul>" if missing else "<p class='muted'>無。</p>"))
    o.append("</div></body></html>")
    return "".join(o)


def build_csv(case: dict, reviews: list[dict], decisions: dict, laws: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["案件", "檔案", "樓層", "編號", "嚴重度", "類別", "缺失", "判定理由", "改善建議", "要補的資料", "依據", "審核", "審核備註", "識別碼"])
    for r, fl, f, d, _b in _rows(reviews, decisions):
        w.writerow([case["name"], r["name"], fl, f["no"], SEV[f["severity"]], f["category"], f["title"], f["why"], f["fix"],
                    "；".join(f.get("missing", [])), "、".join(laws.get(i, {}).get("citation", i) for i in f["law"]),
                    DECISION[d.get("decision")], d.get("note") or "", f.get("key", "")])
    return "﻿" + buf.getvalue()                    # 加 BOM，Excel 才認得 UTF-8
