"""產生 data/tables/D0120029_18.yaml 與 D0120029_157.yaml（開發者轉錄版，status=draft）。

2026-10-01 一次性執行，保留作為轉錄紀錄。需要 pymupdf（pip install pymupdf）與 data/raw/D0120029_18_full.pdf。
第 18 條各列的○是開發者目視 PDF 判讀並與獨立轉錄逐格比對；列名與註以程式比對 PDF 文字層。
之後若消防設備師校對修正，直接改 YAML（不要重跑本腳本覆蓋）。
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf  # noqa: E402
import yaml  # noqa: E402

from litian.lawdb.tables import parse_157  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "tables"
OUT.mkdir(parents=True, exist_ok=True)

# ---------- 第 18 條（開發者目視轉錄，PDF 150dpi） ----------
pdf = ROOT / "data" / "raw" / "D0120029_18_full.pdf"
doc = pymupdf.open(pdf)
layer = re.sub(r"\s|○", "", "".join(p.get_text() for p in doc))

rows18 = [
    (1, "屋頂直昇機停機場（坪）。", ["泡沫", "乾粉"]),
    (2, "飛機修理廠、飛機庫樓地板面積在二百平方公尺以上者。", ["泡沫", "乾粉"]),
    (3, "汽車修理廠、室內停車空間在第一層樓地板面積五百平方公尺以上者；在地下層或第二層以上樓地板面積在二百平方公尺以上者；"
        "在屋頂設有停車場樓地板面積在三百平公尺以上者。", ["水霧", "泡沫", "二氧化碳或惰性氣體", "鹵化烴", "乾粉"]),
    (4, "昇降機械式停車場可容納十輛以上者。", ["水霧", "泡沫", "二氧化碳或惰性氣體", "鹵化烴", "乾粉"]),
    (5, "發電機室、變壓器室及其他類似之電器設備場所，樓地板面積在二百平方公尺以上者。", ["水霧", "二氧化碳或惰性氣體", "鹵化烴", "乾粉"]),
    (6, "鍋爐房、廚房等大量使用火源之場所，樓地板面積在二百平方公尺以上者。", ["二氧化碳或惰性氣體", "鹵化烴", "乾粉"]),
    (7, "電信機械室、電腦室或總機室及其他類似場所，樓地板面積在二百平方公尺上者。", ["二氧化碳或惰性氣體", "鹵化烴", "乾粉"]),
]
notes18 = [
    "一、大量使用火源場所，指最大消費熱量合計在每小時三十萬千卡以上者。",
    "二、廚房如設有自動撒水設備，且排油煙管及煙罩設簡易自動滅火裝置時，得不受本表限制。",
    "三、停車空間內車輛採一列停放，並能同時通往室外者，得不受本表限制。",
    "四、本表項目三及項目四所列應設場所得設置自動撒水設備；項目七所列應設場所得設置預動式自動撒水設備，不受本表限制。",
    "五、平時有特定或不特定人員使用之中央管理室、防災中心等類似處所，不得設置二氧化碳滅火設備。",
]
for _, place, _ in rows18:
    assert re.sub(r"\s", "", place) in layer, f"列名與 PDF 文字層不符：{place}"
for n in notes18:
    assert re.sub(r"\s", "", n) in layer, f"註與 PDF 文字層不符：{n}"

t18 = {
    "node_id": "D0120029/18",
    "citation": "設置標準第18條第1項附表",
    "title": "水霧、泡沫、二氧化碳或惰性氣體、鹵化烴、乾粉滅火設備選擇設置表",
    "law_version": "20240424",
    "source": {
        "kind": "pdf",
        "url": "https://law.moj.gov.tw/LawClass/LawGetFile.ashx?FileId=0000366988",
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "pages": [1, 2],
        "why": "網頁與 API 條文文字沒有這張表，只在官方「完整條文」PDF 中",
    },
    "status": "draft",
    "verified_by": None,
    "verified_at": None,
    "transcription": [
        {"by": "開發者（Claude Opus 5.5）", "date": "2026-10-01",
         "method": "PDF 150dpi 目視判讀○；列名與註以程式比對 PDF 文字層逐字確認"},
    ],
    "meaning": "○＝原表該格標有○（條文：下表所列之場所，應就各該滅火設備選擇設置之）。如何適用由消防設備師判斷。",
    "columns": ["水霧", "泡沫", "二氧化碳或惰性氣體", "鹵化烴", "乾粉"],
    "rows": [{"no": no, "place": place, "marks": marks} for no, place, marks in rows18],
    "notes": notes18,
    "source_quirks": [
        "表頭「二氧化碳或惰性氣體」為同一欄（條文本文分列兩種，表格合併）",
        "項目三原文「三百平公尺」缺「方」字，照原文保留",
        "項目七原文「二百平方公尺上者」缺「以」字，照原文保留",
    ],
}

# ---------- 第 157 條（方框字元表格，程式解析） ----------
nodes = {json.loads(l)["node_id"]: json.loads(l) for l in open(ROOT / "data/lawdb/nodes.jsonl", encoding="utf-8")}
p157 = parse_157(nodes["D0120029/157"]["text"])
t157 = {
    "node_id": "D0120029/157",
    "citation": "設置標準第157條附表",
    "title": "避難器具選擇設置表",
    "law_version": "20240424",
    "source": {
        "kind": "text",
        "url": "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=D0120029&flno=157",
        "why": "條文文字內以方框字元排版；由 litian.lawdb.tables.parse_157 解析，測試會核對與現行條文一致",
    },
    "status": "draft",
    "verified_by": None,
    "verified_at": None,
    "transcription": [
        {"by": "開發者（Claude Opus 5.5）", "date": "2026-10-01",
         "method": "程式解析方框字元表格；儲存格跨行接回後以頓號切分器具；「同上」往上找最近非空格"},
    ],
    "meaning": "各格＝原表所列避難器具（條文：依下表選擇設置之）；空白格＝原表空白；「同上」已依程式規則展開，原文保留在 raw。如何適用由消防設備師判斷。",
    "columns": p157["columns"],
    "rows": p157["rows"],
    "notes": p157["notes"],
    "source_quirks": [
        "第 2 列原文「住宿型精神復建機構」（第 12 條寫「復健」），照原文保留",
    ],
}
assert "復建" in json.dumps(p157, ensure_ascii=False), "157 原文用字變了，請重新確認 source_quirks"

for t in (t18, t157):
    path = OUT / (t["node_id"].replace("/", "_") + ".yaml")
    path.write_text(yaml.safe_dump(t, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print("wrote", path, path.stat().st_size, "bytes")
