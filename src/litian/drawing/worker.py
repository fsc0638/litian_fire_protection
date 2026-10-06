"""處理程序：從 case_file 佇列取檔 →（DWG 交給轉檔服務）→ 子行程抽取中介資料（限時、限記憶體）→ 存資料庫
→ 有平面圖的檔案再用子行程跑逐項檢核（review.engine），結果與各樓層標示圖存起來
→ 檢核成功的排入 CAD 原樣圖佇列：佇列空下來時在背景用子行程畫各樓層圖的圖磚（review.cadview，見 CadRunner），
  畫圖期間照常處理新上傳的檔；失敗不影響檢核結果。

啟動：python -m litian.drawing.worker
環境變數：DATABASE_URL、CONVERT_SPOOL（預設 /data/convert）
DXF 一律當不可信輸入，抽取放在子行程跑：卡住或吃光記憶體只會砍掉子行程，worker 本身繼續處理下一個檔。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from litian.review import cadview as CV

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
CAD_STALE_S = CAD_TIMEOUT + 600       # 「畫圖中」超過這麼久還沒結束：當成沒有人在畫（worker 當掉）
EXTRACT_MEM = int(os.environ.get("SUBPROC_MEM_MB", "2400")) * 1024 * 1024      # 子行程位址空間上限
CAD_MEM = int(os.environ.get("CAD_MEM_MB", "3500")) * 1024 * 1024              # 畫圖子行程（竣工圖實測峰值約 1.5 GB）
# 數值函式庫預設會開多執行緒、預留大量位址空間；子行程限記憶體時改單執行緒
SUBPROC_ENV = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
CONVERT_TIMEOUT = 420                 # 等轉檔服務（含排隊）
IDLE_S = 2


def _limit_memory():                  # 只在 Linux 子行程裡執行
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (EXTRACT_MEM, EXTRACT_MEM))


def _cad_limits():                    # 只在 Linux 子行程裡執行：限記憶體、限 CPU 秒數（逾時的保險）、降低優先順序
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (CAD_MEM, CAD_MEM))
    resource.setrlimit(resource.RLIMIT_CPU, (CAD_TIMEOUT, CAD_TIMEOUT + 60))
    os.nice(10)


def review_dir_of(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".review")


def drawing_source(job: dict) -> Path:
    """轉好（或已綁定外部參考）的 DXF：只重跑檢核、背景畫圖都用這個（不必再轉檔）。"""
    path = Path(job["path"])
    bound = path.with_name(path.stem + ".bound.dxf")
    converted = path.with_name(path.stem + ".converted.dxf")
    return bound if bound.exists() else (converted if job["kind"] == "dwg" else path)


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


def bind_xrefs(job: dict, src: Path, spool: Path, skip=()) -> tuple[Path, dict]:
    """主圖的外部參考（建築底圖等）在同一案件裡有上傳的話，綁定後另存 <檔名>.bound.dxf 供抽取與檢核。
    參考檔還沒轉檔的先送轉檔服務（同名的最新上傳先轉，轉不了改轉較早的）。skip：處理失敗的上傳檔名，不用。
    讀 DXF 一律在子行程（限時、限記憶體）。"""
    skip = [str(s) for s in skip]
    skipped = {s.lower() for s in skip}
    path = Path(job["path"])
    try:
        refs = json.loads(_run(["litian.drawing.xref", "list", str(src)], BIND_TIMEOUT, "外部參考讀取") or "[]")
    except Exception:
        return src, {}                    # 讀不了的圖讓後面的抽取步驟回報真正原因
    if not refs:
        return src, {}
    cands = XR.case_candidates(path.parent, exclude=path)
    for k, (_name, _ref, key) in enumerate(refs):
        for cand in cands.get(key, []):                          # 同名的最新上傳先轉；轉不了改用較早上傳的
            if cand.name.lower() in skipped:
                continue
            conv = cand.with_name(cand.stem + ".converted.dxf")
            if cand.suffix.lower() != ".dwg" or conv.exists():
                break
            try:                                                 # 工作代號帶上傳序號：逾時殘留的結果不會被別的檔拿去用
                _convert(spool, f"f{job['id']}x{k}u{XR.upload_no(cand.name)}", cand, conv)
                break
            except CC.ConvertError as e:
                log.warning("xref convert failed file=%s ref=%s: %s", job["id"], cand.name, e)
    out = path.with_name(path.stem + ".bound.dxf")
    info = json.loads(_run(["litian.drawing.xref", "bind", str(src), str(path.parent), str(out), str(path),
                            json.dumps(skip, ensure_ascii=False)], BIND_TIMEOUT, "外部參考綁定"))
    return Path(info["path"]), {"bound": info["bound"], "bound_files": info.get("bound_files", []),
                                "missing": info["missing"], "failed": info.get("failed", [])}


def requeue_xref_dependents(conn, job: dict) -> int:
    """剛處理完的檔若是別的檔的外部參考，把那些檔重新排入處理（完整重跑，才能綁定）：
    缺這個參考的；綁的是同名但較早上傳、內容不同的（重新上傳底圖：改用最新的；內容一樣就不必重跑）；
    改成「同名取最新」之前綁的舊資料（只記圖塊名、當時綁最早那份）在這份比最早那份新時。
    排隊「只重跑檢核」的也改成完整重跑（只重跑檢核沿用舊的綁定結果）。
    這份是同名主圖的另一版（綁進過別的同名上傳檔）時不排；主圖上次已經試過這份、讀不了的也不排（不會一直互相重跑）。"""
    stored = Path(job["path"]).name
    key, no = XR.name_key(stored), XR.upload_no(stored)
    rows = conn.execute("SELECT id, path, kind, sha256, status, review_only, stats FROM case_file WHERE case_id = %s",
                        (job["case_id"],)).fetchall()
    me = next((r for r in rows if r["id"] == job["id"]), None) or {}
    xstats = lambda r: (r.get("stats") or {}).get("xref") or {}
    if XR.main_version(stored, xstats(me)):
        return 0
    sha = {Path(r["path"]).name.lower(): r["sha256"] for r in rows}
    same = sorted((XR.upload_no(Path(r["path"]).name), Path(r["path"]).name.lower()) for r in rows
                  if r.get("kind") in ("dwg", "dxf") and XR.name_key(Path(r["path"]).name) == key
                  and not XR.main_version(Path(r["path"]).name, xstats(r)))
    mine = me.get("sha256")

    def stale(r: dict) -> bool:
        if r["id"] == job["id"] or not (r["status"] in ("done", "failed") or (r["status"] == "queued" and r["review_only"])):
            return False
        x = xstats(r)
        tried = {XR.strip_note(f).lower() for f in x.get("failed") or [] if not str(f).endswith("（尚未轉檔）")}
        if stored.lower() in tried:                       # 上次就是這份讀不了：重跑也一樣
            return False
        # 缺的參考：原始參考名（可能數字開頭，用 ref_key）；舊版綁定失敗時記的是存檔名（錯誤），用 name_key
        if any(XR.ref_key(XR.strip_note(m)) == key or ("（" in m and XR.name_key(m) == key) for m in x.get("missing") or []):
            return True
        if isinstance(x.get("bound_files"), list):
            return any(XR.name_key(b) == key and XR.upload_no(b) < no and sha.get(b.lower()) != mine for b in x["bound_files"])
        if any(XR.ref_key(b) == key for b in x.get("bound") or []) and same and no > same[0][0]:
            return sha.get(same[0][1]) != mine
        return False

    ids = [r["id"] for r in rows if stale(r)]
    for i in ids:
        conn.execute("UPDATE case_file SET status = 'queued', review_only = false, attempts = 0, updated_at = now() "
                     "WHERE id = %s", (i,))
    return len(ids)


def xref_skip(conn, job: dict) -> list[str]:
    """綁定外部參考時不用的上傳檔（存檔名）：同名主圖的其他版本（綁進過別的同名上傳檔）；處理失敗、而且還有其他
    同名檔可以用的（只有這一份時照試，讀不了會記在 failed）。"""
    rows = conn.execute("SELECT path, status, stats FROM case_file WHERE case_id = %s AND id <> %s AND kind IN ('dwg', 'dxf')",
                        (job.get("case_id"), job["id"])).fetchall()
    keys: dict[str, list[dict]] = {}
    for r in rows:
        keys.setdefault(XR.name_key(Path(r["path"]).name), []).append(r)
    out = []
    for r in rows:
        name = Path(r["path"]).name
        others = [o for o in keys[XR.name_key(name)] if o is not r]
        if XR.main_version(name, ((r.get("stats") or {}).get("xref"))) or \
                (r["status"] == "failed" and any(o["status"] != "failed" for o in others)):
            out.append(name)
    return out


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


def run_review(conn, job: dict, src: Path, work: Path, stats: dict | None, queue_cad: str | None = None) -> bool:
    """檢核失敗不影響抽取結果（文字、圖紙照常可看），只記下原因。回傳檢核是否成功。
    queue_cad（有樓層圖時排入 CAD 原樣圖佇列）："always"＝整個重新處理（圖可能變了）；
    "missing"＝只重跑檢核：底圖沒變、疊圖資料由檢核更新，只有還沒排過（第一次檢核失敗）或上次畫失敗的才排。
    排隊要在標成完成之前：工作台一看到完成就看得到「原圖排隊中」、會繼續自動重查。"""
    svg_dir = review_dir_of(job["path"])
    ok = False
    try:
        result = review_in_subprocess(src, work, svg_dir, ST.get_context(conn, job["case_id"]))
        ST.save_review(conn, job["id"], "done", result, None, str(svg_dir))
        ok = True
        if stats is not None:
            stats["review"] = {"floors": len(result["floors"]),
                               "findings": sum(len(f["findings"]) for f in result["floors"])}
        if queue_cad == "always" and result["floors"]:
            _set_status(svg_dir, CV.queue_status)
            ST.queue_cad(conn, job["id"])
        elif queue_cad == "missing" and result["floors"] and ST.queue_cad_if_missing(conn, job["id"]):
            _set_status(svg_dir, CV.queue_status)
    except Exception as e:
        ST.save_review(conn, job["id"], "failed", None, f"{type(e).__name__}: {e}", None)
        log.warning("review failed file=%s error=%s", job["id"], e)
    ST.mark(conn, job["id"], "done", stats)
    return ok


def _tail(path: Path, n: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _set_status(review_dir: Path, fn, *args) -> None:
    """改檢核資料夾裡的畫圖狀態檔；資料夾不在了（案件刪除）不重建，寫不進去只記日誌。"""
    if not review_dir.is_dir():
        return
    try:
        fn(review_dir, *args)
    except OSError as e:
        log.warning("cannot write cad status dir=%s error=%s", review_dir, e)


XCPU = -getattr(signal, "SIGXCPU", 24)  # 子行程超過 CPU 秒數上限（RLIMIT_CPU）時的結束代碼
CAD_BACKOFF_S = 60                      # 暫時性錯誤（磁碟滿等）後隔多久再試
CAD_TRANSIENT_MAX = 3                   # 同一個檔連續幾次暫時性錯誤就記失敗（不讓它一直擋住其他檔）


class CadRunner:
    """CAD 原樣圖在背景畫：佇列在資料庫（case_file.cad_state），一次一個子行程（限時、限記憶體、低優先順序），
    畫圖期間照常處理新上傳的檔。圖磚與各張狀態在檢核資料夾 cad/（review.cadview）。
    重新部署（worker 收到 SIGTERM，見 shutdown）、暫時性錯誤退回排隊，不算次數；被系統砍掉（多半是記憶體不足）、
    worker 當掉的重新排隊，最多畫 ST.CAD_MAX_ATTEMPTS 次；逾時、程式錯誤重畫結果相同，直接記失敗。
    主機上只有一個 worker（見 recover_cad_on_start）。"""

    def __init__(self):
        self.cur: dict | None = None
        self.claimed: dict | None = None               # 已認領、子行程還沒開始（這段收到 SIGTERM 也要退回排隊）
        self.retry_at = 0.0
        self.transient: dict[int, int] = {}            # 檔案 id → 連續暫時性錯誤次數

    def busy(self) -> bool:
        return self.cur is not None

    def start(self, conn) -> bool:
        """佇列空下來時呼叫：有排隊的就開始畫（不等畫完）。回傳是否處理了一筆排隊（含馬上失敗的）。"""
        if self.cur is not None or time.monotonic() < self.retry_at:
            return False
        for r in ST.recover_cad(conn, CAD_STALE_S):
            log.warning("stale cad render file=%s -> %s", r["id"], r["cad_state"])
            _sync_recovered(r)
        job = ST.claim_cad(conn)
        if not job:
            return False
        self.claimed = job
        try:
            return self._launch(conn, job)
        finally:
            self.claimed = None

    def _launch(self, conn, job: dict) -> bool:
        review_dir = review_dir_of(job["path"])
        work = None
        try:
            ir, review, src = ST.load_ir(conn, job["id"]), ST.load_review(conn, job["id"]), drawing_source(job)
            if ir is None or review is None or not src.is_file() or not review_dir.is_dir():
                raise RuntimeError("找不到圖面中介資料、檢核結果或圖檔")
            work = Path(tempfile.mkdtemp(prefix="litian-cad-"))
            (work / "ir.json").write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
            (work / "review.json").write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            CV.start_status(review_dir)                # 工作台看得到「畫圖中」
            with open(work / "out.txt", "wb") as out, open(work / "err.txt", "wb") as err:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "litian.review.cadview", str(src), str(work / "ir.json"),
                     str(work / "review.json"), str(review_dir)],
                    stdout=out, stderr=err, env=SUBPROC_ENV, preexec_fn=_cad_limits if os.name == "posix" else None)
        except Exception as e:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            n = self.transient.get(job["id"], 0) + 1
            if isinstance(e, OSError) and n < CAD_TRANSIENT_MAX:
                # 磁碟滿、暫存區寫不進去：退回排隊（不算次數），過一陣子再試
                self.transient[job["id"]] = n
                ST.requeue_cad(conn, job["id"], job["cad_gen"])
                _set_status(review_dir, CV.queue_status)
                self.retry_at = time.monotonic() + CAD_BACKOFF_S
                log.warning("cad start failed file=%s (will retry): %s", job["id"], e)
                return False
            self.transient.pop(job["id"], None)
            self._end(conn, job, review_dir, "failed", {"state": "failed", "error": str(e)[:200]}, str(e))
            return True
        self.transient.pop(job["id"], None)
        self.cur = {"job": job, "proc": proc, "work": work, "review_dir": review_dir, "t0": time.monotonic()}
        log.info("cad start file=%s attempt=%s", job["id"], job["cad_attempts"])
        return True

    def poll(self, conn) -> None:
        """畫完了就記結果；超過時間就砍掉。結果確實記進資料庫才收尾（記的時候資料庫斷線：重連後再記一次）。"""
        c = self.cur
        if c is None:
            return
        if "result" not in c:
            rc = c["proc"].poll()
            timed_out = rc is None and time.monotonic() - c["t0"] > CAD_TIMEOUT
            if rc is None and not timed_out:
                return
            if timed_out:
                c["proc"].kill()
                rc = c["proc"].wait()
            c["result"] = self._outcome(c, rc, timed_out)
        state, info, error = c["result"]
        self._end(conn, c["job"], c["review_dir"], state, info, error)
        shutil.rmtree(c["work"], ignore_errors=True)
        self.cur = None

    @staticmethod
    def _outcome(c: dict, rc: int, timed_out: bool) -> tuple[str, dict, str | None]:
        job = c["job"]
        secs = round(time.monotonic() - c["t0"], 1)
        if rc == 0:
            try:
                summary = json.loads(_tail(c["work"] / "out.txt").strip().splitlines()[-1])
            except (ValueError, IndexError):
                summary = {}
            summary = summary if isinstance(summary, dict) else {}
            sheets = summary.get("sheets") if isinstance(summary.get("sheets"), dict) else {}
            return "done", {"state": "done", "sheets": len(sheets), "failed": sum(v != "done" for v in sheets.values()),
                            "peak_mb": summary.get("peak_mb"), "seconds": secs}, None
        if timed_out or rc == XCPU:
            return "failed", {"state": "failed", "error": "畫圖逾時", "seconds": secs}, "畫圖逾時"
        if rc < 0 and job["cad_attempts"] < ST.CAD_MAX_ATTEMPTS:
            # 被系統砍掉（記憶體不足等，可能剛好同時在處理別的大檔）：重新排隊
            return "pending", {"state": "pending", "error": f"被中斷（{rc}）", "seconds": secs}, None
        msg = (_tail(c["work"] / "err.txt").strip().splitlines() or [f"結束代碼 {rc}"])[-1][:300]
        return "failed", {"state": "failed", "error": msg[:200], "seconds": secs}, msg

    def cancel(self, file_id: int) -> None:
        """同一個檔要整個重新處理（圖可能變了）：畫到一半的作廢（cad_gen 也會變，結果不會被記下）。"""
        c = self.cur
        if c is None or c["job"]["id"] != file_id:
            return
        self._kill(c)
        self.cur = None
        log.info("cad cancelled file=%s (reprocessing)", file_id)

    def shutdown(self, conn) -> None:
        """worker 要停（重新部署、docker stop）：已畫完的照結果記；畫到一半（或剛認領還沒開始）的砍掉、退回排隊，
        不算次數（否則每次部署都吃掉一次）。conn 為 None（資料庫連不上）：只砍子行程，下次啟動再收拾。"""
        if self.claimed is not None and self.cur is None:
            job, self.claimed = self.claimed, None
            if conn is not None and ST.requeue_cad(conn, job["id"], job["cad_gen"]):
                _set_status(review_dir_of(job["path"]), CV.queue_status)
            return
        c = self.cur
        if c is None:
            return
        if "result" in c or c["proc"].poll() is not None:
            if conn is not None:
                self.poll(conn)                        # 已畫完：照結果記（記不進去就留給下次啟動的 _finished_meanwhile）
            return
        self._kill(c)
        self.cur = None
        if conn is not None and ST.requeue_cad(conn, c["job"]["id"], c["job"]["cad_gen"]):
            _set_status(c["review_dir"], CV.queue_status)
        log.info("cad render of file=%s stopped for shutdown, requeued", c["job"]["id"])

    @staticmethod
    def _kill(c: dict) -> None:
        if c["proc"].poll() is None:
            c["proc"].kill()
        c["proc"].wait()
        shutil.rmtree(c["work"], ignore_errors=True)

    @staticmethod
    def _end(conn, job: dict, review_dir: Path, state: str, info: dict, error: str | None) -> None:
        # 先記資料庫：畫圖期間檔案重新排隊過（cad_gen 變了）就不動狀態檔（新一輪的排隊狀態不能被蓋掉）
        if not ST.finish_cad(conn, job["id"], job["cad_gen"], state, info):
            log.info("cad result dropped file=%s (requeued meanwhile)", job["id"])
            return
        if state == "failed":
            _set_status(review_dir, CV.mark_failed, error or "畫圖失敗")
        elif state == "pending":
            _set_status(review_dir, CV.queue_status)
        log.log(logging.WARNING if state == "failed" else logging.INFO, "cad file=%s %s", job["id"], info)


def _sync_recovered(row: dict) -> None:
    review_dir = review_dir_of(row["path"])
    if row["cad_state"] == "pending":
        _set_status(review_dir, CV.queue_status)
    else:
        _set_status(review_dir, CV.mark_failed, "畫圖多次中斷（處理程序重新啟動或記憶體不足）")


def _finished_meanwhile(row: dict) -> tuple[str, dict] | None:
    """上次停在「畫圖中」，其實子行程已經畫完（worker 還沒來得及記就重啟）：照狀態檔的結果記，不重畫。"""
    st = CV.read_status(review_dir_of(row["path"])) or {}
    try:
        fin = datetime.fromisoformat(st.get("finished_at") or "")
    except (TypeError, ValueError):
        return None
    started = row.get("cad_started_at")
    if st.get("state") not in ("done", "failed") or started is None or fin.tzinfo is None \
            or fin < started - timedelta(seconds=2):                          # 狀態檔是秒，開始時間更精確
        return None
    sheets = st.get("sheets") if isinstance(st.get("sheets"), dict) else {}
    return st["state"], {"state": st["state"], "sheets": len(sheets), "failed": sum(v != "done" for v in sheets.values()),
                         "recovered": True}


def _has_overlays(row: dict) -> bool:
    d = Path(row["svg_dir"] or review_dir_of(row["path"]))
    return all((d / f"{n}.overlay.json").is_file() for n in (row.get("names") or []) if isinstance(n, str))


def recover_cad_on_start(conn) -> tuple[int, int]:
    """worker 啟動（主機上只有一個 worker）：停在「畫圖中」的，子行程已畫完的照結果記；其餘是上次被中斷的 →
    重新排隊（次數用完記失敗）。檢核成功、有樓層圖卻從沒排過的（這個功能上線前的檔案）補排隊；舊版檢核沒有產生
    缺失疊圖資料的，先排只重跑檢核（完成後才會畫）。回傳（重新排隊、記失敗或照結果記的數量, 補排隊數）。"""
    done = 0
    for r in ST.rendering_cad(conn):
        got = _finished_meanwhile(r)
        if got and ST.finish_cad(conn, r["id"], r["cad_gen"], got[0], got[1]):
            done += 1
            log.info("cad file=%s finished before restart: %s", r["id"], got[1])
    rows = ST.recover_cad(conn, 0)
    for r in rows:
        _sync_recovered(r)
    filled = ST.backfill_cad(conn)
    for r in filled:
        _set_status(review_dir_of(r["path"]), CV.queue_status)
        if not _has_overlays(r) and ST.requeue_review(conn, r["id"]):
            log.info("file=%s has no overlay data; re-running review before drawing", r["id"])
    return done + len(rows), len(filled)


def process(conn, job: dict, spool: Path, cad: CadRunner | None = None) -> dict:
    path = Path(job["path"])
    converted = path.with_name(path.stem + ".converted.dxf")
    if job.get("review_only"):
        # 只重跑檢核（檢核條件改了）：用已存的中介資料與轉好（或已綁定外部參考）的 DXF
        ir = ST.load_ir(conn, job["id"])
        src = drawing_source(job)
        if ir is not None and src.exists():
            with tempfile.TemporaryDirectory() as d:
                work = Path(d)
                (work / "ir.json").write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
                ST.mark(conn, job["id"], "reviewing")
                run_review(conn, job, src, work, None, queue_cad="missing")
            return {"review_only": True}
    ST.reset_cad(conn, job["id"])                      # 整個重新處理：圖可能變了，舊的原圖排隊作廢
    if cad is not None:
        cad.cancel(job["id"])
    if job["kind"] == "dwg":
        src = _convert(spool, f"f{job['id']}", path, converted)   # 保留轉好的 DXF，之後重新抽取、重跑檢核不必再轉
    else:
        src = path
    src, xinfo = bind_xrefs(job, src, spool, skip=xref_skip(conn, job))
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        ir, stats = extract_in_subprocess(src, work, expand=xinfo.get("bound", []))
        if xinfo:
            stats["xref"] = xinfo
        review = has_floor_plans(ir)
        with conn.transaction():
            ST.save_result(conn, job["id"], ir, stats, status="reviewing" if review else "done")
        if review:
            run_review(conn, job, src, work, stats, queue_cad="always")
    return stats


_current: dict = {"job": None}                         # 主佇列正在處理的檔（SIGTERM 時退回排隊）


def run_once(conn, spool: Path, cad: CadRunner | None = None) -> bool:
    job = ST.claim(conn)
    if not job:
        return False
    _current["job"] = job
    try:
        return _run_job(conn, job, spool, cad)
    finally:
        _current["job"] = None


def _run_job(conn, job: dict, spool: Path, cad: CadRunner | None) -> bool:
    try:
        stats = process(conn, job, spool, cad)
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


class Stop(BaseException):
    """收到 SIGTERM（docker stop、重新部署）。不是 Exception：處理中的 except Exception 不會把它當成檔案失敗。"""


def _on_term(signum, frame):
    raise Stop()


def tick(conn, spool: Path, cad: CadRunner) -> bool:
    """主迴圈的一步：先收畫圖結果，再處理新上傳的（優先），佇列空了才開始畫原圖。回傳是否做了事（沒做事就休息）。
    背景畫圖出的錯（資料庫斷線以外）只記日誌，不讓 worker 停掉、也不擋新上傳的處理。"""
    import psycopg

    def guarded(fn) -> bool:
        try:
            return bool(fn(conn))
        except psycopg.OperationalError:
            raise
        except Exception:
            log.exception("cad background error")
            return False

    guarded(cad.poll)
    if run_once(conn, spool, cad):
        return True
    return guarded(cad.start)


def main() -> int:
    import psycopg
    from psycopg.rows import dict_row
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    spool = Path(os.environ.get("CONVERT_SPOOL", "/data/convert"))
    cad = CadRunner()                                  # 斷線重連時沿用：畫到一半的子行程照常畫
    signal.signal(signal.SIGTERM, _on_term)            # python 是容器的 PID 1：要自己接 SIGTERM 才收得到
    try:
        while True:
            try:
                with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
                    ST.ensure_schema(conn)
                    # 主機上只有一個 worker：連上（啟動或重連）時「處理中」的都沒有人在處理了（重連前的處理已因斷線中止；
                    # 也包含認領已寫進資料庫、還沒收到回應就被停掉的）→ 馬上退回排隊，不必等逾時
                    n = ST.recover_stale(conn, 0)
                    m = recover_cad_on_start(conn) if not cad.busy() else (0, 0)
                    log.info("worker ready (recovered %s stale jobs; cad recovered %s, backfilled %s)", n, *m)
                    while True:
                        if not tick(conn, spool, cad):
                            time.sleep(IDLE_S)
            except psycopg.OperationalError as e:
                log.warning("database unavailable: %s; retry in 5s", type(e).__name__)
                time.sleep(5)
    except Stop:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        log.info("SIGTERM: stopping")
        job = _current["job"]
        try:
            with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True,
                                 connect_timeout=5) as conn:
                if job is not None and ST.requeue_job(conn, job["id"]):     # 處理到一半的檔：退回排隊、不算次數
                    log.info("file=%s requeued for shutdown", job["id"])
                cad.shutdown(conn)
        except psycopg.Error:
            cad.shutdown(None)                         # 記不進去：下次啟動照「被中斷」處理（算一次）
        return 0


if __name__ == "__main__":
    sys.exit(main())
