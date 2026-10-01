"""中文數字與阿拉伯數字互轉（法規條號用，範圍 0～9999）。"""

from __future__ import annotations

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")

CN_NUM_CHARS = "零〇一二兩三四五六七八九十百千"


def to_int(s: str) -> int:
    """'十二'→12、'一百十'→110、'二百三十九'→239、'18'→18、'１８'→18。無法解析時拋 ValueError。"""
    s = s.strip().translate(_FULLWIDTH)
    if not s:
        raise ValueError("empty numeral")
    if s.isdigit():
        return int(s)
    total, digit = 0, None
    for ch in s:
        if ch in _DIGITS:
            digit = _DIGITS[ch]
        elif ch in _UNITS:
            total += (1 if digit is None else digit) * _UNITS[ch]
            digit = None
        else:
            raise ValueError(f"not a numeral: {s!r}")
    return total + (digit or 0)


def to_cn(n: int) -> str:
    """12→'十二'、110→'一百十'、239→'二百三十九'（法規慣用寫法：十位為一且在最高位時省略「一」）。"""
    if n < 0 or n > 9999:
        raise ValueError(n)
    if n == 0:
        return "零"
    names = "零一二三四五六七八九"
    parts, zero_pending = [], False
    for unit_value, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d = n // unit_value % 10
        if d == 0:
            if parts:
                zero_pending = True
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        if unit == "十" and d == 1 and not parts:
            parts.append("十")
        else:
            parts.append(names[d] + unit)
    return "".join(parts)
