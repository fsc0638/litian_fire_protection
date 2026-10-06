"""條文裡的方框字元表格 → 區塊清單（前端畫成真正的表格）。

全國法規資料庫的表格用方框字元排版，欄位靠「顯示寬度」對齊：方框字元與中文字佔 2 格、半形英數佔 1 格。
常見合併儲存格（跨列、跨欄）、一格多行（文字在格內換行），以及「部分分隔線」（同一行對某些欄是分隔線、其他欄是文字續行）。

做法：
1. 把條文切成「表格行」（第一個非空白字元是方框字元）與一般文字；
2. 表格依顯示寬度排成字元格，所有直線類字元的位置合起來就是欄界；
3. 每一行的每一欄位叫一個「單位」：內容全是「─」且左右至少一側連著橫線＝分隔線，其餘為文字；
4. 同一行相鄰文字單位之間沒有直線就併在一起，上下相鄰的文字單位也併在一起，併出來的每一塊就是一個儲存格（必須是長方形）；
5. 有分隔線的行把表格切成邏輯列，由儲存格涵蓋的列與欄算出跨列、跨欄。

區塊格式（前後端共用）：
  {"type": "text", "text": 原文（保留換行與行首縮排）}
  {"type": "table", "rows": [[{"text", "rowspan", "colspan"}, ...], ...], "header_rows": 表頭列數}
     rows 為邏輯列，每列只列出「從這一列開始」的儲存格（由左到右），與 HTML 表格語意相同。
  {"type": "pre", "text": 原樣方框字元}   表格對不齊、解析不了時的退路（不丟錯）
"""

from __future__ import annotations

import unicodedata

BOX = "┌┐└┘├┤┬┴┼─│"
VERT = "┌┐└┘├┤┬┴┼│"         # 有直線的字元（欄界）
TO_RIGHT = "┌└├┼┬┴─"         # 向右連出橫線
TO_LEFT = "┐┘┤┼┬┴─"          # 向左連出橫線


def _width(ch: str) -> int:
    """顯示寬度。寬度不明確（A）的字：拉丁補充區（³、×）佔 1 格，其餘（–、○、Ⅰ、℃）在法規表格裡佔 2 格。"""
    if ch in BOX:
        return 2
    e = unicodedata.east_asian_width(ch)
    if e in "WF" or (e == "A" and ord(ch) > 0xFF):
        return 2
    return 1


def _is_table_line(line: str) -> bool:
    s = line.lstrip()
    return bool(s) and s[0] in BOX


def _segments(text: str) -> list[tuple[bool, list[str]]]:
    """切成（是否表格, 各行）。連續兩行以上的表格行才算表格；單獨一行（例：公式的根號上橫線）當一般文字。"""
    lines = (text or "").split("\n")
    tab = [_is_table_line(l) for l in lines]
    segs: list[tuple[bool, list[str]]] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and tab[j] == tab[i]:
            j += 1
        is_tab = tab[i] and j - i >= 2
        if segs and not is_tab and not segs[-1][0]:
            segs[-1][1].extend(lines[i:j])
        else:
            segs.append((is_tab, lines[i:j]))
        i = j
    return segs


def has_table(text: str) -> bool:
    return any(t for t, _ in _segments(text))


def _join(parts: list[str]) -> str:
    """格內各行接起來不加分隔（中文在格內斷行）；兩側都是半形英數時補一個空白。"""
    out = ""
    for p in parts:
        if not p:
            continue
        if out and _alnum(out[-1]) and _alnum(p[0]):
            out += " "
        out += p
    return out


def _alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _conn(ch: str | None, chars: str) -> bool:
    return ch is not None and ch in chars


def _text_block(lines: list[str]) -> dict | None:
    """一般文字：去掉頭尾的空白行（只是和表格隔開用），其餘原樣保留。"""
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return {"type": "text", "text": "\n".join(lines)} if lines else None


def _parse(lines: list[str]) -> dict:
    """一段表格行 → table 區塊；對不齊、不是長方形等任何不一致都丟 ValueError。"""
    lay = []                                            # 每行：[(x, 字, 寬)]
    for line in lines:
        row, x = [], 0
        for ch in line.rstrip():
            w = _width(ch)
            row.append((x, ch, w))
            x += w
        lay.append(row)
    bounds = sorted({x for row in lay for x, ch, _ in row if ch in VERT})
    if len(bounds) < 2:
        raise ValueError("欄界不足")
    if any(b - a < 4 for a, b in zip(bounds, bounds[1:])):
        raise ValueError("欄界太近（直線錯位）")
    nslot, nline = len(bounds) - 1, len(lay)

    def cover(row, b):
        for x, ch, w in row:
            if x <= b < x + w:
                return ch
        return None

    content: list[list[str]] = []                       # [行][欄位] 內容
    kind: list[list[str]] = []                          # [行][欄位] rule / text
    vert: list[list[bool]] = []                         # [行][欄界] 這一行在欄界上是不是直線
    for row in lay:
        if any(not ch.isspace() for x, ch, _ in row if x < bounds[0]):
            raise ValueError("表格左框外有文字")
        if any(not ch.isspace() and not (x == bounds[-1] and ch in BOX) for x, ch, _ in row if x >= bounds[-1]):
            raise ValueError("表格右框外有文字")
        edge = [cover(row, b) for b in bounds]
        if not _conn(edge[0], VERT) or not _conn(edge[-1], VERT):
            raise ValueError("左右外框沒對齊")
        vert.append([c is not None and c in VERT for c in edge])
        cs, ks = [], []
        for i in range(nslot):
            lo, hi = bounds[i], bounds[i + 1]
            s = "".join(ch for x, ch, _ in row if lo <= x < hi and not (x == lo and ch in VERT))
            # 全是「─」還要左右至少一側連著橫線才是分隔線；「│─│」是格內文字（表示「無」）
            rule = bool(s) and set(s) == {"─"} and (_conn(edge[i], TO_RIGHT) or _conn(edge[i + 1], TO_LEFT))
            cs.append(s)
            ks.append("rule" if rule else "text")
        content.append(cs)
        kind.append(ks)

    parent = list(range(nline * nslot))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for l in range(nline):
        for i in range(nslot):
            if kind[l][i] != "text":
                continue
            if i + 1 < nslot and kind[l][i + 1] == "text" and not vert[l][i + 1]:
                union(l * nslot + i, l * nslot + i + 1)
            if l + 1 < nline and kind[l + 1][i] == "text":
                union(l * nslot + i, (l + 1) * nslot + i)
    groups: dict[int, list[tuple[int, int]]] = {}
    for l in range(nline):
        for i in range(nslot):
            if kind[l][i] == "text":
                groups.setdefault(find(l * nslot + i), []).append((l, i))

    # 邏輯列：有分隔線的行把其餘的行切成一段一段
    ruled = [any(k == "rule" for k in ks) for ks in kind]
    band = [-1] * nline
    starts, ends = [], []
    for l in range(nline):
        if ruled[l]:
            continue
        if l == 0 or ruled[l - 1]:
            starts.append(l)
            ends.append(l)
        band[l] = len(starts) - 1
        ends[-1] = l

    cells = []
    for units in groups.values():
        l0, l1 = min(u[0] for u in units), max(u[0] for u in units)
        s0, s1 = min(u[1] for u in units), max(u[1] for u in units)
        if len(units) != (l1 - l0 + 1) * (s1 - s0 + 1):
            raise ValueError("儲存格不是長方形")
        if any(vert[l][i] for l in range(l0, l1 + 1) for i in range(s0 + 1, s1 + 1)):
            raise ValueError("儲存格內有直線")
        bs = [band[l] for l in range(l0, l1 + 1) if band[l] >= 0]
        if not bs or starts[min(bs)] != l0 or ends[max(bs)] != l1:
            raise ValueError("儲存格與分隔線對不齊")
        text = _join(["".join(content[l][i] for i in range(s0, s1 + 1)).strip() for l in range(l0, l1 + 1)])
        cells.append({"r": (min(bs), max(bs) + 1), "c": (s0, s1 + 1), "text": text})
    if not cells:
        raise ValueError("沒有儲存格")

    # 只留真的是儲存格邊界的列界與欄界
    rk = sorted({e for c in cells for e in c["r"]})
    ck = sorted({e for c in cells for e in c["c"]})
    nrow, ncol = len(rk) - 1, len(ck) - 1
    grid = [[False] * ncol for _ in range(nrow)]
    rows: list[list[dict]] = [[] for _ in range(nrow)]
    boxes = []                                          # (起列, 迄列, 起欄, 迄欄)，迄為不含
    for c in sorted(cells, key=lambda c: (c["r"][0], c["c"][0])):
        r0, r1 = rk.index(c["r"][0]), rk.index(c["r"][1])
        c0, c1 = ck.index(c["c"][0]), ck.index(c["c"][1])
        for r in range(r0, r1):
            for k in range(c0, c1):
                if grid[r][k]:
                    raise ValueError("儲存格重疊")
                grid[r][k] = True
        rows[r0].append({"text": c["text"], "rowspan": r1 - r0, "colspan": c1 - c0})
        boxes.append((r0, r1, c0, c1))
    if not all(all(g) for g in grid):
        raise ValueError("表格有缺格")
    return {"type": "table", "rows": rows, "header_rows": _header_rows(boxes, nrow)}


def _header_rows(boxes: list[tuple[int, int, int, int]], nrow: int) -> int:
    """表頭列數：預設 1。
    第一列有跨列的格子 → 表頭延伸到該格底下（例：「裝置面高度」跨 2 列）；
    表頭下一列全是單列格子、且把表頭裡第一欄以外的跨欄格子再細分 → 這一列也是表頭
    （例：「未滿四公尺」下分「防火構造建築物／其他建築物」）。
    第一欄的跨欄格子不算（例：「區分」「設置場所應設數量」底下就是資料列）。"""
    h = max(r1 for r0, r1, _, _ in boxes if r0 == 0)
    while h < nrow - 1:
        nxt = [b for b in boxes if b[0] == h]
        if any(b[1] - b[0] > 1 for b in nxt):
            break
        above = [b for b in boxes if b[1] == h and b[2] > 0 and b[3] - b[2] > 1]
        if not any(a[2] <= n[2] and n[3] <= a[3] and n[3] - n[2] < a[3] - a[2] for a in above for n in nxt):
            break
        h += 1
    return h if h < nrow else 1


def blocks(text: str) -> list[dict]:
    """條文 → 區塊清單（依原文順序）；表格解析不了時那一段改成 pre 區塊，不丟錯。"""
    out: list[dict] = []
    for table, seg in _segments(text):
        if table:
            try:
                out.append(_parse(seg))
            except Exception:                           # 解析不了就原樣顯示，不能讓條文顯示壞掉
                out.append({"type": "pre", "text": "\n".join(seg)})
        elif b := _text_block(seg):
            out.append(b)
    return out
