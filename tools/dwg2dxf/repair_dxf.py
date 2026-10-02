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


# 3) 長文字（MTEXT 等）每 250 字切一段（群組碼 3…3、最後 1）；切點落在 \U+XXXX 中間時，
#    殘缺的跳脫碼會讓 ezdxf 讀檔中斷 → 把殘段移到下一段開頭；後面沒有接續段就丟掉殘段。
PARTIAL_ESC = re.compile(rb"\\[Uu](?:\+[0-9A-Fa-f]{0,3})?$|\\$")


def repair(src: str, dst: str) -> dict:
    stats = {"merged": 0, "fixed": 0, "escapes": 0}
    recent = deque(maxlen=6)          # 往前找 handle（群組碼 5）用
    want, handle = 0, b"X"            # 剛看到 AcDbRegAppTableRecord：接下來 5 組內的群組碼 2 是名稱
    carry = b""                       # 上一段被切斷的跳脫碼殘段
    with open(src, "rb") as f, open(dst, "wb", buffering=1 << 20) as out:
        for code, val in pairs(f, stats):
            c = code.strip()
            if carry:
                if c in (b"1", b"3"):
                    val = carry + val
                carry = b""
            m = PARTIAL_ESC.search(val)
            if m and c in (b"1", b"3"):
                carry, val = (val[m.start():], val[:m.start()]) if c == b"3" else (b"", val[:m.start()])
                stats["escapes"] += 1
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
    print(f"{sys.argv[1].split('/')[-1]}: merged_lines={s['merged']} appid_fixed={s['fixed']} split_escapes={s['escapes']}")
