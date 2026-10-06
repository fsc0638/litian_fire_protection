"""檢核報告匯出：可列印（存成 PDF）的 HTML，以及 Excel 可開的 CSV。

報告內容：檢核條件、應設設備判定、各樓層標示圖與缺失（含審核結果：接受／退回／未處理）、要補的資料彙總。
「退回」的缺失不列入缺失表，只在統計中註明筆數；未處理的照列並標「未審核」。
樓層圖：原圖已畫好的用 CAD 原樣圖（列印用整張圖＋向量缺失標示，編號與缺失表相同，退回的不畫），
否則用簡化標示圖；樓層頁改成 A4 橫向。
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime
from html import escape

SEV = {"RED": "不符", "ORANGE": "需確認", "YELLOW": "資料不足", "BLUE": "建議"}
SEV_COLOR = {"RED": "#c5221f", "ORANGE": "#c25d00", "YELLOW": "#8a6d00", "BLUE": "#1a56c4"}
STATUS = {"REQUIRED": "應設", "NOT_REQUIRED": "未達門檻", "UNKNOWN": "無法判定"}
DECISION = {"accept": "接受", "reject": "退回", None: "未審核"}
MARK = {"RED": "#d93025", "ORANGE": "#e8710a", "YELLOW": "#c9a100", "BLUE": "#1a73e8"}   # 與工作台原圖的標示同色
MARK_ORDER = {"BLUE": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}                                # 嚴重的畫在上層

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
/* 原圖：整張放得進一頁（橫向 A4 扣掉標題約 165 mm 高），標示層跟著圖的實際大小（容器縮到圖寬） */
.plan.cadplan { position: relative; width: fit-content; max-width: 100%; margin: 6px auto; line-height: 0; background: #fff; break-inside: avoid; }
.cadplan img { display: block; width: auto; height: auto; max-width: 100%; max-height: 150mm; }
.floor .plan ~ table { break-before: page; }           /* 樓層第一頁只放標題與圖，缺失表從下一頁開始 */
.floor h2 { break-after: avoid; }
thead { display: table-header-group; }                  /* 表頭每頁重複 */
.cadplan svg.marks { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.plan.fallback { display: none; }
@page plan { size: A4 landscape; margin: 10mm; }
.box { border: 1px solid #c9c4b8; border-radius: 6px; padding: 8px 10px; margin: 8px 0; background: #faf9f6; }
.floor { break-before: page; page: plan; }
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
            yield r, f.get("floor") or "全棟", f, _decided(decisions, r["file_id"], f.get("key")), True, None
        for fl in res.get("floors", []):
            for f in fl["findings"]:
                yield r, fl["label"], f, _decided(decisions, r["file_id"], f.get("key")), False, fl


def cad_overlay_svg(meta: dict, overlay: dict, drop: set) -> str:
    """原圖上的缺失標示（向量）：與底圖同一套原尺寸像素座標（viewBox＝原圖寬高），範圍半透明塗色（「建議」只畫外框）、
    線畫線、點畫圓，編號圓點放在 anchor（沒有時放範圍中心）。drop：不畫的缺失識別碼（退回的）。畫法與工作台相同。"""
    a, b, c, d, e, f = (float(v) for v in meta["transform"])
    W, H = int(meta["width"]), int(meta["height"])
    P = lambda x, y: (a * x + b * y + c, d * x + e * y + f)
    ppm = math.hypot(a, d) or 1.0                                    # 每公尺幾個像素
    size = max(W, H)
    sw, R = size * 0.0007, size * 0.009                              # 線寬、編號圓點半徑：跟著圖的大小，印出來看得清楚
    ok = lambda pt: isinstance(pt, (list, tuple)) and len(pt) >= 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in pt[:2])
    items = [x for x in overlay.get("findings") or [] if isinstance(x, dict) and isinstance(x.get("no"), int)
             and x.get("key") not in drop]
    geo, lbl = [], []
    for fd in sorted(items, key=lambda x: MARK_ORDER.get(x.get("severity"), 0)):
        box = [math.inf, math.inf, -math.inf, -math.inf]

        def ext(u, w):
            box[0], box[1], box[2], box[3] = min(box[0], u), min(box[1], w), max(box[2], u), max(box[3], w)

        def ring(pts, close):
            s = ""
            for i, pt in enumerate(p for p in (pts or []) if ok(p)):
                u, w = P(pt[0], pt[1])
                ext(u, w)
                s += ("L" if i else "M") + f"{u:.1f},{w:.1f}"
            return s + "Z" if s and close else s

        def dot(pt):
            if not ok(pt):
                return ""
            u, w = P(pt[0], pt[1])
            r = max(4.0, 0.4 * ppm)
            ext(u - r, w - r), ext(u + r, w + r)
            return f"M{u - r:.1f},{w:.1f}a{r:.1f},{r:.1f} 0 1,0 {2 * r:.1f},0a{r:.1f},{r:.1f} 0 1,0 {-2 * r:.1f},0Z"

        area, line = [], []

        def walk(g):
            if not isinstance(g, dict):
                return
            cs = g.get("coordinates") if isinstance(g.get("coordinates"), list) else []
            t = g.get("type")
            if t == "Polygon":
                area.extend(ring(rg, True) for rg in cs)
            elif t == "MultiPolygon":
                area.extend(ring(rg, True) for pg in cs for rg in (pg or []))
            elif t == "LineString":
                line.append(ring(cs, False))
            elif t == "MultiLineString":
                line.extend(ring(ln, False) for ln in cs)
            elif t == "Point":
                area.append(dot(cs))
            elif t == "MultiPoint":
                area.extend(dot(p) for p in cs)
            elif t == "GeometryCollection":
                for x in g.get("geometries") or []:
                    walk(x)

        walk(fd.get("geom"))
        col = MARK.get(fd.get("severity"), "#5f6368")
        if "".join(area):
            fill = "none" if fd.get("severity") == "BLUE" else col
            geo.append(f'<path d="{"".join(area)}" fill="{fill}" fill-opacity="0.2" stroke="{col}" stroke-width="{sw:.1f}" fill-rule="evenodd"/>')
        if "".join(line):
            geo.append(f'<path d="{"".join(line)}" fill="none" stroke="{col}" stroke-width="{sw:.1f}"/>')
        at = fd.get("anchor")
        if ok(at):
            u, w = P(at[0], at[1])
        elif box[0] <= box[2]:
            u, w = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        else:
            continue
        lbl.append(f'<g><circle cx="{u:.1f}" cy="{w:.1f}" r="{R:.1f}" fill="{col}" stroke="#fff" stroke-width="{sw:.1f}"/>'
                   f'<text x="{u:.1f}" y="{w:.1f}" font-size="{R * 1.2:.1f}" font-weight="700" fill="#fff" text-anchor="middle" '
                   f'dominant-baseline="central" font-family="sans-serif">{fd["no"]}</text></g>')
    return (f'<svg class="marks" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" preserveAspectRatio="none" '
            f'stroke-linejoin="round" aria-hidden="true">{"".join(geo)}{"".join(lbl)}</svg>')


def build_html(case: dict, context: dict, occupancy: dict, reviews: list[dict], decisions: dict, laws: dict,
               svgs: dict, user: str, now: datetime | None = None, cads: dict | None = None) -> str:
    now = now or datetime.now()
    rows = list(_rows(reviews, decisions))
    kept = [x for x in rows if x[3].get("decision") != "reject"]
    rejected = len(rows) - len(kept)
    counts = {k: sum(1 for x in kept if x[2]["severity"] == k) for k in SEV}
    undecided = sum(1 for x in kept if not x[3].get("decision"))
    o = [f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
         f"<title>消防設備自審報告｜{escape(case['name'])}</title><style>{CSS}</style></head><body><div class='wrap'>"]
    # 原圖還在載入時印出來會是空白：按鈕等全部圖面載好（或載不到、已退回簡化圖）才能按
    o.append("<div class='toolbar'><button id='print' onclick='window.print()' disabled>圖面載入中…</button></div>")
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
        out = ["<table><thead><tr><th>#</th><th>嚴重度</th><th>缺失與判定理由</th><th>改善建議／要補的資料</th><th>依據</th><th style='width:5.5em'>審核</th></tr></thead><tbody>"]
        for _r, _fl, f, d, _b, _sheet in items:
            miss = "<div><b>要補：</b>" + escape("；".join(f["missing"])) + "</div>" if f.get("missing") else ""
            note = f"<div class='small'>{escape(d.get('note') or '')}</div>" if d.get("note") else ""
            out.append(f"<tr class='nobreak'><td>{f['no']}</td><td class='sev' style='color:{SEV_COLOR[f['severity']]}'>{SEV[f['severity']]}</td>"
                       f"<td><b>{escape(f['title'])}</b><div class='small'>{escape(f['why'])}</div></td>"
                       f"<td class='small'>{escape(f['fix'])}{miss}</td><td class='small'>{_law(f['law'], laws)}</td>"
                       f"<td class='small'>{DECISION[d.get('decision')]}{note}</td></tr>")
        out.append("</tbody></table>")
        return "".join(out)

    o.append("<h2>四、全棟與系統圖缺失</h2>")
    bitems = [x for x in kept if x[4]]
    o.append(table(bitems) if bitems else "<p class='muted'>無。</p>")
    for r in reviews:
        for fl in (r.get("result") or {}).get("floors", []):
            items = [x for x in kept if x[0] is r and x[5] is fl]           # 同一樓層可能有好幾張圖
            if not fl["findings"] and not fl["equipment"]:
                continue
            o.append(f"<section class='floor'><h2>{escape(fl['label'])}｜{escape(fl.get('number') or '')} {escape(fl['title'])}</h2>"
                     f"<div class='small muted'>{escape(r['name'])}｜樓地板約 {fl['area']:,.0f} ㎡</div>")
            svg = svgs.get((r["file_id"], fl.get("svg_name") or fl["label"]))
            cad = (cads or {}).get((r["file_id"], fl.get("svg_name") or fl["label"]))
            if cad:
                drop = {k for k, v in decisions.get(str(r["file_id"]), {}).items() if (v or {}).get("decision") == "reject"}
                o.append(f"<div class='plan cadplan'><img src='{escape(cad['src'])}' alt='{escape(fl['label'])} 原圖' "
                         f"width='{int(cad['meta']['width'])}' height='{int(cad['meta']['height'])}' loading='eager' decoding='sync'>"
                         f"{cad_overlay_svg(cad['meta'], cad['overlay'], drop)}</div>")
                if svg:                                               # 原圖載不到時改顯示簡化標示圖
                    o.append(f"<div class='plan fallback'>{svg}</div>")
            elif svg:
                o.append(f"<div class='plan'>{svg}</div>")
            o.append(table(items) if items else "<p class='muted'>本層未列缺失。</p>")
            notes = fl.get("notes") or []
            if notes:
                o.append("<div class='small muted'>" + "".join(f"<div>※ {escape(n['text'])}（{_law(n['law'], laws)}）</div>" for n in notes) + "</div>")
            o.append("</section>")

    missing = sorted({m for x in kept for m in x[2].get("missing", [])})
    o.append("<h2>五、要補的資料</h2>" + ("<ul>" + "".join(f"<li>{escape(m)}</li>" for m in missing) + "</ul>" if missing else "<p class='muted'>無。</p>"))
    o.append("</div><script>(() => {"
             "const b = document.getElementById('print'), imgs = [...document.querySelectorAll('.cadplan img')];"
             "let n = imgs.length; const done = () => { if (--n <= 0) { b.disabled = false; b.textContent = '列印／另存 PDF'; } };"
             "imgs.forEach((im) => {"
             "const bad = () => { const box = im.closest('.cadplan'), fb = box.nextElementSibling;"
             "if (fb && fb.classList.contains('fallback')) { fb.style.display = 'block'; box.remove(); } else box.style.display = 'none'; };"
             "if (im.complete) { if (!im.naturalWidth) bad(); done(); }"
             "else { im.addEventListener('load', done, { once: true }); im.addEventListener('error', () => { bad(); done(); }, { once: true }); }"
             "}); n++; done();"
             "})();</script></body></html>")
    return "".join(o)


def build_csv(case: dict, reviews: list[dict], decisions: dict, laws: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["案件", "檔案", "樓層", "編號", "嚴重度", "類別", "缺失", "判定理由", "改善建議", "要補的資料", "依據", "審核", "審核備註", "識別碼"])
    for r, fl, f, d, _b, _sheet in _rows(reviews, decisions):
        w.writerow([case["name"], r["name"], fl, f["no"], SEV[f["severity"]], f["category"], f["title"], f["why"], f["fix"],
                    "；".join(f.get("missing", [])), "、".join(laws.get(i, {}).get("citation", i) for i in f["law"]),
                    DECISION[d.get("decision")], d.get("note") or "", f.get("key", "")])
    return "﻿" + buf.getvalue()                    # 加 BOM，Excel 才認得 UTF-8
