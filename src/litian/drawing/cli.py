"""命令列（在 worker 容器內執行）。

  python -m litian.drawing.cli ingest --name 案件名稱 檔案…   建立案件、複製檔案、排入佇列
  python -m litian.drawing.cli status 案件ID                    各檔處理狀態與抽取摘要
  python -m litian.drawing.cli sheets 案件ID                    各張圖的圖號、圖名、比例、單位
環境變數：DATABASE_URL、CASES_DIR（預設 /data/cases）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

from . import store as ST

SAFE = re.compile(r"[^\w.\-（）()一-鿿]+")


def safe_name(name: str) -> str:
    """只留檔名本身，去掉路徑與奇怪字元（中文保留）。"""
    base = Path(name.replace("\\", "/")).name
    return SAFE.sub("_", base).strip("._") or "file"


def ingest(conn, name: str, files: list[Path], cases_dir: Path) -> int:
    with conn.transaction():
        cid = ST.create_case(conn, name, created_by="cli")
        d = cases_dir / str(cid)
        d.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(files, 1):
            data = f.read_bytes()
            dst = d / f"{i:03d}_{safe_name(f.name)}"
            dst.write_bytes(data)
            ST.add_file(conn, cid, f.name, len(data), hashlib.sha256(data).hexdigest(), str(dst))
    return cid


def main(argv: list[str]) -> int:
    import psycopg
    from psycopg.rows import dict_row
    ap = argparse.ArgumentParser(prog="litian.drawing.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ingest")
    a.add_argument("--name", required=True)
    a.add_argument("files", nargs="+", type=Path)
    s = sub.add_parser("status")
    s.add_argument("case_id", type=int)
    h = sub.add_parser("sheets")
    h.add_argument("case_id", type=int)
    args = ap.parse_args(argv)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        ST.ensure_schema(conn)
        if args.cmd == "ingest":
            cid = ingest(conn, args.name, args.files, Path(os.environ.get("CASES_DIR", "/data/cases")))
            print(f"案件 {cid}：已排入 {len(args.files)} 個檔案")
        elif args.cmd == "status":
            rows = ST.case_status(conn, args.case_id)
            for r in rows:
                st = r["stats"] or {}
                info = (f"圖 {st.get('sheets')}｜文字 {st.get('texts')}｜圖塊 {st.get('inserts')}｜線段 {st.get('segments')}"
                        f"｜圖號 {','.join(x or '—' for x in st.get('sheet_numbers', []))}") if st else (r["error"] or "")
                print(f"{r['status']:10s} {r['name'][:30]:32s} {info}")
            done = sum(1 for r in rows if r["status"] == "done")
            print(f"合計 {len(rows)}：完成 {done}、失敗 {sum(1 for r in rows if r['status'] == 'failed')}、"
                  f"略過 {sum(1 for r in rows if r['status'] == 'skipped')}、處理中或排隊 "
                  f"{sum(1 for r in rows if r['status'] in ('queued', 'processing'))}")
        else:
            for r in conn.execute("SELECT f.name, s.idx, s.number, s.title, s.scale, s.unit FROM case_sheet s "
                                  "JOIN case_file f ON f.id = s.file_id WHERE f.case_id = %s ORDER BY s.number, f.name",
                                  (args.case_id,)).fetchall():
                print(f"{r['number'] or '—':8s} {r['title'] or '':24s} {r['scale'] or '':8s} {r['unit'] or '':4s} ← {r['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
