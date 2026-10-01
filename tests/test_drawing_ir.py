"""圖面中介資料抽取：用 ezdxf 造一個小 DXF，驗證圖紙切分、圖號、文字歸屬、線段、多邊形。"""

import ezdxf
import pytest

from litian.drawing import ir as IR


def make_dxf(path, *, frames=True, two_sheets=True):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    # 圖框圖塊：40 條以上線段＋8 個以上文字（格線編號）
    frame = doc.blocks.new("TITLE-A1")
    for i in range(45):
        frame.add_line((i * 20, 0), (i * 20, 594))
    for i in range(10):
        frame.add_text(chr(65 + i), dxfattribs={"insert": (i * 80, 600), "height": 5})
    # 圖紙資訊屬性圖塊
    meta = doc.blocks.new("A")
    for tag in ("圖號", "中文圖名", "比例", "單位"):
        meta.add_attdef(tag, (0, 0))
    # 大型內容圖塊（平面圖）：線段、文字都多，但不包住圖號 → 不能被當成圖框
    plan = doc.blocks.new("PLAN")
    for i in range(60):
        plan.add_line((0, i * 2), (100, i * 2))
    for i in range(12):
        plan.add_text(f"房間{i}", dxfattribs={"insert": (5, i * 5), "height": 2})
    # 一般小圖塊（含文字）→ 文字要展開
    tag = doc.blocks.new("ROOMTAG")
    tag.add_text("儲藏室", dxfattribs={"insert": (0, 0), "height": 3})

    sheets = [(0, "A1-05", "面積計算表")] + ([(1000, "A1-06", "各層樓地板面積計算圖")] if two_sheets else [])
    for x0, no, title in sheets:
        if frames:
            msp.add_blockref("TITLE-A1", (x0, 0))
        ref = msp.add_blockref("A", (x0 + 800, 20))
        ref.add_auto_attribs({"圖號": no, "中文圖名": title, "比例": "1:500", "單位": "cm"})
        msp.add_text(f"{no} 內文", dxfattribs={"insert": (x0 + 100, 300), "height": 10, "layer": "DIM-TXT"})
    msp.add_blockref("PLAN", (100, 100))            # 內容圖塊位在第一張圖內
    msp.add_blockref("ROOMTAG", (200, 200))
    # 表格線與封閉多段線（第一張圖內）、舊式 POLYLINE
    msp.add_line((100, 400), (300, 400), dxfattribs={"layer": "TABLE"})
    msp.add_lwpolyline([(400, 400), (500, 400), (500, 500), (400, 500)], close=True, dxfattribs={"layer": "ROOM"})
    msp.add_polyline2d([(600, 100), (700, 100), (700, 200)], close=True)
    msp.add_text("壞字\ud800元", dxfattribs={"insert": (150, 150), "height": 5})
    doc.saveas(path)


def test_sheets_split_by_frames_with_meta(tmp_path):
    p = tmp_path / "a.dxf"
    make_dxf(p)
    ir = IR.extract(p)
    assert [s["meta"]["圖號"] for s in ir["sheets"]] == ["A1-05", "A1-06"]
    assert ir["sheets"][0]["meta"]["單位"] == "cm" and ir["sheets"][0]["frame_block"] == "TITLE-A1"
    texts = {t["t"]: t for t in ir["texts"]}
    assert texts["A1-05 內文"]["f"] == 0 and texts["A1-06 內文"]["f"] == 1
    assert "A" not in texts and "B" not in texts                       # 圖框格線編號不收
    assert texts["儲藏室"]["src"] == "block:ROOMTAG" and texts["儲藏室"]["f"] == 0
    assert texts["房間3"]["src"] == "block:PLAN"                         # 內容圖塊照常展開，沒被當成圖框
    assert "壞字�元" in texts                                        # 無效字元換掉，可寫成 JSON
    assert ir["unassigned_meta"] == []


def test_segments_and_polygons(tmp_path):
    p = tmp_path / "b.dxf"
    make_dxf(p)
    ir = IR.extract(p)
    assert [100.0, 400.0, 300.0, 400.0, "TABLE", 0] in ir["segments"]
    areas = sorted(x["area"] for x in ir["polygons"])
    assert areas == [5000.0, 10000.0]                                    # 舊式 POLYLINE 三角形 5000、矩形 10000
    s = IR.summary(ir)
    assert s["sheets"] == 2 and s["sheet_numbers"] == ["A1-05", "A1-06"] and s["polygons"] == 2


def test_no_frames_single_meta_is_one_sheet(tmp_path):
    p = tmp_path / "c.dxf"
    make_dxf(p, frames=False, two_sheets=False)
    ir = IR.extract(p)
    assert len(ir["sheets"]) == 1 and ir["sheets"][0]["meta"]["圖號"] == "A1-05"
    assert all(t["f"] == 0 for t in ir["texts"])


def test_no_frames_many_meta_leaves_content_unassigned(tmp_path):
    p = tmp_path / "d.dxf"
    make_dxf(p, frames=False, two_sheets=True)
    ir = IR.extract(p)
    assert [s["meta"]["圖號"] for s in ir["sheets"]] == ["A1-05", "A1-06"]
    assert all(t["f"] is None for t in ir["texts"])                     # 不硬塞給其中一張


def test_entity_limit(tmp_path, monkeypatch):
    p = tmp_path / "e.dxf"
    make_dxf(p)
    monkeypatch.setattr(IR, "MAX_ENTITIES", 3)
    with pytest.raises(ValueError, match="超過上限"):
        IR.extract(p)
