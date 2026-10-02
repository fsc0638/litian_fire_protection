"""資料庫整合測試（真的 PostgreSQL）：佇列、圖面中介資料存檔、帳號、邀請與 LINE 登入。

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
    c.execute("DROP TABLE IF EXISTS file_review, case_sheet, file_ir, case_file, review_case, login_state, app_invite, "
              "app_session, app_user CASCADE")
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
    # 檢核中當掉也要能救回；檢核結果可覆寫
    conn.execute("UPDATE case_file SET status = 'reviewing', attempts = 1, updated_at = now() - interval '2 hours' WHERE id = %s", (fid,))
    assert ST.recover_stale(conn, 60) == 1
    ST.save_review(conn, fid, "failed", None, "RuntimeError: 檢核失敗", None)
    ST.save_review(conn, fid, "done", {"floors": []}, None, "/x/a.dxf.review")
    row = conn.execute("SELECT status, result, error, svg_dir FROM file_review WHERE file_id = %s", (fid,)).fetchone()
    assert (row["status"], row["result"], row["error"], row["svg_dir"]) == ("done", {"floors": []}, None, "/x/a.dxf.review")


SUB_A, SUB_B = "U" + "a" * 32, "U" + "b" * 32


def test_old_password_schema_migrates_in_place(conn):
    """上線中的舊資料表（有 password_hash NOT NULL）：重跑 ensure_schema 後密碼欄位移除、帳號保留、可綁 LINE。"""
    from litian import auth as AU
    conn.execute("DROP TABLE IF EXISTS login_state, app_invite, app_session, app_user CASCADE")
    conn.execute("CREATE TABLE app_user (id bigserial PRIMARY KEY, username text NOT NULL UNIQUE, password_hash text NOT NULL, "
                 "role text NOT NULL DEFAULT 'reviewer', disabled boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now())")
    conn.execute("INSERT INTO app_user (username, password_hash, role) VALUES ('boss', 'scrypt$x', 'admin')")
    conn.execute("CREATE TABLE app_session (token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES app_user "
                 "ON DELETE CASCADE, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL)")
    conn.execute("INSERT INTO app_session (token_hash, user_id, expires_at) VALUES ('old', 1, now() + interval '1 hour')")
    conn.execute("CREATE TABLE app_invite (id bigserial PRIMARY KEY, token_hash text NOT NULL UNIQUE, username text NOT NULL, "
                 "role text NOT NULL, kind text NOT NULL, created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), "
                 "expires_at timestamptz NOT NULL, used_at timestamptz, revoked_at timestamptz)")   # 早期草稿的結構（沒有發出者編號）
    AU.ensure_schema(conn)
    assert conn.execute("SELECT count(*) AS n FROM app_session").fetchone()["n"] == 0   # 密碼時代的登入全部登出
    AU.ensure_schema(conn)                                               # 可重複執行
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'app_user'").fetchall()}
    assert "password_hash" not in cols and {"line_user_id", "line_name", "last_login_at"} <= cols
    token, inv = AU.create_invite(conn, "boss", "reviewer", "主機命令列")
    assert inv["kind"] == "bind" and inv["role"] == "admin"              # 既有帳號＝重新綁定，角色不變
    _, user = AU.line_login(conn, SUB_A, "老闆", inv["id"])
    assert user["username"] == "boss" and user["role"] == "admin"


def test_invite_is_single_use_and_creates_bound_account(conn):
    from litian import auth as AU
    token, inv = AU.create_invite(conn, "amy", "reviewer", "boss")
    stored = conn.execute("SELECT token_hash FROM app_invite").fetchone()["token_hash"]
    assert stored != token and len(stored) == 64                          # 只存雜湊
    assert AU.invite_info(conn, token)["username"] == "amy" and AU.invite_info(conn, "x" * 43) is None
    with pytest.raises(AU.AuthError, match="not_registered"):
        AU.line_login(conn, SUB_A, "Amy")                                 # 沒有邀請的 LINE 帳號進不來
    sess, user = AU.line_login(conn, SUB_A, "Amy", inv["id"])
    assert user["username"] == "amy" and AU.session_user(conn, sess)["line_name"] == "Amy"
    assert AU.invite_info(conn, token) is None
    with pytest.raises(AU.AuthError, match="invite_invalid"):
        AU.line_login(conn, SUB_B, "Mallory", inv["id"])                  # 同一張邀請不能再用
    sess2, _ = AU.line_login(conn, SUB_A, "Amy 新名字")                      # 之後直接用 LINE 登入
    row = conn.execute("SELECT line_name, last_login_at FROM app_user WHERE username = 'amy'").fetchone()
    assert row["line_name"] == "Amy 新名字" and row["last_login_at"] is not None
    stored = conn.execute("SELECT token_hash FROM app_session LIMIT 1").fetchone()["token_hash"]
    assert len(stored) == 64 and stored not in (sess, sess2)
    AU.logout(conn, sess2)
    assert AU.session_user(conn, sess2) is None and AU.session_user(conn, sess) is not None
    conn.execute("UPDATE app_session SET expires_at = %s", (datetime.now(timezone.utc) - timedelta(seconds=1),))
    assert AU.session_user(conn, sess) is None                            # 過期


def test_rebind_revokes_old_invites_and_logs_out_old_line(conn):
    from litian import auth as AU
    _, inv = AU.create_invite(conn, "amy", "reviewer", "boss")
    old_sess, _ = AU.line_login(conn, SUB_A, "Amy", inv["id"])
    t1, b1 = AU.create_invite(conn, "amy", "admin", "boss")
    t2, b2 = AU.create_invite(conn, "amy", "admin", "boss")              # 再發一次：前一張作廢
    assert b2["kind"] == "bind" and b2["role"] == "reviewer"             # 換綁不改角色
    assert AU.invite_info(conn, t1) is None and AU.invite_info(conn, t2)
    _, user = AU.line_login(conn, SUB_B, "Amy 新手機", b2["id"])
    assert user["username"] == "amy" and AU.session_user(conn, old_sess) is None   # 換綁後舊登入失效
    with pytest.raises(AU.AuthError, match="not_registered"):
        AU.line_login(conn, SUB_A, "Amy")                                 # 舊 LINE 帳號不能再登入


def test_line_account_cannot_take_over_another_account(conn):
    from litian import auth as AU
    _, a = AU.create_invite(conn, "amy", "reviewer", "boss")
    AU.line_login(conn, SUB_A, "Amy", a["id"])
    tb, b = AU.create_invite(conn, "bob", "reviewer", "boss")
    with pytest.raises(AU.AuthError, match="line_taken"):
        AU.line_login(conn, SUB_A, "Amy", b["id"])                        # Amy 的 LINE 不能再綁 bob
    assert AU.invite_info(conn, tb) is not None                           # 失敗整筆還原，bob 的邀請還能用
    assert conn.execute("SELECT count(*) AS n FROM app_user WHERE username = 'bob'").fetchone()["n"] == 0
    tc, c = AU.create_invite(conn, "carol", "reviewer", "boss")
    conn.execute("INSERT INTO app_user (username) VALUES ('carol')")      # 名稱在邀請發出後被占用
    with pytest.raises(AU.AuthError, match="invite_invalid"):
        AU.line_login(conn, SUB_B, "Carol", c["id"])
    conn.execute("UPDATE app_invite SET expires_at = now() - interval '1 second' WHERE id = %s", (b["id"],))
    with pytest.raises(AU.AuthError, match="invite_invalid"):
        AU.line_login(conn, SUB_B, "Bob", b["id"])                        # 過期
    tr, r = AU.create_invite(conn, "dave", "reviewer", "boss")
    assert AU.revoke_invite(conn, r["id"]) and not AU.revoke_invite(conn, r["id"])
    assert AU.invite_info(conn, tr) is None


def test_login_state_is_single_use_and_bound_to_browser(conn, monkeypatch):
    from litian import auth as AU
    AU.save_login_state(conn, "state-1", "browser-1", "nonce-1", "v" * 86, None)
    assert AU.take_login_state(conn, "state-1", "browser-2") is None       # 別的瀏覽器：拿不到，也不會刪掉
    assert AU.take_login_state(conn, "state-1", "browser-1")["nonce"] == "nonce-1"
    assert AU.take_login_state(conn, "state-1", "browser-1") is None       # 只能用一次
    AU.save_login_state(conn, "state-2", "browser-1", "nonce-2", "v" * 86, None)
    assert AU.take_login_state(conn, "state-2", "browser-1") == {"nonce": "nonce-2", "verifier": "v" * 86, "invite_id": None}
    AU.save_login_state(conn, "state-3", "browser-1", "nonce-3", "v" * 86, None)
    conn.execute("UPDATE login_state SET expires_at = now() - interval '1 second'")
    assert AU.take_login_state(conn, "state-3", "browser-1") is None       # 逾時
    assert conn.execute("SELECT count(*) AS n FROM login_state").fetchone()["n"] == 0
    assert conn.execute("SELECT state_hash FROM login_state").fetchall() == []
    monkeypatch.setattr(AU, "MAX_PENDING_LOGINS", 2)
    AU.save_login_state(conn, "s-a", "b", "n", "v" * 86, None)
    AU.save_login_state(conn, "s-b", "b", "n", "v" * 86, None)
    with pytest.raises(AU.AuthError, match="busy"):                         # 全站暫存上限（不用 IP 封鎖）
        AU.save_login_state(conn, "s-c", "b", "n", "v" * 86, None)


def test_disable_role_and_last_admin_guard(conn):
    from litian import auth as AU
    _, a = AU.create_invite(conn, "boss", "admin", "主機命令列")
    sess, _ = AU.line_login(conn, SUB_A, "Boss", a["id"])
    _, b = AU.create_invite(conn, "amy", "reviewer", "boss")
    amy_sess, amy = AU.line_login(conn, SUB_B, "Amy", b["id"])
    boss_id = AU.user_id_of(conn, "boss")
    with pytest.raises(ValueError, match="管理者"):
        AU.update_user(conn, boss_id, disabled=True)                      # 唯一的管理者不能停用
    with pytest.raises(ValueError, match="管理者"):
        AU.update_user(conn, boss_id, role="reviewer")
    AU.update_user(conn, amy["id"], role="admin")
    assert AU.session_user(conn, amy_sess) is None                        # 改角色後要重新登入
    AU.update_user(conn, boss_id, role="reviewer")                        # 還有另一位管理者，可以降
    u = AU.update_user(conn, boss_id, disabled=True)
    assert u["disabled"] and AU.session_user(conn, sess) is None
    with pytest.raises(AU.AuthError, match="disabled"):
        AU.line_login(conn, SUB_A, "Boss")
    with pytest.raises(ValueError, match="停用"):
        AU.create_invite(conn, "boss", "reviewer", "amy")                 # 停用帳號不能發換綁邀請
    with pytest.raises(LookupError):
        AU.update_user(conn, 99999, disabled=True)
    names = {x["username"]: x for x in AU.list_users(conn)}
    assert names["amy"]["line_bound"] and names["amy"]["role"] == "admin" and names["boss"]["disabled"]


def test_invite_modes_do_not_silently_rebind(conn):
    from litian import auth as AU
    _, a = AU.create_invite(conn, "amy", "admin", "boss", mode="new")
    AU.line_login(conn, SUB_A, "Amy", a["id"])
    with pytest.raises(AU.Conflict, match="已存在"):
        AU.create_invite(conn, "amy", "reviewer", "boss", mode="new")      # 開新帳號遇到同名：拒絕，不會變換綁
    with pytest.raises(AU.Conflict, match="沒有帳號"):
        AU.create_invite(conn, "nobody", "reviewer", "boss", mode="rebind")
    _, b = AU.create_invite(conn, "amy", "reviewer", "boss", mode="rebind")
    assert b["kind"] == "bind" and b["role"] == "admin"


def test_removed_admin_cannot_use_invites_he_issued(conn):
    from litian import auth as AU
    _, a = AU.create_invite(conn, "boss", "admin", "主機命令列")
    AU.line_login(conn, SUB_A, "Boss", a["id"])
    _, r = AU.create_invite(conn, "rogue", "admin", "主機命令列")
    AU.line_login(conn, SUB_B, "Rogue", r["id"])
    rogue_id, boss_id = AU.user_id_of(conn, "rogue"), AU.user_id_of(conn, "boss")
    tok, backup = AU.create_invite(conn, "backup", "admin", "rogue", 168, mode="new", created_by_id=rogue_id)
    tok2, amy = AU.create_invite(conn, "amy", "reviewer", "rogue", mode="new", created_by_id=rogue_id)
    AU.update_user(conn, rogue_id, disabled=True)                          # 停用 → 他發的邀請一併作廢
    assert AU.invite_info(conn, tok) is None and AU.invite_info(conn, tok2) is None
    # 就算邀請沒被作廢（例：直接改資料庫），兌換時也會確認發出者仍是啟用中的管理者
    conn.execute("UPDATE app_invite SET revoked_at = NULL WHERE id = %s", (backup["id"],))
    with pytest.raises(AU.AuthError, match="invite_invalid"):
        AU.line_login(conn, "U" + "c" * 32, "Backup", backup["id"])
    # 別的管理者發的邀請不受影響；取消管理者身分也會作廢他發的邀請
    tok3, _ = AU.create_invite(conn, "carol", "reviewer", "boss", mode="new", created_by_id=boss_id)
    AU.update_user(conn, rogue_id, disabled=False)
    assert AU.invite_info(conn, tok3) is not None
    tok4, _ = AU.create_invite(conn, "dave", "reviewer", "rogue", mode="new", created_by_id=rogue_id)
    AU.update_user(conn, rogue_id, role="reviewer")
    assert AU.invite_info(conn, tok4) is None and AU.invite_info(conn, tok3) is not None


def test_concurrent_admin_changes_keep_one_admin(conn):
    """兩位管理者同時互相停用：一定至少留下一位（全域鎖讓人數檢查排隊）。"""
    import psycopg
    from psycopg.rows import dict_row
    from litian import auth as AU
    _, a = AU.create_invite(conn, "adminA", "admin", "主機命令列")
    AU.line_login(conn, SUB_A, "A", a["id"])
    _, b = AU.create_invite(conn, "adminB", "admin", "主機命令列")
    AU.line_login(conn, SUB_B, "B", b["id"])
    ids = [AU.user_id_of(conn, "adminA"), AU.user_id_of(conn, "adminB")]
    for _ in range(5):
        conn.execute("UPDATE app_user SET disabled = false")
        barrier, errors = threading.Barrier(2), []

        def worker(target):
            with psycopg.connect(URL, row_factory=dict_row) as c2:          # 非 autocommit，和正式環境的連線池相同
                barrier.wait()
                try:
                    AU.update_user(c2, target, disabled=True)
                    c2.commit()
                except ValueError as e:
                    errors.append(str(e))
        ts = [threading.Thread(target=worker, args=(t,)) for t in ids]
        [t.start() for t in ts]
        [t.join() for t in ts]
        n = conn.execute("SELECT count(*) AS n FROM app_user WHERE role = 'admin' AND NOT disabled").fetchone()["n"]
        assert n == 1 and len(errors) == 1


def test_concurrent_invites_for_same_name_leave_one_valid(conn):
    import psycopg
    from psycopg.rows import dict_row
    from litian import auth as AU
    for _ in range(5):
        conn.execute("UPDATE app_invite SET revoked_at = now() WHERE revoked_at IS NULL")
        barrier, tokens = threading.Barrier(2), []

        def worker():
            with psycopg.connect(URL, row_factory=dict_row) as c2:
                barrier.wait()
                tokens.append(AU.create_invite(c2, "amy", "reviewer", "boss", mode="new")[0])
                c2.commit()
        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sum(AU.invite_info(conn, t) is not None for t in tokens) == 1
