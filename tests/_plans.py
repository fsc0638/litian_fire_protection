"""測試用的簡單平面圖（全部由程式畫出，不含任何真實圖說）。

30 m × 15 m 的一層樓，牆厚 20 cm：
- 左半：辦公室（約 14.7 m × 14.6 m）
- 右下：會議室；右上：男廁
- 每道內牆各有一扇門（門扇＋開門弧，會把門洞封起來），門洞兩端有牆的收頭線
"""

from __future__ import annotations

import math

T = 0.2          # 牆厚（m）


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _door(hx, hy, ang_deg, w=1.0, n=12):
    """門：鉸鏈 (hx, hy)，門扇開向 ang 方向，弧從門扇尖端轉 90° 回到門洞另一端。"""
    a = math.radians(ang_deg)
    tip = (hx + w * math.cos(a), hy + w * math.sin(a))
    arc = [(hx + w * math.cos(a + math.pi / 2 * i / n), hy + w * math.sin(a + math.pi / 2 * i / n)) for i in range(n + 1)]
    return [[(hx, hy), tip], arc]


def layers(scale: float = 1.0) -> dict[str, list]:
    """回傳 geometry.by_bbox 格式（圖層 → 折線），座標單位 = 公尺 / scale。"""
    s = 1 / scale
    walls = [_rect(0, 0, 30, 15), _rect(T, T, 30 - T, 15 - T)]
    # 中間直牆 x = 14.9～15.1，門洞 y = 6～7
    walls += [[(14.9, T), (14.9, 6)], [(15.1, T), (15.1, 6)], [(14.9, 7), (14.9, 15 - T)], [(15.1, 7), (15.1, 15 - T)],
              [(14.9, 6), (15.1, 6)], [(14.9, 7), (15.1, 7)]]
    # 右側橫牆 y = 9.9～10.1，門洞 x = 20～21
    walls += [[(15.1, 9.9), (20, 9.9)], [(15.1, 10.1), (20, 10.1)], [(21, 9.9), (30 - T, 9.9)], [(21, 10.1), (30 - T, 10.1)],
              [(20, 9.9), (20, 10.1)], [(21, 9.9), (21, 10.1)]]
    doors = _door(15.0, 6.0, 0) + _door(20.0, 10.0, -90)
    to = lambda polys: [[(x * s, y * s) for x, y in p] for p in polys]  # noqa: E731
    return {"WALL": to(walls), "DOOR": to(doors), "GRID": to([[(-5, 7.5), (35, 7.5)]])}


def texts(scale: float = 1.0) -> list[dict]:
    s = 1 / scale
    items = [("辦公室", 7, 7), ("會議室", 22, 5), ("男廁", 22, 12.5), ("本建物為防火構造", 5, -3), ("PIT:180cm", 3, 3)]
    return [{"t": t, "x": x * s, "y": y * s} for t, x, y in items]
