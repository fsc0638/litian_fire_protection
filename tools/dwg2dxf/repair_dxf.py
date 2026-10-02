# -*- coding: utf-8 -*-
"""
修補 LibreDWG dwg2dxf 輸出的 DXF：
1) DXF 是「群組碼行／值行」成對；若某行該是群組碼卻不是整數，代表上一個值裡夾了換行 → 併回上一個值。
2) APPID 表的名稱（AcDbRegAppTableRecord 後的群組碼 2）若含控制字元或非法位元組 → 換成 APP_<handle>。
逐行串流處理，記憶體用量固定（竣工圖轉出的 DXF 可達數十 MB，整檔讀入會超過轉檔容器的記憶體上限）。
用法：python repair_dxf.py in.dxf out.dxf
"""
import re
import sys
from collections import deque

CODE = re.compile(rb"^\s*-?\d+\s*$")


def pairs(f, stats):
    """逐行讀出（群組碼, 值）；不是整數的群組碼行併回上一個值。檔尾的空行略過。"""
    pending = None
    blanks = 0                        # 群組碼位置的空行先記著：後面還有內容才併回（與舊版一致），到檔尾就丟掉
    it = (line.rstrip(b"\r\n") for line in f)
    for line in it:
        if line.strip() == b"":
            blanks += 1
            continue
        if blanks and pending is not None:
            pending[1] = pending[1] + b" " * blanks
            stats["merged"] += blanks
        blanks = 0
        if CODE.match(line):
            val = next(it, b"")
            if pending is not None:
                yield pending
            pending = [line, val.rstrip(b"\r\n") if isinstance(val, bytes) else val]
        elif pending is not None:
            pending[1] = pending[1] + b" " + line.strip()
            stats["merged"] += 1
    if pending is not None:
        yield pending


def bad_name(name: bytes) -> bool:
    if any(b < 0x20 or b == 0x7F for b in name) or b"?" in name:
        return True
    try:
        name.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def repair(src: str, dst: str) -> dict:
    stats = {"merged": 0, "fixed": 0}
    recent = deque(maxlen=6)          # 往前找 handle（群組碼 5）用
    want, handle = 0, b"X"            # 剛看到 AcDbRegAppTableRecord：接下來 5 組內的群組碼 2 是名稱
    with open(src, "rb") as f, open(dst, "wb", buffering=1 << 20) as out:
        for code, val in pairs(f, stats):
            c = code.strip()
            if want:
                if c == b"2":
                    if bad_name(val):
                        val = b"APP_" + handle
                        stats["fixed"] += 1
                    want = 0
                else:
                    want -= 1
            if c == b"100" and val.strip() == b"AcDbRegAppTableRecord":
                want = 5
                handle = next((v.strip() for k, v in reversed(recent) if k.strip() == b"5"), b"X")
            recent.append((code, val))
            out.write(code + b"\r\n" + val + b"\r\n")
    return stats


if __name__ == "__main__":
    s = repair(sys.argv[1], sys.argv[2])
    print(f"{sys.argv[1].split('/')[-1]}: merged_lines={s['merged']} appid_fixed={s['fixed']}")
