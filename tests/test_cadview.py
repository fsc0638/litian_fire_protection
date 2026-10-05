"""CAD 原樣圖（review.cadview）與缺失疊圖資料（engine 的 overlay.json）。全部用程式畫的圖。

驗證：圖磚層級與數量（Deep Zoom 規則）、meta 欄位、公尺 → 像素的轉換（在已知公尺座標畫紅色實心方塊，
依 transform 換算的像素確實是紅色）、配置頁與模型空間圖框兩種來源、失敗記在 status.json、沒有中文字型照畫。"""

import json
import math

import ezdxf
import pytest
from PIL import Image
from shapely.geometry import Point, shape

from litian.drawing import ir as IR
from litian.review import cadview as CV
from litian.review import engine as EN

from .test_plan_review import make_fire_dxf
from .test_xref_layouts import make_layout_dxf

RED = (22.0, 5.0)        # 紅色方塊中心（公尺）：會議室裡
SIDE = 1.0
LONG = 1500              # 測試用長邊像素（實際約 A3 300 dpi 以上）


def _red_square(doc, unit: float, at=RED, side=SIDE):
    x, y = at[0] / unit, at[1] / unit
    h = side / 2 / unit
    hatch = doc.modelspace().add_hatch(color=1)
    hatch.paths.add_polyline_path([(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)])


def layout_dxf(path):
    """配置頁版：模擬轉檔後的樣子（視埠狀態全是 0，另有一個代表整張紙的視埠）。"""
    make_layout_dxf(path)
    doc = ezdxf.readfile(path)
    _red_square(doc, 0.001)
    lay = doc.layouts.get("FE-311")
    lay.add_viewport(center=(100, 80), size=(150, 100), view_center_point=(100, 80), view_height=100)
    for la in doc.layouts:
        for vp in la.query("VIEWPORT"):
            vp.dxf.status = 0
    doc.saveas(path)


def model_dxf(path):
    make_fire_dxf(path)
    doc = ezdxf.readfile(path)
    _red_square(doc, 0.01)
    _red_square(doc, 0.01, at=(900.0, 900.0))       # 圖框外很遠的實體：不畫、也不撐大範圍
    doc.saveas(path)


def _review(p):
    ir = IR.extract(p)
    return ir, EN.to_dict(EN.review_dxf(p, ir=ir), geom=False)


def _px(meta, x, y):
    a, b, c, d, e, f = meta["transform"]
    return a * x + b * y + c, d * x + e * y + f


def _pixel(sheet_dir, meta, x, y):
    px, py = _px(meta, x, y)
    col, row = int(px) // 512, int(py) // 512
    tile = Image.open(sheet_dir / str(meta["max_level"]) / f"{col}_{row}.png").convert("RGB")
    return tile.getpixel((int(px) - col * 512, int(py) - row * 512))


def _check_tiles(sheet_dir, meta):
    W, H, L = meta["width"], meta["height"], meta["max_level"]
    assert L == math.ceil(math.log2(max(W, H)))
    for level in range(L + 1):
        w, h = math.ceil(W / 2 ** (L - level)), math.ceil(H / 2 ** (L - level))
        files = sorted(p.name for p in (sheet_dir / str(level)).iterdir())
        assert len(files) == math.ceil(w / 512) * math.ceil(h / 512), level
        last = Image.open(sheet_dir / str(level) / f"{math.ceil(w / 512) - 1}_{math.ceil(h / 512) - 1}.png")
        assert last.size == (w - (math.ceil(w / 512) - 1) * 512, h - (math.ceil(h / 512) - 1) * 512)
    assert Image.open(sheet_dir / "0" / "0_0.png").size == (1, 1)
    assert not (sheet_dir / str(L + 1)).exists()


def test_layout_sheet_renders_viewport_and_transform(tmp_path):
    p = tmp_path / "fire.dxf"
    layout_dxf(p)
    ir, review = _review(p)
    name = review["floors"][0]["svg_name"]
    st = CV.render_all(p, ir, review, tmp_path / "fire.dxf.review", long_px=LONG)
    assert st["state"] == "done" and st["sheets"] == {name: "done"} and st["error"] is None
    on_disk = CV.read_status(tmp_path / "fire.dxf.review")
    assert on_disk["state"] == "done" and on_disk["finished_at"] and "seconds" not in on_disk
    d = tmp_path / "fire.dxf.review" / "cad" / name
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert {k: meta[k] for k in ("version", "tile_size", "overlap", "format", "source", "layout")} == {
        "version": 1, "tile_size": 512, "overlap": 0, "format": "png", "source": "layout", "layout": "FE-311"}
    assert isinstance(meta["dpi"], int) and meta["renderer"].startswith("ezdxf ") and meta["rendered_at"]
    assert max(meta["width"], meta["height"]) == pytest.approx(LONG, rel=0.02)
    _check_tiles(d, meta)
    # 視埠原本是「關閉」：打開後才畫得出模型空間的紅色方塊；位置與轉換一致
    assert _pixel(d, meta, *RED) == (255, 0, 0)
    assert _pixel(d, meta, RED[0] + 0.3, RED[1] - 0.3) == (255, 0, 0)
    assert _pixel(d, meta, RED[0] - 3, RED[1] - 2) == (255, 255, 255)
    a, b, _c, dd, e, _f = meta["transform"]
    assert b == pytest.approx(0, abs=1e-6) and dd == pytest.approx(0, abs=1e-6) and a == pytest.approx(-e)   # y 向下
    assert not list((tmp_path / "fire.dxf.review" / "cad").glob(".tmp-*"))


def _count(sheet_dir, meta, rgb) -> int:
    """原尺寸那一層所有圖磚中，恰為 rgb 的像素數。"""
    return sum(n for t in (sheet_dir / str(meta["max_level"])).iterdir()
               for n, c in Image.open(t).convert("RGB").getcolors(512 * 512) if c == rgb)


@pytest.mark.parametrize("paper", [None, 0, 1, 2])
def test_layout_main_viewport_status_1_still_drawn(tmp_path, paper):
    """主視埠狀態是 1（真實圖也有）：ezdxf 會把排第一個、狀態 1 的視埠當成整張紙丟掉不畫。
    整張紙視埠不存在、關閉、狀態 1 但排在主視埠後面、或狀態 2 時，都要畫出主視埠內容，且不把整張紙視埠當內容畫。"""
    p = tmp_path / "fire.dxf"
    make_layout_dxf(p)
    doc = ezdxf.readfile(p)
    _red_square(doc, 0.001)
    # 模型空間 (100, 80) mm 附近：主視埠、小視埠都框不到，只有整張紙視埠被當成內容畫時才會出現
    _red_square(doc, 0.001, at=(0.1, 0.08), side=0.3)
    lay = doc.layouts.get("FE-311")
    main, small = sorted(lay.query("VIEWPORT"), key=lambda v: -float(v.dxf.width))
    main.dxf.status, small.dxf.status = 1, 2
    if paper is not None:                              # 加在主視埠之後：同為狀態 1 時排在後面
        lay.add_viewport(center=(100, 80), size=(150, 100), view_center_point=(100, 80),
                         view_height=100).dxf.status = paper
    doc.saveas(p)
    ir, review = _review(p)
    name = review["floors"][0]["svg_name"]
    st = CV.render_all(p, ir, review, tmp_path / "r", long_px=LONG)
    assert st["sheets"] == {name: "done"}
    d = tmp_path / "r" / "cad" / name
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert _pixel(d, meta, *RED) == (255, 0, 0)
    side = SIDE * abs(meta["transform"][0])            # 1 m 方塊的像素邊長
    assert 0.6 * side ** 2 < _count(d, meta, (255, 0, 0)) < 1.2 * side ** 2      # 只有視埠裡那一塊


def test_model_frame_renders_only_frame_area(tmp_path):
    p = tmp_path / "fire.dxf"
    model_dxf(p)
    ir, review = _review(p)
    assert ir["sheets"][0]["layout"] is None
    name = review["floors"][0]["svg_name"]
    st = CV.render_all(p, ir, review, tmp_path / "r", long_px=LONG)
    assert st["sheets"] == {name: "done"}
    d = tmp_path / "r" / "cad" / name
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "model" and meta["layout"] is None
    _check_tiles(d, meta)
    assert _pixel(d, meta, *RED) == (255, 0, 0)
    # 範圍＝圖框（單位 cm → 公尺），四周只留長邊 1%：圖框角落落在影像邊緣附近，很遠的實體沒把範圍撐大
    x0, y0, x1, y1 = (v * 0.01 for v in ir["sheets"][0]["bbox"])
    for (x, y), (ex, ey) in (((x0, y1), (0, 0)), ((x1, y0), (meta["width"], meta["height"]))):
        px, py = _px(meta, x, y)
        assert abs(px - ex) < 0.02 * meta["width"] and abs(py - ey) < 0.02 * meta["width"]


def _flat(c) -> list:
    return [c] if isinstance(c, (int, float)) else [v for x in c for v in _flat(x)]


def test_overlay_json_matches_review_numbering(tmp_path):
    p = tmp_path / "fire.dxf"
    make_fire_dxf(p)
    out = tmp_path / "review.json"
    assert EN.main(["engine", str(p), str(out), "--svg", str(tmp_path / "svg"), "--no-geom"]) == 0
    fl = json.loads(out.read_text(encoding="utf-8"))["floors"][0]
    ov = json.loads((tmp_path / "svg" / f"{fl['svg_name']}.overlay.json").read_text(encoding="utf-8"))
    assert {k: ov[k] for k in ("version", "sheet", "number", "label")} == {"version": 1, "sheet": fl["sheet"],
                                                                            "number": "F-101", "label": "1F"}
    assert [(f["no"], f["key"], f["severity"], f["rule"], f["title"]) for f in ov["findings"]] == \
        [(f["no"], f["key"], f["severity"], f["rule"], f["title"]) for f in fl["findings"]]
    with_geom = [f for f in ov["findings"] if f["geom"]]
    assert with_geom and any(f["geom"] is None and f["anchor"] is None for f in ov["findings"])
    for f, r in ((f, next(x for x in fl["findings"] if x["no"] == f["no"])) for f in with_geom):
        g = shape(f["geom"])
        assert g.buffer(0.06).covers(Point(f["anchor"]))                    # 標號在範圍內
        assert g.bounds == pytest.approx(r["bbox"], abs=0.06)              # 簡化約 5 cm、與檢核結果同一座標系（公尺）
        assert all(round(v, 2) == v for v in _flat(f["geom"]["coordinates"]) + f["anchor"])     # 四捨五入到公分


def test_geojson_handles_geometry_collection():
    from shapely.geometry import GeometryCollection, box
    g = EN._geo(GeometryCollection([Point(0.123, 0), box(0, 0, 1, 1)]))
    assert g["type"] == "GeometryCollection" and [x["type"] for x in g["geometries"]] == ["Point", "Polygon"]
    assert g["geometries"][0]["coordinates"] == [0.12, 0]


def test_failed_sheet_and_unreadable_file_recorded_in_status(tmp_path):
    p = tmp_path / "fire.dxf"
    layout_dxf(p)
    ir, review = _review(p)
    ok = review["floors"][0]["svg_name"]
    review["floors"].append({**review["floors"][0], "sheet": 99, "svg_name": "2F-99", "label": "2F"})   # 中介資料沒有這張
    st = CV.render_all(p, ir, review, tmp_path / "r", long_px=600)
    assert st["state"] == "done" and st["sheets"] == {ok: "done", "2F-99": "failed"}
    assert "單位" in st["sheet_errors"]["2F-99"] and not (tmp_path / "r" / "cad" / "2F-99").exists()
    # 重畫時已不存在的樓層圖資料夾清掉
    (tmp_path / "r" / "cad" / "9F-1").mkdir()
    review["floors"].pop()
    assert CV.render_all(p, ir, review, tmp_path / "r", long_px=600)["sheets"] == {ok: "done"}
    assert sorted(x.name for x in (tmp_path / "r" / "cad").iterdir()) == [ok, "status.json"]

    bad = tmp_path / "bad.dxf"
    bad.write_bytes(b"\x00\x01 not a dxf")
    (tmp_path / "ir.json").write_text(json.dumps(ir), encoding="utf-8")
    (tmp_path / "review.json").write_text(json.dumps(review), encoding="utf-8")
    assert CV.main(["cadview", str(bad), str(tmp_path / "ir.json"), str(tmp_path / "review.json"), str(tmp_path / "b")]) == 1
    st = CV.read_status(tmp_path / "b")
    assert st["state"] == "failed" and st["error"] and st["finished_at"]


def test_interrupted_render_marked_failed(tmp_path):
    rd = tmp_path / "12" / "001_x.dxf.review"
    CV.start_status(rd)
    CV.write_status(tmp_path / "12" / "002_y.dxf.review", {"state": "done", "sheets": {"1F-0": "done"}})
    assert CV.fail_interrupted(tmp_path) == 1
    st = CV.read_status(rd)
    assert st["state"] == "failed" and "中斷" in st["error"]
    assert CV.read_status(tmp_path / "12" / "002_y.dxf.review")["state"] == "done"


def test_prepare_fixes_fonts_inline_fonts_viewports_and_layers(monkeypatch):
    from ezdxf.fonts import fonts
    monkeypatch.setattr(fonts.font_manager, "_fallback_font_name", fonts.font_manager._fallback_font_name)
    doc = ezdxf.new("R2018")
    doc.styles.add("CHT", font="chineset.shx").dxf.bigfont = "chineset.shx"
    m = doc.modelspace().add_mtext(r"{\f新細明體|b0|i0;一層}平面", dxfattribs={"layer": "沒定義的圖層"})
    lay = doc.layouts.new("FE-101")
    paper = lay.add_viewport(center=(100, 80), size=(150, 100), view_center_point=(100, 80), view_height=100)
    main = lay.add_viewport(center=(200, 150), size=(380, 260), view_center_point=(0, 0), view_height=20000)
    detail = lay.add_viewport(center=(40, 40), size=(30, 20), view_center_point=(500, 500), view_height=500)
    paper2 = lay.add_viewport(center=(300, 200), size=(60, 40), view_center_point=(300, 200), view_height=40)
    paper.dxf.status = main.dxf.status = 0
    detail.dxf.status, paper2.dxf.status = 1, 3
    n = CV.prepare(doc, "CJK.ttf")
    assert doc.styles.get("CHT").dxf.font == "CJK.ttf" and doc.styles.get("CHT").dxf.bigfont == ""
    assert m.text == "{一層}平面" and n["viewports"] == 3
    assert (main.dxf.status, detail.dxf.status, paper.dxf.status, paper2.dxf.status) == (2, 2, 0, 0)
    assert doc.layers.has_entry("沒定義的圖層") and n["layers"] >= 1


def test_no_cjk_font_still_renders_with_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("LITIAN_CAD_FONT", raising=False)
    monkeypatch.setattr(CV, "FONTS", ("沒有這個字型.ttf",))
    assert CV.pick_font()[0] is None
    p = tmp_path / "fire.dxf"
    model_dxf(p)
    ir, review = _review(p)
    st = CV.render_all(p, ir, review, tmp_path / "r", long_px=600)
    assert st["state"] == "done" and any("中文字型" in w for w in st["warnings"])
    assert any("中文字型" in w for w in CV.read_status(tmp_path / "r")["warnings"])


class _Conn:
    def __init__(self):
        self.sql = []

    def transaction(self):
        class T:
            def __enter__(s): return s
            def __exit__(s, *a): return False
        return T()

    def execute(self, sql, params=None):
        from types import SimpleNamespace
        self.sql.append((sql, params))
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None, rowcount=0)


def _worker(monkeypatch, path, review_only=False):
    from litian.drawing import worker as W
    reviews, failed = [], []
    monkeypatch.setattr(W.ST, "claim", lambda conn: {"id": 7, "case_id": 1, "name": path.name, "kind": "dxf", "path": str(path),
                                                    "attempts": 1, "review_only": review_only})
    monkeypatch.setattr(W.ST, "get_context", lambda conn, cid: {"occupancy": "乙-6"})
    monkeypatch.setattr(W.ST, "save_result", lambda *a, **k: None)
    monkeypatch.setattr(W.ST, "mark", lambda *a, **k: None)
    monkeypatch.setattr(W.ST, "save_review", lambda conn, fid, status, result, error, svg_dir: reviews.append(status))
    monkeypatch.setattr(W.ST, "save_failure", lambda conn, fid, err, retry: failed.append(err))
    return W, reviews, failed


def test_worker_renders_after_review_and_records_stats(tmp_path, monkeypatch):
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed = _worker(monkeypatch, p)
    conn = _Conn()
    assert W.run_once(conn, tmp_path) is True
    assert failed == [] and reviews == ["done"]
    rd = tmp_path / "001_F-101.dxf.review"
    assert CV.read_status(rd)["state"] == "done" and (rd / "cad" / "1F-0" / "meta.json").is_file()
    assert (rd / "1F-0.overlay.json").is_file() and (rd / "1F-0.svg").is_file()
    sql, params = next(x for x in conn.sql if "'cad'" in x[0])
    info = json.loads(params[0])
    assert info["state"] == "done" and info["sheets"] == 1 and info["failed"] == 0 and info["seconds"] >= 0


def test_worker_render_failure_keeps_review(tmp_path, monkeypatch):
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed = _worker(monkeypatch, p)
    run = W._run

    def fake(args, timeout, what):
        if args[0] == "litian.review.cadview":
            assert timeout == W.CAD_TIMEOUT
            raise RuntimeError("CAD 原樣圖失敗：MemoryError")
        return run(args, timeout, what)

    monkeypatch.setattr(W, "_run", fake)
    conn = _Conn()
    assert W.run_once(conn, tmp_path) is True
    assert failed == [] and reviews == ["done"]                     # 檢核結果照常
    st = CV.read_status(tmp_path / "001_F-101.dxf.review")
    assert st["state"] == "failed" and "MemoryError" in st["error"]
    assert json.loads(next(x for x in conn.sql if "'cad'" in x[0])[1][0])["state"] == "failed"


def test_review_only_rerun_does_not_redraw(tmp_path, monkeypatch):
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed = _worker(monkeypatch, p, review_only=True)
    monkeypatch.setattr(W.ST, "load_ir", lambda conn, fid: IR.extract(p))
    monkeypatch.setattr(W, "render_cad", lambda *a: (_ for _ in ()).throw(AssertionError("只重跑檢核不重畫")))
    assert W.run_once(_Conn(), tmp_path) is True
    assert failed == [] and reviews == ["done"]
    assert (tmp_path / "001_F-101.dxf.review" / "1F-0.overlay.json").is_file()      # 疊圖資料由檢核更新
