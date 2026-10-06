"""CAD 原樣圖的文字貼近 AutoCAD（review.cadview 呼叫）：
1) 字寬：沒有原字型、改用中文字型時，依原字型校正中文字（全形字）的寬度（style_font；多行文字內嵌的字型切換
   依各自的原字型，inline_fonts）；
2) 換行：有欄寬的多行文字可在中文字之間換行，套用斷行禁則與 AutoCAD 容許的超出量（draw_mtext）；
3) 段落縮排：數值明顯是圖面單位時不再乘字高（_paragraph；所有帶縮排碼的多行文字）。

ezdxf 沒有公開的做法：字型別名要登記進字型管理器與字形快取（私有成員），排版程式複製自 ezdxf 1.4.4 的
AbstractMTextRenderer.layout_engine。兩者都綁定 EZDXF 這個版本（pyproject 鎖定；版本或內部改了，
tests/test_cadtext.py 會失敗，要重新核對）。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from ezdxf.addons.drawing.config import TextPolicy
from ezdxf.addons.drawing.frontend import is_spatial_text
from ezdxf.addons.drawing.mtext_complex import ComplexMTextRenderer
from ezdxf.fonts import fonts
from ezdxf.fonts.font_face import FontFace
from ezdxf.fonts.font_manager import CacheEntry
from ezdxf.fonts.ttfonts import TTFontRenderer
from ezdxf.math import Matrix44, Vec3
from ezdxf.render.abstract_mtext_renderer import (column_heights, defined_width, make_default_tab_stops,
                                                  new_paragraph, super_glue)
from ezdxf.tools import text_layout as tl
from ezdxf.tools.text import MTextParser, ParagraphProperties, TokenType, valid_text_height

EZDXF = "1.4.4"
REVISION = "text1"                    # 寫進 meta.json 的 renderer：分得出哪些圖有這些修正

# ---------- 字寬 ----------
# 原字型的中文字前進寬度 ÷ 字高（AutoCAD 實測）：
# - SHX 大字體：1.0（字高×寬度係數；由 AutoCAD 存檔的 TEXT 對齊點反推，純中文字串中位數 1.000，4～6 字剛好 4.000～6.000）
# - TrueType：AutoCAD 的字高＝「A」字頂到基線（y=0）的高度。由 AutoCAD 存的 TEXT 對齊點反推字高（字型單位），
#   正黑體 1549（8 筆）、標楷體 724（13 筆、3 張圖）、新細明體 688（1 筆）都剛好是該字型「A」的字頂；多行文字存的
#   範圍寬度（墨跡）也吻合（內嵌正黑體 Light 的 6 字，與 AutoCAD 存的值相同到小數 12 位）。所以中文字寬：
#   正黑體（含 Light）2048/1549、標楷體 1024/724、細明體與新細明體 1024/688
# ezdxf 縮放 TrueType 用的大寫字高是「A」字頂到「x」字底（標楷體、細明體的 x 底在基線下，比 AutoCAD 的大）；
# 替代字型照 ezdxf 量（例：思源黑體 1000/733＝1.364），中文字照原寬會與原字型不同寬
JHENGHEI = 2048 / 1549
KAIU = 1024 / 724
MINGLIU = 1024 / 688
FULL = 0.95                           # 前進寬度 ≥ 0.95 em 的非 ASCII 字當全形字（中文）縮放；英數不動
SHX_SPACE = 0.95                      # SHX 的空白寬（字高倍數）：AutoCAD 存檔寬度中含空白的 SHX 字串約 0.9～1.0
SHX_LIFT = 0.12                       # 字型沒有標準中文字身框時，SHX 中文字上移量（em）
ALIAS_FAMILY = "litian-cjk"           # 別名（檔名＝字族名）的開頭：依真實字族找字型時不會選到


def _target(font: str, bigfont: str, family: str) -> tuple[float | None, bool]:
    """原字型（樣式的字型檔、大字體、XDATA 字族）→（中文字寬÷字高, 是否 SHX）；不認得的字型 None（照替代字型原寬）。"""
    f, fam = font.lower(), family.lower()
    if bigfont or f.endswith(".shx") or (f and "." not in f):
        return 1.0, True
    name = f or fam                                      # 字型檔名空白時字族名在 XDATA
    if "jhenghei" in name or name.startswith("msjh") or "正黑" in name:
        return JHENGHEI, False
    if "kaiu" in name or "dfkai" in name or "標楷" in name:
        return KAIU, False
    if "mingliu" in name or "細明" in name:
        return MINGLIU, False
    return None, False


class _Sub:
    """替代字型的一個字面：路徑、中文字寬÷字高（照 ezdxf 的大寫字高縮放量）、中文字身框下緣、各別名共用的字形快取。"""

    def __init__(self, path: Path, face: int):
        from fontTools.ttLib import TTFont
        self.path, self.face = path, face
        self.base = TTFontRenderer(TTFont(str(path), fontNumber=face, lazy=True))
        tt = self.base.font
        self.upm = tt["head"].unitsPerEm
        adv = self.base.get_glyph_width("中")
        self.ratio = adv / self.base.font_measurements.cap_height if adv >= FULL * self.upm else None
        os2 = tt["OS/2"]
        # 中文字身框：思源黑體、正黑體的 typo 上升＋下降＝1 em，下緣＝typo 下降（約 -0.12 em）
        em_box = os2.sTypoAscender - os2.sTypoDescender == self.upm and os2.sTypoDescender < 0
        self.lift = -os2.sTypoDescender if em_box else SHX_LIFT * self.upm


_SUBS: dict[str, _Sub | None] = {}


def _tc_face(path: Path) -> int:
    """字型集（.ttc）裡繁體中文的字面（思源黑體 TC），沒有就第 0 個。"""
    if path.suffix.lower() != ".ttc":
        return 0
    from fontTools.ttLib import TTCollection
    with TTCollection(str(path), lazy=True) as coll:
        for i, f in enumerate(coll.fonts):
            if re.search(r"\bTC\b", f["name"].getDebugName(1) or ""):
                return i
    return 0


def _sub(name: str) -> _Sub | None:
    key = name.lower()
    if key not in _SUBS:
        sub = None
        try:
            path = Path(fonts.font_manager._font_cache[name].file_path)     # ezdxf 沒有公開的「字型檔路徑」
            sub = _Sub(path, _tc_face(path))
        except Exception:              # 沒有這個字型、讀不了：照原本的替代字型畫
            sub = None
        _SUBS[key] = sub if sub is not None and sub.ratio else None
    return _SUBS[key]


class _ScaledCJK(TTFontRenderer):
    """替代字型的字形，全形字（中文）x、y 都乘 k；SHX 另把中文字身框上移到基線～字高、空白寬改成 SHX 的寬度。"""

    def __init__(self, sub: _Sub, k: float, shx: bool):
        self._space = None
        super().__init__(sub.base.font)
        base = sub.base                          # 同一字面的別名共用原始字形與字寬快取（字形是未縮放的）
        self._glyph_path_cache = base._glyph_path_cache
        self._generic_glyph_cache = base._generic_glyph_cache
        self._glyph_width_cache = base._glyph_width_cache
        self.k = k
        self.lift = sub.lift if shx else 0.0
        self.full = FULL * sub.upm
        if shx:
            self._space = SHX_SPACE * self.font_measurements.cap_height
            self.space_width = self._space

    def get_glyph_width(self, char: str) -> float:
        if char == " " and self._space is not None:
            return self._space
        return super().get_glyph_width(char)

    def _k(self, c: str) -> float:
        return self.k if c > "\x7f" and super().get_glyph_width(c) >= self.full else 1.0

    def get_text_glyph_paths(self, s: str, cap_height: float = 1.0, width_factor: float = 1.0) -> list:
        out, x = [], 0.0
        f = self.get_scaling_factor(cap_height)
        base = self.font_measurements.baseline
        for c in s:
            k = self._k(c)
            m = Matrix44.scale(f * width_factor * k, f * k, 1.0)
            m[3, 0] = x
            m[3, 1] = (-base + (self.lift if k != 1.0 else 0.0)) * f * k
            p = self.get_glyph_path(c)
            p.transform_inplace(m)
            if len(p):
                out.append(p)
            x += self.get_glyph_width(c) * f * width_factor * k
        return out

    def get_text_length(self, s: str, cap_height: float = 1.0, width_factor: float = 1.0) -> float:
        return sum(self.get_glyph_width(c) * self._k(c) for c in s) * self.get_scaling_factor(cap_height) * width_factor


def style_font(substitute: str, font: str, bigfont: str, family: str, alias: bool = False) -> str:
    """原字型要換成替代字型 substitute 時，樣式該用的字型名：替代字型的別名（中文字依原字型縮放、用繁體字面），
    不必縮放時（alias＝False）與替代字型讀不到時就是 substitute 本身。alias：一定用別名（內嵌字型要依字族找到它）。"""
    sub = _sub(substitute)
    if sub is None:
        return substitute
    r, shx = _target(font, bigfont, family)
    k = r / sub.ratio if r else 1.0
    if abs(k - 1.0) < 1e-4 and not shx and sub.face == 0 and not alias:
        return substitute
    shx_tag = "-shx" if shx else ""
    name = f"{ALIAS_FAMILY}-{Path(substitute).stem}-{sub.face}-{round(k * 10000)}{shx_tag}{sub.path.suffix}"
    key = name.lower()
    # ezdxf 依樣式字型名（內嵌字型則依字族名，開頭相同就算）找字型（字型管理器）、再依檔名找字形（TrueTypeFont 的
    # 字形快取）：兩處都登記別名。字族名＝檔名：各別名都以副檔名結尾，彼此不會是開頭相同
    fm = fonts.font_manager
    if key not in fm._font_cache._cache:
        fm._font_cache._cache[key] = CacheEntry(sub.path, FontFace(filename=name, family=name))
    if key not in fonts.TrueTypeFont._glyph_caches:
        fonts.TrueTypeFont._glyph_caches[key] = _ScaledCJK(sub, k, shx)
    return name


# 多行文字內嵌的字型切換：\f新細明體|b0|i0;、\Ftxt.shx;。前面偶數個反斜線（\\ 是字面的反斜線，不是格式碼）
# 放在第 1 組、取代時留著；第 2 組 f／F、第 3 組字族或檔名
INLINE_FONT = re.compile(r"(?<!\\)((?:\\\\)*)\\([fF])([^|;\\]*)[^;\\]*;")


def inline_fonts(text: str, substitute: str, current: str) -> str:
    """多行文字內嵌的字型切換（\\f 是 TrueType 字族、\\F 是 SHX 檔）改用替代字型：每段的中文字寬依各自的原字型
    （AutoCAD 照內嵌的字型畫，不是樣式的字型）。全部與樣式的字型 current 相同時直接拿掉（照樣式畫）；
    否則每段改成別名的字族（不能只拿掉相同的那段：沒有大括號時，前一段的字型會延續下去）。"""
    def target(m) -> tuple[str, str, str]:
        name = m.group(3).strip()
        return (name, "", "") if m.group(2) == "F" else ("", "", name)

    codes = [m for m in INLINE_FONT.finditer(text) if m.group(3).strip()]
    if any(m.group(3).lower().startswith(ALIAS_FAMILY) for m in codes):    # 已改寫過（prepare 跑第二次）
        return text
    if all(style_font(substitute, *target(m)) == current for m in codes):
        return INLINE_FONT.sub(r"\1", text)

    def repl(m) -> str:
        if not m.group(3).strip():                       # 字族空白：ezdxf 不換字型
            return m.group(1)
        return f"{m.group(1)}\\f{style_font(substitute, *target(m), alias=True)}|b0|i0;"

    return INLINE_FONT.sub(repl, text)


# ---------- 中文換行 ----------
# AutoCAD 的多行文字在中文字之間換行（ezdxf 只在空白換行，整句中文會排成一行）。
# 容許超出：AutoCAD 存檔的多行文字範圍顯示，前進寬度超出欄寬 0.11、0.17 倍字高的行都沒有換行（墨跡也超出約 0.1 倍），
# 所以取 0.2 倍字高內不換行。
TOLERANCE = 0.2
NO_START = set("，。、；：？！）」』〉》】〕｝］…‥・ー～％．,.;:?!)]}%”’°℃")      # 不放行首
NO_END = set("（「『〈《【〔｛［([{“‘$＄")                                       # 不放行尾
_CJK = re.compile(r"[⺀-鿿가-힯豈-﫿︰-﹏＀-￯\U00020000-\U0003ffff]")


def is_cjk(ch: str) -> bool:
    return bool(_CJK.match(ch)) or unicodedata.east_asian_width(ch) in ("W", "F")


def has_cjk(s: str) -> bool:
    return bool(_CJK.search(s))


def can_break(a: str, b: str) -> bool:
    """字 a、b 之間可否換行：至少一邊是中文；不在行首禁則字之前、行尾禁則字之後；數字與單位字（㎡）不拆。"""
    if b in NO_START or a in NO_END or not (is_cjk(a) or is_cjk(b)):
        return False
    return not (a.isdigit() and "㌀" <= b <= "㏿")


def split_cjk(word: str) -> list[str]:
    """沒有空白的字詞 → 在可換行處切開的片段（英數串如 FE-101、1234.56㎡ 保持完整）。"""
    segs, start = [], 0
    for i in range(1, len(word)):
        if can_break(word[i - 1], word[i]):
            segs.append(word[start:i])
            start = i
    segs.append(word[start:])
    return segs


# 欄寬 0 的多行文字：縮排超過幾倍字高才改當圖面單位（見 _paragraph）。正式圖存成圖面單位的值是 150、137（字高 800），
# 當字高倍數的縮排、定位點一般只有幾倍（ezdxf 預設定位點 4、8、12…）
INDENT_W0 = 20.0


def _tab_unit(stop, cap: float):
    """定位點改當圖面單位（ezdxf 會乘字高，先除掉）：數值，或帶 c／r 前置（置中、靠右）的字串。"""
    return f"{stop[0]}{float(stop[1:]) / cap}" if isinstance(stop, str) else stop / cap


def _paragraph(p: ParagraphProperties, cap: float, width: float, tol: float) -> ParagraphProperties:
    """段落屬性（縮排是字高倍數）：
    - ezdxf 把 \\pi、\\pl、\\pr 的值乘上字高；不少圖存的是圖面單位（例：字高 800、\\pl150 → 接續行被推到 12 萬單位外，
      在視埠外看不到）。乘上字高會超出欄寬 width 的就改當圖面單位，同一碼的定位點（\\pt）一起換算（AutoCAD 的語意
      沒有文件，這只是合理化）。欄寬 0（width＝0）時改看是否超過 INDENT_W0 倍字高：ezdxf 依內容估的寬度不能當門檻，
      否則同樣的碼會因字串長短換算或不換算；
    - 容許超出量 tol 加在不影響位置的一側：靠左加在右邊、置中兩邊各半、靠右加在左邊（左右對齊不加）。"""
    big = max(abs(p.left), abs(p.left + p.indent), abs(p.right))
    if (big * cap > width) if width > 0 else (big > INDENT_W0):
        p = p._replace(indent=p.indent / cap, left=p.left / cap, right=p.right / cap,
                       tab_stops=tuple(_tab_unit(t, cap) for t in p.tab_stops))
    align = int(p.align)                       # 0 預設、1 靠左、2 靠右、3 置中、4/5 左右對齊
    if align in (0, 1):
        p = p._replace(right=p.right - tol)
    elif align == 3:
        p = p._replace(left=p.left - tol / 2, right=p.right - tol / 2)
    elif align == 2:
        p = p._replace(left=p.left - tol)
    return p


def _cells(r: ComplexMTextRenderer, items: list, split: bool) -> list:
    """一個段落的字詞 → 排版格。split：字詞在中文可換行處切開，片段之間放寬度 0、可換行的空白。"""
    cells: list = []
    last = ""                                  # 緊接在前的字詞最後一個字（字詞換格式時也可能可以換行）
    for kind, data, ctx in items:
        if kind == TokenType.WORD:
            if cells and isinstance(cells[-1], (tl.Text, tl.Fraction)):
                cells.append(tl.Space(width=0) if split and last and can_break(last, data[0]) else super_glue())
            for i, seg in enumerate(split_cjk(data) if split else [data]):
                if i:
                    cells.append(tl.Space(width=0))
                cells.append(r.word(seg, ctx))
            last = data[-1]
            continue
        last = ""
        if kind == TokenType.SPACE:
            cells.append(r.space(ctx))
        elif kind == TokenType.NBSP:
            cells.append(r.non_breaking_space(ctx))
        elif kind == TokenType.TABULATOR:
            cells.append(r.tabulator(ctx))
        elif kind == TokenType.STACK:
            if cells and isinstance(cells[-1], (tl.Text, tl.Fraction)):
                cells.append(super_glue())
            cells.append(r.fraction(data, ctx))
    return cells


def _layout(r: ComplexMTextRenderer, mtext, tol: float, split: bool) -> tl.Layout:
    """複製 ezdxf 1.4.4 的 AbstractMTextRenderer.layout_engine（render/abstract_mtext_renderer.py），改了：
    split 時段落一行放不下才把字詞在中文可換行處切開（多數段落不切：格數少、畫得快）、段落屬性經 _paragraph。
    split＝False、tol＝0 時只差縮排合理化。"""
    cap = valid_text_height(mtext.dxf.char_height)
    line_spacing = mtext.dxf.line_spacing_factor
    width = defined_width(mtext)
    default_stops = make_default_tab_stops(cap, width)
    layout = tl.Layout(width=width)
    bg = r.make_bg_renderer(mtext)
    if mtext.has_columns:
        columns = mtext.columns
        col_w = columns.width
        for height in column_heights(columns):
            layout.append_column(width=columns.width, height=height, gutter=columns.gutter_width, renderer=bg)
    else:
        col_w = width
        layout.append_column(renderer=bg)
    fixed_w = col_w if mtext.has_columns or mtext.dxf.get("width", 0.0) >= 1e-6 else 0.0   # 欄寬 0：沒有欄寬可比
    ctx = r.make_mtext_context(mtext)
    items: list = []

    def append_paragraph():
        c = ctx.copy()
        p = c.paragraph = _paragraph(ctx.paragraph, cap, fixed_w, tol)
        cells = _cells(r, items, False)
        left = p.left * cap
        room = col_w - max(left, left + p.indent * cap) - p.right * cap
        if split and (any(kind == TokenType.TABULATOR for kind, _, _ in items)
                      or sum(x.total_width for x in cells) > room):
            cells = _cells(r, items, True)
        layout.append_paragraphs([new_paragraph(cells, c, cap, line_spacing, width, default_stops)])
        items.clear()

    for token in MTextParser(mtext.all_columns_raw_content(), ctx):
        ctx = token.ctx
        if token.type in (TokenType.NEW_PARAGRAPH, TokenType.NEW_COLUMN):
            append_paragraph()
            if token.type == TokenType.NEW_COLUMN:
                layout.next_column()
        elif token.type in (TokenType.SPACE, TokenType.NBSP, TokenType.TABULATOR, TokenType.WORD, TokenType.STACK):
            items.append((token.type, token.data, ctx))
    if items:
        append_paragraph()
    return layout


INDENT = re.compile(r"\\p[^;]*?[ilr]-?[\d.]")         # 段落縮排碼（\pi、\pl、\pr 帶數值）


def draw_mtext(frontend, mtext, properties) -> bool:
    """用這裡的排版畫出、回傳 True 的多行文字：含中文、有欄寬的（可在中文字間換行、容許超出量），以及帶縮排碼的
    （只做縮排合理化，其他與 ezdxf 相同）。其他（欄寬 0 本來就不換行、立體文字、設定不畫文字、排版失敗）回傳 False，
    照 ezdxf 原本的畫法。"""
    if frontend.config.text_policy == TextPolicy.IGNORE or is_spatial_text(Vec3(mtext.dxf.extrusion)):
        return False
    raw = mtext.all_columns_raw_content()
    split = mtext.dxf.get("width", 0.0) >= 1e-6 and has_cjk(raw)
    if not split and not INDENT.search(raw):
        return False
    r = ComplexMTextRenderer(frontend.ctx, frontend.pipeline, properties)
    try:
        layout = _layout(r, mtext, TOLERANCE if split else 0.0, split)
        layout.place(align=tl.LayoutAlignment(mtext.dxf.attachment_point))
    except tl.LayoutError:
        return False
    layout.render(mtext.ucs().matrix)
    return True
