# -*- coding: utf-8 -*-
"""轉檔服務（容器內常駐）：監看 /convert/in，把 DWG 轉成修補後的 DXF，交到 /convert/out。

容器設定（docker-compose.yml 的 converter）：無網路、唯讀根目錄、限記憶體與 CPU、不碰資料庫。
LibreDWG 仍是 beta、近期還在修記憶體安全漏洞，而輸入是外部上傳的檔案；就算被惡意 DWG 攻破，
這個容器也只碰得到 /convert 這個交接資料夾。

交接協定（與 src/litian/drawing/convert_client.py 對應）：
- 只處理 in/<job>.dwg（worker 先寫 .part 再改名，看到 .dwg 代表檔案完整）
- 產出 out/<job>.dxf（修補後），最後寫 out/<job>.json；處理完刪掉輸入
"""
import json
import os
import re
import subprocess
import sys
import time

BASE = os.environ.get("CONVERT_DIR", "/convert")
IN, OUT, WORK = (os.path.join(BASE, d) for d in ("in", "out", "work"))
JOB_RE = re.compile(r"^([A-Za-z0-9_-]{1,64})\.dwg$")
TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT", "180"))
MAX_BYTES = int(os.environ.get("CONVERT_MAX_BYTES", str(200 * 1024 * 1024)))
HERE = os.path.dirname(os.path.abspath(__file__))


def write_json(path, data):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def convert(job, src):
    res = {"job": job, "ok": False}
    t0 = time.time()
    size = os.path.getsize(src)
    res["bytes"] = size
    if size > MAX_BYTES:
        res["error"] = f"檔案過大（{size} 位元組，上限 {MAX_BYTES}）"
        return res
    raw = os.path.join(WORK, job + ".raw.dxf")
    fixed = os.path.join(WORK, job + ".dxf")
    try:
        r = subprocess.run(["dwg2dxf", "-y", "-o", raw, src], capture_output=True, timeout=TIMEOUT)
        res["dwg2dxf_rc"] = r.returncode
        res["stderr_tail"] = r.stderr.decode("utf-8", "replace")[-400:]
    except subprocess.TimeoutExpired:
        res["error"] = f"轉檔逾時（{TIMEOUT} 秒）"
        return res
    if not os.path.exists(raw) or os.path.getsize(raw) == 0:
        res["error"] = "dwg2dxf 沒有產生 DXF（檔案可能損壞或版本不支援）"
        return res
    rep = subprocess.run([sys.executable, os.path.join(HERE, "repair_dxf.py"), raw, fixed],
                         capture_output=True, text=True, timeout=TIMEOUT)
    os.remove(raw)
    if rep.returncode != 0 or not os.path.exists(fixed):
        res["error"] = "DXF 修補失敗：" + (rep.stderr.strip().splitlines() or [""])[-1][:200]
        return res
    res["repair"] = rep.stdout.strip().split(": ", 1)[-1]
    os.replace(fixed, os.path.join(OUT, job + ".dxf"))
    res["ok"] = True
    res["seconds"] = round(time.time() - t0, 2)
    return res


def main():
    for d in (IN, OUT, WORK):
        os.makedirs(d, exist_ok=True)
    print(json.dumps({"converter": "ready", "dir": BASE}), flush=True)
    while True:
        for name in sorted(os.listdir(IN)):
            m = JOB_RE.match(name)
            if not m:
                continue
            job, src = m.group(1), os.path.join(IN, name)
            try:
                res = convert(job, src)
            except Exception as e:  # 任何意外都要回報給 worker，不能讓它乾等
                res = {"job": job, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            write_json(os.path.join(OUT, job + ".json"), res)
            try:
                os.remove(src)
            except FileNotFoundError:
                pass
            print(json.dumps(res, ensure_ascii=False), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
