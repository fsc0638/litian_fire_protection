"""CAD 原樣圖：把圖檔照 CAD 的樣子（圖層顏色、線型、文字、圖框、配置頁）畫成白底高解析度圖磚，
審核工作台可縮放平移，缺失（overlay.json，公尺座標）依 meta.json 的轉換疊上去。

每張樓層圖（檢核結果 floors[]）畫一張：有配置頁的畫該配置頁（連同視埠內容）；模型空間圖框的只畫框內的實體。
輸出放在檢核資料夾（<檔名>.review）：
- cad/<svg_name>/meta.json：原尺寸寬高、圖磚規格、公尺座標 → 原尺寸像素的仿射轉換
  （px＝a*X+b*Y+c，py＝d*X+e*Y+f，py 向下）
- cad/<svg_name>/<層級>/<欄>_<列>.png：Deep Zoom 層級規則（最大層＝原尺寸，每往下一層長寬減半、無條件進位，第 0 層 1×1）
- cad/status.json：整批狀態 rendering｜done｜failed、各張結果、警告（例：找不到中文字型）

命令列：python -m litian.review.cadview <dxf> <ir.json> <review.json> <review_dir>（worker 用子行程呼叫，限時、限記憶體）
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
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


def mark_failed(review_dir: str | Path, error: str) -> None:
    """子行程逾時、被砍（記憶體不足）或中斷：已畫好的各張照舊，整批記失敗。"""
    st = read_status(review_dir) or {"sheets": {}, "warnings": [], "started_at": None}
    st.update(state="failed", error=error[:500], finished_at=_now())
    write_status(review_dir, st)


def fail_interrupted(cases_dir: str | Path) -> int:
    """worker 啟動時：上次停在「畫圖中」的（worker 當掉、容器重啟）改記失敗，工作台才不會一直顯示畫圖中。"""
    n = 0
    for p in Path(cases_dir).glob("*/*.review/cad/status.json"):
        st = read_status(p.parent.parent)
        if st and st.get("state") == "rendering":
            mark_failed(p.parent.parent, "畫圖中斷（處理程序重新啟動），重新處理檔案即可重畫")
            n += 1
    return n


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
    2) 多行文字內嵌的字型切換拿掉（改用上面的中文字型）；
    3) 轉檔後視埠的狀態常被設成「關閉」（0）：整張紙以外的視埠打開，才畫得出視埠內容；
    4) 實體用到、圖層表卻沒有的圖層補上（預設白／黑色，跟 AutoCAD 開檔時一樣；否則 ezdxf 一律畫成白色）。"""
    from ezdxf.fonts import fonts
    out = {"styles": 0, "mtext": 0, "viewports": 0, "layers": 0}
    if font:
        fm = fonts.font_manager
        fm._fallback_font_name = font          # ezdxf 沒有公開的設定方法
        for st in doc.styles:
            f = st.dxf.get("font", "") or ""
            if f.lower().endswith(".shx") or "." not in f or st.dxf.get("bigfont", "") or not fm.has_font(f):
                st.dxf.font = font
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
            if not _paper_vp(vp) and vp.dxf.get("status", 0) <= 0:
                vp.dxf.status = 2
                out["viewports"] += 1
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

def _placement(box, page, settings):
    """圖面座標 → PDF 點（左上原點、y 向下）：與 PyMuPdfBackend.get_replay 內部同一套算法。"""
    import copy

    from ezdxf.addons.drawing import layout, pymupdf
    out = layout.Layout(box, flip_y=True)
    final = out.get_final_page(page, settings)
    s2 = copy.copy(settings)
    s2.output_coordinate_space = pymupdf.get_coordinate_output_space(final)
    return out.get_placement_matrix(final, settings=s2, top_origin=True)


def _affine(fn) -> list[float]:
    """（X, Y）→（px, py）的函式 → 仿射係數 [a, b, c, d, e, f]。"""
    ox, oy = fn(0.0, 0.0)
    ax, ay = fn(1.0, 0.0)
    bx, by = fn(0.0, 1.0)
    return [round(v, 9) for v in (ax - ox, bx - ox, ox, ay - oy, by - oy, oy)]


def render_sheet(doc, sheet: dict, cache, long_px: int | None = None):
    """畫一張 → (PIL 影像, meta, 警告)。long_px：指定長邊像素（測試用）；未指定時約 A3 300 dpi，紙張大的提高到上限 MAX_PX。"""
    import ezdxf
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, pymupdf
    from ezdxf.math import BoundingBox2d, Matrix44, Vec2, Vec3
    from PIL import Image

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
        unit_mm = 25.4 if lay.dxf_layout.dxf.get("plot_paper_units", 1) == 0 else 1.0
        paper_mm = max(box.size.x, box.size.y) * unit_mm
        source, draw = "layout", (lambda fe: fe.draw_layout(lay, finalize=True))
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

        source, draw = "model", (lambda fe: fe.draw_layout(msp, finalize=True, filter_func=inside))
    paper_mm = paper_mm if paper_mm and 50 <= paper_mm <= 3000 else 420.0      # 紙張大小不明：當 A3
    box = _pad(box)
    if long_px is None:
        long_px = max(A3_PX, round(paper_mm / 25.4 * DPI))
    long_px = min(MAX_PX, long_px)
    w, h = box.size.x, box.size.y
    k = paper_mm / max(w, h)                                     # 圖面單位 → 紙面 mm
    dpi = max(1, int(long_px * 25.4 / (max(w, h) * k)))
    page = layout.Page(w * k, h * k, layout.Units.mm)            # 版面不留邊，頁面長寬比＝範圍長寬比
    settings = layout.Settings(fit_page=True)

    backend = pymupdf.PyMuPdfBackend()
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.COLOR,
                               image_policy=config.ImagePolicy.RECT)   # 圖片只畫外框：DXF 可指向任意本機檔案
    draw(Frontend(RenderContext(doc), backend, config=cfg, bbox_cache=cache))
    place = _placement(box, page, settings)
    replay = backend.get_replay(page, settings=settings, render_box=box)
    del backend                                                  # 記錄的圖元已轉成 PDF 頁面，先放掉
    pix = replay.get_pixmap(dpi=dpi)
    # 直接讀點陣記憶體（不經 samples 的 bytes 複本、不另 copy：8000 px 時每份約 140～180 MB）
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples_mv, "raw", "RGB", pix.stride)
    del pix, replay

    zoom = dpi / 72                                              # PDF 點 → 像素

    def to_px(x: float, y: float) -> tuple[float, float]:
        p = place.transform(to_paper.transform(Vec3(x / scale, y / scale, 0)))
        return p.x * zoom, p.y * zoom

    meta = {"version": 1, "width": img.width, "height": img.height, "tile_size": TILE, "overlap": 0, "format": "png",
            "max_level": max_level(img.width, img.height), "transform": _affine(to_px), "source": source,
            "layout": sheet.get("layout") if source == "layout" else None, "dpi": dpi,
            "rendered_at": _now(), "renderer": f"ezdxf {ezdxf.__version__}"}
    return img, meta, notes


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
            del img
            _write_json(tmp / "meta.json", meta)
            _swap(tmp, cad / name)
            st["sheets"][name] = "done"
        except Exception as e:
            st["sheets"][name] = "failed"
            errors[name] = f"{type(e).__name__}: {e}"[:300]
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
