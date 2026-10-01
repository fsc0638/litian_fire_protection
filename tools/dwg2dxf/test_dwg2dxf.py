# -*- coding: utf-8 -*-
"""
LibreDWG dwg2dxf 實測腳本
用法：python test_dwg2dxf.py <dwg2dxf.exe 路徑> <DWG 來源資料夾> <DXF 輸出資料夾> <報告.md 路徑>
每個 DWG：限時 120 秒、記錄退出碼／stderr 摘要／DXF 大小；轉成功者用 ezdxf 開啟並統計
TEXT/MTEXT/INSERT/LWPOLYLINE/圖層數，抽前 10 個中文文字當抽樣證據。
"""
import sys, os, subprocess, time, re, io, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pylib"))

exe, src, dst, report = sys.argv[1:5]
os.makedirs(dst, exist_ok=True)
rows = []
dwgs = sorted(f for f in os.listdir(src) if f.lower().endswith(".dwg"))
print(f"found {len(dwgs)} dwg files")

for f in dwgs:
    inp = os.path.join(src, f)
    out = os.path.join(dst, os.path.splitext(f)[0] + ".dxf")
    if os.path.exists(out):
        os.remove(out)
    t0 = time.time()
    try:
        p = subprocess.run([exe, "-y", "-o", out, inp], capture_output=True, timeout=120)
        rc, err = p.returncode, (p.stderr or b"").decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        rc, err = "TIMEOUT", ""
    dt = round(time.time() - t0, 1)
    size = os.path.getsize(out) if os.path.exists(out) else 0
    row = {"file": f, "rc": rc, "sec": dt, "dxf_mb": round(size / 1e6, 2),
           "err_lines": len(err.splitlines()), "err_head": " | ".join(err.splitlines()[:3])[:300]}
    # 統計
    if size > 0:
        try:
            import ezdxf
            doc = ezdxf.readfile(out)
            msp = doc.modelspace()
            cnt = {}
            for e in msp:
                cnt[e.dxftype()] = cnt.get(e.dxftype(), 0) + 1
            texts = []
            for e in msp.query("TEXT MTEXT"):
                s = e.dxf.text if e.dxftype() == "TEXT" else e.text
                if re.search(r"[一-鿿]", s or ""):
                    texts.append(re.sub(r"\s+", " ", s)[:30])
                if len(texts) >= 10:
                    break
            row.update({"dxfver": doc.dxfversion, "layers": len(doc.layers),
                        "TEXT": cnt.get("TEXT", 0), "MTEXT": cnt.get("MTEXT", 0),
                        "INSERT": cnt.get("INSERT", 0), "LWPOLYLINE": cnt.get("LWPOLYLINE", 0),
                        "entities": sum(cnt.values()), "sample_text": texts})
        except Exception as ex:
            row["ezdxf_error"] = f"{type(ex).__name__}: {str(ex)[:200]}"
    rows.append(row)
    print(f"{f}: rc={rc} {dt}s dxf={row['dxf_mb']}MB entities={row.get('entities','-')} err_lines={row['err_lines']}")

ok = sum(1 for r in rows if r["rc"] == 0 and r["dxf_mb"] > 0 and "ezdxf_error" not in r)
with io.open(report, "w", encoding="utf-8") as fh:
    fh.write("# LibreDWG 0.14 dwg2dxf 實測報告\n\n")
    fh.write(f"- 執行檔：`{exe}`\n- 來源：`{src}`\n- 輸出：`{dst}`\n- 日期：{time.strftime('%Y-%m-%d %H:%M')}\n")
    fh.write(f"- 結果：**{ok}/{len(rows)} 個 DWG 轉檔成功且 ezdxf 可讀**\n\n")
    fh.write("| 檔名 | 退出碼 | 秒 | DXF MB | DXF 版本 | 圖層 | TEXT | MTEXT | INSERT(圖塊) | LWPOLYLINE | 實體總數 | stderr 行數 | 備註 |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        note = r.get("ezdxf_error", "") or r["err_head"]
        fh.write(f"| {r['file']} | {r['rc']} | {r['sec']} | {r['dxf_mb']} | {r.get('dxfver','-')} | {r.get('layers','-')} | {r.get('TEXT','-')} | {r.get('MTEXT','-')} | {r.get('INSERT','-')} | {r.get('LWPOLYLINE','-')} | {r.get('entities','-')} | {r['err_lines']} | {note} |\n")
    fh.write("\n## 中文文字抽樣（每檔前 10 筆，證明文字層可讀）\n\n")
    for r in rows:
        if r.get("sample_text"):
            fh.write(f"- **{r['file']}**：{'；'.join(r['sample_text'])}\n")
    fh.write("\n## 原始結果 JSON\n\n```json\n" + json.dumps(rows, ensure_ascii=False, indent=1) + "\n```\n")
print(f"\nREPORT: {report}\nSUCCESS {ok}/{len(rows)}")
