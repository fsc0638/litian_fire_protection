"""處理程序：從 case_file 佇列取檔 →（DWG 交給轉檔服務）→ 子行程抽取中介資料（限時、限記憶體）→ 存資料庫。

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
EXTRACT_MEM = 1200 * 1024 * 1024      # 子行程位址空間上限
CONVERT_TIMEOUT = 420                 # 等轉檔服務（含排隊）
IDLE_S = 2


def _limit_memory():                  # 只在 Linux 子行程裡執行
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (EXTRACT_MEM, EXTRACT_MEM))


def extract_in_subprocess(dxf: Path) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "ir.json"
        r = subprocess.run([sys.executable, "-m", "litian.drawing.ir", str(dxf), str(out)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=EXTRACT_TIMEOUT, preexec_fn=_limit_memory if os.name == "posix" else None)
        if r.returncode != 0 or not out.exists():
            last = (r.stderr.strip().splitlines() or ["未知錯誤"])[-1]
            raise RuntimeError(f"抽取失敗：{last[:300]}")
        ir = json.loads(out.read_text(encoding="utf-8"))
    return ir, summary(ir)


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
    ir, stats = extract_in_subprocess(src)
    with conn.transaction():
        ST.save_result(conn, job["id"], ir, stats)
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
