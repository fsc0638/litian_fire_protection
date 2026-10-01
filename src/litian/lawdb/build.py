"""建置法規庫資料檔（確定性輸出，可重跑）。

用法：python -m litian.lawdb.build [--refresh] [--raw data/raw] [--out data/lawdb]
  --refresh  重新從全國法規資料庫 Open API 下載（預設沿用 data/raw 內既有的 ZIP）

輸出（data/lawdb/）：
  laws.json         各法規基本資料（名稱、位階、修正日、生效日、條數）
  nodes.jsonl       每行一個節點（條／項／款／目／細目），含原文、引用寫法、所屬編章節
  occupancy.json    設置標準第 12 條場所分類代碼表
  xrefs.jsonl       條文間交叉引用
  legend.json       消防署審查及查驗作業基準附件三「消防圖說圖示範例」圖例（名稱、類別、備註、符號圖檔名）
  tables.json       法定表格結構化檔（來源 data/tables/*.yaml；第 18 條選設表、第 157 條避難器具表）
  legend/*.png      圖例符號圖
  build_report.json 來源資料日期、各項數量、解析警告
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import shutil
from pathlib import Path

from . import nfa, tables as T
from .fetch import fetch_all
from .occupancy import build_occupancy
from .parse import parse_law
from .sources import LAWS
from .xref import extract_xrefs


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def build(raw_dir: Path, out_dir: Path, refresh: bool = False, table_dir: Path = T.TABLE_DIR) -> dict:
    # 只有缺 ZIP 或 --refresh 才下載；每次都重新抽取，順便取得官方資料包的更新日期（稽核要記法規版本）
    update = fetch_all(raw_dir, refresh)["_update"]

    laws, nodes, warnings, legend = [], [], [], []
    legend_dir = out_dir / "legend"
    if legend_dir.exists():
        shutil.rmtree(legend_dir)
    for src in LAWS:
        if src.kind == "nfa":
            paths = nfa.fetch(src.pcode, raw_dir, refresh)
            law, ns, w = nfa.parse_rule(paths["html"], src)
            for att in nfa.ATTACHMENTS.get(src.pcode, ()):
                lnodes, entries, lw = nfa.parse_legend(paths[f"{att.key}.odt"], src, att, legend_dir)
                ns += lnodes
                legend += entries
                w += lw
            for i, n in enumerate(ns):
                n.seq = i
        else:
            law, ns, w = parse_law(raw_dir / f"{src.pcode}.xml")
        laws.append(law)
        nodes += ns
        warnings += w
    ids = {n.node_id for n in nodes}
    tables = T.load_tables(table_dir)
    for t in tables:
        if t["node_id"] not in ids:
            warnings.append(f"表格 {t['node_id']} 對應的條文不在法規庫")
    by_id = {n.node_id: n for n in nodes}
    for t in tables:                  # 方框字元表格：審閱過的 YAML 必須仍與現行條文解析結果一致
        if t["node_id"] == "D0120029/157":
            p = T.parse_157(by_id[t["node_id"]].text)
            if p["rows"] != t["rows"] or p["columns"] != t["columns"]:
                warnings.append("第 157 條條文的表格與已審閱的 data/tables/D0120029_157.yaml 不一致，需重新校對")
    occupancy = build_occupancy(nodes)
    xrefs = extract_xrefs(nodes)

    out_dir.mkdir(parents=True, exist_ok=True)
    _dump(out_dir / "laws.json", [l.to_dict() for l in laws])
    with open(out_dir / "nodes.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for n in nodes:
            fh.write(json.dumps(n.to_dict(), ensure_ascii=False) + "\n")
    _dump(out_dir / "occupancy.json", [o.to_dict() for o in occupancy])
    with open(out_dir / "xrefs.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for x in xrefs:
            fh.write(json.dumps(x.to_dict(), ensure_ascii=False) + "\n")
    _dump(out_dir / "legend.json", [e.to_dict() for e in legend])
    _dump(out_dir / "tables.json", tables)

    levels: dict[str, int] = {}
    for n in nodes:
        levels[n.level] = levels.get(n.level, 0) + 1
    report = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "全國法規資料庫 Open API（law.moj.gov.tw/api）；消防署法令查詢系統行動版（law.nfa.gov.tw/MOBILE）",
        "source_update": update,
        "laws": [{"pcode": l.pcode, "name": l.name, "modified": l.modified,
                  "articles": l.article_count, "deleted": l.deleted_count} for l in laws],
        "nodes": len(nodes),
        "levels": levels,
        "pdf_table_articles": sum(1 for n in nodes if n.pdf_table_url),
        "occupancy_codes": len(occupancy),
        "legend_entries": len(legend),
        "legend_categories": len({e.category for e in legend}),
        "tables": [{"node_id": t["node_id"], "status": t["status"], "rows": len(t["rows"])} for t in tables],
        "xrefs": {"total": len(xrefs), "resolved": sum(x.resolved for x in xrefs),
                  "external": sum(x.external for x in xrefs),
                  "unresolved": sum(1 for x in xrefs if not x.resolved and not x.external)},
        "warnings": warnings,
    }
    _dump(out_dir / "build_report.json", report)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="建置法規庫資料檔")
    ap.add_argument("--raw", type=Path, default=Path("data/raw"))
    ap.add_argument("--out", type=Path, default=Path("data/lawdb"))
    ap.add_argument("--refresh", action="store_true", help="重新下載官方資料")
    a = ap.parse_args()
    r = build(a.raw, a.out, a.refresh)
    print(json.dumps({k: v for k, v in r.items() if k != "warnings"}, ensure_ascii=False, indent=1))
    print(f"warnings: {len(r['warnings'])}")


if __name__ == "__main__":
    main()
