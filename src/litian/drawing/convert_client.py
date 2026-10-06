"""worker 端與轉檔服務（無網路沙箱）之間的檔案交接協定。

共用資料夾 spool/：
- worker 先寫 in/<job>.dwg.part，再改名成 in/<job>.dwg（改名是原子動作，轉檔服務看到 .dwg 就代表檔案完整）
- 轉檔服務寫 out/<job>.dxf（修補後的 DXF），最後才寫 out/<job>.json（結果）；worker 看到 json 才讀
- worker 取走結果後刪掉 out/ 裡的兩個檔；送件前先清掉同代號的舊結果（見 submit）
轉檔服務的程式在 deploy/oracle/libredwg/converter.py。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ConvertError(Exception):
    pass


def submit(spool: Path, job: str, dwg: Path) -> None:
    if not JOB_RE.match(job):
        raise ValueError(f"工作代號不合法：{job}")
    # 工作代號固定（f<檔案id>…）：等轉檔逾時、worker 重啟後才交出的結果沒人取走，不清掉會被這次直接拿去用
    # （例：轉檔器升級前的舊結果）
    cleanup(spool, job)
    inbox = spool / "in"
    inbox.mkdir(parents=True, exist_ok=True)
    part = inbox / f"{job}.dwg.part"
    shutil.copyfile(dwg, part)
    os.replace(part, inbox / f"{job}.dwg")


def wait(spool: Path, job: str, timeout_s: float, poll_s: float = 1.0) -> tuple[Path, dict]:
    """等轉檔結果；成功回傳（DXF 路徑, 結果），失敗丟 ConvertError。逾時會把未處理的輸入撤回。"""
    res_path = spool / "out" / f"{job}.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if res_path.exists():
            res = json.loads(res_path.read_text(encoding="utf-8"))
            if not res.get("ok"):
                cleanup(spool, job)
                raise ConvertError(res.get("error") or "轉檔失敗")
            dxf = spool / "out" / f"{job}.dxf"
            if dxf.exists():
                return dxf, res
            # 沒有 DXF：同代號的舊工作交件到一半（DXF 已寫、結果還沒寫）時被 submit 清掉了，繼續等這次送出的
        time.sleep(poll_s)
    (spool / "in" / f"{job}.dwg").unlink(missing_ok=True)
    raise ConvertError(f"等待轉檔逾時（{int(timeout_s)} 秒），轉檔服務可能沒有在跑")


def cleanup(spool: Path, job: str) -> None:
    for ext in (".dxf", ".json"):
        (spool / "out" / f"{job}{ext}").unlink(missing_ok=True)
