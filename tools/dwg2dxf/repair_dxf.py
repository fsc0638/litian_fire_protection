# -*- coding: utf-8 -*-
"""
修補 LibreDWG dwg2dxf 輸出的 DXF：
1) DXF 是「群組碼行／值行」成對；若某行該是群組碼卻不是整數，代表上一個值裡夾了換行 → 併回上一個值。
2) APPID 表的名稱（AcDbRegAppTableRecord 後的群組碼 2）若含控制字元或非法位元組 → 換成 APP_<handle>。
用法：python repair_dxf.py in.dxf out.dxf
"""
import sys, re
src, dst = sys.argv[1], sys.argv[2]
raw = open(src, "rb").read()
lines = raw.split(b"\n")
lines = [l.rstrip(b"\r") for l in lines]
while lines and lines[-1].strip() == b"":   # 檔尾空行不是斷行，不要併進 EOF
    lines.pop()
code_re = re.compile(rb"^\s*-?\d+\s*$")
out = []
i = 0
merged = 0
while i < len(lines):
    code = lines[i]
    if not code_re.match(code):
        # 這行不是群組碼：併回前一個值
        if out:
            out[-1] = out[-1] + b" " + code.strip()
            merged += 1
        i += 1
        continue
    val = lines[i + 1] if i + 1 < len(lines) else b""
    out.append(code); out.append(val)
    i += 2
# 修 APPID 名稱
fixed = 0
for k in range(0, len(out) - 1, 2):
    if out[k].strip() == b"100" and out[k + 1].strip() == b"AcDbRegAppTableRecord":
        # 往後找群組碼 2
        for m in range(k + 2, min(k + 12, len(out) - 1), 2):
            if out[m].strip() == b"2":
                name = out[m + 1]
                bad = any(b < 0x20 or b == 0x7F for b in name) or b"?" in name
                try:
                    name.decode("utf-8")
                except UnicodeDecodeError:
                    bad = True
                if bad:
                    # 找 handle（群組碼 5，在 k 之前）
                    h = b"X"
                    for q in range(k - 2, max(k - 12, 0), -2):
                        if out[q].strip() == b"5":
                            h = out[q + 1].strip(); break
                    out[m + 1] = b"APP_" + h
                    fixed += 1
                break
open(dst, "wb").write(b"\r\n".join(out) + b"\r\n")
print(f"{src.split('/')[-1]}: merged_lines={merged} appid_fixed={fixed}")
