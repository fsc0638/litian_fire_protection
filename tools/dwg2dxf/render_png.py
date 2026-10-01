# -*- coding: utf-8 -*-
"""不靠 AutoCAD，用 ezdxf + matplotlib 把 DXF 畫成 PNG。

用法：python render_png.py in.dxf out.png [長邊像素，預設 1800] [中文字型檔，預設 msjh.ttc]

會先把所有文字樣式的字型換成同一個中文 TrueType 字型：
圖內常混用 SHX（simplex、romans、chineset/bigfont）與 kaiu.ttf 等，
其中有的字形資料會讓 fontTools 報錯（2026-09-30 法規檢討圖實際遇到）。
Windows 用 msjh.ttc（微軟正黑體）；Linux 伺服器改傳 NotoSansCJK-Regular.ttc。
"""
import sys, time
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf import recover
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

src, dst = sys.argv[1], sys.argv[2]
px = int(sys.argv[3]) if len(sys.argv) > 3 else 1800
font = sys.argv[4] if len(sys.argv) > 4 else "msjh.ttc"

t0 = time.time()
doc, aud = recover.readfile(src)
for style in doc.styles:
    style.dxf.font = font
    if style.dxf.hasattr("bigfont"):
        style.dxf.discard("bigfont")
fig = plt.figure()
ax = fig.add_axes([0, 0, 1, 1])
Frontend(RenderContext(doc), MatplotlibBackend(ax)).draw_layout(doc.modelspace(), finalize=True)
w, h = fig.get_size_inches()
fig.savefig(dst, dpi=px / max(w, h), facecolor="white")
print(f"rendered {dst} in {time.time() - t0:.1f}s")
