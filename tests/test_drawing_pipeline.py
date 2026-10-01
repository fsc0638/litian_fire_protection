"""圖面管線：轉檔交接協定、轉檔服務（假的 dwg2dxf）、worker 流程、檔名清理。不連資料庫。"""

import importlib.util
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from litian.drawing import cli as CLI
from litian.drawing import convert_client as CC
from litian.drawing import worker as W
from tests.test_drawing_ir import make_dxf

ROOT = Path(__file__).resolve().parents[1]


def load_converter(monkeypatch, base: Path):
    monkeypatch.setenv("CONVERT_DIR", str(base))
    spec = importlib.util.spec_from_file_location("converter", ROOT / "deploy/oracle/libredwg/converter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = str(ROOT / "tools/dwg2dxf")          # 容器內 repair_dxf.py 與 converter.py 同資料夾
    for d in (mod.IN, mod.OUT, mod.WORK):
        Path(d).mkdir(parents=True, exist_ok=True)
    return mod


def test_submit_is_atomic_and_rejects_bad_job(tmp_path):
    src = tmp_path / "a.dwg"
    src.write_bytes(b"DWG")
    CC.submit(tmp_path / "spool", "f12", src)
    assert (tmp_path / "spool/in/f12.dwg").read_bytes() == b"DWG"
    assert not list((tmp_path / "spool/in").glob("*.part"))
    with pytest.raises(ValueError):
        CC.submit(tmp_path / "spool", "../x", src)


def test_wait_success_failure_and_timeout(tmp_path):
    spool = tmp_path
    (spool / "out").mkdir()
    (spool / "out/f1.dxf").write_text("DXF")
    (spool / "out/f1.json").write_text(json.dumps({"ok": True, "seconds": 0.3}))
    dxf, res = CC.wait(spool, "f1", 1, poll_s=0.01)
    assert dxf.read_text() == "DXF" and res["ok"]
    (spool / "out/f2.json").write_text(json.dumps({"ok": False, "error": "轉檔逾時"}))
    with pytest.raises(CC.ConvertError, match="轉檔逾時"):
        CC.wait(spool, "f2", 1, poll_s=0.01)
    assert not (spool / "out/f2.json").exists()                     # 失敗結果取走後清掉
    (spool / "in").mkdir()
    (spool / "in/f3.dwg").write_bytes(b"x")
    with pytest.raises(CC.ConvertError, match="逾時"):
        CC.wait(spool, "f3", 0.05, poll_s=0.01)
    assert not (spool / "in/f3.dwg").exists()                       # 逾時要撤回輸入


def test_converter_converts_and_repairs(tmp_path, monkeypatch):
    conv = load_converter(monkeypatch, tmp_path)
    # 假的 dwg2dxf：輸出一個含「值裡夾換行」的 DXF，修補程式要把它接回去
    broken = "0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nTEXT\n1\n第一行\n第二行\n0\nENDSEC\n0\nEOF\n"
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] == "dwg2dxf":
            Path(cmd[cmd.index("-o") + 1]).write_text(broken, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, b"", b"Warning: x")
        return real_run(cmd, **kw)
    monkeypatch.setattr(conv.subprocess, "run", fake_run)
    src = Path(conv.IN) / "f7.dwg"
    src.write_bytes(b"AC1027")
    res = conv.convert("f7", str(src))
    assert res["ok"] and res["repair"].startswith("merged_lines=1")
    out = (Path(conv.OUT) / "f7.dxf").read_text(encoding="utf-8")
    assert "第一行 第二行" in out and not list(Path(conv.WORK).iterdir())


def test_converter_reports_missing_output_and_size(tmp_path, monkeypatch):
    conv = load_converter(monkeypatch, tmp_path)
    monkeypatch.setattr(conv.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"bad"))
    src = Path(conv.IN) / "f8.dwg"
    src.write_bytes(b"x")
    assert "沒有產生 DXF" in conv.convert("f8", str(src))["error"]
    monkeypatch.setattr(conv, "MAX_BYTES", 0)
    assert "檔案過大" in conv.convert("f8", str(src))["error"]


class FakeConn:
    def transaction(self):
        class T:
            def __enter__(s): return s
            def __exit__(s, *a): return False
        return T()


def test_worker_processes_dwg_through_converter(tmp_path, monkeypatch):
    spool, case = tmp_path / "spool", tmp_path / "cases"
    case.mkdir()
    dwg = case / "001_A1-05.dwg"
    dwg.write_bytes(b"AC1027")
    saved, failed = [], []
    monkeypatch.setattr(W.ST, "claim", lambda conn: {"id": 5, "name": "A1-05.dwg", "kind": "dwg", "path": str(dwg), "attempts": 1})
    monkeypatch.setattr(W.ST, "save_result", lambda conn, fid, ir, stats: saved.append((fid, stats)))
    monkeypatch.setattr(W.ST, "save_failure", lambda conn, fid, err, retry: failed.append((fid, err, retry)))

    def fake_converter():                        # 模擬轉檔服務：看到 in/f5.dwg 就交出 DXF
        for _ in range(200):
            src = spool / "in/f5.dwg"
            if src.exists():
                make_dxf(spool / "out/f5.dxf")
                (spool / "out/f5.json").write_text(json.dumps({"ok": True}))
                src.unlink()
                return
            time.sleep(0.02)
    (spool / "out").mkdir(parents=True)
    t = threading.Thread(target=fake_converter)
    t.start()
    assert W.run_once(FakeConn(), spool) is True
    t.join()
    assert failed == [] and saved[0][0] == 5 and saved[0][1]["sheet_numbers"] == ["A1-05", "A1-06"]
    assert (case / "001_A1-05.converted.dxf").exists() and not list((spool / "out").iterdir())


def test_worker_marks_failure_without_retry_for_bad_dxf(tmp_path, monkeypatch):
    bad = tmp_path / "x.dxf"
    bad.write_text("這不是 DXF")
    failed = []
    monkeypatch.setattr(W.ST, "claim", lambda conn: {"id": 9, "name": "x.dxf", "kind": "dxf", "path": str(bad), "attempts": 1})
    monkeypatch.setattr(W.ST, "save_failure", lambda conn, fid, err, retry: failed.append((fid, err, retry)))
    W.run_once(FakeConn(), tmp_path)
    assert failed and failed[0][0] == 9 and failed[0][2] is False and "抽取失敗" in failed[0][1]


def test_worker_idle_when_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(W.ST, "claim", lambda conn: None)
    assert W.run_once(FakeConn(), tmp_path) is False


def test_safe_name():
    assert CLI.safe_name("../../etc/passwd") == "passwd"
    assert CLI.safe_name("C:\\圖\\A1-05_面積計算表.dwg") == "A1-05_面積計算表.dwg"
    assert CLI.safe_name("a b|c.dwg") == "a_b_c.dwg"
