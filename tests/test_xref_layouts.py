"""配置頁（paper space）出圖、外部參考綁定、依圖層名稱辨識設備（竣工圖實測後新增）。全部用程式畫的圖。"""

import ezdxf
import pytest

from litian.drawing import ir as IR
from litian.drawing import xref as XR
from litian.plan import floor as F
from litian.review import engine as EN
from litian.review import equipment as E

from . import _plans as P


def _frame(doc):
    fr = doc.blocks.new("圖框A3")
    for i in range(45):
        fr.add_line((i * 10, 0), (i * 10, 290))
    return fr


def _layout(doc, name, title_lines, center, height, extra_vp=None):
    lay = doc.layouts.new(name)
    lay.add_blockref("圖框A3", (0, 0))
    for k, t in enumerate(title_lines):
        lay.add_text(t, dxfattribs={"insert": (300, 30 - k * 8), "height": 4})
    lay.add_text("變更設計內容為：設備數量修正。", dxfattribs={"insert": (20, 20), "height": 3})
    lay.add_text("311", dxfattribs={"insert": (400, 10), "height": 3})
    lay.add_viewport(center=(200, 150), size=(380, 260), view_center_point=center, view_height=height)
    if extra_vp:
        lay.add_viewport(center=(40, 40), size=(30, 20), view_center_point=extra_vp, view_height=500)
    return lay


def make_layout_dxf(path):
    """模型空間兩個區域（1F 平面在 (0..30000, 0..15000)，系統圖在 (100000.., 0..)），單位 mm；
    配置頁：FE-311（主圖）、FE-311 (1)（細部）、1F室栓涵蓋（同範圍的檢討頁）、FE-201（昇位圖）。"""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    _frame(doc)
    msp = doc.modelspace()
    for layer, polys in P.layers(0.001).items():
        for pts in polys:
            msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
    for t in P.texts(0.001):
        msp.add_text(t["t"], dxfattribs={"insert": (t["x"], t["y"]), "height": 300})
    doc.blocks.new("室內消防栓").add_circle((0, 0), 150)
    msp.add_blockref("室內消防栓", (15000, 7500))
    msp.add_text("消防栓立管 Ø50", dxfattribs={"insert": (105000, 5000), "height": 300})
    _layout(doc, "FE-311", ["一層室內栓、火警", "設備平面圖"], (15000, 7500), 20000, extra_vp=(500, 500))
    _layout(doc, "FE-311 (1)", ["一層室內栓、火警設備平面圖(1)"], (7000, 7000), 8000)
    _layout(doc, "1F室栓涵蓋", ["一層室內栓涵蓋"], (15000, 7400), 20200)
    _layout(doc, "FE-201", ["室內栓設備昇位圖"], (110000, 5000), 15000)
    doc.saveas(path)


def test_layout_sheets_roles_titles_and_assignment(tmp_path):
    p = tmp_path / "fire.dxf"
    make_layout_dxf(p)
    ir = IR.extract(p)
    by = {s["layout"]: s for s in ir["sheets"]}
    assert set(by) == {"FE-311", "FE-311 (1)", "1F室栓涵蓋", "FE-201"}
    assert by["FE-311"]["role"] == "main" and by["FE-201"]["role"] == "main"
    assert by["FE-311 (1)"]["role"] == "detail" and by["1F室栓涵蓋"]["role"] == "duplicate"
    assert IR.sheet_title(by["FE-311"]["meta"]) == "一層室內栓、火警設備平面圖"     # 兩行合併、略過變更說明與數字
    assert IR.sheet_number(by["FE-311"]["meta"]) == "FE-311"
    assert F.floor_label(IR.sheet_title(by["FE-311"]["meta"])) == "1F"
    w = by["FE-311"]["bbox"]
    half_w = 20000 * 380 / 260 / 2                                            # 視埠範圍＝中心 ± 高度×寬高比
    assert w == pytest.approx([15000 - half_w, 7500 - 10000, 15000 + half_w, 7500 + 10000], abs=0.05)
    hyd = next(i for i in ir["inserts"] if i["name"] == "室內消防栓")
    assert hyd["f"] == by["FE-311"]["idx"]                                    # 內容只分配給主圖，不給細部與檢討頁
    riser = next(t for t in ir["texts"] if "立管" in t["t"])
    assert riser["f"] == by["FE-201"]["idx"]


def test_layout_drawing_reviews_floor_in_millimetres(tmp_path):
    p = tmp_path / "fire.dxf"
    make_layout_dxf(p)
    res = EN.review_dxf(p)
    assert [(fr.floor.label, fr.number) for fr in res.floors] == [("1F", "FE-311")]
    assert res.floors[0].floor.area == pytest.approx(450, rel=0.02)          # 單位 mm（$INSUNITS=4）換算正確
    assert [e.legend for e in res.floors[0].equipment] == ["室內消防栓"]
    assert any(f.rule == "PIPE-32" for f in res.building_findings)           # 昇位圖上的立管標註照樣檢查


def make_host_and_xref(case):
    """主圖（消防設備＋外部參考 Area_1F）與參考檔（牆、門、房名），模擬同一案件上傳的兩個檔。"""
    ref = ezdxf.new("R2018")
    rmsp = ref.modelspace()
    for layer, polys in P.layers(0.01).items():           # 參考檔單位 cm
        for pts in polys:
            rmsp.add_lwpolyline(pts, dxfattribs={"layer": layer})
    door = ref.blocks.new("D1")
    door.add_line((0, 0), (100, 0))
    rmsp.add_blockref("D1", (1500, 600), dxfattribs={"layer": "door"})
    for t in P.texts(0.01):
        rmsp.add_text(t["t"], dxfattribs={"insert": (t["x"], t["y"]), "height": 30})
    ref.saveas(case / "001_Area_1F.converted.dxf")
    (case / "001_Area_1F.dwg").write_bytes(b"AC1027")

    host = ezdxf.new("R2018")
    host.header["$INSUNITS"] = 4
    host.add_xref_def(r"d:\工程\平面圖0407\Area_1F.dwg", "Area_1F")
    host.add_xref_def(r"..\TITLE-A1.dwg", "TITLE-A1")
    msp = host.modelspace()
    msp.add_blockref("Area_1F", (0, 0), dxfattribs={"xscale": 10, "yscale": 10, "layer": "ARCH"})
    host.blocks.new("室內消防栓").add_circle((0, 0), 150)
    msp.add_blockref("室內消防栓", (15000, 7500))
    _frame(host)
    _layout(host, "FE-311", ["一層室內栓、火警設備平面圖"], (15000, 7500), 20000)
    host.saveas(case / "002_main.converted.dxf")
    (case / "002_main.dwg").write_bytes(b"AC1032")


def test_xref_bind_from_case_files_and_expand(tmp_path):
    make_host_and_xref(tmp_path)
    info = XR.bind(tmp_path / "002_main.converted.dxf", tmp_path, tmp_path / "002_main.bound.dxf",
                   original=tmp_path / "002_main.dwg")
    assert info["bound"] == ["Area_1F"] and info["missing"] == ["TITLE-A1.dwg"]
    assert info["bound_files"] == ["001_Area_1F.dwg"]                         # 工作台據此標出「已併入主圖」的檔
    ir = IR.extract(info["path"], expand=info["bound"])
    names = {t["t"] for t in ir["texts"] if t["src"] == "xref:Area_1F"}
    assert {"辦公室", "會議室", "男廁"} <= names                                 # 房名在參考檔裡
    door = next(i for i in ir["inserts"] if i.get("src") == "xref:Area_1F" and i["name"].endswith("D1"))
    assert door["x"] == pytest.approx(15000) and door["f"] == ir["sheets"][0]["idx"]   # 參考檔 cm × 插入比例 10 → mm
    res = EN.review_dxf(info["path"], ir=ir)
    fl = res.floors[0].floor
    assert {r.name for r in fl.rooms} == {"辦公室", "會議室", "男廁"} and len(fl.doors) == 1


def test_xref_key_matches_windows_paths_and_upload_prefix(tmp_path):
    assert XR.ref_key(r"d:\工程\240801\平面圖0407\Area_3F.dwg") == "area_3f"
    assert XR.ref_key(r"..\送審圖框A3_.dwg") == "送審圖框a3_"
    (tmp_path / "003_Area_3F.dwg").write_bytes(b"x")
    (tmp_path / "003_Area_3F.converted.dxf").write_bytes(b"x")
    assert set(XR.case_candidates(tmp_path)) == {"area_3f"}


def test_case_candidates_newest_upload_first(tmp_path):
    # 同名重新上傳：最新的排最前（第 1000 個起序號是 4 位數，照數字比，不照字串比）
    for n in ("002_Area_1F.dwg", "010_Area_1F.dwg", "1000_Area_1F.dxf", "003_Other.dxf", "999_area_1f.DWG"):
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "1000_Area_1F.bound.dxf").write_bytes(b"x")
    got = XR.case_candidates(tmp_path, exclude=tmp_path / "003_Other.dxf")
    assert set(got) == {"area_1f"}
    assert [p.name for p in got["area_1f"]] == ["1000_Area_1F.dxf", "999_area_1f.DWG", "010_Area_1F.dwg", "002_Area_1F.dwg"]
    assert XR.upload_no("1000_Area_1F.dxf") == 1000 and XR.upload_no("Area_1F.dwg") == 0
    assert XR.name_key("012_Area_1F.dwg（DXFStructureError）") == XR.name_key(r"d:\圖\AREA_1F.dwg") == "area_1f"


def _ref_copy(case, stored, room):
    """同名底圖的另一個上傳版本（房名不同，才分得出綁的是哪一份）。"""
    doc = ezdxf.new("R2018")
    for layer, polys in P.layers(0.01).items():
        for pts in polys:
            doc.modelspace().add_lwpolyline(pts, dxfattribs={"layer": layer})
    doc.modelspace().add_text(room, dxfattribs={"insert": (500, 500), "height": 30})
    doc.saveas(case / (stored + ".converted.dxf"))
    (case / (stored + ".dwg")).write_bytes(b"AC1027")


def _bind(case):
    info = XR.bind(case / "002_main.converted.dxf", case, case / "002_main.bound.dxf", original=case / "002_main.dwg")
    texts = {t["t"] for t in IR.extract(info["path"], expand=info["bound"])["texts"] if t.get("src") == "xref:Area_1F"}
    return info, texts


def test_xref_bind_uses_newest_same_name_upload(tmp_path):
    make_host_and_xref(tmp_path)
    _ref_copy(tmp_path, "003_Area_1F", "新版機房")
    info, texts = _bind(tmp_path)
    assert info["bound_files"] == ["003_Area_1F.dwg"] and info["missing"] == ["TITLE-A1.dwg"]
    assert "新版機房" in texts and "辦公室" not in texts


def test_xref_bind_falls_back_to_older_when_newest_unreadable(tmp_path):
    make_host_and_xref(tmp_path)
    (tmp_path / "004_Area_1F.converted.dxf").write_bytes(b"\x00\x01 not a dxf")    # 最新的壞了
    (tmp_path / "004_Area_1F.dwg").write_bytes(b"AC1027")
    (tmp_path / "003_Area_1F.dwg").write_bytes(b"AC1027")                          # 次新的還沒轉檔（也不能用）
    info, texts = _bind(tmp_path)
    assert info["bound_files"] == ["001_Area_1F.dwg"] and info["missing"] == ["TITLE-A1.dwg"]   # 有綁到就不算缺
    assert info["failed"][0].startswith("004_Area_1F.dwg（") and info["failed"][1] == "003_Area_1F.dwg（尚未轉檔）"
    assert "辦公室" in texts
    for p in tmp_path.glob("00[1-3]_Area_1F*"):                                    # 都讀不了
        p.unlink()
    info, _ = _bind(tmp_path)
    assert info["bound_files"] == [] and info["missing"] == ["Area_1F.dwg", "TITLE-A1.dwg"]  # 缺的一律記原始參考名
    assert info["failed"][0].startswith("004_Area_1F.dwg（")


def test_xref_bind_skips_failed_uploads_and_same_name_main_versions(tmp_path):
    # 處理失敗的上傳檔不用；同名主圖的另一版（自己也引用同名參考）不是底圖，退回真正的底圖
    make_host_and_xref(tmp_path)
    _ref_copy(tmp_path, "003_Area_1F", "新版機房")
    info = XR.bind(tmp_path / "002_main.converted.dxf", tmp_path, tmp_path / "002_main.bound.dxf",
                   original=tmp_path / "002_main.dwg", skip=["003_AREA_1F.dwg"])
    assert info["bound_files"] == ["001_Area_1F.dwg"]
    main_v2 = ezdxf.new("R2018")
    main_v2.add_xref_def(r"..\建築\Area_1F.dwg", "Area_1F")                         # 同名主圖：也引用 Area_1F
    main_v2.modelspace().add_blockref("Area_1F", (0, 0))
    main_v2.saveas(tmp_path / "005_Area_1F.converted.dxf")
    (tmp_path / "005_Area_1F.dwg").write_bytes(b"AC1032")
    info, texts = _bind(tmp_path)
    assert info["bound_files"] == ["003_Area_1F.dwg"] and "新版機房" in texts and info["failed"] == []


def test_explode_keeps_only_wanted_layers_inside_windows():
    """竣工圖全部展開要數 GB：只展開牆柱門窗圖層、樓層平面圖範圍附近的實體。"""
    from litian.plan import geometry as G
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "WALL"})
    msp.add_line((0, 10), (100, 10), dxfattribs={"layer": "FP-配線"})
    msp.add_line((5000, 0), (5100, 0), dxfattribs={"layer": "WALL"})            # 系統圖區，離平面圖很遠
    blk = doc.blocks.new("柱")
    blk.add_lwpolyline([(0, 0), (1, 0), (1, 1)])
    msp.add_blockref("柱", (50, 50), dxfattribs={"layer": "COLUMN"})
    msp.add_circle((50, 50), 10, dxfattribs={"layer": "WALL"})
    prims = G.explode(doc, keep=lambda layer: layer in {"WALL", "COLUMN"}, boxes=[[0, 0, 200, 100]], flatten=0.5)
    assert sorted({layer for layer, _ in prims}) == ["COLUMN", "WALL"]
    assert all(max(x for x, _ in pts) < 1000 for _, pts in prims)
    circle = max((pts for layer, pts in prims if len(pts) > 4), key=len)
    assert len(circle) > 8                                                     # 圓依容許誤差轉成折線
    assert len(G.explode(doc)) == 5                                            # 不給條件時全部展開


def test_dictionary_norm_layer_fallback_and_defaults():
    legend = E.load_legend()
    d = E.Dictionary(legend, blocks=[(r"^Dlight\d*$", "避難方向指示燈（單面單向）", {"grade": "B"})])
    assert E.norm("補償式侷限型探測器（1種）") == E.norm("補償式局限型探測器(1種)")
    assert d.match("A$C3A1D3E3B", "定溫式局限型探測器(1種，定址式)") == ("定溫式局限型探測器（1種、定址式）", {})
    assert d.match("A$C2D2A4668", "揚聲器(吸頂式)")[0] == "揚聲器（吸頂式）"
    assert d.match("A$Cxxxx", "ARCH") is None
    found, unknown = E.recognize([{"name": "Dlight3", "x": 0, "y": 0, "layer": "方向指示燈", "attribs": {}},
                                  {"name": "Dlight3", "x": 500, "y": 0, "layer": "x", "attribs": {"等級": "A"}}], 1.0, d)
    assert [e.spec["grade"] for e in found] == ["B", "A"] and not unknown         # 圖塊屬性優先於字典預設
    with pytest.raises(ValueError, match="不在附件三"):
        E.Dictionary(legend, blocks=[("^X$", "不存在的設備")])
    E.Dictionary.default()                                                    # 隨附字典的圖例名稱都存在
