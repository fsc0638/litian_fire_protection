"""worker 端與轉檔服務（無網路沙箱）之間的檔案交接協定。

共用資料夾 spool/：
- worker 先寫 in/<job>.dwg.part，再改名成 in/<job>.dwg（改名是原子動作，轉檔服務看到 .dwg 就代表檔案完整）
- 轉檔服務寫 out/<job>.dxf（修補後的 DXF），最後才寫 out/<job>.json（結果）；worker 看到 json 才讀
- worker 取走結果後刪掉 out/ 裡的兩個檔
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
            return spool / "out" / f"{job}.dxf", res
        time.sleep(poll_s)
    (spool / "in" / f"{job}.dwg").unlink(missing_ok=True)
    raise ConvertError(f"等待轉檔逾時（{int(timeout_s)} 秒），轉檔服務可能沒有在跑")


def cleanup(spool: Path, job: str) -> None:
    for ext in (".dxf", ".json"):
        (spool / "out" / f"{job}{ext}").unlink(missing_ok=True)
