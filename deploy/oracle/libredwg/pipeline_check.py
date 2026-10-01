# -*- coding: utf-8 -*-
"""容器內的三步管線檢查：dwg2dxf → repair_dxf.py → ezdxf.recover。
用法：pipeline_check.py <DWG 資料夾> <輸出資料夾>
輸出：<輸出資料夾>/report.json、report.md；最後一行印 SUCCESS n/N。
"""
import json, os, platform, re, resource, subprocess, sys, time

src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))
ver = subprocess.run(["dwg2dxf", "--version"], capture_output=True, text=True).stdout.strip()

rows = []
for f in sorted(x for x in os.listdir(src) if x.lower().endswith(".dwg")):
    stem = f[:-4]
    dwg = os.path.join(src, f)
    dxf = os.path.join(dst, stem + ".dxf")
    fixed = os.path.join(dst, stem + ".repaired.dxf")
    row = {"file": stem}
    t0 = time.time()
    try:
        r = subprocess.run(["dwg2dxf", "-y", "-o", dxf, dwg], capture_output=True, timeout=120)
        row["conv_rc"] = r.returncode
    except subprocess.TimeoutExpired:
        row["conv_rc"] = "TIMEOUT"
    row["conv_s"] = round(time.time() - t0, 2)
    if not os.path.exists(dxf):
        rows.append(row); print(row, flush=True); continue
    rep = subprocess.run([PY, os.path.join(HERE, "repair_dxf.py"), dxf, fixed], capture_output=True, text=True)
    row["repair"] = rep.stdout.strip().split(": ", 1)[-1]
    t1 = time.time()
    try:
        from ezdxf import recover
        doc, aud = recover.readfile(fixed)
        cnt = {}
        for e in doc.modelspace():
            cnt[e.dxftype()] = cnt.get(e.dxftype(), 0) + 1
        zh = []
        for e in doc.modelspace().query("TEXT MTEXT"):
            s = e.dxf.text if e.dxftype() == "TEXT" else e.text
            if re.search(r"[一-鿿]", s or ""):
                zh.append(re.sub(r"\s+", " ", s)[:20])
            if len(zh) >= 3:
                break
        row.update(ok=True, entities=sum(cnt.values()), INSERT=cnt.get("INSERT", 0),
                   TEXT=cnt.get("TEXT", 0), MTEXT=cnt.get("MTEXT", 0), layers=len(doc.layers), zh=zh)
    except Exception as ex:
        row.update(ok=False, error=f"{type(ex).__name__}: {str(ex)[:160]}")
    row["load_s"] = round(time.time() - t1, 2)
    rows.append(row)
    print(row, flush=True)

ok = sum(1 for r in rows if r.get("ok"))
child_peak_mb = round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024)
self_peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
summary = {"libredwg": ver, "arch": platform.machine(), "success": f"{ok}/{len(rows)}",
           "child_peak_mb": child_peak_mb, "loader_peak_mb": self_peak_mb,
           "total_conv_s": round(sum(r.get("conv_s", 0) for r in rows), 2)}
json.dump({"summary": summary, "rows": rows}, open(os.path.join(dst, "report.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
with open(os.path.join(dst, "report.md"), "w", encoding="utf-8") as fh:
    fh.write(f"# LibreDWG 管線檢查（{summary['arch']}）\n\n")
    for k, v in summary.items():
        fh.write(f"- {k}: {v}\n")
    fh.write("\n| 檔名 | 轉檔碼 | 轉檔秒 | 修補 | 可讀 | 實體 | 圖塊 | 中文抽樣 |\n|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        fh.write(f"| {r['file']} | {r.get('conv_rc')} | {r.get('conv_s')} | {r.get('repair','-')} | {r.get('ok')} | "
                 f"{r.get('entities','-')} | {r.get('INSERT','-')} | {'；'.join(r.get('zh', [])) or r.get('error','')} |\n")
print("SUMMARY", json.dumps(summary, ensure_ascii=False))
print(f"SUCCESS {ok}/{len(rows)}")
