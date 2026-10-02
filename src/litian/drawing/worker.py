"""處理程序：從 case_file 佇列取檔 →（DWG 交給轉檔服務）→ 子行程抽取中介資料（限時、限記憶體）→ 存資料庫
→ 有平面圖的檔案再用子行程跑逐項檢核（review.engine），結果與各樓層標示圖存起來。

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
from .ir import summary

log = logging.getLogger("litian.worker")
EXTRACT_TIMEOUT = 300                 # 秒
REVIEW_TIMEOUT = 300
EXTRACT_MEM = 1200 * 1024 * 1024      # 子行程位址空間上限
# 數值函式庫預設會開多執行緒、預留大量位址空間；子行程限記憶體時改單執行緒
SUBPROC_ENV = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
CONVERT_TIMEOUT = 420                 # 等轉檔服務（含排隊）
IDLE_S = 2


def _limit_memory():                  # 只在 Linux 子行程裡執行
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (EXTRACT_MEM, EXTRACT_MEM))


def _run(args: list[str], timeout: int, what: str) -> None:
    r = subprocess.run([sys.executable, "-m", *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=SUBPROC_ENV,
                       preexec_fn=_limit_memory if os.name == "posix" else None)
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["未知錯誤"])[-1]
        raise RuntimeError(f"{what}失敗：{last[:300]}")


def extract_in_subprocess(dxf: Path, workdir: Path) -> tuple[dict, dict]:
    out = workdir / "ir.json"
    _run(["litian.drawing.ir", str(dxf), str(out)], EXTRACT_TIMEOUT, "抽取")
    ir = json.loads(out.read_text(encoding="utf-8"))
    return ir, summary(ir)


def has_floor_plans(ir: dict) -> bool:
    from litian.plan.floor import floor_label
    from .ir import sheet_title
    return any(floor_label(sheet_title(s["meta"])) for s in ir["sheets"])


def review_in_subprocess(dxf: Path, workdir: Path, svg_dir: Path) -> dict:
    out = workdir / "review.json"
    _run(["litian.review.engine", str(dxf), str(out), "--ir", str(workdir / "ir.json"), "--svg", str(svg_dir),
          "--no-geom"], REVIEW_TIMEOUT, "檢核")
    return json.loads(out.read_text(encoding="utf-8"))


def process(conn, job: dict, spool: Path) -> dict:
    path = Path(job["path"])
    if job["kind"] == "dwg":
        jid = f"f{job['id']}"
        CC.submit(spool, jid, path)
        dxf, _ = CC.wait(spool, jid, CONVERT_TIMEOUT)
        src = path.with_name(path.stem + ".converted.dxf")   # 保留轉好的 DXF，之後重新抽取不必再轉
        shutil.move(str(dxf), src)
        CC.cleanup(spool, jid)
    else:
        src = path
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ir, stats = extract_in_subprocess(src, work)
        review = has_floor_plans(ir)
        with conn.transaction():
            ST.save_result(conn, job["id"], ir, stats, status="reviewing" if review else "done")
        if review:
            # 檢核失敗不影響抽取結果（文字、圖紙照常可看），只記下原因
            svg_dir = path.with_name(path.name + ".review")
            try:
                result = review_in_subprocess(src, work, svg_dir)
                ST.save_review(conn, job["id"], "done", result, None, str(svg_dir))
                stats["review"] = {"floors": len(result["floors"]),
                                   "findings": sum(len(f["findings"]) for f in result["floors"])}
            except Exception as e:
                ST.save_review(conn, job["id"], "failed", None, f"{type(e).__name__}: {e}", None)
                log.warning("review failed file=%s error=%s", job["id"], e)
            ST.mark(conn, job["id"], "done", stats)
    return stats


def run_once(conn, spool: Path) -> bool:
    job = ST.claim(conn)
    if not job:
        return False
    try:
        stats = process(conn, job, spool)
        log.info("done file=%s name=%s sheets=%s texts=%s", job["id"], job["name"], stats["sheets"], stats["texts"])
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
