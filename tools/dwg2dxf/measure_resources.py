# -*- coding: utf-8 -*-
"""量測三步管線每一步的尖峰記憶體、耗時與 CPU 使用核數。
步驟 1：dwg2dxf.exe（子行程，輪詢 peak working set 與 CPU 時間）
步驟 2：repair_dxf.py（只對 4 個需修補檔）
步驟 3：ezdxf.recover.readfile（子行程，量該 Python 行程的尖峰記憶體）
"""
import os, sys, time, subprocess, json, psutil

S = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(S, "libredwg", "dwg2dxf.exe")
if len(sys.argv) < 2:
    sys.exit("用法：python measure_resources.py <DWG樣本資料夾>")
SRC = sys.argv[1]
OUT = os.path.join(S, "measure_out")
os.makedirs(OUT, exist_ok=True)
PY = sys.executable
ENV = dict(os.environ, PYTHONPATH=os.path.join(S, "pylib"), PYTHONIOENCODING="utf-8")
NEED_REPAIR = {"A1-10_防火區劃+步行距離", "A7-01_建築物安全維護裝置平面圖", "A7-07_天花板平面圖", "A7-09_樓地板裝修平面圖"}

def run_measured(cmd, env=None):
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    ps = psutil.Process(p.pid)
    peak = 0; cpu = 0.0
    while p.poll() is None:
        try:
            mi = ps.memory_info()
            peak = max(peak, getattr(mi, "peak_wset", mi.rss))
            ct = ps.cpu_times(); cpu = ct.user + ct.system
        except psutil.Error:
            pass
        time.sleep(0.005)
    wall = time.perf_counter() - t0
    return p.returncode, wall, peak, cpu

LOAD = r'''
import sys, os, psutil
from ezdxf import recover
doc, aud = recover.readfile(sys.argv[1])
n = sum(1 for _ in doc.modelspace())
mi = psutil.Process().memory_info()
print(n, getattr(mi, "peak_wset", mi.rss))
'''
rows = []
for f in sorted(x for x in os.listdir(SRC) if x.lower().endswith(".dwg")):
    stem = f[:-4]
    dwg = os.path.join(SRC, f); dxf = os.path.join(OUT, stem + ".dxf")
    rc1, w1, m1, c1 = run_measured([EXE, "-y", "-o", dxf, dwg])
    target = dxf; w2 = 0.0; m2 = 0
    if stem in NEED_REPAIR:
        target = os.path.join(OUT, stem + ".repaired.dxf")
        _, w2, m2, _ = run_measured([PY, os.path.join(S, "repair_dxf.py"), dxf, target], env=ENV)
    t0 = time.perf_counter()
    r = subprocess.run([PY, "-c", LOAD, target], capture_output=True, env=ENV, text=True)
    w3 = time.perf_counter() - t0
    try:
        n, m3 = r.stdout.split(); n = int(n); m3 = int(m3)
    except Exception:
        n, m3 = -1, 0
    rows.append(dict(file=stem, dwg_mb=round(os.path.getsize(dwg)/1e6, 2), dxf_mb=round(os.path.getsize(dxf)/1e6, 2),
                     conv_s=round(w1, 2), conv_cpu_s=round(c1, 2), conv_peak_mb=round(m1/1e6),
                     repair_s=round(w2, 2), repair_peak_mb=round(m2/1e6),
                     load_s=round(w3, 2), load_peak_mb=round(m3/1e6), entities=n))
    print(rows[-1], flush=True)

def mx(k): return max(r[k] for r in rows)
summary = dict(files=len(rows), total_conv_s=round(sum(r["conv_s"] for r in rows), 2),
               max_conv_peak_mb=mx("conv_peak_mb"), max_repair_peak_mb=mx("repair_peak_mb"),
               max_load_peak_mb=mx("load_peak_mb"), max_load_s=mx("load_s"),
               total_pipeline_s=round(sum(r["conv_s"] + r["repair_s"] + r["load_s"] for r in rows), 1),
               cpu_util_during_conv=round(sum(r["conv_cpu_s"] for r in rows) / max(sum(r["conv_s"] for r in rows), 1e-9), 2))
print("SUMMARY", json.dumps(summary, ensure_ascii=False))
json.dump(dict(rows=rows, summary=summary), open(os.path.join(S, "measure_result.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
