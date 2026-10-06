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


def _deliver(spool: Path, job: str, text: str, delay: float = 0.1) -> threading.Thread:
    """模擬轉檔服務：看到這次的輸入 in/<job>.dwg 後才交件（DXF 先、結果 json 最後，都用改名）。"""
    def run():
        for _ in range(500):
            src = spool / "in" / f"{job}.dwg"
            if src.exists():
                time.sleep(delay)
                (spool / "out" / f"{job}.dxf.part").write_text(text, encoding="utf-8")
                (spool / "out" / f"{job}.dxf.part").replace(spool / "out" / f"{job}.dxf")
                (spool / "out" / f"{job}.json.part").write_text(json.dumps({"ok": True}))
                (spool / "out" / f"{job}.json.part").replace(spool / "out" / f"{job}.json")
                src.unlink()
                return
            time.sleep(0.01)
    t = threading.Thread(target=run)
    t.start()
    return t


def test_convert_ignores_stale_result_of_same_job(tmp_path):
    """工作代號固定（f<檔案id>）：上次逾時、worker 重啟後才交出、沒人取走的舊結果（例：轉檔器升級前的）不可以被這次拿去用。"""
    spool = tmp_path / "spool"
    (spool / "out").mkdir(parents=True)
    (spool / "out/f5.dxf").write_text("舊轉檔器的結果", encoding="utf-8")
    (spool / "out/f5.json").write_text(json.dumps({"ok": True}))
    src = tmp_path / "001_A.dwg"
    src.write_bytes(b"AC1027")
    t = _deliver(spool, "f5", "新的結果")
    dst = W._convert(spool, "f5", src, tmp_path / "001_A.converted.dxf")
    t.join()
    assert dst.read_text(encoding="utf-8") == "新的結果" and not list((spool / "out").iterdir())


def test_wait_keeps_waiting_when_result_has_no_dxf(tmp_path):
    """舊工作交件到一半（DXF 已寫、結果還沒寫）時被 submit 清掉 DXF：只剩結果的那份不收，等這次送出的。"""
    spool = tmp_path
    (spool / "out").mkdir()
    (spool / "in").mkdir()
    (spool / "in/f8.dwg").write_bytes(b"x")
    (spool / "out/f8.json").write_text(json.dumps({"ok": True}))
    t = _deliver(spool, "f8", "DXF")
    dxf, res = CC.wait(spool, "f8", 5, poll_s=0.01)
    got = dxf.read_text(encoding="utf-8") if dxf.exists() else None        # 收下的當下 DXF 就要在
    t.join()
    assert res["ok"] and got == "DXF"


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

    def execute(self, *a, **k):                    # 查外部參考相依檔等：沒有資料
        from types import SimpleNamespace
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None, rowcount=0)


def test_worker_processes_dwg_through_converter(tmp_path, monkeypatch):
    spool, case = tmp_path / "spool", tmp_path / "cases"
    case.mkdir()
    dwg = case / "001_A1-05.dwg"
    dwg.write_bytes(b"AC1027")
    saved, failed = [], []
    monkeypatch.setattr(W.ST, "claim", lambda conn: {"id": 5, "case_id": 1, "name": "A1-05.dwg", "kind": "dwg", "path": str(dwg), "attempts": 1})
    monkeypatch.setattr(W.ST, "save_result", lambda conn, fid, ir, stats, status="done": saved.append((fid, stats)))
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


def test_repair_streams_and_rejoins_split_unicode_escapes(tmp_path):
    """長文字每 250 字切段時切在 Unicode 跳脫碼（反斜線 U+XXXX）中間 → 殘段移到下一段；APPID 壞名稱照舊修補。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("repair_dxf", "tools/dwg2dxf/repair_dxf.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    bs = "\\"
    src = tmp_path / "in.dxf"
    lines = ["0", "SECTION", "5", "1A", "100", "AcDbRegAppTableRecord", "2", "BAD\x01NAME",
             "0", "MTEXT", "3", f"{bs}U+5357{bs}U+57", "3", f"12{bs}U+5340", "1", f"{bs}U+64F4{bs}U+",
             "0", "EOF", "", ""]
    src.write_bytes("\r\n".join(lines).encode("utf-8"))
    stats = R.repair(str(src), str(tmp_path / "out.dxf"))
    out = (tmp_path / "out.dxf").read_bytes().decode("utf-8").split("\r\n")
    assert stats == {"merged": 0, "fixed": 1, "escapes": 2}
    assert out[out.index("AcDbRegAppTableRecord") + 2] == "APP_1A"
    assert out[out.index("MTEXT") + 2] == f"{bs}U+5357" and out[out.index("MTEXT") + 4] == f"{bs}U+5712{bs}U+5340"
    assert out[out.index("MTEXT") + 6] == f"{bs}U+64F4"            # 最後一段殘缺的 \U+ 丟掉
    assert out[-3:] == ["0", "EOF", ""]
