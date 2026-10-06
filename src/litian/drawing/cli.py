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
import errno
import hashlib
import os
import re
import sys
import time
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


LOCK_TIMEOUT = "60s"   # 鎖不到就整個不動（認領、存檢核條件都只鎖一下子）
# 沒鎖到的 DWG／DXF：處理中、檢核中，或鎖定之後才處理完、才上傳的
UNLOCKED_SQL = ("SELECT case_id, name, status FROM case_file WHERE case_id = ANY(%s) AND kind IN ('dwg', 'dxf') "
                "AND status <> 'skipped' AND NOT id = ANY(%s) ORDER BY id")
KEEP = ".pre-reprocess"   # 舊的轉檔結果移到案件資料夾裡的這個子資料夾（<執行時間>/），worker 不看子資料夾


def _movable(f: Path) -> bool:
    return f.is_file() and os.access(f.parent, os.W_OK | os.X_OK)


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


# 整個重新處理（轉檔器升級後，舊的轉檔結果可能夾帶錯誤內容）。主機上只有一個 worker，可能同時在跑：
# - 全部在一個交易裡：先鎖住這些案件的 DWG／DXF（worker 認領、背景畫圖都 SKIP LOCKED 跳過），移走所有轉檔結果才一起排入。
#   逐檔提交的話，worker 可能先重跑主圖、綁到還沒移走的舊參考檔轉檔結果；參考檔之後重跑也不會再排主圖
#   （requeue_xref_dependents 看的是綁了哪份上傳檔，同一份不排）。
# - worker 正在處理這些案件的檔（ST.ACTIVE）就整個不動（rollback），等它處理完再執行一次：處理中的可能已讀進舊的
#   參考檔轉檔結果，只重跑檢核的還用著自己舊的轉檔結果，略過它們會留下一半新、一半舊。
#   鎖定後才查，和 worker 認領（FOR UPDATE SKIP LOCKED）不會錯過：認領碰到這裡鎖住的檔會跳過；在鎖定前提交的、
#   或這裡等它提交的（PostgreSQL 重新判斷後不回傳），鎖定後一查都是處理中。鎖定後才處理完、才上傳的沒鎖到，也一樣停下；
#   查的時候還沒提交的新上傳看不到，不在這批（worker 之後照常處理）。
# - 舊的轉檔結果不刪，移到 <案件資料夾>/.pre-reprocess/<執行時間>/：新版轉檔器轉不了的檔（當掉、逾時），還能把舊的搬回來用
#   （步驟見 docs/部署.md）。worker 只看案件資料夾本身的檔（case_candidates、drawing_source），不看子資料夾。
#   先確認每個都移得走才開始移；還是移不走就整批不排（rollback；已移走的照樣在 .pre-reprocess，可以搬回原處）。
# - 找不到上傳原檔的檔不動（轉檔結果是僅存的圖）；上次綁了它的主圖也不動：重跑時案件資料夾裡沒有它，會綁不到。
# - 參考檔的 .converted.dxf 一定要移走：bind_xrefs 有就直接用。移走之後主圖比參考檔先處理時，bind_xrefs 自己送轉檔
#   （只看檔案在不在，不看參考檔在資料庫的狀態，排隊中也照轉），參考檔輪到自己時再轉一次、覆蓋同一個檔。
#   requeue_xref_dependents 只排完成、失敗、只重跑檢核的檔，這批排隊中的不會重複排。
# - .bound.dxf 也移走（DXF 上傳檔也是）：這次沒綁到參考時不會重寫，drawing_source 卻會一直優先用舊的。
# - 轉檔服務交接資料夾裡沒人取走的舊結果：convert_client.submit 送件前先清掉（工作代號固定，不清會被拿去用）。
# - 畫到一半的 CAD 原樣圖照畫（開著的檔移走照樣讀得到；還沒開檔就被移走的記失敗）；worker 重跑到那個檔時
#   process() 會 reset_cad、取消畫圖。排隊中的原圖要檔案完成才會畫，不會用到移走的 DXF。
def reprocess(conn, case_ids: list[int] | None, dry_run: bool = False) -> dict:
    """case_ids 為 None：全部案件。回傳 {"cases": {案件ID: {name, queued, skipped, moved, size, kept, unsupported, busy,
    unmovable}}, "missing": [沒有的案件ID]}；queued＝排入的檔名、skipped＝[(檔名, 原因)]、moved＝移走的轉檔結果檔名、
    size＝它們共幾個位元組、kept＝移到哪個資料夾（試跑時執行時間記成「<執行時間>」）、unsupported＝不支援、不處理的檔數；
    busy（試跑）＝[(worker 正在處理的檔名, 狀態)]，正式執行會停下；unmovable（試跑）＝移不走的轉檔結果，正式執行會在
    移檔前停下。dry_run：只列出，不動。"""
    sql = "SELECT id, name FROM review_case" + ("" if case_ids is None else " WHERE id = ANY(%s)") + " ORDER BY id"
    cases = conn.execute(sql, () if case_ids is None else (case_ids,)).fetchall()
    rep = {c["id"]: {"name": c["name"], "queued": [], "skipped": [], "moved": [], "size": 0, "kept": None,
                     "unsupported": 0, "busy": [], "unmovable": []} for c in cases}
    out = {"cases": rep, "missing": sorted(set(case_ids or ()) - set(rep))}
    ids = list(rep)
    if not ids:
        return out
    import psycopg
    try:
        with conn.transaction():
            if dry_run:
                todo = conn.execute("SELECT id, case_id, name, kind, path, status, stats FROM case_file "
                                    "WHERE case_id = ANY(%s) AND status IN ('queued', 'done', 'failed') "
                                    "AND kind IN ('dwg', 'dxf') ORDER BY id", (ids,)).fetchall()
            else:
                conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                todo = ST.lock_for_reprocess(conn, ids)
            busy = [(r["case_id"], r["name"], ST.ACTIVE.get(r["status"], "剛有變動"))
                    for r in conn.execute(UNLOCKED_SQL, (ids, [r["id"] for r in todo]))]
            if busy and not dry_run:
                raise ReprocessError("worker 正在處理：" + "、".join(f"案件 {c} {n}（{s}）" for c, n, s in busy)
                                     + "，沒有任何更動，等它處理完再執行一次。")
            for c, n, s in busy:
                rep[c]["busy"].append((n, s))
            _apply(conn, ids, rep, todo, dry_run, "<執行時間>" if dry_run else time.strftime("%Y%m%d-%H%M%S"))
    except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as e:
        raise ReprocessError("其他程序正鎖著這些檔。沒有任何更動，稍後再執行一次。") from e
    return out


def _bound_refs(r: dict) -> dict[str, str]:
    """上次綁進這個檔的參考檔：比對鍵 → 名稱（存檔名；舊資料只有圖塊名，通常＝參考檔的檔名主體）。"""
    from . import xref as XR                              # xref 也引用本模組，在這裡才載入
    x = (r.get("stats") or {}).get("xref") or {}
    if isinstance(x.get("bound_files"), list):
        return {XR.name_key(str(b)): str(b) for b in x["bound_files"]}
    return {XR.ref_key(str(b)): str(b) for b in x.get("bound") or []}


def _apply(conn, ids: list[int], rep: dict, todo: list[dict], dry_run: bool, stamp: str) -> None:
    """（在交易裡）把 todo 各檔的轉檔結果移到 <案件資料夾>/.pre-reprocess/<stamp>/、排回完整重跑，結果記進 rep。
    dry_run：只記不動。"""
    from . import xref as XR
    rows = conn.execute("SELECT case_id, path, kind FROM case_file WHERE case_id = ANY(%s)", (ids,)).fetchall()
    uploads = {Path(r["path"]) for r in rows}
    # 上傳原檔不見了的 DWG／DXF（案件, 比對鍵）：重跑時 case_candidates 找不到，綁了它的主圖會綁不到
    lost = {(r["case_id"], XR.name_key(Path(r["path"]).name)) for r in rows
            if r["kind"] in ("dwg", "dxf") and not Path(r["path"]).is_file()}
    plan = []
    for r in todo:
        c, p = rep[r["case_id"]], Path(r["path"])
        if not p.is_file():
            c["skipped"].append((r["name"], "找不到上傳的原檔，不動（轉檔結果是僅存的圖）"))
            continue
        refs = {k: n for k, n in _bound_refs(r).items() if (r["case_id"], k) in lost}
        if refs:                                              # 同名的還有別的上傳檔：重跑改綁那份，不算綁不到
            cands = XR.case_candidates(p.parent, exclude=p)
            refs = {k: n for k, n in refs.items() if k not in cands}
        if refs:
            c["skipped"].append((r["name"], f"上次綁進來的參考檔 {'、'.join(refs.values())} 找不到上傳的原檔，"
                                            "重跑會綁不到，不動"))
            continue
        gone = [f for f in derived_files(r["path"], r["kind"]) if f.exists() and f not in uploads]   # 上傳檔一律不動
        plan.append((r, gone))
        c["unmovable"] += [f.name for f in gone if not _movable(f)]
        c["size"] += sum(f.stat().st_size for f in gone if f.is_file())
    stuck = [f"案件 {cid} {n}" for cid, c in rep.items() for n in c["unmovable"]]
    if stuck and not dry_run:
        raise ReprocessError(f"移不走 {len(stuck)} 個轉檔結果（權限不足或不是一般檔案）：{'、'.join(stuck[:5])}"
                             f"{'…' if len(stuck) > 5 else ''}。沒有任何更動，排除問題後再執行一次。")
    keeps = {Path(r["path"]).parent / KEEP / stamp for r, gone in plan if gone}
    if keeps and not dry_run:
        try:                                                  # 放舊結果的資料夾先建好：建不了就還沒移任何檔
            for k in sorted(keeps):
                k.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ReprocessError(f"建不了 {e.filename or KEEP}：{e.strerror or e}。沒有移走任何檔、沒有排入，"
                                 "排除問題後再執行一次。") from e
    moved = 0
    for r, gone in plan:
        c, keep = rep[r["case_id"]], Path(r["path"]).parent / KEEP / stamp
        if not dry_run:
            for f in gone:
                try:
                    if (keep / f.name).exists():               # 不蓋掉別次留下的舊結果
                        raise FileExistsError(errno.EEXIST, "已經有同名的檔", str(keep / f.name))
                    f.rename(keep / f.name)                    # 同一個資料夾底下改名：不複製、不多佔空間
                except OSError as e:
                    raise ReprocessError(f"移不走 {f.name}：{e.strerror or e}。這次全部沒有排入（已移走 {moved} 個轉檔結果，"
                                         f"在各案件資料夾的 {KEEP}/{stamp}/，可以搬回原處），排除問題後再執行一次。") from e
                moved += 1
            ST.requeue_full(conn, r["id"])
        c["queued"].append(r["name"])
        c["moved"] += [f.name for f in gone]
        if gone:
            c["kept"] = str(keep)
    for r in conn.execute("SELECT case_id, count(*) AS n FROM case_file WHERE case_id = ANY(%s) "
                          "AND kind NOT IN ('dwg', 'dxf') GROUP BY case_id", (ids,)):
        rep[r["case_id"]]["unsupported"] = r["n"]


def report_lines(res: dict, dry_run: bool) -> list[str]:
    verb, move = ("會排入", "會移走") if dry_run else ("排入", "移走")
    lines = ["試跑：只列出會做的事，沒有任何更動"] if dry_run else []
    for cid, c in res["cases"].items():
        lines.append(f"案件 {cid}「{c['name']}」：{verb}重新處理 {len(c['queued'])} 個檔"
                     + (f"；不支援的檔 {c['unsupported']} 個不處理" if c["unsupported"] else ""))
        lines += [f"  {name}（{st}）：worker 正在處理，正式執行會停下" for name, st in c["busy"]]
        lines += [f"  略過 {name}：{why}" for name, why in c["skipped"]]
        if c["moved"]:
            lines.append(f"  {'會移走' if dry_run else '已移走'}舊的轉檔結果 {len(c['moved'])} 個（{_mb(c['size'])}）到 "
                         f"{c['kept']}：{'、'.join(c['moved'])}")
        if c["unmovable"]:
            lines.append(f"  移不走（權限不足或不是一般檔案）{len(c['unmovable'])} 個：{'、'.join(c['unmovable'])}；"
                         "正式執行會在移任何檔之前停下")
    lines += [f"案件 {cid}：沒有這個案件" for cid in res["missing"]]
    cs = res["cases"].values()
    lines.append(f"合計 {len(res['cases'])} 個案件：{verb} {sum(len(c['queued']) for c in cs)} 個檔、"
                 f"略過 {sum(len(c['skipped']) for c in cs)} 個、{move}舊的轉檔結果 "
                 f"{sum(len(c['moved']) for c in cs)} 個（{_mb(sum(c['size'] for c in cs))}）")
    if any(c["moved"] for c in cs):
        lines.append(f"舊的轉檔結果不刪，{'會' if dry_run else ''}留在各案件資料夾的 {KEEP}/：重跑期間新舊並存，磁碟剩餘空間"
                     "要比上面的總大小多；新版轉不了的檔可以把舊的搬回來用（docs/部署.md），全部完成、確認沒問題再刪")
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
            return 1 if res["missing"] or any(c["unmovable"] or c["busy"] for c in res["cases"].values()) else 0
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
                  f"{sum(1 for r in rows if r['status'] in ('queued', 'processing', 'reviewing'))}")
        else:
            for r in conn.execute("SELECT f.name, s.idx, s.number, s.title, s.scale, s.unit FROM case_sheet s "
                                  "JOIN case_file f ON f.id = s.file_id WHERE f.case_id = %s ORDER BY s.number, f.name",
                                  (args.case_id,)).fetchall():
                print(f"{r['number'] or '—':8s} {r['title'] or '':24s} {r['scale'] or '':8s} {r['unit'] or '':4s} ← {r['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
