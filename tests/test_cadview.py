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


def _worker(monkeypatch, path, review_only=False, missing=False):
    from litian.drawing import worker as W
    reviews, failed, calls = [], [], []
    monkeypatch.setattr(W.ST, "claim", lambda conn: {"id": 7, "case_id": 1, "name": path.name, "kind": "dxf", "path": str(path),
                                                    "attempts": 1, "review_only": review_only})
    monkeypatch.setattr(W.ST, "get_context", lambda conn, cid: {"occupancy": "乙-6"})
    monkeypatch.setattr(W.ST, "save_result", lambda *a, **k: None)
    monkeypatch.setattr(W.ST, "mark", lambda conn, fid, status, stats=None: calls.append(("mark", status)))
    monkeypatch.setattr(W.ST, "queue_cad", lambda conn, fid: calls.append(("queue", fid)))
    monkeypatch.setattr(W.ST, "reset_cad", lambda conn, fid: calls.append(("reset", fid)))
    monkeypatch.setattr(W.ST, "queue_cad_if_missing", lambda conn, fid: calls.append(("queue_missing", fid)) or missing)
    monkeypatch.setattr(W.ST, "save_review", lambda conn, fid, status, result, error, svg_dir: reviews.append(status))
    monkeypatch.setattr(W.ST, "save_failure", lambda conn, fid, err, retry: failed.append(err))
    return W, reviews, failed, calls


def test_worker_queues_cad_before_marking_done(tmp_path, monkeypatch):
    # 處理流程裡不畫（不卡住佇列）：排入原圖佇列，而且排在「完成」之前（工作台一看到完成就會繼續重查）
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed, calls = _worker(monkeypatch, p)
    assert W.run_once(_Conn(), tmp_path) is True
    assert failed == [] and reviews == ["done"]
    assert calls[0] == ("reset", 7) and calls.index(("queue", 7)) < calls.index(("mark", "done"))
    rd = tmp_path / "001_F-101.dxf.review"
    assert CV.read_status(rd)["state"] == "pending" and not (rd / "cad" / "1F-0").exists()
    assert (rd / "1F-0.overlay.json").is_file() and (rd / "1F-0.svg").is_file()


def test_review_only_rerun_does_not_redraw(tmp_path, monkeypatch):
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed, calls = _worker(monkeypatch, p, review_only=True)
    monkeypatch.setattr(W.ST, "load_ir", lambda conn, fid: IR.extract(p))
    cad = W.CadRunner()
    proc = _Proc(None)
    cad.cur = {"job": {"id": 7}, "proc": proc, "work": tmp_path / "w", "review_dir": tmp_path, "t0": 0}
    assert W.run_once(_Conn(), tmp_path, cad) is True
    # 已畫好、排隊中、畫圖中的不重排（底圖沒變）、不作廢；只問「還沒排過或上次失敗」的要不要排
    assert failed == [] and reviews == ["done"] and calls == [("mark", "reviewing"), ("queue_missing", 7), ("mark", "done")]
    assert cad.busy() and not proc.killed                                          # 同一個檔畫到一半的照常畫
    assert (tmp_path / "001_F-101.dxf.review" / "1F-0.overlay.json").is_file()      # 疊圖資料由檢核更新
    assert CV.read_status(tmp_path / "001_F-101.dxf.review") is None


def test_review_only_rerun_queues_when_never_drawn(tmp_path, monkeypatch):
    # 第一次檢核失敗（從沒排過原圖）、或上次畫失敗：只重跑檢核成功時排隊，在標成完成之前
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed, calls = _worker(monkeypatch, p, review_only=True, missing=True)
    monkeypatch.setattr(W.ST, "load_ir", lambda conn, fid: IR.extract(p))
    assert W.run_once(_Conn(), tmp_path) is True
    assert calls == [("mark", "reviewing"), ("queue_missing", 7), ("mark", "done")]
    assert CV.read_status(tmp_path / "001_F-101.dxf.review")["state"] == "pending"


def test_sigterm_during_processing_is_not_a_file_failure(tmp_path, monkeypatch):
    # 重新部署時的 SIGTERM 不能被「處理失敗」吃掉（否則檔案被記成失敗）
    from litian.drawing import worker as W
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed, calls = _worker(monkeypatch, p)
    seen = []

    def stop(*a):
        seen.append(W._current["job"]["id"])                                       # 處理中的檔記著（SIGTERM 時退回排隊）
        raise W.Stop()

    monkeypatch.setattr(W, "process", stop)
    with pytest.raises(W.Stop):
        W.run_once(_Conn(), tmp_path)
    assert failed == [] and seen == [7] and W._current["job"] is None


def test_full_reprocess_cancels_running_render_of_same_file(tmp_path, monkeypatch):
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    W, reviews, failed, calls = _worker(monkeypatch, p)
    cad = W.CadRunner()
    other, same = _Proc(None), _Proc(None)
    work = tmp_path / "w"
    work.mkdir()
    cad.cur = {"job": {"id": 8}, "proc": other, "work": work, "review_dir": tmp_path, "t0": 0}
    cad.cancel(7)
    assert cad.busy() and not other.killed                                         # 別的檔不動
    cad.cur = {"job": {"id": 7}, "proc": same, "work": work, "review_dir": tmp_path, "t0": 0}
    assert W.run_once(_Conn(), tmp_path, cad) is True
    assert same.killed and not cad.busy() and not work.exists() and ("reset", 7) in calls


# ---------- 背景畫圖（CadRunner） ----------

class _Proc:
    """假的子行程：rc＝None 表示還在跑。"""

    def __init__(self, rc):
        self.rc, self.killed = rc, False

    def poll(self):
        return self.rc

    def kill(self):
        self.killed, self.rc = True, -9

    def wait(self):
        return self.rc


def _cad_queue(monkeypatch, W, jobs, ir=None, review=None, finish=True):
    finished, recovered = [], []
    monkeypatch.setattr(W.ST, "recover_cad", lambda conn, s: recovered.append(s) or [])
    monkeypatch.setattr(W.ST, "claim_cad", lambda conn: jobs.pop(0) if jobs else None)
    monkeypatch.setattr(W.ST, "load_ir", lambda conn, fid: ir)
    monkeypatch.setattr(W.ST, "load_review", lambda conn, fid: review)
    monkeypatch.setattr(W.ST, "finish_cad", lambda conn, fid, gen, state, info: finished.append((fid, gen, state, info)) or finish)
    return finished, recovered


def _job(path, fid=7, gen=3, attempts=1):
    return {"id": fid, "case_id": 1, "name": path.name, "kind": "dxf", "path": str(path), "cad_gen": gen, "cad_attempts": attempts}


def _wait(cad, conn, timeout=180):
    import time
    t0 = time.time()
    while cad.busy():
        assert time.time() - t0 < timeout, "畫圖子行程沒有結束"
        cad.poll(conn)
        time.sleep(0.2)


def test_cad_runner_renders_queued_file_in_background(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    ir, review = _review(p)
    rd = tmp_path / "001_F-101.dxf.review"
    rd.mkdir()
    finished, recovered = _cad_queue(monkeypatch, W, [_job(p)], ir, review)
    cad = W.CadRunner()
    assert cad.start(_Conn()) is True and cad.busy()                               # 不等畫完就回來
    assert recovered == [W.CAD_STALE_S] and CV.read_status(rd)["state"] == "rendering"
    assert cad.start(_Conn()) is False                                             # 一次只畫一個
    _wait(cad, _Conn())
    (fid, gen, state, info), = finished
    assert (fid, gen, state) == (7, 3, "done") and info["sheets"] == 1 and info["failed"] == 0 and info["seconds"] >= 0
    assert CV.read_status(rd)["state"] == "done" and (rd / "cad" / "1F-0" / "meta.json").is_file()
    assert cad.start(_Conn()) is False                                             # 佇列空了


def test_cad_runner_records_render_failure(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    ir, review = _review(p)
    p.write_bytes(b"\x00\x01 not a dxf")                                           # 圖檔壞了：讀不了
    (tmp_path / "001_F-101.dxf.review").mkdir()
    finished, _ = _cad_queue(monkeypatch, W, [_job(p)], ir, review)
    cad = W.CadRunner()
    assert cad.start(_Conn()) is True
    _wait(cad, _Conn())
    (_, _, state, info), = finished
    st = CV.read_status(tmp_path / "001_F-101.dxf.review")
    assert state == "failed" and info["error"] and st["state"] == "failed" and st["error"]


def test_cad_runner_missing_inputs_fail_without_subprocess(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    (tmp_path / "001_F-101.dxf.review").mkdir()
    gone = tmp_path / "gone" / "002_x.dxf"                                         # 案件資料夾被刪了
    finished, _ = _cad_queue(monkeypatch, W, [_job(p), _job(gone, fid=8)], ir=None, review={"floors": []})
    cad = W.CadRunner()
    assert cad.start(_Conn()) is True and not cad.busy()                           # 處理了一筆（馬上失敗）
    assert cad.start(_Conn()) is True and not cad.busy()
    assert [(f[0], f[2]) for f in finished] == [(7, "failed"), (8, "failed")]
    assert CV.read_status(tmp_path / "001_F-101.dxf.review")["state"] == "failed"
    assert not (tmp_path / "gone").exists()                                        # 不重建已刪的資料夾


def _running(W, tmp_path, rc, attempts=1, age=0.0):
    import time
    rd = tmp_path / "x.dxf.review"
    rd.mkdir(exist_ok=True)
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    (work / "err.txt").write_text("一些警告\nMemoryError: 畫布太大\n", encoding="utf-8")
    CV.start_status(rd)
    cad = W.CadRunner()
    proc = _Proc(rc)
    cad.cur = {"job": _job(tmp_path / "x.dxf", attempts=attempts), "proc": proc, "work": work, "review_dir": rd,
               "t0": time.monotonic() - age}
    return cad, proc, rd


@pytest.mark.parametrize("rc,attempts,state,status,error", [
    (-9, 1, "pending", "pending", None),                 # 被系統砍掉（記憶體不足）：重新排隊
    (-9, 3, "failed", "failed", "MemoryError"),          # 次數用完
    (1, 1, "failed", "failed", "MemoryError"),           # 程式錯誤：重畫結果相同，不重排
    ("xcpu", 1, "failed", "failed", "逾時"),             # 超過 CPU 秒數上限
])
def test_cad_runner_exit_codes(tmp_path, monkeypatch, rc, attempts, state, status, error):
    from litian.drawing import worker as W
    finished, _ = _cad_queue(monkeypatch, W, [])
    cad, _, rd = _running(W, tmp_path, W.XCPU if rc == "xcpu" else rc, attempts)
    cad.poll(_Conn())
    assert not cad.busy() and not (tmp_path / "w").exists()
    (_, gen, got, info), = finished
    assert (gen, got) == (3, state) and (error is None or error in info["error"])
    st = CV.read_status(rd)
    assert st["state"] == status and (error is None or error in st["error"])


def test_cad_runner_timeout_kills_and_running_is_left_alone(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    finished, _ = _cad_queue(monkeypatch, W, [])
    cad, proc, _ = _running(W, tmp_path, None)
    cad.poll(_Conn())
    assert cad.busy() and not proc.killed and finished == []                       # 還在畫：不動
    cad.cur["t0"] -= W.CAD_TIMEOUT + 1
    cad.poll(_Conn())
    assert proc.killed and not cad.busy() and finished[0][2] == "failed" and "逾時" in finished[0][3]["error"]


def test_cad_result_dropped_when_file_requeued_meanwhile(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    finished, _ = _cad_queue(monkeypatch, W, [], finish=False)                     # cad_gen 變了
    cad, _, rd = _running(W, tmp_path, 1)
    CV.queue_status(rd)                                                            # 新一輪已排隊
    cad.poll(_Conn())
    assert finished and CV.read_status(rd)["state"] == "pending"                   # 新一輪的狀態沒被蓋掉


def test_recover_cad_on_start(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from litian.drawing import worker as W
    a, b, c, e, f, g = (tmp_path / f"{n}.dxf" for n in "abcefg")
    for x in (a, b, c, e, f, g):
        W.review_dir_of(x).mkdir()
        CV.start_status(W.review_dir_of(x))
    gone = tmp_path / "gone" / "d.dxf"
    now = datetime.now(timezone.utc)
    CV.write_status(W.review_dir_of(e), {"state": "done", "sheets": {"1F-0": "done", "2F-1": "failed"},
                                         "finished_at": now.isoformat(timespec="seconds")})
    CV.write_status(W.review_dir_of(a), {"state": "done", "sheets": {"1F-0": "done"},           # 上一輪的（比這次開始早）
                                         "finished_at": (now - timedelta(hours=1)).isoformat(timespec="seconds")})
    finished, seen, reviewed = [], [], []
    monkeypatch.setattr(W.ST, "rendering_cad", lambda conn: [
        {"id": 5, "path": str(e), "cad_gen": 4, "cad_started_at": now - timedelta(minutes=9)},
        {"id": 1, "path": str(a), "cad_gen": 2, "cad_started_at": now - timedelta(minutes=5)}])
    monkeypatch.setattr(W.ST, "finish_cad", lambda conn, fid, gen, state, info: finished.append((fid, gen, state, info)) or True)
    monkeypatch.setattr(W.ST, "recover_cad", lambda conn, s: seen.append(s) or [
        {"id": 1, "path": str(a), "cad_state": "pending"}, {"id": 2, "path": str(b), "cad_state": "failed"},
        {"id": 3, "path": str(gone), "cad_state": "pending"}])
    (W.review_dir_of(f) / "1F-0.overlay.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(W.ST, "backfill_cad", lambda conn: [
        {"id": 4, "path": str(c), "svg_dir": str(W.review_dir_of(c)), "names": ["1F-0"]},       # 舊版檢核：沒有疊圖資料
        {"id": 6, "path": str(f), "svg_dir": str(W.review_dir_of(f)), "names": ["1F-0"]}])
    monkeypatch.setattr(W.ST, "requeue_review", lambda conn, fid: reviewed.append(fid) or True)
    assert W.recover_cad_on_start(_Conn()) == (4, 2) and seen == [0]               # 啟動時「畫圖中」的全部算中斷
    # 子行程其實已經畫完（重啟前沒來得及記）：照結果記、不重畫
    assert [(x[0], x[1], x[2]) for x in finished] == [(5, 4, "done")] and finished[0][3]["failed"] == 1
    assert CV.read_status(W.review_dir_of(a))["state"] == "pending"
    st = CV.read_status(W.review_dir_of(b))
    assert st["state"] == "failed" and "中斷" in st["error"]
    assert CV.read_status(W.review_dir_of(c))["state"] == "pending"
    assert reviewed == [4]                                                         # 先重跑檢核產生疊圖資料
    assert not (tmp_path / "gone").exists()


def test_poll_keeps_result_until_recorded(tmp_path, monkeypatch):
    # 記結果時資料庫斷線：保留畫圖結果，重連後再記（不能當成中斷重畫）
    from litian.drawing import worker as W
    finished, _ = _cad_queue(monkeypatch, W, [])
    cad, _, rd = _running(W, tmp_path, 0)
    (tmp_path / "w" / "out.txt").write_text('{"sheets": {"1F-0": "done"}, "peak_mb": 900}\n', encoding="utf-8")
    calls = []

    def flaky(conn, fid, gen, state, info):
        calls.append(state)
        if len(calls) == 1:
            raise ConnectionError("db down")
        return True

    monkeypatch.setattr(W.ST, "finish_cad", flaky)
    with pytest.raises(ConnectionError):
        cad.poll(_Conn())
    assert cad.busy() and (tmp_path / "w").exists()
    cad.poll(_Conn())
    assert calls == ["done", "done"] and not cad.busy() and not (tmp_path / "w").exists()


def test_cad_start_disk_error_requeues_without_counting(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    p = tmp_path / "001_F-101.dxf"
    make_fire_dxf(p)
    rd = tmp_path / "001_F-101.dxf.review"
    rd.mkdir()
    finished, _ = _cad_queue(monkeypatch, W, [_job(p) for _ in range(3)], ir={"sheets": []}, review={"floors": []})
    requeued = []
    monkeypatch.setattr(W.ST, "requeue_cad", lambda conn, fid, gen: requeued.append((fid, gen)) or True)
    monkeypatch.setattr(W.tempfile, "mkdtemp", lambda **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    cad = W.CadRunner()
    assert cad.start(_Conn()) is False and requeued == [(7, 3)] and finished == []
    assert CV.read_status(rd)["state"] == "pending"
    assert cad.start(_Conn()) is False and requeued == [(7, 3)]                    # 等一陣子再試，不空轉
    cad.retry_at = 0
    assert cad.start(_Conn()) is False and requeued == [(7, 3), (7, 3)]
    cad.retry_at = 0                                                               # 連續第 3 次：記失敗，不再擋住其他檔
    assert cad.start(_Conn()) is True and [x[2] for x in finished] == ["failed"] and len(requeued) == 2
    assert not cad.busy() and cad.claimed is None


def test_shutdown_requeues_running_render_without_counting(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    requeued = []
    monkeypatch.setattr(W.ST, "requeue_cad", lambda conn, fid, gen: requeued.append((fid, gen)) or True)
    cad, proc, rd = _running(W, tmp_path, None)
    cad.shutdown(_Conn())
    assert proc.killed and not cad.busy() and requeued == [(7, 3)] and not (tmp_path / "w").exists()
    assert CV.read_status(rd)["state"] == "pending"
    cad.shutdown(_Conn())                                                          # 沒在畫：不動
    assert requeued == [(7, 3)]


def test_shutdown_records_finished_render_instead_of_requeue(tmp_path, monkeypatch):
    # 子行程已畫完、worker 還沒記（正在處理別的檔）就遇到部署：照結果記，不重畫
    from litian.drawing import worker as W
    finished, _ = _cad_queue(monkeypatch, W, [])
    requeued = []
    monkeypatch.setattr(W.ST, "requeue_cad", lambda conn, fid, gen: requeued.append(fid) or True)
    cad, proc, rd = _running(W, tmp_path, 0)
    (tmp_path / "w" / "out.txt").write_text('{"sheets": {"1F-0": "done"}}\n', encoding="utf-8")
    CV.write_status(rd, {"state": "done", "sheets": {"1F-0": "done"}})
    cad.shutdown(_Conn())
    assert requeued == [] and [x[2] for x in finished] == ["done"] and not cad.busy()
    assert CV.read_status(rd)["state"] == "done"
    cad, proc, rd = _running(W, tmp_path, 0)                                       # 資料庫連不上：不動（下次啟動收拾）
    cad.shutdown(None)
    assert requeued == [] and len(finished) == 1


def test_shutdown_requeues_claimed_but_not_started(tmp_path, monkeypatch):
    from litian.drawing import worker as W
    requeued = []
    monkeypatch.setattr(W.ST, "requeue_cad", lambda conn, fid, gen: requeued.append((fid, gen)) or True)
    rd = tmp_path / "x.dxf.review"
    rd.mkdir()
    cad = W.CadRunner()
    cad.claimed = _job(tmp_path / "x.dxf")
    cad.shutdown(_Conn())
    assert requeued == [(7, 3)] and cad.claimed is None and CV.read_status(rd)["state"] == "pending"


def test_backend_draws_points_and_dash_dots_by_lineweight():
    import matplotlib
    matplotlib.use("Agg")
    from ezdxf.addons.drawing import config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.properties import BackendProperties
    from ezdxf.math import Vec2
    from matplotlib.figure import Figure
    ax = Figure(dpi=100).add_axes((0, 0, 1, 1))
    be = CV._backend(MatplotlibBackend)(ax, adjust_figure=False)
    be.configure(config.Configuration(lineweight_scaling=72 / 25.4))
    props = BackendProperties(color="#ff000080", lineweight=1.0)
    be.draw_solid_lines([(Vec2(0, 0), Vec2(0, 0)), (Vec2(1, 0), Vec2(3, 0)), (Vec2(5, 0), Vec2(5, 0))], props)
    be.draw_point(Vec2(9, 9), props)
    dots = [ln for ln in ax.lines if ln.get_marker() == "o"]
    assert [len(d.get_xdata()) for d in dots] == [2, 1]                            # 點劃線的點、POINT 都照線寬
    assert all(d.get_markersize() == pytest.approx(72 / 25.4) for d in dots)
    assert len(ax.collections) == 1 and len(ax.collections[0].get_segments()) == 1


def test_tick_isolates_background_errors(tmp_path, monkeypatch):
    import psycopg
    from litian.drawing import worker as W

    class Cad:
        def poll(self, conn): raise ValueError("壞掉的狀態")
        def start(self, conn): raise ValueError("又壞了")
    ran = []
    monkeypatch.setattr(W, "run_once", lambda conn, spool, cad: ran.append(1) or False)
    assert W.tick(_Conn(), tmp_path, Cad()) is False and ran == [1]                # 新上傳照常處理

    class Down(Cad):
        def poll(self, conn): raise psycopg.OperationalError("斷線")
    with pytest.raises(psycopg.OperationalError):                                  # 斷線照樣往外丟（重連）
        W.tick(_Conn(), tmp_path, Down())


def test_model_frame_clips_block_content_outside_frame(tmp_path, monkeypatch):
    # 整棟各層放在同一個圖塊：篩選只看最上層實體，圖塊裡框外的線要靠裁切擋掉，不能全送去畫
    from ezdxf import bbox
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    doc = ezdxf.new("R2018")
    blk = doc.blocks.new("ALL_FLOORS")
    for k in range(40):
        blk.add_line((k, 0), (k, 10))                                              # 本層：0～40
        blk.add_line((5000 + k, 0), (5000 + k, 10))                                # 別層：遠在框外
    doc.modelspace().add_blockref("ALL_FLOORS", (0, 0))
    n = []
    draw_line = MatplotlibBackend.draw_line
    monkeypatch.setattr(MatplotlibBackend, "draw_line", lambda self, *a: n.append(1) or draw_line(self, *a))
    sheet = {"name": "1F-1", "sheet": 0, "bbox": [-5, -5, 45, 15], "meta": {}, "scale": 1.0, "layout": None}
    img, meta, _ = CV.render_sheet(doc, sheet, bbox.Cache(), long_px=400)
    assert 40 <= len(n) < 60 and meta["source"] == "model"



# ---------- 同名底圖重新上傳 ----------

class _RowsConn:
    def __init__(self, rows):
        self.rows, self.requeued = rows, []

    def execute(self, sql, params=None):
        from types import SimpleNamespace
        if sql.lstrip().startswith("SELECT"):
            return SimpleNamespace(fetchall=lambda: self.rows)
        self.requeued.append(params[0])
        return SimpleNamespace(rowcount=1)


def test_requeue_mains_when_newer_same_name_base_processed():
    from litian.drawing import worker as W
    x = lambda **k: {"stats": {"xref": {"bound": [], "missing": [], **k}}}
    rows = [{"id": 1, **x(bound=["Area_1F"], bound_files=["002_Area_1F.dwg"])},        # 綁的是較早的 → 重排
            {"id": 2, **x(bound=["Area_1F"], bound_files=["005_Area_1F.dwg"])},        # 已經是這份 → 不動
            {"id": 3, **x(bound=["AREA_1F"])},                                         # 舊資料只有圖塊名 → 重排
            {"id": 4, **x(missing=["Area_1F.dwg"])},                                   # 缺這個參考 → 重排
            {"id": 5, **x(missing=["002_Area_1F.dwg（DXFStructureError）"])},          # 上次讀不了 → 重排
            {"id": 6, **x(bound=["Area_2F"], bound_files=["003_Area_2F.dwg"])},        # 別的參考 → 不動
            {"id": 7, **x(bound=["Area_1F"], bound_files=["009_Area_1F.dwg"])}]        # 綁的比這份新 → 不動
    conn = _RowsConn(rows)
    assert W.requeue_xref_dependents(conn, {"id": 9, "case_id": 1, "path": "/cases/1/005_Area_1F.dwg"}) == 4
    assert conn.requeued == [1, 3, 4, 5]


def test_bind_xrefs_converts_newest_and_falls_back(tmp_path, monkeypatch):
    # 同名的最新上傳先送轉檔；轉不了改轉較早上傳的，主圖照樣處理
    from litian.drawing import convert_client as CC
    from litian.drawing import worker as W
    for n in ("003_Area_1F.dwg", "002_Area_1F.dwg", "001_main.dwg"):
        (tmp_path / n).write_bytes(b"AC1027")
    calls = []

    def run(args, timeout, what):
        if args[1] == "list":
            return json.dumps([["Area_1F", "Area_1F.dwg", "area_1f"]])
        return json.dumps({"path": args[2], "bound": [], "bound_files": [], "missing": []})

    def convert(spool, jid, src, dst):
        calls.append((jid, src.name))
        if src.name.startswith("003_"):
            raise CC.ConvertError("轉檔失敗")
        dst.write_bytes(b"x")
        return dst
    monkeypatch.setattr(W, "_run", run)
    monkeypatch.setattr(W, "_convert", convert)
    W.bind_xrefs({"id": 7, "path": str(tmp_path / "001_main.dwg")}, tmp_path / "001_main.converted.dxf", tmp_path)
    assert calls == [("f7x0", "003_Area_1F.dwg"), ("f7x0v1", "002_Area_1F.dwg")]
    calls.clear()
    W.bind_xrefs({"id": 7, "path": str(tmp_path / "001_main.dwg")}, tmp_path / "001_main.converted.dxf", tmp_path)
    assert calls == [("f7x0", "003_Area_1F.dwg")]                    # 較早的已轉好：最新的再試一次，失敗就用轉好的
