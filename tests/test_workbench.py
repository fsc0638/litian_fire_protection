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


def test_reviews_require_login_and_attach_svg_urls_and_laws(client, monkeypatch):
    assert client.get("/api/cases/3/reviews").status_code == 401
    assert client.get("/api/cases/3/files/7/review/1F.svg").status_code == 401
    client.cookies.set("fr_session", "good-token")
    result = {"floors": [{"label": "1F", "findings": [{"law": ["D0120029/34/1/1/1"]}], "notes": [{"law": ["D0120029/49/1/1"]}]}],
              "warnings": []}

    def fake_all(sql, *a):
        if "FROM file_review" in sql:
            return [{"file_id": 7, "name": "F-101.dxf", "status": "done", "error": None, "result": result, "svg_dir": None, "created_at": "t"}]
        assert "law_node" in sql and a[0] == ["D0120029/34/1/1/1", "D0120029/49/1/1"]
        return [{"node_id": "D0120029/34/1/1/1", "citation": "設置標準第34條第1項第1款第1目", "text": "各層任一點…"}]
    monkeypatch.setattr(api, "_all", fake_all)
    monkeypatch.setattr(api.DS, "get_context", lambda c, cid: {"occupancy": "丁-2"})
    monkeypatch.setattr(api.DS, "decisions", lambda c, cid: {"7": {"abc": {"decision": "accept"}}})
    d = client.get("/api/cases/3/reviews").json()
    assert d["context"] == {"occupancy": "丁-2"} and d["decisions"]["7"]["abc"]["decision"] == "accept"
    assert "svg_dir" not in d["reviews"][0]
    assert d["reviews"][0]["result"]["floors"][0]["svg"] == "/api/cases/3/files/7/review/1F.svg"
    assert d["laws"]["D0120029/34/1/1/1"]["citation"].startswith("設置標準第34條")


def test_review_svg_served_only_from_cases_dir(client, monkeypatch, tmp_path):
    client.cookies.set("fr_session", "good-token")
    rev = tmp_path / "3" / "001_F.dxf.review"
    rev.mkdir(parents=True)
    (rev / "1F.svg").write_text("<svg/>", encoding="utf-8")
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / "1F.svg").write_text("<svg/>", encoding="utf-8")
    svg_dir = {"v": str(rev)}
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"svg_dir": svg_dir["v"]})
    r = client.get("/api/cases/3/files/7/review/1F.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert client.get("/api/cases/3/files/7/review/x.svg").status_code == 404          # 樓層代號格式不符
    svg_dir["v"] = str(outside)                                                       # 資料庫裡的路徑不在案件資料夾內
    assert client.get("/api/cases/3/files/7/review/1F.svg").status_code == 404


def test_context_validates_and_requeues(client, monkeypatch):
    client.cookies.set("fr_session", "good-token")
    saved = {}
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"ok": 1} if "occupancy_code" in sql and a[0] == "丁-2" else
                        ({"id": 3, "name": "案", "created_by": "amy", "created_at": "t"} if "review_case" in sql else None))
    monkeypatch.setattr(api.DS, "save_context", lambda c, cid, ctx, u: saved.update(ctx=ctx, by=u))
    monkeypatch.setattr(api.DS, "requeue_reviews", lambda c, cid: 2)
    body = {"occupancy": "丁-2", "ceiling_height": {"1F": 6.5}, "no_opening": ["B1"], "stories": 3, "fireproof": True}
    r = client.put("/api/cases/3/context", json=body)
    assert r.json() == {"saved": True, "requeued": 2} and saved["ctx"]["ceiling_height"] == {"1F": 6.5} and saved["by"] == "amy"
    assert client.put("/api/cases/3/context", json={"occupancy": "甲-99"}).status_code == 422
    assert client.put("/api/cases/3/context", json={"ceiling_height": {"一樓": 3}}).status_code == 422
    assert client.put("/api/cases/3/context", json={"ceiling_height": {"1F": 300}}).status_code == 422
    assert client.put("/api/cases/3/context", json={"stories": 0}).status_code == 422
    r = client.put("/api/cases/3/context", json={"policy": {"shaft_in_coverage": True}})       # 法規解讀設定
    assert r.status_code == 200 and saved["ctx"]["policy"] == {"shaft_in_coverage": True}
    assert client.put("/api/cases/3/context", json={"policy": {"不存在": True}}).status_code == 422


def test_workbench_policy_switches_match_defaults():
    """工作台的法規解讀勾選項與檢核程式的預設一致（鍵、預設值）。"""
    import re
    from pathlib import Path

    from litian.review.checks import DEFAULT_POLICY
    html = (Path(api.__file__).parent / "web" / "workbench.html").read_text(encoding="utf-8")
    boxes = dict(re.findall(r'data-k="(\w+)" data-def="([01])"', html))
    assert boxes == {k: "1" if v else "0" for k, v in DEFAULT_POLICY.items()}


def test_decisions_store_accept_reject_and_undo(client, monkeypatch):
    client.cookies.set("fr_session", "good-token")
    calls = []
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"ok": 1} if a == (7, 3) else None)
    monkeypatch.setattr(api.DS, "decide", lambda c, fid, key, dec, note, u: calls.append((fid, key, dec, note, u)))
    assert client.post("/api/cases/3/files/7/decisions", json={"key": "0123456789ab", "decision": "reject", "note": " 圖上已註明免設 "}).status_code == 200
    assert client.post("/api/cases/3/files/7/decisions", json={"key": "0123456789ab", "decision": None}).status_code == 200
    assert calls == [(7, "0123456789ab", "reject", "圖上已註明免設", "amy"), (7, "0123456789ab", None, None, "amy")]
    assert client.post("/api/cases/3/files/7/decisions", json={"key": "x", "decision": "accept"}).status_code == 422
    assert client.post("/api/cases/3/files/7/decisions", json={"key": "0123456789ab", "decision": "maybe"}).status_code == 422
    assert client.post("/api/cases/3/files/8/decisions", json={"key": "0123456789ab", "decision": "accept"}).status_code == 404


def test_report_html_and_csv(client, monkeypatch, tmp_path):
    client.cookies.set("fr_session", "good-token")
    rev = tmp_path / "3" / "001.dxf.review"
    rev.mkdir(parents=True)
    (rev / "1F.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>plan</title></svg>', encoding="utf-8")
    f1 = {"no": 1, "key": "aaaaaaaaaaaa", "rule": "HYD-34", "severity": "RED", "category": "距離超過", "floor": "1F",
          "title": "辦公室有 30 ㎡ 不在消防栓 25 m 內", "why": "水平距離 25 m", "fix": "增設", "law": ["D0120029/34/1/1/1"],
          "missing": [], "rooms": ["辦公室"]}
    f2 = dict(f1, no=2, key="bbbbbbbbbbbb", severity="YELLOW", title="探測器需補資料", missing=["1F 天花板高度"])
    f3 = dict(f1, no=3, key="cccccccccccc", title="已退回的缺失")
    result = {"floors": [{"label": "1F", "number": "F-101", "title": "壹層", "area": 450, "equipment": {"hydrant": 1},
                          "findings": [f1, f2, f3], "notes": []}],
              "building": {"profile": None, "requirements": [{"key": "15", "equipment": "室內消防栓設備", "kinds": ["hydrant"],
                           "status": "REQUIRED", "why": "五層以下…", "law": ["D0120029/15/1/1"], "floors": None, "missing": [], "notes": []}],
                           "findings": [], "notes": []}}
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"id": 3, "name": "測試案", "created_by": "amy", "created_at": "t"})
    monkeypatch.setattr(api, "_all", lambda sql, *a: [{"file_id": 7, "name": "F.dxf", "status": "done", "error": None,
                                                       "result": result, "svg_dir": str(rev), "created_at": "t"}]
                        if "file_review" in sql else ([{"code": "丁-2", "text": "中度危險工作場所。"}] if "occupancy_code" in sql else []))
    monkeypatch.setattr(api.DS, "get_context", lambda c, cid: {"occupancy": "丁-2", "ceiling_height": {"1F": 3.2}})
    monkeypatch.setattr(api.DS, "decisions", lambda c, cid: {"7": {"aaaaaaaaaaaa": {"decision": "accept", "note": "確認"},
                                                                   "cccccccccccc": {"decision": "reject", "note": None}}})
    html = client.get("/api/cases/3/report").text
    assert "消防安全設備圖說自審報告" in html and "丁-2　中度危險工作場所" in html and "<title>plan</title>" in html
    assert "辦公室有 30 ㎡" in html and "已退回的缺失" not in html and "1F 天花板高度" in html and "室內消防栓設備" in html
    csv = client.get("/api/cases/3/report.csv")
    assert csv.headers["content-disposition"].startswith("attachment") and csv.content.startswith("\ufeff".encode())
    text = csv.content.decode("utf-8-sig")
    assert text.count("\n") == 4 and "退回" in text and "接受" in text and "未審核" in text
