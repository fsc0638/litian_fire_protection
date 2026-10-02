"""管線檢查：管徑標註解析與條文下限（第 32、56、181 條）。"""

import pytest

from litian.review import checks as K
from litian.review import equipment as E
from litian.review import piping as PIPE


@pytest.mark.parametrize("text,size", [
    ("消防栓立管 Ø50", 50), ("連結送水管立管 100A", 100), ("SP 立管 4\"", 100), ("2-1/2\" 支管", 65),
    ("DN65", 65), ("末端查驗閥 25mm", 25), ("PIT:180cm", None), ("Ø37 管", None),
])
def test_parse_size(text, size):
    assert PIPE.parse_size(text) == size


def ir(*texts):
    meta = {"圖號": "F-501", "中文圖名": "消防系統昇位圖"}
    return {"sheets": [{"idx": 0, "meta": meta}], "texts": [{"t": t, "f": 0} for t in texts]}


def hyd(cls=None):
    attrs = {"種類": f"第{cls}種"} if cls else {}
    return E.Equipment("", "室內消防栓", "室內消防栓", E.kinds_of("室內消防栓"), 0, 0, "F", E.specs("室內消防栓", attrs))


def rules(findings):
    return [(f.rule, f.severity) for f in findings]


def test_hydrant_riser_depends_on_hydrant_class():
    f, notes = PIPE.check_texts(ir("消防栓立管 Ø50", "配管 CNS 6445"), [hyd()], K.Context())
    assert rules(f) == [("PIPE-32", K.YELLOW)] and "種類" in f[0].missing[0] and f[0].floor == "F-501 消防系統昇位圖"
    f, _ = PIPE.check_texts(ir("消防栓立管 Ø50", "配管 CNS 6445"), [hyd("一")], K.Context())
    assert rules(f) == [("PIPE-32", K.RED)] and f[0].law == ["D0120029/32/1/1/4"]
    assert PIPE.check_texts(ir("消防栓立管 Ø50", "配管 CNS 6445"), [hyd("二")], K.Context())[0] == []
    assert "水力計算" in notes[0].text


def test_standpipe_riser_and_end_valve_minimums():
    f, _ = PIPE.check_texts(ir("連結送水管立管 80A", "消防栓兼連結送水立管 Ø65", "末端查驗閥 20mm", "CNS 4626"), [], K.Context())
    assert rules(f) == [("PIPE-181", K.RED), ("PIPE-181", K.RED), ("PIPE-56", K.RED)]
    assert f[1].law == ["D0120029/181/1/1", "D0120029/32/1/1/3"]
    assert PIPE.check_texts(ir("連結送水管立管 100A", "CNS 6445"), [], K.Context())[0] == []


def test_material_note_required_only_with_pipe_labels():
    f, _ = PIPE.check_texts(ir("撒水支管 Ø32"), [], K.Context())
    assert rules(f) == [("PIPE-32", K.YELLOW)] and "材質" in f[0].missing[0]
    assert PIPE.check_texts(ir("1F 樓板 150mm 厚", "PIT:180cm"), [], K.Context()) == ([], [])     # 建築圖的尺寸不算管徑


def test_end_test_valve_per_floor():
    from types import SimpleNamespace as NS
    fl = NS(label="2F")
    spk = E.Equipment("", "密閉式撒水頭（向下型）", "密閉式撒水頭（向下型）", E.kinds_of("密閉式撒水頭（向下型）"), 0, 0, "F", {})
    valve = E.Equipment("", "末端查驗閥", "末端查驗閥", E.kinds_of("末端查驗閥"), 0, 0, "F", {})
    f, _ = PIPE.end_test_valve(fl, [spk], K.Context())
    assert rules(f) == [("PIPE-56", K.RED)] and f[0].law == ["D0120029/56/1/2"]
    f, notes = PIPE.end_test_valve(fl, [spk, valve], K.Context())
    assert f == [] and "最遠支管末端" in notes[0].text
    assert PIPE.end_test_valve(fl, [valve], K.Context()) == ([], [])
