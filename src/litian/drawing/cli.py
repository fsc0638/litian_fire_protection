"""命令列（在 worker 容器內執行）。

  python -m litian.drawing.cli ingest --name 案件名稱 檔案…   建立案件、複製檔案、排入佇列
  python -m litian.drawing.cli status 案件ID                    各檔處理狀態與抽取摘要
  python -m litian.drawing.cli sheets 案件ID                    各張圖的圖號、圖名、比例、單位
  python -m litian.drawing.cli reprocess --all | --case 案件ID …  [--dry-run]
                                                               整個重新處理（轉檔器升級後）：重新轉檔、綁定、抽取、檢核、畫原圖
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


class ReprocessError(RuntimeError):
    pass


def derived_files(path: str, kind: str) -> list[Path]:
    """worker 在上傳檔旁邊產生的 DXF：DWG 轉檔結果 <主檔名>.converted.dxf；綁定外部參考後的 <主檔名>.bound.dxf。"""
    p = Path(path)
    return [p.with_name(p.stem + s) for s in (".converted.dxf", ".bound.dxf") if kind == "dwg" or s == ".bound.dxf"]


# 整個重新處理（轉檔器升級後，舊的轉檔結果可能夾帶錯誤內容）。主機上只有一個 worker，可能同時在跑：
# - 處理中（ST.ACTIVE）的檔略過、不刪它的檔（worker 正在用）；它可能已讀進舊的參考檔轉檔結果，處理完要再執行一次。
# - 全部在一個交易裡：先鎖住要重跑的檔（worker 認領、背景畫圖都 SKIP LOCKED 跳過），刪完所有轉檔結果才一起排入。
#   逐檔提交的話，worker 可能先重跑主圖、綁到還沒刪的舊參考檔轉檔結果；參考檔之後重跑也不會再排主圖
#   （requeue_xref_dependents 看的是綁了哪份上傳檔，同一份不排）。刪檔無法復原：刪不掉就整批不排（rollback）。
#   鎖在刪檔前一次拿完（之後只有別人等這裡，這裡不再等鎖）：撞上死結也是在刪任何檔之前失敗。
# - 參考檔的 .converted.dxf 一定要刪：bind_xrefs 有就直接用。刪了之後主圖比參考檔先處理時，bind_xrefs 自己送轉檔
#   （只看檔案在不在，不看參考檔在資料庫的狀態，排隊中也照轉），參考檔輪到自己時再轉一次、覆蓋同一個檔。
#   requeue_xref_dependents 只排完成、失敗、只重跑檢核的檔，這批排隊中的不會重複排。
# - .bound.dxf 也刪（DXF 上傳檔也是）：這次沒綁到參考時不會重寫，drawing_source 卻會一直優先用舊的。
# - 畫到一半的 CAD 原樣圖照畫（Linux 刪掉開著的檔照樣讀得到；還沒開檔就被刪的記失敗）；worker 重跑到那個檔時
#   process() 會 reset_cad、取消畫圖。排隊中的原圖要檔案完成才會畫，不會用到刪掉的 DXF。
def reprocess(conn, case_ids: list[int] | None, dry_run: bool = False) -> dict:
    """case_ids 為 None：全部案件。回傳 {"cases": {案件ID: {name, queued, skipped, deleted, unsupported}}, "missing": [沒有的案件ID]}；
    queued＝排入的檔名、skipped＝[(檔名, 原因)]、deleted＝刪掉的轉檔結果檔名、unsupported＝不支援、不處理的檔數。dry_run：只列出，不動。"""
    sql = "SELECT id, name FROM review_case" + ("" if case_ids is None else " WHERE id = ANY(%s)") + " ORDER BY id"
    cases = conn.execute(sql, () if case_ids is None else (case_ids,)).fetchall()
    rep = {c["id"]: {"name": c["name"], "queued": [], "skipped": [], "deleted": [], "unsupported": 0} for c in cases}
    out = {"cases": rep, "missing": sorted(set(case_ids or ()) - set(rep))}
    ids = list(rep)
    if not ids:
        return out
    with conn.transaction():
        if dry_run:
            todo = conn.execute("SELECT id, case_id, name, kind, path, status FROM case_file WHERE case_id = ANY(%s) "
                                "AND status IN ('queued', 'done', 'failed') AND kind IN ('dwg', 'dxf') ORDER BY id",
                                (ids,)).fetchall()
        else:
            todo = ST.lock_for_reprocess(conn, ids)
        uploads = {Path(r["path"]) for r in conn.execute("SELECT path FROM case_file WHERE case_id = ANY(%s)", (ids,))}
        removed: list[Path] = []
        for r in todo:
            c = rep[r["case_id"]]
            if not Path(r["path"]).is_file():
                c["skipped"].append((r["name"], "找不到上傳的原檔，不動（轉檔結果是僅存的圖）"))
                continue
            gone = [f for f in derived_files(r["path"], r["kind"]) if f.exists() and f not in uploads]   # 上傳檔一律不刪
            if not dry_run:
                for f in gone:
                    try:
                        f.unlink(missing_ok=True)
                    except OSError as e:
                        raise ReprocessError(f"刪不掉 {f.name}：{e.strerror or e}。這次全部沒有排入（已刪掉 {len(removed)} 個"
                                             "轉檔結果），排除問題後再執行一次。") from e
                    removed.append(f)
                ST.requeue_full(conn, r["id"])
            c["queued"].append(r["name"])
            c["deleted"] += [f.name for f in gone]
        locked = {r["id"] for r in todo}
        for r in conn.execute("SELECT id, case_id, name, kind, status FROM case_file WHERE case_id = ANY(%s) ORDER BY id",
                              (ids,)):
            if r["id"] in locked:
                continue
            if r["status"] in ST.ACTIVE:
                rep[r["case_id"]]["skipped"].append((r["name"], f"{ST.ACTIVE[r['status']]}，略過（處理完後再對這個案件執行一次）"))
            elif r["kind"] not in ST.SUPPORTED:
                rep[r["case_id"]]["unsupported"] += 1
            # 其餘是鎖定之後才上傳的新檔：worker 照常處理
    return out


def report_lines(res: dict, dry_run: bool) -> list[str]:
    verb = "會排入" if dry_run else "排入"
    lines = ["試跑：只列出會做的事，沒有任何更動"] if dry_run else []
    for cid, c in res["cases"].items():
        lines.append(f"案件 {cid}「{c['name']}」：{verb}重新處理 {len(c['queued'])} 個檔"
                     + (f"；不支援的檔 {c['unsupported']} 個不處理" if c["unsupported"] else ""))
        lines += [f"  略過 {name}：{why}" for name, why in c["skipped"]]
        if c["deleted"]:
            lines.append(f"  {'會刪除' if dry_run else '已刪除'}轉檔結果 {len(c['deleted'])} 個：{'、'.join(c['deleted'])}")
    lines += [f"案件 {cid}：沒有這個案件" for cid in res["missing"]]
    cs = res["cases"].values()
    lines.append(f"合計 {len(res['cases'])} 個案件：{verb} {sum(len(c['queued']) for c in cs)} 個檔、"
                 f"略過 {sum(len(c['skipped']) for c in cs)} 個、{'會刪除' if dry_run else '刪除'}轉檔結果 "
                 f"{sum(len(c['deleted']) for c in cs)} 個")
    if not dry_run and any(c["queued"] for c in cs):
        lines.append("進度：python -m litian.drawing.cli status 案件ID（排隊中的檔都完成後才開始畫 CAD 原樣圖）")
    return lines


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
    r = sub.add_parser("reprocess")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--case", type=int, action="append", dest="cases")
    r.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        ST.ensure_schema(conn)
        if args.cmd == "reprocess":
            try:
                res = reprocess(conn, None if args.all else args.cases, dry_run=args.dry_run)
            except ReprocessError as e:
                print(e, file=sys.stderr)
                return 1
            print("\n".join(report_lines(res, args.dry_run)))
            return 1 if res["missing"] else 0
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
