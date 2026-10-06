"""整個重新處理（cli reprocess，轉檔器升級後用）：選案件、只移走轉檔結果（不刪，新版轉不了時搬得回來）、試跑不動、
重跑一次結果相同、與 worker 同時跑時的鎖（worker 正在處理就整個不動、認領到一半不會只做一半）、移不走就不動、
上傳原檔不見的參考檔與綁了它的主圖都不動，以及主圖比參考檔先重跑時會重新轉參考檔（不綁舊的轉檔結果）。全部用程式產生的檔。

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


def _kept(root: Path, c: dict) -> set[str]:
    """移走的轉檔結果在 <案件資料夾>/.pre-reprocess/<執行時間>/ 的路徑（含兩層資料夾），與 _files 同格式。"""
    k = Path(c["kept"]).relative_to(root)
    return {k.parent.as_posix(), k.as_posix()} | {(k / n).as_posix() for n in c["moved"]}


def test_reprocess_case_requeues_and_moves_derived_only(conn, tmp_path):
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
    assert c["moved"] == ["001_A.converted.dxf", "001_A.bound.dxf", "002_B.bound.dxf", "003_C.converted.dxf"]
    assert [n for n, _ in c["skipped"]] == ["G.dwg"] and "原檔" in c["skipped"][0][1]
    rows = _rows(conn)
    assert {rows[f[n][0]] for n in ("A.dwg", "B.dxf", "C.dwg")} == {("queued", False, 0)}     # 完整重跑、次數歸零
    for n in ("D.dwl", "G.dwg"):
        assert rows[f[n][0]] == before[f[n][0]]
    assert rows[g["Z.dwg"][0]] == before[g["Z.dwg"][0]]                                    # 別的案件不動
    gone = {f"{cid}/{x}" for x in c["moved"]}
    kept = Path(c["kept"])
    assert kept.parent == tmp_path / str(cid) / ".pre-reprocess"
    assert _files(tmp_path) == files_before - gone | _kept(tmp_path, c)                    # 上傳檔、檢核資料夾都在
    assert {(kept / n).read_text(encoding="utf-8") for n in c["moved"]} == {"舊的轉檔結果"}   # 舊的整份留著
    # 中介資料、檢核結果、原圖狀態不清（重跑時才覆蓋）
    assert ST.load_ir(conn, a) == {"sheets": []} and ST.load_review(conn, a) == {"floors": [{"label": "1F"}]}
    assert conn.execute("SELECT cad_state FROM case_file WHERE id = %s", (a,)).fetchone()["cad_state"] == "done"
    assert ST.claim(conn)["id"] == a and ST.requeue_job(conn, a)                            # worker 照 id 順序認領

    # 再執行一次：結果一樣，沒有東西可移
    snap, files_after = _rows(conn), _files(tmp_path)
    res2 = CLI.reprocess(conn, [cid])
    assert res2["cases"][cid]["queued"] == c["queued"] and res2["cases"][cid]["moved"] == []
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
    assert res["cases"][cid]["queued"] == ["A.dwg", "A.converted.dxf"] and res["cases"][cid]["moved"] == []
    assert up.is_file() and f["A.dwg"][1].is_file()


def test_reprocess_all_dry_run_changes_nothing_then_cli_runs(conn, tmp_path, monkeypatch, capsys):
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    one, f = _case(conn, tmp_path, "甲案", [("A.dwg", "done", [C, B]), ("B.dxf", "processing", [B])])
    two, g = _case(conn, tmp_path, "乙案", [("Z.dxf", "failed", [B]), ("Z.pdf", "skipped", [])])
    monkeypatch.setenv("DATABASE_URL", URL)
    snap = conn.execute("SELECT * FROM case_file ORDER BY id").fetchall()
    files = _files(tmp_path)
    assert CLI.main(["reprocess", "--all", "--dry-run"]) == 1                              # 有檔在處理：結束碼 1
    out = capsys.readouterr().out
    assert conn.execute("SELECT * FROM case_file ORDER BY id").fetchall() == snap and _files(tmp_path) == files
    keep = tmp_path / str(one) / ".pre-reprocess"
    assert "試跑" in out and (f"會移走舊的轉檔結果 2 個（0.0 MB）到 {keep / '<執行時間>'}："
                              "001_A.converted.dxf、001_A.bound.dxf") in out
    assert "B.dxf（處理中）：worker 正在處理，正式執行會停下" in out and "不支援的檔 1 個" in out
    assert "合計 2 個案件：會排入 2 個檔、略過 0 個、會移走舊的轉檔結果 3 個（0.0 MB）" in out
    assert "磁碟剩餘空間要比上面的總大小多" in out

    assert CLI.main(["reprocess", "--all"]) == 1                                           # 正式執行：整個不動
    cap = capsys.readouterr()
    assert cap.out == "" and f"worker 正在處理：案件 {one} B.dxf（處理中），沒有任何更動，等它處理完再執行一次。" in cap.err
    assert conn.execute("SELECT * FROM case_file ORDER BY id").fetchall() == snap and _files(tmp_path) == files

    ST.mark(conn, f["B.dxf"][0], "done")                                                   # worker 處理完 B
    assert CLI.main(["reprocess", "--all"]) == 0
    out = capsys.readouterr().out
    assert "合計 2 個案件：排入 3 個檔、略過 0 個、移走舊的轉檔結果 4 個（0.0 MB）" in out and "進度" in out
    (stamp,) = [p.name for p in keep.iterdir()]
    assert f"已移走舊的轉檔結果 1 個（0.0 MB）到 {tmp_path / str(two) / '.pre-reprocess' / stamp}：001_Z.bound.dxf" in out
    rows = _rows(conn)
    assert {rows[f["A.dwg"][0]], rows[f["B.dxf"][0]], rows[g["Z.dxf"][0]]} == {("queued", False, 0)}
    moved = {one: ["001_A.converted.dxf", "001_A.bound.dxf", "002_B.bound.dxf"], two: ["001_Z.bound.dxf"]}
    assert _files(tmp_path) == files - {f"{cid}/{n}" for cid, ns in moved.items() for n in ns} | {
        x for cid, ns in moved.items()
        for x in _kept(tmp_path, {"kept": tmp_path / str(cid) / ".pre-reprocess" / stamp, "moved": ns})}
    assert CLI.main(["reprocess", "--case", str(one), "--case", "999999"]) == 1           # 打錯案件 ID：回報、結束碼 1
    assert "案件 999999：沒有這個案件" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["processing", "reviewing"])
def test_reprocess_stops_while_worker_is_busy(conn, tmp_path, monkeypatch, capsys, status):
    """worker 正在處理這些案件的檔（處理中、檢核中）：整個不動、說出是哪個檔，鎖隨即放掉（worker、工作台照常）；
    處理完再執行一次就全部排入（處理中的可能已綁到舊的參考檔轉檔結果，只重跑檢核的還用著自己舊的轉檔結果）。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "忙", [("A.dwg", "done", [C, B]), ("Q.dwg", "queued", [C]), ("X.dwg", status, [C, B])])
    other, g = _case(conn, tmp_path, "別案", [("Z.dwg", "done", [C])])
    conn.execute("UPDATE case_file SET review_only = %s WHERE id = %s", (status == "reviewing", f["X.dwg"][0]))
    ST.save_review(conn, f["A.dwg"][0], "done", {"floors": []}, None, "/x/a.review")
    monkeypatch.setenv("DATABASE_URL", URL)
    before, files = _rows(conn), _files(tmp_path)
    assert CLI.main(["reprocess", "--all"]) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err.strip() == (f"worker 正在處理：案件 {cid} X.dwg（{ST.ACTIVE[status]}），"
                                                 "沒有任何更動，等它處理完再執行一次。")
    assert _rows(conn) == before and _files(tmp_path) == files                             # 別的案件也不動
    with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as w:
        w.execute("SET lock_timeout = '2s'")
        assert ST.requeue_reviews(w, cid) == 1                                             # 鎖都放掉了
        assert ST.claim(w)["id"] == f["A.dwg"][0]
        ST.mark(w, f["A.dwg"][0], "done")
        ST.mark(w, f["X.dwg"][0], "done")                                                  # worker 處理完
    c = CLI.reprocess(conn, [cid])["cases"][cid]
    assert c["queued"] == ["A.dwg", "Q.dwg", "X.dwg"] and c["busy"] == [] and c["skipped"] == []
    assert c["moved"] == ["001_A.converted.dxf", "001_A.bound.dxf", "002_Q.converted.dxf", "003_X.converted.dxf",
                          "003_X.bound.dxf"]
    rows = _rows(conn)
    assert {rows[f[n][0]] for n in f} == {("queued", False, 0)} and rows[g["Z.dwg"][0]] == before[g["Z.dwg"][0]]


@pytest.mark.parametrize("meanwhile", ["finished", "uploaded"])
def test_reprocess_stops_when_a_file_finishes_or_appears_right_after_locking(conn, tmp_path, monkeypatch, meanwhile):
    """鎖定之後、檢查之前 worker 剛處理完手上的檔（或剛上傳新檔）：那個檔沒鎖到，不能只排其餘的，一樣整個不動。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "差一點", [("A.dwg", "done", [C]),
                                             ("X.dwg", "processing" if meanwhile == "finished" else "done", [C, B])])
    files, lock = _files(tmp_path), ST.lock_for_reprocess

    def lock_then_worker(c, ids):
        rows = lock(c, ids)
        with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as w:
            if meanwhile == "finished":
                ST.mark(w, f["X.dwg"][0], "done")
            else:
                ST.add_file(w, cid, "N.dwg", 6, "n" * 64, str(tmp_path / str(cid) / "003_N.dwg"))
        return rows
    monkeypatch.setattr(ST, "lock_for_reprocess", lock_then_worker)
    name = "X.dwg" if meanwhile == "finished" else "N.dwg"
    with pytest.raises(CLI.ReprocessError, match=f"worker 正在處理：案件 {cid} {name}（剛有變動），沒有任何更動"):
        CLI.reprocess(conn, [cid])
    rows = _rows(conn)
    assert rows[f["A.dwg"][0]] == rows[f["X.dwg"][0]] == ("done", False, 2) and _files(tmp_path) == files


def test_claim_in_flight_stops_reprocess_or_stays_out_of_it(conn, tmp_path, monkeypatch):
    """worker 認領到一半（還沒提交）：認領的是這批的檔 → 等它提交、查到處理中，整個不動，鎖隨即放掉（PostgreSQL 等到認領
    提交後雖不回傳它，卻鎖住它的新版本；worker 下一步要這把鎖）。認領的是查的時候還看不到的新上傳 → 不在這批、不動它，
    其餘照常排入（這批的檔都鎖著，認領跳過）。不會只做一半。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "認領", [("A.dwg", "queued", [C]), ("B.dwg", "done", [C, B]), ("Q.dwg", "queued", [C])])
    a = f["A.dwg"][0]
    before, files = _rows(conn), _files(tmp_path)
    res, err = [], []

    def run():
        try:
            res.append(CLI.reprocess(conn, [cid]))
        except CLI.ReprocessError as e:
            err.append(str(e))
    with psycopg.connect(URL, row_factory=dict_row) as w:                       # 不自動提交：認領停在交易裡
        assert ST.claim(w)["id"] == a
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        assert t.is_alive()                                                     # 等認領的鎖
        w.commit()
        t.join(10)
        assert not t.is_alive() and res == [] and err == [
            f"worker 正在處理：案件 {cid} A.dwg（處理中），沒有任何更動，等它處理完再執行一次。"]
        assert {i: r for i, r in _rows(conn).items() if i != a} == {i: r for i, r in before.items() if i != a}
        assert _files(tmp_path) == files
        w.execute("SET lock_timeout = '2s'")
        ST.reset_cad(w, a)                                                      # worker 的下一步不卡
        ST.mark(w, a, "done")
        w.commit()

    n = tmp_path / str(cid) / "004_N.dwg"
    n.write_bytes(b"AC1032")
    lock, new = ST.lock_for_reprocess, []
    with psycopg.connect(URL, row_factory=dict_row) as w:

        def lock_then_upload(c, ids):
            rows = lock(c, ids)
            new.append(ST.add_file(w, cid, "N.dwg", 6, "n" * 64, str(n)))     # 上傳、馬上被認領，還沒提交
            assert ST.claim(w)["id"] == new[0]                                  # Q 鎖著：認領跳過
            return rows
        monkeypatch.setattr(ST, "lock_for_reprocess", lock_then_upload)
        c = CLI.reprocess(conn, [cid])["cases"][cid]
        monkeypatch.undo()
        w.commit()
    assert c["queued"] == ["A.dwg", "B.dwg", "Q.dwg"] and c["busy"] == []
    assert c["moved"] == ["001_A.converted.dxf", "002_B.converted.dxf", "002_B.bound.dxf", "003_Q.converted.dxf"]
    rows = _rows(conn)
    assert {rows[f[x][0]] for x in f} == {("queued", False, 0)} and rows[new[0]] == ("processing", False, 1)
    assert n.is_file()


def test_reprocess_stops_when_rows_stay_locked(conn, tmp_path, monkeypatch):
    """別的程序一直鎖著這批的檔：鎖不到就整個不動、說明原因（不是丟出資料庫錯誤）。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    cid, f = _case(conn, tmp_path, "鎖著", [("A.dwg", "done", [C]), ("B.dwg", "done", [C])])
    monkeypatch.setattr(CLI, "LOCK_TIMEOUT", "200ms")
    before, files = _rows(conn), _files(tmp_path)
    with psycopg.connect(URL, row_factory=dict_row) as w:
        w.execute("SELECT id FROM case_file WHERE id = %s FOR UPDATE", (f["B.dwg"][0],))
        with pytest.raises(CLI.ReprocessError, match="其他程序正鎖著這些檔。沒有任何更動"):
            CLI.reprocess(conn, [cid])
    assert _rows(conn) == before and _files(tmp_path) == files


def test_reprocess_stops_before_moving_anything_when_a_file_cannot_be_moved(conn, tmp_path, monkeypatch, capsys):
    """有移不走的（資料夾沒有寫入權限、不是一般檔案）就在移第一個檔之前停下；試跑也列出來。"""
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
    assert "移不走（權限不足或不是一般檔案）1 個：002_Y.bound.dxf" in out and "移不走（權限不足或不是一般檔案）1 個：001_Z.converted.dxf" in out
    assert CLI.main(["reprocess", "--all"]) == 1
    err = capsys.readouterr().err
    assert "移不走 2 個轉檔結果" in err and f"案件 {two} 001_Z.converted.dxf" in err and "沒有任何更動" in err
    assert _rows(conn) == before and _files(tmp_path) == files


def test_worker_sees_nothing_until_all_derived_files_are_moved(conn, tmp_path, monkeypatch):
    """一個交易：移檔期間 worker 認領不到（SKIP LOCKED）、背景畫圖也不會挑到，看到的還是舊狀態。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    cid, f = _case(conn, tmp_path, "鎖", [("A.dwg", "done", [C, B]), ("B.dwg", "done", [C]), ("X.dxf", "queued", [])])
    a = f["A.dwg"][0]
    ST.save_review(conn, a, "done", {"floors": [{"label": "1F"}]}, None, "/x/a.review")
    ST.queue_cad(conn, a)
    seen, rename = [], Path.rename

    def spy(self, *args, **kw):
        with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as w:
            seen.append((ST.claim(w), ST.claim_cad(w),
                         w.execute("SELECT count(*) AS n FROM case_file WHERE status = 'queued'").fetchone()["n"]))
        return rename(self, *args, **kw)
    monkeypatch.setattr(Path, "rename", spy)
    res = CLI.reprocess(conn, [cid])
    monkeypatch.undo()
    assert seen == [(None, None, 1)] * 3 and res["cases"][cid]["queued"] == ["A.dwg", "B.dwg", "X.dxf"]
    assert ST.claim(conn)["id"] == a


def _xref_case(conn, tmp_path: Path) -> tuple[Path, Path, int, int, int]:
    """主圖（id 較小）＋外部參考 Area_1F 的案件，都已處理完；參考檔的舊轉檔結果夾帶亂碼文字（舊版轉檔器）。
    回傳 (新版轉檔器會轉出的結果所在資料夾, 案件資料夾, 案件ID, 主圖ID, 參考檔ID)。"""
    import ezdxf
    from litian.drawing import ir as IR
    from litian.drawing import store as ST
    from litian.drawing import xref as XR
    from .test_xref_layouts import make_host_and_xref
    good = tmp_path / "good"
    good.mkdir()
    make_host_and_xref(good)
    cid = ST.create_case(conn, "外部參考", None)
    d = tmp_path / "cases" / str(cid)
    d.mkdir(parents=True)
    for n in ("001_Area_1F.dwg", "002_main.dwg", "002_main.converted.dxf"):
        shutil.copy(good / n, d / n)
    old = ezdxf.readfile(good / "001_Area_1F.converted.dxf")
    old.modelspace().add_text("殘留亂碼", dxfattribs={"insert": (1500, 700), "height": 30})
    old.saveas(d / "001_Area_1F.converted.dxf")
    shutil.copy(d / "002_main.converted.dxf", d / "002_main.bound.dxf")
    ctl = XR.bind(d / "002_main.converted.dxf", d, tmp_path / "ctl.dxf", original=d / "002_main.dwg")
    assert "殘留亂碼" in {t["t"] for t in IR.extract(ctl["path"], expand=ctl["bound"])["texts"]}   # 不移走就會綁到舊的
    main = ST.add_file(conn, cid, "main.dwg", 6, "m" * 64, str(d / "002_main.dwg"))
    ref = ST.add_file(conn, cid, "Area_1F.dwg", 6, "r" * 64, str(d / "001_Area_1F.dwg"))
    conn.execute("UPDATE case_file SET status = 'done'")
    return good, d, cid, main, ref


def test_main_before_xref_reconverts_reference_instead_of_binding_old_result(conn, tmp_path, monkeypatch):
    """主圖排在參考檔前面（id 較小）先重跑：參考檔的舊轉檔結果已移走，bind_xrefs 自己送轉檔（參考檔在資料庫還在排隊也照轉），
    綁進來的是新的轉檔結果；參考檔輪到自己時再轉一次，主圖不會被重複排。"""
    from litian.drawing import cli as CLI
    from litian.drawing import store as ST
    from litian.drawing import worker as W
    good, d, cid, main, ref = _xref_case(conn, tmp_path)
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


# docs/部署.md「新版轉不了某個 DWG」第 2 步：綁不到它（記「尚未轉檔」）的主圖排回完整重跑
RESTORE_SQL = ("UPDATE case_file SET status = 'queued', review_only = false, attempts = 0 WHERE case_id = %s "
               "AND status IN ('done', 'failed') AND stats->'xref'->'failed' ? %s")


def test_old_results_kept_and_restorable_when_new_converter_fails(conn, tmp_path, monkeypatch):
    """新版轉檔器轉不了某個參考檔（當掉、逾時）：主圖先重跑時綁不到它、參考檔自己也失敗。舊的轉檔結果沒刪、整份留在
    .pre-reprocess/<執行時間>/，worker 不會拿去用；照 docs/部署.md 搬回來、把綁不到的主圖排回重跑，主圖就綁回舊的那份。"""
    from litian.drawing import cli as CLI
    from litian.drawing import convert_client as CC
    from litian.drawing import store as ST
    from litian.drawing import worker as W
    good, d, cid, main, ref = _xref_case(conn, tmp_path)
    old = (d / "001_Area_1F.converted.dxf").read_bytes()
    c = CLI.reprocess(conn, [cid])["cases"][cid]
    kept = Path(c["kept"])
    assert c["moved"] == ["002_main.converted.dxf", "002_main.bound.dxf", "001_Area_1F.converted.dxf"]
    assert (kept / "001_Area_1F.converted.dxf").read_bytes() == old                # 舊的整份留著
    assert not (d / "001_Area_1F.converted.dxf").exists()

    calls = []

    def convert(spool, jid, dwg, dst):                          # 新版轉檔器：參考檔轉不了
        calls.append(dwg.name)
        if dwg.name == "001_Area_1F.dwg":
            raise CC.ConvertError("轉檔失敗：dwg2dxf 結束碼 139")
        shutil.copy(good / (dwg.stem + ".converted.dxf"), dst)
        return dst
    monkeypatch.setattr(W, "_convert", convert)
    spool = tmp_path / "spool"
    assert W.run_once(conn, spool) and W.run_once(conn, spool) and not W.run_once(conn, spool)
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, stats FROM case_file").fetchall()}
    assert rows[main]["status"] == "done" and rows[ref]["status"] == "failed"
    assert rows[main]["stats"]["xref"]["failed"] == ["001_Area_1F.dwg（尚未轉檔）"]   # 主圖綁不到（.pre-reprocess 不算）
    assert "Area_1F.dwg" in rows[main]["stats"]["xref"]["missing"]

    # docs/部署.md：舊的搬回原處，綁不到的主圖排回完整重跑（參考檔自己留在失敗，不再送轉檔）
    (kept / "001_Area_1F.converted.dxf").rename(d / "001_Area_1F.converted.dxf")
    assert conn.execute(RESTORE_SQL, (cid, "001_Area_1F.dwg（尚未轉檔）")).rowcount == 1
    assert W.run_once(conn, spool) and not W.run_once(conn, spool)
    assert calls == ["002_main.dwg", "001_Area_1F.dwg", "001_Area_1F.dwg", "002_main.dwg"]
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, stats FROM case_file").fetchall()}
    assert rows[main]["status"] == "done" and rows[main]["stats"]["xref"]["bound_files"] == ["001_Area_1F.dwg"]
    texts = {t["t"] for t in ST.load_ir(conn, main)["texts"] if t.get("src") == "xref:Area_1F"}
    assert "辦公室" in texts                                                       # 牆、房名回來了（帶著舊的亂碼）


def test_keep_folder_problems_change_nothing_and_never_overwrite(conn, tmp_path, monkeypatch):
    """放舊結果的資料夾建不了：還沒移任何檔就停下。.pre-reprocess 裡已有同名的檔：不蓋掉，停下（已移走的照樣在那裡）。"""
    from litian.drawing import cli as CLI
    cid, f = _case(conn, tmp_path, "備份", [("A.dwg", "done", [C, B])])
    d = f["A.dwg"][1].parent
    before, files = _rows(conn), _files(tmp_path)
    mkdir = Path.mkdir

    def no_keep(self, *a, **kw):
        if CLI.KEEP in self.parts:
            raise PermissionError(13, "Permission denied", str(self))
        return mkdir(self, *a, **kw)
    monkeypatch.setattr(Path, "mkdir", no_keep)
    with pytest.raises(CLI.ReprocessError, match="建不了 .*沒有移走任何檔、沒有排入"):
        CLI.reprocess(conn, [cid])
    monkeypatch.undo()
    assert _rows(conn) == before and _files(tmp_path) == files

    monkeypatch.setattr(CLI.time, "strftime", lambda *a: "20261006-120000")
    keep = d / CLI.KEEP / "20261006-120000"
    keep.mkdir(parents=True)
    (keep / "001_A.bound.dxf").write_text("別次留下的", encoding="utf-8")
    with pytest.raises(CLI.ReprocessError, match="移不走 001_A.bound.dxf：已經有同名的檔。這次全部沒有排入（已移走 1 個"):
        CLI.reprocess(conn, [cid])
    assert (keep / "001_A.bound.dxf").read_text(encoding="utf-8") == "別次留下的"
    assert (keep / "001_A.converted.dxf").is_file() and (d / "001_A.bound.dxf").is_file() and _rows(conn) == before


def test_mains_bound_to_a_reference_whose_upload_is_gone_stay_out(conn, tmp_path, monkeypatch, capsys):
    """參考檔的上傳原檔不見了（只剩轉檔結果）：它不動，上次綁了它的主圖也不動（重跑時案件資料夾裡沒有它、會綁不到）；
    舊資料只記圖塊名的也算。同名的還有別的上傳檔時照常重跑（改綁那份）。"""
    from psycopg.types.json import Jsonb
    from litian.drawing import cli as CLI
    one, f = _case(conn, tmp_path, "缺原檔", [("R.dwg", "done", [C]), ("J.dwg", "done", [C, B]), ("K.dwg", "done", [C, B]),
                                             ("S.dwg", "done", [C]), ("L.dwg", "done", [C, B])])
    two, g = _case(conn, tmp_path, "重新上傳", [("T.dwg", "done", [C]), ("T.dwg", "done", [C]), ("N.dwg", "done", [C, B])])
    xref = {"J.dwg": {"bound_files": ["001_R.dwg"], "bound": ["R"]}, "K.dwg": {"bound": ["R"]},     # K：舊資料
            "L.dwg": {"bound_files": ["004_S.dwg"], "bound": ["S"]}}
    for n, x in xref.items():
        conn.execute("UPDATE case_file SET stats = %s WHERE id = %s", (Jsonb({"xref": x}), f[n][0]))
    conn.execute("UPDATE case_file SET stats = %s WHERE id = %s",
                 (Jsonb({"xref": {"bound_files": ["001_T.dwg"], "bound": ["T"]}}), g["N.dwg"][0]))
    f["R.dwg"][1].unlink()
    (tmp_path / str(two) / "001_T.dwg").unlink()                # 較早上傳的 T 不見了，較新的還在
    monkeypatch.setenv("DATABASE_URL", URL)
    before, files = _rows(conn), _files(tmp_path)
    assert CLI.main(["reprocess", "--all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "略過 J.dwg：上次綁進來的參考檔 001_R.dwg 找不到上傳的原檔，重跑會綁不到，不動" in out
    assert "略過 K.dwg：上次綁進來的參考檔 R 找不到上傳的原檔，重跑會綁不到，不動" in out
    assert _rows(conn) == before and _files(tmp_path) == files

    res = CLI.reprocess(conn, None)["cases"]
    assert res[one]["queued"] == ["S.dwg", "L.dwg"] and [n for n, _ in res[one]["skipped"]] == ["R.dwg", "J.dwg", "K.dwg"]
    assert res[two]["queued"] == ["T.dwg", "N.dwg"] and [n for n, _ in res[two]["skipped"]] == ["T.dwg"]
    rows = _rows(conn)
    assert all(rows[f[n][0]] == before[f[n][0]] for n in ("R.dwg", "J.dwg", "K.dwg"))   # 狀態、轉檔結果都不動
    assert {f"{one}/{x}" for x in ("001_R" + C, "002_J" + C, "002_J" + B, "003_K" + C, "003_K" + B)} <= _files(tmp_path)
    assert rows[f["L.dwg"][0]] == rows[g["N.dwg"][0]] == ("queued", False, 0)
