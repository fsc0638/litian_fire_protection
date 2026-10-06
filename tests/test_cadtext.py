"""CAD 原樣圖的文字貼近 AutoCAD（review.cadtext）與照出圖畫（出圖範圍、不出圖圖層）。全部用程式畫的圖。

字型用本機有的中文字型（cadview.pick_font），期望值依該字型執行時量出來，不寫死。"""

import hashlib
import inspect
import re
from pathlib import Path

import ezdxf
import pytest
from ezdxf.fonts import fonts
from ezdxf.math import Vec3

from litian.review import cadtext as CT
from litian.review import cadview as CV

FONT = CV.pick_font()[0]
need_font = pytest.mark.skipif(FONT is None, reason="本機沒有中文字型")

LONG = "消防安全設備之設置應依各類場所消防安全設備設置標準辦理，室內消防栓FE-101設於樓梯間旁（詳圖），面積1234.56㎡。"


def test_ezdxf_version_and_internals_pinned():
    """用到 ezdxf 的私有成員、複製了它的排版程式：版本或這些程式一改就要重新核對 cadtext（並更新這裡的指紋）。"""
    from ezdxf.fonts import font_manager
    from ezdxf.fonts.ttfonts import TTFontRenderer
    from ezdxf.render.abstract_mtext_renderer import AbstractMTextRenderer
    assert ezdxf.__version__ == CT.EZDXF
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"ezdxf=={CT.EZDXF}"' in pyproject
    fm = fonts.font_manager
    assert isinstance(fm._font_cache._cache, dict) and isinstance(fm._fallback_font_name, str)
    assert isinstance(fonts.TrueTypeFont._glyph_caches, dict)
    sha = {name: hashlib.sha256(inspect.getsource(obj).encode()).hexdigest()[:16] for name, obj in (
        ("layout_engine", AbstractMTextRenderer.layout_engine), ("TTFontRenderer", TTFontRenderer),
        ("create_cache", fonts.TrueTypeFont.create_cache), ("FontCache", font_manager.FontCache))}
    assert sha == {"layout_engine": "d9293a7341c44e5d", "TTFontRenderer": "705d3cc209431304",
                   "create_cache": "ba6ef91c59d9477a", "FontCache": "78ef76f264a21998"}


def test_line_break_rules():
    assert CT.split_cjk("設備FE-101，面積1234.56㎡（含）。") == ["設", "備", "FE-101，", "面", "積", "1234.56㎡", "（含）。"]
    assert CT.split_cjk("ABC-12 x") == ["ABC-12 x"]                              # 沒有中文：不切
    assert not CT.can_break("字", "，") and not CT.can_break("「", "字")          # 行首、行尾禁則
    assert not CT.can_break("…", "…") and CT.can_break("。", "下")
    assert not CT.can_break("5", "㎡") and CT.can_break("字", "F") and not CT.can_break("F", "-")


# ---------- 中文換行 ----------

def _lines(doc):
    """照 cadview 的前端畫模型空間，每行由上而下：（該行文字, 左緣 x, 右緣 x）。"""
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.recorder import Recorder
    fe = CV._frontend(Frontend)(RenderContext(doc, export_mode=True), Recorder())
    out = []
    draw_text, engine = fe.pipeline.draw_text, fe.pipeline.text_engine

    def record(text, transform, properties, cap_height, dxftype="TEXT"):
        w = engine.get_text_line_width(text, properties.font or fe.pipeline.default_font_face, cap_height)
        a, b = transform.transform(Vec3(0, 0, 0)), transform.transform(Vec3(w, 0, 0))
        out.append((text, a.x, b.x, round(a.y, 6)))
        draw_text(text, transform, properties, cap_height, dxftype)

    fe.pipeline.draw_text = record
    fe.draw_layout(doc.modelspace())
    rows = {}
    for t, x0, x1, y in out:
        r = rows.setdefault(y, ["", x0, x1])
        r[0] += t
        r[1], r[2] = min(r[1], x0), max(r[2], x1)
    return [tuple(rows[y]) for y in sorted(rows, reverse=True)]


def _doc(text, width, h=2.5, **attribs):
    doc = ezdxf.new("R2018")
    CV.prepare(doc, FONT)
    doc.modelspace().add_mtext(text, dxfattribs={"insert": (100, 50), "char_height": h, "width": width, **attribs})
    return doc


def _width(text, h):
    """預設樣式（prepare 後）的文字寬。"""
    doc = ezdxf.new("R2018")
    CV.prepare(doc, FONT)
    return fonts.make_font(doc.styles.get("Standard").dxf.font, h).text_width(text)


@need_font
@pytest.mark.parametrize("text", [LONG, r"{\C1;" + LONG[:20] + "}" + LONG[20:]])   # 沒有／有格式碼（ezdxf 兩條路）
def test_cjk_mtext_wraps_within_width_and_keeps_text(text):
    h, width = 2.5, 25.0
    rows = _lines(_doc(text, width, h))
    assert len(rows) >= 5
    assert "".join(r[0] for r in rows) == LONG                                    # 字都在、順序不變
    for t, x0, x1 in rows:
        assert x0 == pytest.approx(100, abs=1e-6) and x1 <= 100 + width + CT.TOLERANCE * h + 1e-6, t
        assert t[0] not in CT.NO_START and t[-1] not in CT.NO_END
    assert "FE-101" in "|".join(r[0] for r in rows) and any("1234.56㎡" in r[0] for r in rows)
    assert max(r[2] for r in rows) > 100 + width * 0.8                            # 有排滿，不是一字一行


@need_font
def test_cjk_mtext_tolerance_keeps_slightly_long_line():
    """前進寬度只超出欄寬一點點（< 0.2 倍字高）的行不換行（AutoCAD 也不換）。"""
    h = 2.5
    w = _width("消防安全設備", h)
    assert len(_lines(_doc("消防安全設備", w - 0.1 * h, h))) == 1
    assert len(_lines(_doc("消防安全設備", w - 0.3 * h, h))) == 2


@need_font
def test_width_zero_mtext_not_wrapped():
    rows = _lines(_doc(LONG, 0.0))
    assert len(rows) == 1 and rows[0][0] == LONG


@need_font
@pytest.mark.parametrize("align", [2, 3])                                          # 置中、靠右：位置不受容許量影響
def test_cjk_mtext_center_and_right_aligned(align):
    h, width = 2.5, 25.0
    rows = _lines(_doc(LONG, width, h, attachment_point=align))
    assert len(rows) >= 5
    for t, x0, x1 in rows:
        assert ((x0 + x1) / 2 if align == 2 else x1) == pytest.approx(100, abs=1e-6)     # 中心／右緣在插入點
        assert x1 - x0 <= width + CT.TOLERANCE * h + 1e-6


@need_font
def test_indent_in_drawing_units_keeps_continuation_lines_in_column():
    """縮排存成圖面單位（字高 800、\\pl150）：ezdxf 乘上字高會把接續行推到 12 萬單位外；改當圖面單位。"""
    h, width = 800.0, 40000.0
    rows = _lines(_doc(r"\pxi-150,l150;一、" + LONG * 2, width, h))
    assert len(rows) >= 3
    assert rows[0][1] == pytest.approx(100, abs=1e-6)                             # 第一行：左縮排＋首行縮排＝0
    for t, x0, x1 in rows[1:]:
        assert x0 == pytest.approx(100 + 150, abs=1e-6), t
        assert x1 <= 100 + width + CT.TOLERANCE * h + 1e-6
    small = _lines(_doc(r"\pxi-1,l1;一、" + LONG * 2, width, h))                 # 合理的字高倍數照舊
    assert small[1][1] == pytest.approx(100 + 800, abs=1e-6)


# ---------- 字寬 ----------

def _style_font(doc, name, **dxf):
    st = doc.styles.add(name, font=dxf.pop("font", ""))
    for k, v in dxf.items():
        st.dxf.set(k, v) if k != "family" else st.set_extended_font_data(v)
    return st


@need_font
def test_shx_substituted_cjk_advance_is_char_height_ascii_unchanged():
    doc = ezdxf.new("R2018")
    st = _style_font(doc, "CHT", font="txt.shx", bigfont="chineset.shx", width=0.8)
    CV.prepare(doc, FONT)
    assert st.dxf.font != FONT and st.dxf.bigfont == ""
    h = 2.0
    f, sub = fonts.make_font(st.dxf.font, h, 0.8), fonts.make_font(FONT, h, 0.8)
    assert f.text_width("消防設備") == pytest.approx(4 * h * 0.8, rel=1e-6)       # 大字體：每字 1.0×字高×寬度係數
    assert f.text_width("FE-101") == pytest.approx(sub.text_width("FE-101"), rel=1e-9)
    assert f.text_width("A B") - f.text_width("AB") == pytest.approx(CT.SHX_SPACE * h * 0.8, rel=1e-6)
    assert f.space_width() == pytest.approx(CT.SHX_SPACE * h * 0.8, rel=1e-6)    # 多行文字的空白格
    # 中文字墨跡在基線與字高之間（SHX 大字體的字身＝基線～字高）
    ys = [v.y for p in f.text_glyph_paths("消防國中", h) for v in p.control_vertices()]
    assert -0.02 * h < min(ys) and max(ys) < 1.02 * h
    ys = [v.y for p in f.text_glyph_paths("E", h) for v in p.control_vertices()]
    assert max(ys) == pytest.approx(h, rel=1e-3)                                   # 英數照舊：大寫字高＝字高


@need_font
@pytest.mark.parametrize("dxf, ratio", [({"family": "Microsoft JhengHei"}, 2048 / 1549),
                                        ({"font": "msjhl.ttc"}, 2048 / 1549),
                                        ({"font": "kaiu.ttf"}, 1.340)])
def test_ttf_target_ratio(dxf, ratio, monkeypatch):
    """原字型本機沒有、改用替代字型：中文字寬照原字型（正黑體 1.322、標楷體 1.340 倍字高）。"""
    from ezdxf.fonts.font_manager import FontManager
    has = FontManager.has_font
    monkeypatch.setattr(FontManager, "has_font", lambda self, n: n.lower() not in ("msjhl.ttc", "kaiu.ttf")
                        and has(self, n))
    doc = ezdxf.new("R2018")
    st = _style_font(doc, "S", **dxf)
    CV.prepare(doc, FONT)
    f = fonts.make_font(st.dxf.font, 3.0)
    assert f.text_width("中文") == pytest.approx(2 * 3.0 * ratio, rel=1e-6)


@need_font
def test_unknown_font_keeps_substitute_width_and_missing_substitute_is_kept():
    doc = ezdxf.new("R2018")
    st = _style_font(doc, "S", font="沒有這個字型.ttf")
    CV.prepare(doc, FONT)
    sub = CT._sub(FONT)
    assert fonts.make_font(st.dxf.font, 1.0).text_width("中") == pytest.approx(sub.ratio, rel=1e-9)
    assert CT.style_font("沒有這個替代字型.ttf", "txt.shx", "chineset.shx", "") == "沒有這個替代字型.ttf"


# ---------- 照出圖畫 ----------

def _count(img, rgb) -> int:
    return sum(n for n, c in img.getcolors(img.width * img.height) if c == rgb)


def _hatch(space, x0, y0, x1, y1, **attribs):
    hatch = space.add_hatch(**attribs)
    hatch.paths.add_polyline_path([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def test_non_plot_layers_not_drawn():
    from ezdxf import bbox
    doc = ezdxf.new("R2018")
    doc.layers.add("不出圖").dxf.plot = 0
    msp = doc.modelspace()
    _hatch(msp, 0, 0, 10, 10, color=5)
    _hatch(msp, 20, 0, 30, 10, color=1, dxfattribs={"layer": "不出圖"})
    _hatch(msp, 40, 0, 50, 10, color=3, dxfattribs={"layer": "Defpoints"})
    CV.prepare(doc, FONT)
    sheet = {"name": "1F-1", "sheet": 0, "bbox": [-5, -5, 55, 15], "meta": {}, "scale": 1.0, "layout": None}
    img, _, _ = CV.render_sheet(doc, sheet, bbox.Cache(), long_px=600)
    assert _count(img, (0, 0, 255)) > 1000
    assert _count(img, (255, 0, 0)) == 0 and _count(img, (0, 255, 0)) == 0


def _plot_window_doc(window, plot_type=4):
    """A3 配置頁：圖框 0..420×0..297，紙面上另有一塊在圖框外的紅色（AutoCAD 依視窗出圖時不會印）。"""
    doc = ezdxf.new("R2018")
    _hatch(doc.modelspace(), 0, 0, 1000, 1000, color=5)
    lay = doc.layouts.new("FE-1")
    lay.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    lay.add_viewport(center=(200, 150), size=(380, 260), view_center_point=(500, 500), view_height=260)
    _hatch(lay, 500, 100, 560, 160, color=1)
    d = lay.dxf_layout.dxf
    d.plot_type = plot_type
    (d.plot_window_x1, d.plot_window_y1), (d.plot_window_x2, d.plot_window_y2) = window
    d.limmin, d.limmax = window
    CV.prepare(doc, FONT)
    return doc


def _ratio(w, h):
    """影像長寬比：範圍四周各留長邊 1%。"""
    m = max(w, h) * 0.01
    return (w + 2 * m) / (h + 2 * m)


@pytest.mark.parametrize("plot_type", [4, 2])
def test_layout_plot_window_bounds_image_and_clips_outside(plot_type):
    from ezdxf import bbox
    doc = _plot_window_doc(((0, 0), (420, 297)), plot_type)
    sheet = {"name": "1F-1", "sheet": 0, "bbox": None, "meta": {}, "scale": 1.0, "layout": "FE-1"}
    img, meta, _ = CV.render_sheet(doc, sheet, bbox.Cache(), long_px=840)
    assert img.width / img.height == pytest.approx(_ratio(420, 297), rel=0.005)  # 範圍＝出圖視窗，不含框外的紅色
    assert _count(img, (255, 0, 0)) == 0 and _count(img, (0, 0, 255)) > 10000
    assert re.fullmatch(rf"ezdxf \S+ \+ matplotlib \S+ \+ {CT.REVISION}", meta["renderer"])


def test_layout_plot_window_not_covering_content_falls_back():
    from ezdxf import bbox
    doc = _plot_window_doc(((0, 0), (100, 100)))                                  # 沒涵蓋主視埠：不採用
    sheet = {"name": "1F-1", "sheet": 0, "bbox": None, "meta": {}, "scale": 1.0, "layout": "FE-1"}
    img, _, _ = CV.render_sheet(doc, sheet, bbox.Cache(), long_px=840)
    assert img.width / img.height == pytest.approx(_ratio(560, 297), rel=0.005) and _count(img, (255, 0, 0)) > 0
