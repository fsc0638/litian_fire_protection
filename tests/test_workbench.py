"""審核工作台：登入、權限、上傳（本機，不連資料庫；資料庫的實際 SQL 由 test_db_integration.py 在主機上驗）。"""

from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from litian import api
from litian import auth as AU

USER = {"id": 1, "username": "amy", "role": "reviewer"}


def test_password_hash_roundtrip_and_salt():
    h1, h2 = AU.hash_password("correct horse 1"), AU.hash_password("correct horse 1")
    assert h1 != h2 and h1.startswith("scrypt$")                      # 每次鹽不同
    assert AU.verify_password("correct horse 1", h1) and not AU.verify_password("wrong", h1)
    assert not AU.verify_password("x", "garbage") and not AU.verify_password("x", "md5$1$2$3$4$5")


def test_password_policy():
    with pytest.raises(ValueError, match="至少"):
        AU.check_new_password("short")


@pytest.fixture
def client(monkeypatch, tmp_path):
    @contextmanager
    def conn():
        yield NS()
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    monkeypatch.setattr(api, "CASES_DIR", tmp_path)
    api._login_fails.clear()
    state = {"files": []}
    monkeypatch.setattr(api.AU, "session_user", lambda c, token: USER if token == "good-token" else None)
    monkeypatch.setattr(api.AU, "login", lambda c, u, p: ("good-token", USER) if (u, p) == ("amy", "pw-1234567890") else None)
    monkeypatch.setattr(api.AU, "logout", lambda c, token: state.setdefault("logout", token))
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"id": a[0], "name": "案", "created_by": "amy", "created_at": "t"}
                        if "FROM review_case" in sql else {"n": 0})
    monkeypatch.setattr(api.DS, "add_file", lambda c, cid, name, size, sha, path: state["files"].append((cid, name, size, sha, path)) or 7)
    c = TestClient(api.app)
    c.state = state
    return c


def test_requires_login(client):
    for path in ["/api/cases", "/api/auth/me", "/api/cases/1", "/api/cases/1/sheets/1/texts"]:
        assert client.get(path).status_code == 401
    assert client.post("/api/cases", json={"name": "x"}).status_code == 401
    assert client.get("/workbench").status_code == 200                 # 網頁本身可開，資料要登入


def test_login_sets_secure_cookie_and_logout(client):
    r = client.post("/api/auth/login", json={"username": "amy", "password": "pw-1234567890"})
    assert r.status_code == 200 and r.json()["user"]["username"] == "amy"
    sc = r.headers["set-cookie"].lower()
    assert "fr_session=good-token" in sc and "httponly" in sc and "secure" in sc and "samesite=lax" in sc
    client.cookies.set("fr_session", "good-token")
    assert client.get("/api/auth/me").json()["username"] == "amy"
    client.post("/api/auth/logout")
    assert client.state["logout"] == "good-token"


def test_login_lockout_after_failures(client):
    h = {"X-Forwarded-For": "198.51.100.3"}
    codes = [client.post("/api/auth/login", json={"username": "amy", "password": "bad"}, headers=h).status_code
             for _ in range(api.LOGIN_FAIL_MAX)]
    assert codes == [401] * api.LOGIN_FAIL_MAX
    r = client.post("/api/auth/login", json={"username": "amy", "password": "pw-1234567890"}, headers=h)
    assert r.status_code == 429                                         # 鎖定後連對的密碼也擋


def test_upload_streams_to_disk_with_hash(client, tmp_path):
    client.cookies.set("fr_session", "good-token")
    files = [("files", ("../../A1-05 面積計算表.dwg", b"AC1027" + b"x" * 100, "application/octet-stream")),
             ("files", ("A1-05.dwl", b"lock", "application/octet-stream"))]
    r = client.post("/api/cases/3/files", files=files)
    assert r.status_code == 200 and [f["name"] for f in r.json()["files"]] == ["../../A1-05 面積計算表.dwg", "A1-05.dwl"]
    (cid, name, size, sha, path), _ = client.state["files"]
    assert cid == 3 and size == 106 and len(sha) == 64
    assert path.startswith(str(tmp_path / "3")) and path.endswith("001_A1-05_面積計算表.dwg")   # 路徑穿越被清掉
    assert (tmp_path / "3" / "001_A1-05_面積計算表.dwg").read_bytes().startswith(b"AC1027")


def test_upload_rejects_oversize_and_removes_partial(client, tmp_path, monkeypatch):
    client.cookies.set("fr_session", "good-token")
    monkeypatch.setattr(api, "UPLOAD_MAX", 10)
    r = client.post("/api/cases/4/files", files=[("files", ("big.dwg", b"0123456789AB", "application/octet-stream"))])
    assert r.status_code == 413 and "超過單檔上限" in r.json()["detail"]
    assert not list((tmp_path / "4").iterdir())


def test_sheet_texts_sorted_top_to_bottom(client, monkeypatch):
    client.cookies.set("fr_session", "good-token")
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"file_id": 1, "idx": 0, "number": "A1-05", "title": "面積計算表"})
    monkeypatch.setattr(api, "_all", lambda sql, *a: [{"t": "下", "x": 0, "y": 1, "layer": "0"},
                                                       {"t": "右上", "x": 9, "y": 5, "layer": "0"},
                                                       {"t": "左上", "x": 1, "y": 5, "layer": "0"}])
    r = client.get("/api/cases/1/sheets/2/texts")
    assert [t["t"] for t in r.json()["texts"]] == ["左上", "右上", "下"]
