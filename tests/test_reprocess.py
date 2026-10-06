"""整個重新處理（cli reprocess，轉檔器升級後用）：選案件、只刪轉檔結果、試跑不動、重跑一次結果相同、
與 worker 同時跑時的鎖（等處理中的檔處理完、不讓別人等這裡）、刪不掉就不動，以及主圖比參考檔先重跑時會重新轉參考檔
（不綁舊的轉檔結果）。全部用程式產生的檔。

只在設定 TEST_DATABASE_URL 時執行（資料庫名稱必須以 _test 結尾，測試會清空這些資料表）。
"""

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="沒有設定 TEST_DATABASE_URL（主機上用 09_db_tests.sh 執行）")


@pytest.fixture
def conn():
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import store as ST
    c = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    db = c.execute("SELECT current_database() AS d").fetchone()["d"]
    assert db.endswith("_test"), f"拒絕在非測試資料庫 {db} 上執行"
    c.execute("DROP TABLE IF EXISTS file_review, case_sheet, file_ir, case_file, review_case CASCADE")
    ST.ensure_schema(c)
    yield c
    c.close()


def _case(conn, root: Path, name: str, files: list[tuple]) -> tuple[int, dict]:
    """建案件與上傳檔：files＝[(檔名, 狀態, [轉檔結果副檔名…])]；回傳 (案件ID, 檔名 → (檔案ID, 存檔路徑))。"""
    from litian.drawing import store as ST
    cid = ST.create_case(conn, name, None)
    d = root / str(cid)
    d.mkdir(parents=True)
    out = {}
    for i, (n, status, derived) in enumerate(files, 1):
        p = d / f"{i:03d}_{n}"
        p.write_bytes(b"AC1032")
        fid = ST.add_file(conn, cid, n, 6, "0" * 64, str(p))
        if status != "skipped":
            conn.execute("UPDATE case_file SET status = %s, attempts = 2 WHERE id = %s", (status, fid))
        for s in derived:
            p.with_name(p.stem + s).write_text("舊的轉檔結果", encoding="utf-8")
        out[n] = (fid, p)
    return cid, out


def _rows(conn) -> dict:
    return {r["id"]: (r["status"], r["review_only"], r["attempts"])
            for r in conn.execute("SELECT id, status, review_only, attempts FROM case_file").fetchall()}


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


C, B = ".converted.dxf", ".bound.dxf"


def test_reprocess_case_requeues_and_deletes_derived_only(conn, tmp_path):
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "甲案", [
        ("A.dwg", "done", [C, B]), ("B.dxf", "failed", [B]), ("C.dwg", "queued", [C]), ("D.dwl", "skipped", []),
        ("G.dwg", "done", [C])])
    other, g = _case(conn, tmp_path, "乙案", [("Z.dwg", "done", [C, B])])
    a = f["A.dwg"][0]
    conn.execute("UPDATE case_file SET review_only = true WHERE id = %s", (f["C.dwg"][0],))
    conn.execute("UPDATE case_file SET cad_state = 'done' WHERE id = %s", (a,))
    ST.save_review(conn, a, "done", {"floors": [{"label": "1F"}]}, None, str(f["A.dwg"][1]) + ".review")
    conn.execute("INSERT INTO file_ir (file_id, ir) VALUES (%s, '{\"sheets\": []}')", (a,))
    rv = Path(str(f["A.dwg"][1]) + ".review")
    rv.mkdir()
    (rv / "1F.svg").write_text("<svg/>", encoding="utf-8")
    f["G.dwg"][1].unlink()                                       # 原檔不見了：轉檔結果是僅存的圖
    before, files_before = _rows(conn), _files(tmp_path)

    res = CLI.reprocess(conn, [cid])
    c = res["cases"][cid]
    assert list(res["cases"]) == [cid] and res["missing"] == []
    assert c["queued"] == ["A.dwg", "B.dxf", "C.dwg"] and c["unsupported"] == 1
    assert c["deleted"] == ["001_A.converted.dxf", "001_A.bound.dxf", "002_B.bound.dxf", "003_C.converted.dxf"]
    assert [n for n, _ in c["skipped"]] == ["G.dwg"] and "原檔" in c["skipped"][0][1]
    rows = _rows(conn)
    assert {rows[f[n][0]] for n in ("A.dwg", "B.dxf", "C.dwg")} == {("queued", False, 0)}     # 完整重跑、次數歸零
    for n in ("D.dwl", "G.dwg"):
        assert rows[f[n][0]] == before[f[n][0]]
    assert rows[g["Z.dwg"][0]] == before[g["Z.dwg"][0]]                                    # 別的案件不動
    gone = {f"{cid}/{x}" for x in c["deleted"]}
    assert _files(tmp_path) == files_before - gone                                         # 上傳檔、檢核資料夾都在
    # 中介資料、檢核結果、原圖狀態不清（重跑時才覆蓋）
    assert ST.load_ir(conn, a) == {"sheets": []} and ST.load_review(conn, a) == {"floors": [{"label": "1F"}]}
    assert conn.execute("SELECT cad_state FROM case_file WHERE id = %s", (a,)).fetchone()["cad_state"] == "done"
    assert ST.claim(conn)["id"] == a and ST.requeue_job(conn, a)                            # worker 照 id 順序認領

    # 再執行一次：結果一樣，沒有東西可刪
    snap, files_after = _rows(conn), _files(tmp_path)
    res2 = CLI.reprocess(conn, [cid])
    assert res2["cases"][cid]["queued"] == c["queued"] and res2["cases"][cid]["deleted"] == []
    assert res2["cases"][cid]["skipped"] == c["skipped"]
    assert _rows(conn) == snap and _files(tmp_path) == files_after


def test_reprocess_never_deletes_uploads(conn, tmp_path):
    from litian.drawing import cli as CLI
    cid, f = _case(conn, tmp_path, "同名", [("A.dwg", "done", [])])
    up = f["A.dwg"][1].with_name("001_A.converted.dxf")                 # 名字剛好像轉檔結果的上傳檔
    up.write_bytes(b"0\nEOF\n")
    from litian.drawing import store as ST
    fid = ST.add_file(conn, cid, "A.converted.dxf", 6, "1" * 64, str(up))
    conn.execute("UPDATE case_file SET status = 'done' WHERE id = %s", (fid,))
    res = CLI.reprocess(conn, [cid])
    assert res["cases"][cid]["queued"] == ["A.dwg", "A.converted.dxf"] and res["cases"][cid]["deleted"] == []
    assert up.is_file() and f["A.dwg"][1].is_file()


def test_reprocess_all_dry_run_changes_nothing_then_cli_runs(conn, tmp_path, monkeypatch, capsys):
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    one, f = _case(conn, tmp_path, "甲案", [("A.dwg", "done", [C, B]), ("B.dxf", "processing", [B])])
    two, g = _case(conn, tmp_path, "乙案", [("Z.dxf", "failed", [B]), ("Z.pdf", "skipped", [])])
    monkeypatch.setenv("DATABASE_URL", URL)
    snap = conn.execute("SELECT * FROM case_file ORDER BY id").fetchall()
    files = _files(tmp_path)
    assert CLI.main(["reprocess", "--all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert conn.execute("SELECT * FROM case_file ORDER BY id").fetchall() == snap and _files(tmp_path) == files
    assert "試跑" in out and "會刪除轉檔結果 3 個：001_A.converted.dxf、001_A.bound.dxf、002_B.bound.dxf" in out
    assert "B.dxf：處理中，正式執行時會先等它處理完再一起排入" in out and "不支援的檔 1 個" in out
    assert "合計 2 個案件：會排入 3 個檔、略過 0 個、會刪除轉檔結果 4 個" in out

    ST.mark(conn, f["B.dxf"][0], "done")                                                   # worker 處理完 B
    assert CLI.main(["reprocess", "--all"]) == 0
    out = capsys.readouterr().out
    assert "合計 2 個案件：排入 3 個檔、略過 0 個、刪除轉檔結果 4 個" in out and "進度" in out
    rows = _rows(conn)
    assert {rows[f["A.dwg"][0]], rows[f["B.dxf"][0]], rows[g["Z.dxf"][0]]} == {("queued", False, 0)}
    assert _files(tmp_path) == files - {f"{one}/001_A.converted.dxf", f"{one}/001_A.bound.dxf", f"{one}/002_B.bound.dxf",
                                        f"{two}/001_Z.bound.dxf"}
    assert CLI.main(["reprocess", "--case", str(one), "--case", "999999"]) == 1           # 打錯案件 ID：回報、結束碼 1
    assert "案件 999999：沒有這個案件" in capsys.readouterr().out


def test_reprocess_waits_for_files_in_progress_and_includes_them(conn, tmp_path):
    """處理中的不略過：其餘先鎖住（worker 認領不到），等手上的處理完再一起排入；等待期間才上傳、被認領的新檔也一起等。
    處理中的檔可能已綁到舊的參考檔轉檔結果；只重跑檢核的還用著自己舊的轉檔結果，都要一起重跑。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "等", [("A.dwg", "done", [C, B]), ("Q.dwg", "queued", [C]),
                                         ("X.dwg", "processing", [C]), ("R.dwg", "reviewing", [C, B])])
    conn.execute("UPDATE case_file SET review_only = true WHERE id = %s", (f["R.dwg"][0],))
    ST.save_review(conn, f["A.dwg"][0], "done", {"floors": []}, None, "/x/a.review")
    before, files_before = _rows(conn), _files(tmp_path)
    with pytest.raises(CLI.ReprocessError, match=r"X\.dwg（處理中）.*R\.dwg（檢核中）.*沒有任何更動"):
        CLI.reprocess(conn, [cid], wait_s=0)                                    # 等不到：整個不動
    assert _rows(conn) == before and _files(tmp_path) == files_before

    msgs, res = [], []
    t = threading.Thread(target=lambda: res.append(CLI.reprocess(conn, [cid], wait_s=30, poll_s=0.05, notify=msgs.append)))
    t.start()
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as w:
        for _ in range(100):
            if msgs:
                break
            time.sleep(0.05)
        assert "X.dwg（處理中）" in msgs[0] and "R.dwg（檢核中）" in msgs[0]
        assert ST.claim(w) is None                                              # Q 鎖住了：worker 認領不到
        n = tmp_path / str(cid) / "005_N.dwg"
        n.write_bytes(b"AC1032")
        with w.transaction():                                                   # 等待期間上傳、馬上被 worker 認領
            new = ST.add_file(w, cid, "N.dwg", 6, "n" * 64, str(n))
            assert ST.claim(w)["id"] == new
        assert _files(tmp_path) == files_before | {f"{cid}/005_N.dwg"}           # 還沒刪任何檔
        w.execute("SET lock_timeout = '5s'")
        assert ST.requeue_reviews(w, cid) == 1                                  # 工作台存檢核條件：不必等到重新處理結束
        n.with_name("005_N.converted.dxf").write_text("剛轉好", encoding="utf-8")
        ST.mark(w, f["X.dwg"][0], "done", {"sheets": 0})                        # worker 處理完 X、R
        ST.mark(w, f["R.dwg"][0], "done")
        time.sleep(0.3)
        assert t.is_alive() and _rows(w)[f["A.dwg"][0]][:2] == ("queued", True)    # 還沒排入（只重跑檢核是工作台排的）
        ST.mark(w, new, "done", {"sheets": 0})
        t.join(10)
    assert not t.is_alive()
    c = res[0]["cases"][cid]
    assert c["queued"] == ["A.dwg", "Q.dwg", "X.dwg", "R.dwg", "N.dwg"] and c["skipped"] == []
    assert set(c["deleted"]) == {"001_A.converted.dxf", "001_A.bound.dxf", "002_Q.converted.dxf", "003_X.converted.dxf",
                                 "004_R.converted.dxf", "004_R.bound.dxf", "005_N.converted.dxf"}
    assert set(_rows(conn).values()) == {("queued", False, 0)}
    assert ST.claim(conn)["id"] == f["A.dwg"][0]


def test_reprocess_waits_for_claim_in_flight_then_for_the_file(conn, tmp_path):
    """worker 剛認領、還沒提交：重新處理等它提交；變成處理中就等它處理完，再一起排入（處理中不刪它的檔）。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "認領", [("A.dwg", "queued", [C]), ("B.dwg", "queued", [C])])
    res = []
    with psycopg.connect(URL, row_factory=dict_row) as w:                        # 不自動提交：認領停在交易裡
        assert ST.claim(w)["id"] == f["A.dwg"][0]
        t = threading.Thread(target=lambda: res.append(CLI.reprocess(conn, [cid], wait_s=30, poll_s=0.05)))
        t.start()
        time.sleep(0.5)
        assert t.is_alive()                                                     # 等認領的鎖
        w.commit()
        time.sleep(0.3)
        assert t.is_alive() and f["A.dwg"][1].with_name("001_A.converted.dxf").is_file()   # 等 A 處理完、還沒刪
        # PostgreSQL 等到認領提交後雖不回傳 A，卻鎖住了它的新版本：worker 的下一步（process 的 reset_cad）會等這把鎖，
        # 重新處理要先放掉，不然兩邊互等到逾時
        w.execute("SET lock_timeout = '5s'")
        ST.reset_cad(w, f["A.dwg"][0])
        ST.mark(w, f["A.dwg"][0], "done")
        w.commit()
        t.join(10)
    c = res[0]["cases"][cid]
    assert c["queued"] == ["A.dwg", "B.dwg"] and c["skipped"] == []
    assert c["deleted"] == ["001_A.converted.dxf", "002_B.converted.dxf"]


def test_reprocess_stops_before_deleting_anything_when_a_file_cannot_be_deleted(conn, tmp_path, monkeypatch, capsys):
    """刪檔無法復原：有刪不掉的（資料夾沒有寫入權限、不是一般檔案）就在刪第一個檔之前停下；試跑也列出來。"""
    from litian.drawing import cli as CLI
    one, f = _case(conn, tmp_path, "甲案", [("A.dwg", "done", [C, B]), ("Y.dxf", "done", [])])
    two, g = _case(conn, tmp_path, "乙案", [("Z.dwg", "done", [C])])
    f["Y.dxf"][1].with_name("002_Y.bound.dxf").mkdir()                          # 同名的資料夾
    ro = g["Z.dwg"][1].parent
    access = os.access
    monkeypatch.setattr(CLI.os, "access", lambda p, mode: Path(p) != ro and access(p, mode))   # 乙案資料夾不可寫
    monkeypatch.setenv("DATABASE_URL", URL)
    before, files = _rows(conn), _files(tmp_path)
    assert CLI.main(["reprocess", "--all", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "刪不掉（權限不足或不是一般檔案）1 個：002_Y.bound.dxf" in out and "刪不掉（權限不足或不是一般檔案）1 個：001_Z.converted.dxf" in out
    assert CLI.main(["reprocess", "--all"]) == 1
    err = capsys.readouterr().err
    assert "刪不掉 2 個轉檔結果" in err and f"案件 {two} 001_Z.converted.dxf" in err and "沒有任何更動" in err
    assert _rows(conn) == before and _files(tmp_path) == files


def test_worker_sees_nothing_until_all_derived_files_are_deleted(conn, tmp_path, monkeypatch):
    """一個交易：刪檔期間 worker 認領不到（SKIP LOCKED）、背景畫圖也不會挑到，看到的還是舊狀態。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "鎖", [("A.dwg", "done", [C, B]), ("B.dwg", "done", [C]), ("X.dxf", "queued", [])])
    a = f["A.dwg"][0]
    ST.save_review(conn, a, "done", {"floors": [{"label": "1F"}]}, None, "/x/a.review")
    ST.queue_cad(conn, a)
    seen, unlink = [], Path.unlink

    def spy(self, *args, **kw):
        with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as w:
            seen.append((ST.claim(w), ST.claim_cad(w),
                         w.execute("SELECT count(*) AS n FROM case_file WHERE status = 'queued'").fetchone()["n"]))
        return unlink(self, *args, **kw)
    monkeypatch.setattr(Path, "unlink", spy)
    res = CLI.reprocess(conn, [cid])
    monkeypatch.undo()
    assert seen == [(None, None, 1)] * 3 and res["cases"][cid]["queued"] == ["A.dwg", "B.dwg", "X.dxf"]
    assert ST.claim(conn)["id"] == a


def test_main_before_xref_reconverts_reference_instead_of_binding_old_result(conn, tmp_path, monkeypatch):
    """主圖排在參考檔前面（id 較小）先重跑：參考檔的舊轉檔結果已刪，bind_xrefs 自己送轉檔（參考檔在資料庫還在排隊也照轉），
    綁進來的是新的轉檔結果；參考檔輪到自己時再轉一次，主圖不會被重複排。"""
    import ezdxf
    from litian.drawing import cli as CLI
    from litian.drawing import ir as IR
    from litian.drawing import store as ST
    from litian.drawing import worker as W
    from litian.drawing import xref as XR
    from .test_xref_layouts import make_host_and_xref
    good = tmp_path / "good"                                    # 新版轉檔器會轉出的結果
    good.mkdir()
    make_host_and_xref(good)
    cid = ST.create_case(conn, "外部參考", None)
    d = tmp_path / "cases" / str(cid)
    d.mkdir(parents=True)
    for n in ("001_Area_1F.dwg", "002_main.dwg", "002_main.converted.dxf"):
        shutil.copy(good / n, d / n)
    old = ezdxf.readfile(good / "001_Area_1F.converted.dxf")   # 舊版轉檔結果：參考檔夾帶亂碼文字
    old.modelspace().add_text("殘留亂碼", dxfattribs={"insert": (1500, 700), "height": 30})
    old.saveas(d / "001_Area_1F.converted.dxf")
    shutil.copy(d / "002_main.converted.dxf", d / "002_main.bound.dxf")
    ctl = XR.bind(d / "002_main.converted.dxf", d, tmp_path / "ctl.dxf", original=d / "002_main.dwg")
    assert "殘留亂碼" in {t["t"] for t in IR.extract(ctl["path"], expand=ctl["bound"])["texts"]}   # 不刪就會綁到舊的
    main = ST.add_file(conn, cid, "main.dwg", 6, "m" * 64, str(d / "002_main.dwg"))
    ref = ST.add_file(conn, cid, "Area_1F.dwg", 6, "r" * 64, str(d / "001_Area_1F.dwg"))
    conn.execute("UPDATE case_file SET status = 'done'")
    assert CLI.reprocess(conn, [cid])["cases"][cid]["queued"] == ["main.dwg", "Area_1F.dwg"]
    assert not (d / "001_Area_1F.converted.dxf").exists()

    calls = []

    def convert(spool, jid, dwg, dst):                          # 模擬轉檔服務
        calls.append((jid, dwg.name))
        shutil.copy(good / (dwg.stem + ".converted.dxf"), dst)
        return dst
    monkeypatch.setattr(W, "_convert", convert)
    spool = tmp_path / "spool"
    assert W.run_once(conn, spool) and W.run_once(conn, spool) and not W.run_once(conn, spool)
    assert [n for _, n in calls] == ["002_main.dwg", "001_Area_1F.dwg", "001_Area_1F.dwg"]
    assert calls[1][0].startswith(f"f{main}x") and calls[2][0] == f"f{ref}"
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, stats FROM case_file").fetchall()}
    assert rows[main]["status"] == rows[ref]["status"] == "done"
    assert rows[main]["stats"]["xref"]["bound_files"] == ["001_Area_1F.dwg"]
    texts = {t["t"] for t in ST.load_ir(conn, main)["texts"] if t.get("src") == "xref:Area_1F"}
    assert "辦公室" in texts and "殘留亂碼" not in texts
