"""資料庫整合測試（真的 PostgreSQL）：佇列、圖面中介資料存檔、帳號與登入。

只在設定 TEST_DATABASE_URL 時執行，而且資料庫名稱必須以 _test 結尾（測試會清空這些資料表）。
主機上執行：bash /opt/litian/repo/deploy/oracle/09_db_tests.sh
"""

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="沒有設定 TEST_DATABASE_URL（主機上用 09_db_tests.sh 執行）")


@pytest.fixture
def conn():
    import psycopg
    from psycopg.rows import dict_row
    from litian import auth as AU
    from litian.drawing import store as ST
    c = psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    db = c.execute("SELECT current_database() AS d").fetchone()["d"]
    assert db.endswith("_test"), f"拒絕在非測試資料庫 {db} 上執行"
    c.execute("DROP TABLE IF EXISTS file_review, case_sheet, file_ir, case_file, review_case, app_session, app_user CASCADE")
    ST.ensure_schema(c)
    AU.ensure_schema(c)
    yield c
    c.close()


def test_queue_claim_is_exclusive_and_ordered(conn):
    import psycopg
    from psycopg.rows import dict_row
    from litian.drawing import store as ST
    cid = ST.create_case(conn, "測試案件", "tester")
    ids = [ST.add_file(conn, cid, f"A{i}.dwg", 1, "0" * 64, f"/x/{i}") for i in range(6)]
    skipped = ST.add_file(conn, cid, "A.dwl", 1, "0" * 64, "/x/dwl")
    assert conn.execute("SELECT status FROM case_file WHERE id = %s", (skipped,)).fetchone()["status"] == "skipped"
    got, lock = [], threading.Lock()

    def worker():
        with psycopg.connect(URL, row_factory=dict_row, autocommit=True) as c2:
            while (job := ST.claim(c2)):
                with lock:
                    got.append(job["id"])
    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == ids and len(got) == len(set(got))            # 三個處理程序不會搶到同一檔
    assert ST.claim(conn) is None


def test_save_result_failure_and_recover(conn):
    from litian.drawing import store as ST
    cid = ST.create_case(conn, "案", None)
    fid = ST.add_file(conn, cid, "A1-05.dxf", 10, "a" * 64, "/x/a.dxf")
    job = ST.claim(conn)
    ir = {"sheets": [{"idx": 0, "bbox": [0, 0, 1, 1], "meta": {"圖號": "A1-05", "中文圖名": "面積計算表", "比例": "1:500", "單位": "cm"}}],
          "texts": [{"t": "總樓地板面積", "f": 0}]}
    stats = {"sheets": 1, "texts": 1, "sheet_numbers": ["A1-05"]}
    with conn.transaction():
        ST.save_result(conn, job["id"], ir, stats)
    row = conn.execute("SELECT status, stats FROM case_file WHERE id = %s", (fid,)).fetchone()
    assert row["status"] == "done" and row["stats"]["sheet_numbers"] == ["A1-05"]
    s = conn.execute("SELECT number, title, scale, unit, bbox FROM case_sheet WHERE file_id = %s", (fid,)).fetchone()
    assert (s["number"], s["title"], s["scale"], s["unit"], s["bbox"]) == ("A1-05", "面積計算表", "1:500", "cm", [0, 0, 1, 1])
    n = conn.execute("SELECT count(*) AS n FROM file_ir i, jsonb_array_elements(i.ir->'texts') t "
                     "WHERE i.file_id = %s AND t->>'t' = '總樓地板面積'", (fid,)).fetchone()["n"]
    assert n == 1
    # 失敗：可重試→回到排隊；處理中太久→退回排隊，次數用完→失敗
    f2 = ST.add_file(conn, cid, "b.dwg", 1, "b" * 64, "/x/b")
    ST.claim(conn)
    ST.save_failure(conn, f2, "ConvertError: 逾時", retry=True)
    assert conn.execute("SELECT status FROM case_file WHERE id = %s", (f2,)).fetchone()["status"] == "queued"
    ST.claim(conn)
    conn.execute("UPDATE case_file SET updated_at = now() - interval '2 hours' WHERE id = %s", (f2,))
    assert ST.recover_stale(conn, 60) == 1
    assert conn.execute("SELECT status FROM case_file WHERE id = %s", (f2,)).fetchone()["status"] == "queued"
    conn.execute("UPDATE case_file SET status = 'processing', attempts = %s, updated_at = now() - interval '2 hours' WHERE id = %s",
                 (ST.MAX_ATTEMPTS, f2))
    ST.recover_stale(conn, 60)
    assert conn.execute("SELECT status FROM case_file WHERE id = %s", (f2,)).fetchone()["status"] == "failed"


def test_auth_login_session_logout_password_disable(conn):
    from litian import auth as AU
    AU.create_user(conn, "amy", "pw-1234567890")
    with pytest.raises(Exception):
        AU.create_user(conn, "amy", "pw-1234567890")                     # 重複帳號
    assert AU.login(conn, "amy", "wrong-password") is None
    assert AU.login(conn, "nobody", "pw-1234567890") is None
    token, user = AU.login(conn, "amy", "pw-1234567890")
    assert user["username"] == "amy" and AU.session_user(conn, token)["username"] == "amy"
    stored = conn.execute("SELECT token_hash FROM app_session").fetchone()["token_hash"]
    assert stored != token and len(stored) == 64                       # 資料庫只存雜湊
    AU.logout(conn, token)
    assert AU.session_user(conn, token) is None
    token2, _ = AU.login(conn, "amy", "pw-1234567890")
    assert AU.set_password(conn, "amy", "new-pw-0987654321")
    assert AU.session_user(conn, token2) is None                       # 改密碼後原登入失效
    assert AU.login(conn, "amy", "pw-1234567890") is None and AU.login(conn, "amy", "new-pw-0987654321")
    token3, _ = AU.login(conn, "amy", "new-pw-0987654321")
    conn.execute("UPDATE app_session SET expires_at = %s", (datetime.now(timezone.utc) - timedelta(seconds=1),))
    assert AU.session_user(conn, token3) is None                       # 過期
    assert AU.disable_user(conn, "amy")
    assert AU.login(conn, "amy", "new-pw-0987654321") is None           # 停用
