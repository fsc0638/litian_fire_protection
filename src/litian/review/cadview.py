"""CAD 原樣圖：把圖檔照 CAD 的樣子（圖層顏色、線型、文字、圖框、配置頁）畫成白底高解析度圖磚，
審核工作台可縮放平移，缺失（overlay.json，公尺座標）依 meta.json 的轉換疊上去。

每張樓層圖（檢核結果 floors[]）畫一張：有配置頁的畫該配置頁（連同視埠內容）；模型空間圖框的只畫框內的實體。
輸出放在檢核資料夾（<檔名>.review）：
- cad/<svg_name>/meta.json：原尺寸寬高、圖磚規格、公尺座標 → 原尺寸像素的仿射轉換
  （px＝a*X+b*Y+c，py＝d*X+e*Y+f，py 向下）
- cad/<svg_name>/<層級>/<欄>_<列>.png：Deep Zoom 層級規則（最大層＝原尺寸，每往下一層長寬減半、無條件進位，第 0 層 1×1）
- cad/status.json：整批狀態 pending（排隊）｜rendering｜done｜failed、各張結果、警告（例：找不到中文字型）

繪圖：ezdxf 繪圖模組＋matplotlib（Agg 點陣，授權寬鬆）；線寬照出圖紙上的實際粗細；照出圖畫（配置頁只畫出圖範圍、
不出圖的圖層不畫）；文字的字寬與中文換行貼近 AutoCAD（review.cadtext）。
命令列：python -m litian.review.cadview <dxf> <ir.json> <review.json> <review_dir>
（worker 在背景用子行程呼叫：限時、限記憶體、低優先順序；排隊與重畫見 drawing.worker 的 CadRunner）
"""

from __future__ import annotations

import gc
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from litian.drawing import ir as IR
from litian.plan import floor as F

TILE = 512
DPI = 300
A3_PX = round(420 / 25.4 * DPI)        # A3 長邊 300 dpi ≈ 4961 px：最低解析度
MAX_PX = 8000                         # 長邊上限（8000×5700 RGB 約 140 MB，再大子行程記憶體會不夠）
FONTS = ("NotoSansCJK-Regular.ttc", "NotoSansTC-Regular.ttf", "msjh.ttc")   # 依序找第一個有的中文字型
NAME = re.compile(r"^[0-9A-Z]{1,6}(-\d{1,4})?$")                            # 與 API 的 REVIEW_LABEL 相同
INLINE_FONT = re.compile(r"\\[fF][^;\\]*;")                                  # 多行文字內嵌的字型切換（\f新細明體|b0|i0;）
RATIO = re.compile(r"1\s*[:：/]\s*(\d+(?:\.\d+)?)")


# ---------- 狀態檔 ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: dict) -> None:
    """先寫暫存檔再改名：讀的一方不會讀到寫一半的檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def status_path(review_dir: str | Path) -> Path:
    return Path(review_dir) / "cad" / "status.json"


def read_status(review_dir: str | Path) -> dict | None:
    try:
        return json.loads(status_path(review_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_status(review_dir: str | Path, status: dict) -> None:
    _write_json(status_path(review_dir), status)


def start_status(review_dir: str | Path) -> dict:
    st = {"state": "rendering", "sheets": {}, "error": None, "warnings": [], "started_at": _now(), "finished_at": None}
    write_status(review_dir, st)
    return st


def queue_status(review_dir: str | Path) -> None:
    """排入背景畫圖：清掉上一輪各張的結果（圖紙可能變了，舊圖磚不能當成這一輪畫好的）。"""
    write_status(review_dir, {"state": "pending", "sheets": {}, "error": None, "warnings": [], "queued_at": _now(),
                              "started_at": None, "finished_at": None})


def mark_failed(review_dir: str | Path, error: str) -> None:
    """子行程逾時、被砍（記憶體不足）或中斷：已畫好的各張照舊，整批記失敗。"""
    st = read_status(review_dir) or {"sheets": {}, "warnings": [], "started_at": None}
    st.update(state="failed", error=error[:500], finished_at=_now())
    write_status(review_dir, st)


# ---------- 字型、視埠 ----------

def pick_font() -> tuple[str | None, str | None]:
    """（字型檔名, 警告）。環境變數 LITIAN_CAD_FONT 優先，否則依序找 FONTS 中 ezdxf 字型管理器找得到的第一個。"""
    from ezdxf.fonts import fonts
    want = os.environ.get("LITIAN_CAD_FONT", "").strip()
    for name in ([want] if want else []) + list(FONTS):
        if fonts.font_manager.has_font(name):
            return name, (f"找不到指定字型 {want}，改用 {name}" if want and name != want else None)
    return None, "找不到中文字型（" + "、".join(([want] if want else []) + list(FONTS)) + "），中文字可能顯示成方框"


def _paper_vp(vp) -> bool:
    """代表整張紙的視埠（檢視中心＝自身中心、檢視高度＝自身高度）：不畫內容。"""
    d = vp.dxf
    if d.get("id", 0) == 1:
        return True
    c, v = d.center, d.view_center_point
    return abs(v.x - c.x) < 1 and abs(v.y - c.y) < 1 and abs(d.view_height - d.height) < 1


def prepare(doc, font: str | None) -> dict:
    """只改記憶體中的 doc：
    1) .shx、沒有副檔名或本機沒有的字型改用中文字型（否則中文變方框），找不到的字型一律退回中文字型；
       中文字寬依原字型校正（SHX 大字體、正黑體等，見 cadtext.style_font）；
    2) 多行文字內嵌的字型切換拿掉（改用上面的中文字型）；
    3) 視埠狀態不可靠（轉檔後常是「關閉」0；也有內容視埠是 1）：ezdxf 只畫狀態 >0 的視埠，
       且把排第一個、狀態 1 的當成整張紙丟掉。所以整張紙以外的視埠一律設成 ≥2、整張紙的一律關閉，
       畫哪些視埠就只看 _paper_vp，不靠 ezdxf 依狀態猜；
    4) 實體用到、圖層表卻沒有的圖層補上（預設白／黑色，跟 AutoCAD 開檔時一樣；否則 ezdxf 一律畫成白色）；
    5) Defpoints 圖層設成不出圖（AutoCAD 出圖一律不印這層；畫圖照出圖畫，不出圖的圖層不畫）。"""
    from ezdxf.fonts import fonts

    from litian.review import cadtext
    out = {"styles": 0, "mtext": 0, "viewports": 0, "layers": 0}
    if font:
        fm = fonts.font_manager
        fm._fallback_font_name = font          # ezdxf 沒有公開的設定方法
        for st in doc.styles:
            f = st.dxf.get("font", "") or ""
            big = st.dxf.get("bigfont", "") or ""
            if f.lower().endswith(".shx") or "." not in f or big or not fm.has_font(f):
                st.dxf.font = cadtext.style_font(font, f, big, st.get_extended_font_data()[0])
                st.dxf.bigfont = ""
                out["styles"] += 1
    used = set()
    for e in doc.entitydb.values():
        if e.dxftype() == "MTEXT" and INLINE_FONT.search(e.text or ""):
            e.text = INLINE_FONT.sub("", e.text)
            out["mtext"] += 1
        if e.dxf.is_supported("layer"):
            used.add(e.dxf.get("layer", "0"))
    for name in used:
        if name and not doc.layers.has_entry(name):
            try:
                doc.layers.add(name)
                out["layers"] += 1
            except Exception:                  # 名稱含不合法字元：照 ezdxf 預設畫
                pass
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        for vp in lay.query("VIEWPORT"):
            want = 0 if _paper_vp(vp) else max(2, vp.dxf.get("status", 0))
            if vp.dxf.get("status", 0) != want:
                vp.dxf.status = want
                out["viewports"] += 1
    if doc.layers.has_entry("Defpoints"):
        doc.layers.get("Defpoints").dxf.plot = 0
    return out


# ---------- 每張要畫什麼 ----------

def plan_sheets(ir: dict, review: dict) -> list[dict]:
    """檢核結果的每個樓層圖 → 要畫的範圍：配置頁（source layout）或模型空間圖框範圍（source model）。"""
    sheets = {s["idx"]: s for s in ir["sheets"]}
    out = []
    for fl in review.get("floors", []):
        s = sheets.get(fl["sheet"])
        name = fl.get("svg_name") or fl["label"]
        item = {"name": name, "sheet": fl["sheet"], "number": fl.get("number"), "label": fl.get("label")}
        if s is not None:
            item.update(layout=s.get("layout"), bbox=s.get("bbox"), meta=s.get("meta") or {},
                        scale=F.unit_scale(s.get("meta"), ir["dxf"].get("insunits")))
        out.append(item)
    return out


def _find_layout(doc, name: str):
    try:
        return doc.layouts.get(name)
    except KeyError:
        pass
    for lay in doc.layouts:                    # 中介資料的名稱經過清理（無效字元換成 U+FFFD）
        if IR._clean(lay.name) == name:
            return lay
    raise ValueError(f"找不到配置頁 {name}")


def _main_viewport(lay, bbox):
    """配置頁的主視埠：框出的模型空間範圍與中介資料的圖紙範圍最相符者（沒有範圍時取最大的）。"""
    best, key = None, None
    for vp in lay.query("VIEWPORT"):
        if _paper_vp(vp):
            continue
        w = IR._viewport_window(vp)
        if not w:
            continue
        k = (IR._iou(w, bbox), 0.0) if bbox else (0.0, float(vp.dxf.width) * float(vp.dxf.height))
        if key is None or k > key:
            best, key = vp, k
    if best is None:
        raise ValueError(f"配置頁 {lay.name} 沒有視埠")
    if not best.is_top_view:
        raise ValueError(f"配置頁 {lay.name} 的視埠不是俯視圖，無法畫出")
    return best


def _union(boxes):
    from ezdxf.math import BoundingBox2d
    b = BoundingBox2d()
    for x in boxes:
        if x is not None and x.has_data:
            b.extend([x.extmin, x.extmax])
    return b


def _paper_box(lay, cache):
    """配置頁要畫的紙面範圍：紙面上所有實體（視埠取其外框，整張紙的視埠不算）。"""
    from ezdxf import bbox
    from ezdxf.math import BoundingBox2d, Vec2
    parts = []
    for e in lay:
        if e.dxftype() == "VIEWPORT":
            if _paper_vp(e) or e.dxf.get("status", 0) <= 0:
                continue
            c, w, h = e.dxf.center, float(e.dxf.width) / 2, float(e.dxf.height) / 2
            parts.append(BoundingBox2d([Vec2(c.x - w, c.y - h), Vec2(c.x + w, c.y + h)]))
        else:
            b = bbox.extents((e,), fast=True, cache=cache)
            if b.has_data:
                parts.append(BoundingBox2d([Vec2(b.extmin), Vec2(b.extmax)]))
    return _union(parts)


def _plot_area(lay, box):
    """配置頁的出圖範圍（AutoCAD 只印這塊）：出圖設定是視窗（plot_type 4）取出圖視窗、是圖面範圍（2）取 limits；
    其他出圖方式或範圍不合理（沒涵蓋全部內容視埠、與紙面內容範圍 box 重疊不到一半）時 None（照紙面內容範圍）。"""
    from ezdxf.math import BoundingBox2d, Vec2
    d = lay.dxf_layout.dxf
    kind = d.get("plot_type", 5)
    if kind == 4:
        x1, y1, x2, y2 = (float(d.get(k, 0.0)) for k in ("plot_window_x1", "plot_window_y1", "plot_window_x2",
                                                          "plot_window_y2"))
    elif kind == 2:
        (x1, y1), (x2, y2) = Vec2(d.get("limmin", (0, 0))), Vec2(d.get("limmax", (0, 0)))
    else:
        return None
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 - x1 <= 0 or y2 - y1 <= 0 or not box.has_data:
        return None
    tol = max(x2 - x1, y2 - y1) * 0.02
    for vp in lay.query("VIEWPORT"):
        if _paper_vp(vp) or vp.dxf.get("status", 0) <= 0:
            continue
        c, w, h = vp.dxf.center, float(vp.dxf.width) / 2, float(vp.dxf.height) / 2
        if c.x - w < x1 - tol or c.x + w > x2 + tol or c.y - h < y1 - tol or c.y + h > y2 + tol:
            return None
    ix = min(box.extmax.x, x2) - max(box.extmin.x, x1)
    iy = min(box.extmax.y, y2) - max(box.extmin.y, y1)
    if ix <= 0 or iy <= 0 or ix * iy < 0.5 * box.size.x * box.size.y:
        return None
    return BoundingBox2d([Vec2(x1, y1), Vec2(x2, y2)])


def _pad(box, k: float = 0.01):
    """四周各留長邊 1%：圖框線剛好在範圍邊上時不會被切掉一半。"""
    from ezdxf.math import BoundingBox2d, Vec2
    m = max(box.size.x, box.size.y) * k
    return BoundingBox2d([box.extmin - Vec2(m, m), box.extmax + Vec2(m, m)])


def _model_paper_mm(meta: dict, box_long: float, scale: float) -> float | None:
    """模型空間圖框的紙張長邊（mm）：依圖框「比例」欄（1:200）換算；沒有或不合理時 None。"""
    m = RATIO.search(IR.meta_field(meta, "比例") or "")
    if not m or not float(m.group(1)):
        return None
    mm = box_long * scale * 1000 / float(m.group(1))
    return mm if 50 <= mm <= 3000 else None


# ---------- 畫圖 ----------

def _affine(fn) -> list[float]:
    """（X, Y）→（px, py）的函式 → 仿射係數 [a, b, c, d, e, f]。"""
    ox, oy = fn(0.0, 0.0)
    ax, ay = fn(1.0, 0.0)
    bx, by = fn(0.0, 1.0)
    return [round(v, 9) for v in (ax - ox, bx - ox, ox, ay - oy, by - oy, oy)]


def render_sheet(doc, sheet: dict, cache, long_px: int | None = None):
    """畫一張 → (PIL 影像, meta, 警告)。long_px：指定長邊像素（測試用）；未指定時約 A3 300 dpi，紙張大的提高到上限 MAX_PX。"""
    import ezdxf
    import matplotlib
    matplotlib.use("Agg")                                        # 無螢幕的點陣輸出；要在載入 ezdxf 的 matplotlib 模組前設定
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.math import BoundingBox2d, Matrix44, Vec2, Vec3
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from PIL import Image

    from litian.review import cadtext

    scale = sheet.get("scale")
    if not scale:
        raise ValueError("無法判斷圖面單位")
    notes = []
    if sheet.get("layout"):
        lay = _find_layout(doc, sheet["layout"])
        vp = _main_viewport(lay, sheet.get("bbox"))
        twist = float(vp.dxf.get("view_twist_angle", 0) or 0)
        if twist:
            notes.append(f"{sheet['name']}：視埠有扭轉角 {twist:g}°，疊圖依 ezdxf 的視埠換算")
        to_paper = vp.get_transformation_matrix()                # 模型 → 紙面（與畫視埠內容同一個矩陣）
        box = _paper_box(lay, cache)
        if not box.has_data:
            raise ValueError("配置頁沒有內容")
        area, clip = _plot_area(lay, box), None
        if area is not None:                                     # 只畫出圖範圍；外擴長邊 0.12%：邊上的圖框線不被切掉一半
            box = area
            m = max(area.size.x, area.size.y) * 0.0012
            clip = (area.extmin - Vec2(m, m), area.extmax + Vec2(m, m))
        unit_mm = 25.4 if lay.dxf_layout.dxf.get("plot_paper_units", 1) == 0 else 1.0
        paper_mm = max(box.size.x, box.size.y) * unit_mm

        def draw(fe) -> None:
            if clip is not None:                                 # 紙面實體與視埠內容一起裁切
                from ezdxf.tools.clipping_portal import ClippingRect
                fe.pipeline.push_clipping_shape(ClippingRect(clip), None)
            fe.draw_layout(lay, finalize=True)

        source = "layout"
    else:
        msp = doc.modelspace()
        if sheet.get("bbox"):
            x0, y0, x1, y1 = sheet["bbox"]
            box = BoundingBox2d([Vec2(x0, y0), Vec2(x1, y1)])
        else:                                                    # 沒有圖框：整個模型空間是一張圖
            b = bbox.extents(msp, fast=True, cache=cache)
            box = BoundingBox2d([Vec2(b.extmin), Vec2(b.extmax)]) if b.has_data else BoundingBox2d()
        if not box.has_data or box.size.x <= 0 or box.size.y <= 0:
            raise ValueError("圖框範圍無效")
        to_paper = Matrix44()
        paper_mm = _model_paper_mm(sheet.get("meta") or {}, max(box.size.x, box.size.y), scale)
        lim = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)

        def inside(e) -> bool:                                   # 只畫圖框範圍內（含部分重疊）的實體
            b = bbox.extents((e,), fast=True, cache=cache)
            if not b.has_data:
                return True
            return not (b.extmax.x <= lim[0] or b.extmin.x >= lim[2] or b.extmax.y <= lim[1] or b.extmin.y >= lim[3])

        clip = _pad(box)

        def draw(fe) -> None:
            # 篩選只看最上層實體；整棟各層放在同一個圖塊（例：綁定的建築底圖）時，圖塊展開後的實體全都會送去畫，
            # 再用圖框範圍裁切：範圍外的不產生 matplotlib 圖元（否則記憶體與時間是好幾倍）
            from ezdxf.tools.clipping_portal import ClippingRect
            fe.pipeline.push_clipping_shape(ClippingRect([clip.extmin, clip.extmax]), None)
            fe.draw_layout(msp, finalize=True, filter_func=inside)

        source = "model"
    paper_mm = paper_mm if paper_mm and 50 <= paper_mm <= 3000 else 420.0      # 紙張大小不明：當 A3
    box = _pad(box)
    if long_px is None:
        long_px = max(A3_PX, round(paper_mm / 25.4 * DPI))
    long_px = min(MAX_PX, long_px)
    w, h = box.size.x, box.size.y
    W, H = (long_px, max(1, round(long_px * h / w))) if w >= h else (max(1, round(long_px * w / h)), long_px)
    dpi = long_px * 25.4 / paper_mm                              # 每英吋像素（以紙面算）：線寬照出圖的粗細換算
    # 畫布不留邊、長寬比＝範圍長寬比；多 0.001 像素：Agg 取整數寬高時不會少一個像素
    fig = Figure(figsize=((W + 1e-3) / dpi, (H + 1e-3) / dpi), dpi=dpi, facecolor="white")
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.COLOR,
                               image_policy=config.ImagePolicy.RECT,   # 圖片只畫外框：DXF 可指向任意本機檔案
                               lineweight_scaling=72 / 25.4)           # 這個繪圖後端把線寬（mm）直接當點數：換算成點
    # 照出圖畫（export_mode：不出圖的圖層不畫）
    draw(_frontend(Frontend)(RenderContext(doc, export_mode=True), _backend(MatplotlibBackend)(ax, adjust_figure=False),
                             config=cfg, bbox_cache=cache))
    # 畫完才設範圍（finalize 會自動縮放到全部圖元）：兩軸同一個比例（像素取整數後長寬比有微小差，多的平均留白）
    bw, bh = fig.bbox.width, fig.bbox.height
    s = min(bw / w, bh / h)                                      # 像素／圖面單位
    c = box.center
    ax.set_aspect("auto")
    ax.set_xlim(c.x - bw / s / 2, c.x + bw / s / 2)
    ax.set_ylim(c.y - bh / s / 2, c.y + bh / s / 2)
    canvas.draw()
    buf = canvas.buffer_rgba()
    height = buf.shape[0]
    img = Image.frombuffer("RGBA", (buf.shape[1], height), buf, "raw", "RGBA", 0, 1).convert("RGB")
    # 圖面座標 → 像素：用 matplotlib 實際畫圖的轉換（含等比例時對範圍的微調）；Agg 的 y 從畫布底邊往上
    data_to_display = ax.transData.frozen()
    del buf, canvas, fig, ax
    gc.collect()                    # 圖元與 RGBA 畫布（8000 px 時約 180 MB）互相參照：要回收才會在切圖磚前放掉

    def to_px(x: float, y: float) -> tuple[float, float]:
        p = to_paper.transform(Vec3(x / scale, y / scale, 0))
        dx, dy = data_to_display.transform((p.x, p.y))
        return float(dx), float(height - dy)

    meta = {"version": 1, "width": img.width, "height": img.height, "tile_size": TILE, "overlap": 0, "format": "png",
            "max_level": max_level(img.width, img.height), "transform": _affine(to_px), "source": source,
            "layout": sheet.get("layout") if source == "layout" else None, "dpi": round(dpi),
            "rendered_at": _now(),
            "renderer": f"ezdxf {ezdxf.__version__} + matplotlib {matplotlib.__version__} + {cadtext.REVISION}"}
    return img, meta, notes


_FRONTEND = None


def _frontend(base):
    """ezdxf 的繪圖前端，含中文、有欄寬的多行文字改用 cadtext 的排版（可在中文字間換行，ezdxf 只在空白換行）。"""
    global _FRONTEND
    if _FRONTEND is None:
        from litian.review import cadtext

        class Frontend(base):
            def draw_mtext_entity(self, entity, properties):
                if not cadtext.draw_mtext(self, entity, properties):
                    super().draw_mtext_entity(entity, properties)

        _FRONTEND = Frontend
    return _FRONTEND


_BACKEND = None


def _backend(base):
    """ezdxf 的 matplotlib 後端，點（POINT、長度 0 的線、點劃線裡的點）改照線寬畫圓點（原本固定極小、不管線寬）。"""
    global _BACKEND
    if _BACKEND is None:
        from matplotlib.collections import LineCollection
        from matplotlib.lines import Line2D

        class Backend(base):
            def _dots(self, xs, ys, properties, z):
                self.ax.add_line(Line2D(xs, ys, marker="o", markersize=self.get_lineweight(properties),
                                        markeredgewidth=0, linestyle="none", color=properties.color, zorder=z))

            def draw_point(self, pos, properties):
                self._dots([pos.x], [pos.y], properties, self._get_z())

            def draw_solid_lines(self, lines, properties):
                z = self._get_z()
                segs, xs, ys = [], [], []
                for s, e in lines:
                    if s.isclose(e):
                        xs.append(s.x)
                        ys.append(s.y)
                    else:
                        segs.append(((s.x, s.y), (e.x, e.y)))
                if xs:
                    self._dots(xs, ys, properties, z)
                if segs:
                    self.ax.add_collection(LineCollection(segs, linewidths=self.get_lineweight(properties),
                                                          color=properties.color, zorder=z, capstyle="butt"))

        _BACKEND = Backend
    return _BACKEND


PRINT_MAX = 5000                      # 報告列印用整張圖的長邊上限（A4 橫印約 370 dpi）
_PRINT_LOCK = threading.Lock()        # 報告一次要好幾張：一次只拼一張（API 容器記憶體小）


def _print_shrink(W: int, H: int, L: int, limit: int) -> int:
    """列印圖用第幾層往下縮：長邊不超過 limit 的最大那層（與圖磚層級對齊）。"""
    k = 0
    while math.ceil(max(W, H) / 2 ** k) > limit and k < L:
        k += 1
    return k


def _save_print(img, out: Path) -> None:
    """減成 256 色存 PNG（白底維持純白，約 0.5 MB）；先寫暫存檔再換上，讀的一方不會讀到寫一半的檔。"""
    import uuid

    from PIL import Image
    q = img.quantize(colors=256, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE)
    tmp = out.with_name(f".print.{uuid.uuid4().hex}.tmp")
    try:
        q.save(tmp, format="PNG")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def print_image(sheet_dir: str | Path, limit: int = PRINT_MAX) -> Path:
    """報告（列印、存成 PDF）用的整張原圖 print.png：畫圖時就順便存好；之前畫的（沒有 print.png）才從圖磚拼回
    長邊不超過 limit 的那一層。快取在圖磚資料夾（圖重畫時整個資料夾換掉；meta.json 比較新時也重做）。回傳檔案路徑。"""
    from PIL import Image
    d = Path(sheet_dir)
    meta_path, out = d / "meta.json", d / "print.png"
    fresh = lambda: out.is_file() and out.stat().st_mtime >= meta_path.stat().st_mtime
    if fresh():
        return out
    with _PRINT_LOCK:
        if fresh():                                                          # 等鎖的期間別人做好了
            return out
        before = meta_path.stat()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        W, H, L, T = int(meta["width"]), int(meta["height"]), int(meta["max_level"]), int(meta.get("tile_size") or TILE)
        k = _print_shrink(W, H, L, limit)
        w, h = math.ceil(W / 2 ** k), math.ceil(H / 2 ** k)
        img = Image.new("RGB", (w, h), "white")
        for col in range(math.ceil(w / T)):
            for row in range(math.ceil(h / T)):
                with Image.open(d / str(L - k) / f"{col}_{row}.png") as tile:   # 少一塊就整張失敗（呼叫端退回簡化圖）
                    img.paste(tile.convert("RGB"), (col * T, row * T))
        after = meta_path.stat()
        if (before.st_ino, before.st_mtime_ns) != (after.st_ino, after.st_mtime_ns):   # 拼到一半剛好重畫換了資料夾
            raise OSError("原圖剛重畫，稍後再試")
        _save_print(img, out)
    return out


def max_level(w: int, h: int) -> int:
    return max(0, math.ceil(math.log2(max(w, h, 1))))


def write_tiles(img, out: Path) -> int:
    """Deep Zoom 圖磚：最大層 L＝ceil(log2(長邊)) 是原尺寸，每往下一層長寬各減半（無條件進位），第 0 層 1×1。回傳圖磚數。"""
    from PIL import Image
    n = 0
    cur = img
    for level in range(max_level(img.width, img.height), -1, -1):
        d = out / str(level)
        d.mkdir(parents=True)
        for col in range(math.ceil(cur.width / TILE)):
            for row in range(math.ceil(cur.height / TILE)):
                x, y = col * TILE, row * TILE
                cur.crop((x, y, min(x + TILE, cur.width), min(y + TILE, cur.height))).save(d / f"{col}_{row}.png")
                n += 1
        if level:
            cur = cur.resize((math.ceil(cur.width / 2), math.ceil(cur.height / 2)), Image.Resampling.LANCZOS)
    return n


def _swap(tmp: Path, final: Path) -> None:
    """暫存資料夾畫完才換上：讀的一方不會讀到畫一半的圖。"""
    old = None
    if final.exists():
        old = final.with_name(f".old-{final.name}-{os.getpid()}")
        final.rename(old)
    tmp.rename(final)
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def render_all(dxf: str | Path, ir: dict, review: dict, review_dir: str | Path, long_px: int | None = None) -> dict:
    """一次讀檔、畫各張；每張獨立，失敗的記在 status.json。回傳最後的狀態（另含各張秒數 seconds）。"""
    from ezdxf import bbox, recover
    from PIL import Image

    review_dir = Path(review_dir)
    cad = review_dir / "cad"
    st = start_status(review_dir)
    for p in cad.glob(".tmp-*"):                       # 上次中斷留下的暫存
        shutil.rmtree(p, ignore_errors=True)
    for p in cad.glob(".old-*"):
        shutil.rmtree(p, ignore_errors=True)
    seconds: dict[str, float] = {}
    try:
        font, warn = pick_font()
        if warn:
            st["warnings"].append(warn)
        t0 = time.time()
        doc, _ = recover.readfile(str(dxf))
        seconds["read"] = round(time.time() - t0, 1)
        prepare(doc, font)
        plan = plan_sheets(ir, review)
    except Exception as e:
        st.update(state="failed", error=f"{type(e).__name__}: {e}"[:500], finished_at=_now())
        write_status(review_dir, st)
        st["seconds"] = seconds
        return st
    cache = bbox.Cache()                               # 模型空間實體範圍：各張共用（視埠篩選、圖框篩選）
    errors = {}
    for sh in plan:
        name = sh["name"]
        t0 = time.time()
        try:
            if not NAME.match(name):
                raise ValueError(f"圖名 {name!r} 不合規")
            img, meta, notes = render_sheet(doc, sh, cache, long_px)
            st["warnings"].extend(notes)
            tmp = cad / f".tmp-{name}-{os.getpid()}"
            shutil.rmtree(tmp, ignore_errors=True)
            write_tiles(img, tmp)
            _write_json(tmp / "meta.json", meta)
            k = _print_shrink(img.width, img.height, meta["max_level"], PRINT_MAX)     # 報告用整張圖：手上就有，順便存
            _save_print(img if not k else img.resize((math.ceil(img.width / 2 ** k), math.ceil(img.height / 2 ** k)),
                                                     Image.Resampling.LANCZOS), tmp / "print.png")
            del img
            _swap(tmp, cad / name)
            st["sheets"][name] = "done"
        except Exception as e:
            st["sheets"][name] = "failed"
            errors[name] = f"{type(e).__name__}: {e}"[:300]
        img = None
        gc.collect()                                   # 上一張的圖元（matplotlib 物件互相參照）先回收，峰值不累加
        seconds[name] = round(time.time() - t0, 1)
        write_status(review_dir, st)                   # 每畫完一張就更新：工作台可先看已畫好的
    keep = {sh["name"] for sh in plan}
    for p in cad.iterdir():                            # 已不存在的樓層圖（重新處理後圖紙變了）
        if p.is_dir() and not p.name.startswith(".") and p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)
    done = sum(v == "done" for v in st["sheets"].values())
    st["state"] = "failed" if plan and not done else "done"
    if errors:
        st["sheet_errors"] = errors
        if not done:
            st["error"] = next(iter(errors.values()))
    st["finished_at"] = _now()
    write_status(review_dir, st)
    st["seconds"] = seconds
    return st


def _peak_mb() -> int | None:
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)   # Linux：KB
    except ImportError:
        try:
            import psutil
            return round(psutil.Process().memory_info().peak_wset / 2 ** 20)
        except Exception:
            return None


def main(argv: list[str]) -> int:
    """python -m litian.review.cadview <dxf> <ir.json> <review.json> <review_dir>；最後一行印摘要 JSON。"""
    dxf, ir_path, review_path, review_dir = argv[1:5]
    logging.getLogger("ezdxf").setLevel(logging.ERROR)  # 轉檔後的圖常有大量「參照的樣式不存在」警告，不必寫進日誌
    t0 = time.time()
    try:
        ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
        review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        start_status(review_dir)
        mark_failed(review_dir, f"{type(e).__name__}: {e}")
        print(f"讀取中介資料或檢核結果失敗：{e}", file=sys.stderr)
        return 1
    st = render_all(dxf, ir, review, review_dir)
    print(json.dumps({"state": st["state"], "sheets": st["sheets"], "seconds": st["seconds"],
                      "total": round(time.time() - t0, 1), "peak_mb": _peak_mb()}, ensure_ascii=False))
    if st["state"] == "failed":
        print(st.get("error") or "全部失敗", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
