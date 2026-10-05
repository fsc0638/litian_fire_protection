"""處理程序：從 case_file 佇列取檔 →（DWG 交給轉檔服務）→ 子行程抽取中介資料（限時、限記憶體）→ 存資料庫
→ 有平面圖的檔案再用子行程跑逐項檢核（review.engine），結果與各樓層標示圖存起來
→ 檢核成功後再用子行程照 CAD 原樣畫各樓層圖的圖磚（review.cadview；失敗不影響檢核結果）。

啟動：python -m litian.drawing.worker
環境變數：DATABASE_URL、CONVERT_SPOOL（預設 /data/convert）
DXF 一律當不可信輸入，抽取放在子行程跑：卡住或吃光記憶體只會砍掉子行程，worker 本身繼續處理下一個檔。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import convert_client as CC
from . import store as ST
from . import xref as XR
from .ir import summary

log = logging.getLogger("litian.worker")
# 竣工圖等大型圖（轉出 50 MB 以上、綁定外部參考）在 ARM 主機上要數分鐘；記憶體上限可用環境變數調整
EXTRACT_TIMEOUT = 600                 # 秒
REVIEW_TIMEOUT = 900
BIND_TIMEOUT = 600
CAD_TIMEOUT = 3600                    # CAD 原樣圖：竣工圖讀檔約 30 秒、每張配置頁 1～2 分鐘
EXTRACT_MEM = int(os.environ.get("SUBPROC_MEM_MB", "2400")) * 1024 * 1024      # 子行程位址空間上限
# 數值函式庫預設會開多執行緒、預留大量位址空間；子行程限記憶體時改單執行緒
SUBPROC_ENV = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
CONVERT_TIMEOUT = 420                 # 等轉檔服務（含排隊）
IDLE_S = 2


def _limit_memory():                  # 只在 Linux 子行程裡執行
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (EXTRACT_MEM, EXTRACT_MEM))


def _run(args: list[str], timeout: int, what: str) -> str:
    r = subprocess.run([sys.executable, "-m", *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=SUBPROC_ENV,
                       preexec_fn=_limit_memory if os.name == "posix" else None)
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["未知錯誤"])[-1]
        raise RuntimeError(f"{what}失敗：{last[:300]}")
    return r.stdout


def extract_in_subprocess(dxf: Path, workdir: Path, expand: list[str] | tuple = ()) -> tuple[dict, dict]:
    out = workdir / "ir.json"
    args = ["litian.drawing.ir", str(dxf), str(out)] + (["--expand", ",".join(expand)] if expand else [])
    _run(args, EXTRACT_TIMEOUT, "抽取")
    ir = json.loads(out.read_text(encoding="utf-8"))
    return ir, summary(ir)


def _convert(spool: Path, jid: str, dwg: Path, dst: Path) -> Path:
    CC.submit(spool, jid, dwg)
    dxf, _ = CC.wait(spool, jid, CONVERT_TIMEOUT)
    shutil.move(str(dxf), dst)
    CC.cleanup(spool, jid)
    return dst


def bind_xrefs(job: dict, src: Path, spool: Path) -> tuple[Path, dict]:
    """主圖的外部參考（建築底圖等）在同一案件裡有上傳的話，綁定後另存 <檔名>.bound.dxf 供抽取與檢核。
    參考檔還沒轉檔的先送轉檔服務。讀 DXF 一律在子行程（限時、限記憶體）。"""
    path = Path(job["path"])
    try:
        refs = json.loads(_run(["litian.drawing.xref", "list", str(src)], BIND_TIMEOUT, "外部參考讀取") or "[]")
    except Exception:
        return src, {}                    # 讀不了的圖讓後面的抽取步驟回報真正原因
    if not refs:
        return src, {}
    cands = XR.case_candidates(path.parent, exclude=path)
    for k, (_name, _ref, key) in enumerate(refs):
        cand = cands.get(key)
        if cand is not None and cand.suffix.lower() == ".dwg":
            conv = cand.with_name(cand.stem + ".converted.dxf")
            if not conv.exists():
                _convert(spool, f"f{job['id']}x{k}", cand, conv)
    out = path.with_name(path.stem + ".bound.dxf")
    info = json.loads(_run(["litian.drawing.xref", "bind", str(src), str(path.parent), str(out), str(path)],
                           BIND_TIMEOUT, "外部參考綁定"))
    return Path(info["path"]), {"bound": info["bound"], "missing": info["missing"]}


def requeue_xref_dependents(conn, job: dict) -> int:
    """剛處理完的檔若是別的檔缺的外部參考，把那些檔重新排入處理（完整重跑，才能綁定）。"""
    key = XR.UPLOAD_PREFIX.sub("", Path(job["path"]).stem).lower()
    rows = conn.execute("SELECT id, stats FROM case_file WHERE case_id = %s AND id <> %s AND status IN ('done', 'failed') "
                        "AND stats ? 'xref'", (job["case_id"], job["id"])).fetchall()
    ids = [r["id"] for r in rows if any(XR.ref_key(m) == key for m in (r["stats"]["xref"].get("missing") or []))]
    for i in ids:
        conn.execute("UPDATE case_file SET status = 'queued', review_only = false, attempts = 0, updated_at = now() "
                     "WHERE id = %s", (i,))
    return len(ids)


def has_floor_plans(ir: dict) -> bool:
    from litian.plan.floor import floor_label
    from .ir import sheet_title
    return any(floor_label(sheet_title(s["meta"])) for s in ir["sheets"])


def review_in_subprocess(dxf: Path, workdir: Path, svg_dir: Path, context: dict | None = None) -> dict:
    out = workdir / "review.json"
    ctx = workdir / "context.json"
    ctx.write_text(json.dumps(context or {}, ensure_ascii=False), encoding="utf-8")
    _run(["litian.review.engine", str(dxf), str(out), "--ir", str(workdir / "ir.json"), "--svg", str(svg_dir),
          "--ctx", str(ctx), "--no-geom"], REVIEW_TIMEOUT, "檢核")
    return json.loads(out.read_text(encoding="utf-8"))


def run_review(conn, job: dict, src: Path, work: Path, stats: dict | None) -> bool:
    """檢核失敗不影響抽取結果（文字、圖紙照常可看），只記下原因。回傳檢核是否成功。"""
    path = Path(job["path"])
    svg_dir = path.with_name(path.name + ".review")
    ok = False
    try:
        result = review_in_subprocess(src, work, svg_dir, ST.get_context(conn, job["case_id"]))
        ST.save_review(conn, job["id"], "done", result, None, str(svg_dir))
        ok = True
        if stats is not None:
            stats["review"] = {"floors": len(result["floors"]),
                               "findings": sum(len(f["findings"]) for f in result["floors"])}
    except Exception as e:
        ST.save_review(conn, job["id"], "failed", None, f"{type(e).__name__}: {e}", None)
        log.warning("review failed file=%s error=%s", job["id"], e)
    ST.mark(conn, job["id"], "done", stats)
    return ok


def render_cad(conn, job: dict, src: Path, work: Path, stats: dict) -> None:
    """照 CAD 原樣畫各樓層圖的圖磚（檢核資料夾 cad/）。檔案已標為完成（檢核結果先給人看），畫圖在後面跑；
    失敗、逾時、記憶體不足只記在 cad/status.json 與日誌，不影響檢核結果。耗時另併入 stats（不動狀態欄：
    畫圖期間若檢核條件改了、檔案已重新排隊，不能被蓋回完成）。"""
    from litian.review import cadview as CV
    path = Path(job["path"])
    review_dir = path.with_name(path.name + ".review")
    t0 = time.time()
    info: dict = {}
    try:
        CV.start_status(review_dir)                    # 工作台立刻看得到「畫圖中」
        out = _run(["litian.review.cadview", str(src), str(work / "ir.json"), str(work / "review.json"), str(review_dir)],
                   CAD_TIMEOUT, "CAD 原樣圖")
        try:
            info = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            info = {}
        info = {"state": "done", "sheets": len(info.get("sheets", {})),
                "failed": sum(v != "done" for v in info.get("sheets", {}).values()), "peak_mb": info.get("peak_mb")}
    except Exception as e:
        msg = "畫圖逾時" if isinstance(e, subprocess.TimeoutExpired) else str(e)
        try:
            CV.mark_failed(review_dir, msg)
        except OSError:
            pass
        info = {"state": "failed", "error": msg[:200]}
        log.warning("cad render failed file=%s error=%s", job["id"], msg)
    info["seconds"] = round(time.time() - t0, 1)
    stats["cad"] = info
    conn.execute("UPDATE case_file SET stats = COALESCE(stats, '{}'::jsonb) || jsonb_build_object('cad', %s::jsonb) "
                 "WHERE id = %s", (json.dumps(info, ensure_ascii=False), job["id"]))
    log.info("cad file=%s %s", job["id"], info)


def process(conn, job: dict, spool: Path) -> dict:
    path = Path(job["path"])
    converted = path.with_name(path.stem + ".converted.dxf")
    bound = path.with_name(path.stem + ".bound.dxf")
    if job.get("review_only"):
        # 只重跑檢核（檢核條件改了）：用已存的中介資料與轉好（或已綁定外部參考）的 DXF
        ir = ST.load_ir(conn, job["id"])
        src = bound if bound.exists() else (converted if job["kind"] == "dwg" else path)
        if ir is not None and src.exists():
            with tempfile.TemporaryDirectory() as d:
                work = Path(d)
                (work / "ir.json").write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
                ST.mark(conn, job["id"], "reviewing")
                run_review(conn, job, src, work, None)
            return {"review_only": True}
    if job["kind"] == "dwg":
        src = _convert(spool, f"f{job['id']}", path, converted)   # 保留轉好的 DXF，之後重新抽取、重跑檢核不必再轉
    else:
        src = path
    src, xinfo = bind_xrefs(job, src, spool)
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ir, stats = extract_in_subprocess(src, work, expand=xinfo.get("bound", []))
        if xinfo:
            stats["xref"] = xinfo
        review = has_floor_plans(ir)
        with conn.transaction():
            ST.save_result(conn, job["id"], ir, stats, status="reviewing" if review else "done")
        if review and run_review(conn, job, src, work, stats) and stats["review"]["floors"]:
            render_cad(conn, job, src, work, stats)    # 只重跑檢核時不重畫：底圖沒變，缺失疊圖資料由檢核更新
    return stats


def run_once(conn, spool: Path) -> bool:
    job = ST.claim(conn)
    if not job:
        return False
    try:
        stats = process(conn, job, spool)
        log.info("done file=%s name=%s %s", job["id"], job["name"],
                 "review-only" if stats.get("review_only") else f"sheets={stats['sheets']} texts={stats['texts']}")
        if not stats.get("review_only") and (n := requeue_xref_dependents(conn, job)):
            log.info("requeued %s file(s) referencing %s", n, job["name"])
    except Exception as e:
        # 只有「等轉檔逾時」值得重試（轉檔服務可能剛好在重啟）；其他錯誤重試結果相同
        retry = isinstance(e, CC.ConvertError) and "逾時" in str(e) and job["attempts"] < ST.MAX_ATTEMPTS
        ST.save_failure(conn, job["id"], f"{type(e).__name__}: {e}", retry)
        log.warning("failed file=%s name=%s retry=%s error=%s", job["id"], job["name"], retry, type(e).__name__)
    return True


def main() -> int:
    import psycopg
    from psycopg.rows import dict_row
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    spool = Path(os.environ.get("CONVERT_SPOOL", "/data/convert"))
    from litian.review import cadview as CV
    try:
        if n := CV.fail_interrupted(os.environ.get("CASES_DIR", "/data/cases")):
            log.info("marked %s interrupted CAD render(s) as failed", n)
    except OSError as e:
        log.warning("cannot check interrupted CAD renders: %s", e)
    while True:
        try:
            with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
                ST.ensure_schema(conn)
                n = ST.recover_stale(conn, EXTRACT_TIMEOUT + CONVERT_TIMEOUT + 60)
                log.info("worker ready (recovered %s stale jobs)", n)
                while True:
                    if not run_once(conn, spool):
                        time.sleep(IDLE_S)
        except psycopg.OperationalError as e:
            log.warning("database unavailable: %s; retry in 5s", type(e).__name__)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
