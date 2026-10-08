"""審核工作台：權限、上傳、檢核結果（本機，不連資料庫；LINE 登入見 test_line_login.py，資料庫的實際 SQL 由 test_db_integration.py 在主機上驗）。"""

from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from litian import api
from litian import auth as AU

USER = {"id": 1, "username": "amy", "role": "reviewer"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    @contextmanager
    def conn():
        yield NS()
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    monkeypatch.setattr(api, "CASES_DIR", tmp_path)
    state = {"files": []}
    monkeypatch.setattr(api.AU, "session_user", lambda c, token: USER if token == "good-token" else None)
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


def test_logout_clears_session(client):
    client.cookies.set("__Host-fr_session", "good-token")
    assert client.get("/api/auth/me").json()["username"] == "amy"
    r = client.post("/api/auth/logout")
    assert r.status_code == 200 and client.state["logout"] == "good-token"
    assert r.headers["set-cookie"].startswith("__Host-fr_session=") and "max-age=0" in r.headers["set-cookie"].lower()


def test_password_login_is_gone(client):
    assert client.post("/api/auth/login", json={"username": "amy", "password": "x"}).status_code in (404, 405)
    html = client.get("/workbench").text
    assert 'type="password"' not in html and "/api/auth/line/start" in html and "用 LINE 登入" in html


def test_upload_streams_to_disk_with_hash(client, tmp_path):
    client.cookies.set("__Host-fr_session", "good-token")
    files = [("files", ("../../A1-05 面積計算表.dwg", b"AC1027" + b"x" * 100, "application/octet-stream")),
             ("files", ("A1-05.dwl", b"lock", "application/octet-stream"))]
    r = client.post("/api/cases/3/files", files=files)
    assert r.status_code == 200 and [f["name"] for f in r.json()["files"]] == ["../../A1-05 面積計算表.dwg", "A1-05.dwl"]
    (cid, name, size, sha, path), _ = client.state["files"]
    assert cid == 3 and size == 106 and len(sha) == 64
    assert path.startswith(str(tmp_path / "3")) and path.endswith("001_A1-05_面積計算表.dwg")   # 路徑穿越被清掉
    assert (tmp_path / "3" / "001_A1-05_面積計算表.dwg").read_bytes().startswith(b"AC1027")


def test_upload_rejects_oversize_and_removes_partial(client, tmp_path, monkeypatch):
    client.cookies.set("__Host-fr_session", "good-token")
    monkeypatch.setattr(api, "UPLOAD_MAX", 10)
    r = client.post("/api/cases/4/files", files=[("files", ("big.dwg", b"0123456789AB", "application/octet-stream"))])
    assert r.status_code == 413 and "超過單檔上限" in r.json()["detail"]
    assert not list((tmp_path / "4").iterdir())


def test_sheet_texts_sorted_top_to_bottom(client, monkeypatch):
    client.cookies.set("__Host-fr_session", "good-token")
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"file_id": 1, "idx": 0, "number": "A1-05", "title": "面積計算表"})
    monkeypatch.setattr(api, "_all", lambda sql, *a: [{"t": "下", "x": 0, "y": 1, "layer": "0"},
                                                       {"t": "右上", "x": 9, "y": 5, "layer": "0"},
                                                       {"t": "左上", "x": 1, "y": 5, "layer": "0"}])
    r = client.get("/api/cases/1/sheets/2/texts")
    assert [t["t"] for t in r.json()["texts"]] == ["左上", "右上", "下"]


def test_reviews_require_login_and_attach_svg_urls_and_laws(client, monkeypatch):
    assert client.get("/api/cases/3/reviews").status_code == 401
    assert client.get("/api/cases/3/files/7/review/1F.svg").status_code == 401
    client.cookies.set("__Host-fr_session", "good-token")
    result = {"floors": [{"label": "1F", "findings": [{"law": ["D0120029/34/1/1/1"]}], "notes": [{"law": ["D0120029/49/1/1"]}]}],
              "warnings": []}

    def fake_all(sql, *a):
        if "FROM file_review" in sql:
            return [{"file_id": 7, "name": "F-101.dxf", "status": "done", "error": None, "result": result, "svg_dir": None, "created_at": "t"}]
        if "LIKE ANY" in sql:                                       # 引導句節點的子孫：這裡沒有
            return []
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
    client.cookies.set("__Host-fr_session", "good-token")
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
    client.cookies.set("__Host-fr_session", "good-token")
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
    client.cookies.set("__Host-fr_session", "good-token")
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
    client.cookies.set("__Host-fr_session", "good-token")
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
    assert "print.png" not in html and "@page plan" in html                    # 原圖沒好：用簡化圖
    csv = client.get("/api/cases/3/report.csv")
    assert csv.headers["content-disposition"].startswith("attachment") and csv.content.startswith("\ufeff".encode())
    text = csv.content.decode("utf-8-sig")
    assert text.count("\n") == 4 and "退回" in text and "接受" in text and "未審核" in text


def test_report_uses_cad_original_with_vector_marks(client, monkeypatch, tmp_path):
    """原圖已畫好的樓層：報告放列印用整張原圖＋向量缺失標示（編號同缺失表、退回的不畫），另附簡化圖當載不到時的退路。"""
    import json

    from litian.review import cadview as CV
    from .test_cadview import _tiled_sheet
    client.cookies.set("__Host-fr_session", "good-token")
    rev = tmp_path / "3" / "001.dxf.review"
    (rev / "cad" / "1F").mkdir(parents=True)
    (rev / "1F.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>plan</title></svg>', encoding="utf-8")
    meta = _tiled_sheet(rev / "cad" / "1F", w=1200, h=800)
    (rev / "cad" / "status.json").write_text(json.dumps({"state": "done", "sheets": {"1F": "done"}}), encoding="utf-8")
    sq = [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]]
    ov = {"version": 1, "findings": [
        {"no": 1, "key": "aaaaaaaaaaaa", "severity": "RED", "geom": {"type": "Polygon", "coordinates": sq}, "anchor": [15, 15]},
        {"no": 2, "key": "bbbbbbbbbbbb", "severity": "BLUE", "geom": {"type": "Point", "coordinates": [50, 50]}, "anchor": None},
        {"no": 3, "key": "cccccccccccc", "severity": "RED", "geom": {"type": "Polygon", "coordinates": sq}, "anchor": [12, 12]}]}
    (rev / "1F.overlay.json").write_text(json.dumps(ov), encoding="utf-8")
    f1 = {"no": 1, "key": "aaaaaaaaaaaa", "rule": "HYD-34", "severity": "RED", "category": "距離超過", "floor": "1F",
          "title": "辦公室不在消防栓 25 m 內", "why": "水平距離", "fix": "增設", "law": [], "missing": [], "rooms": []}
    result = {"floors": [{"label": "1F", "svg_name": "1F", "number": "F-101", "title": "壹層", "area": 450,
                          "equipment": {"hydrant": 1}, "notes": [],
                          "findings": [f1, dict(f1, no=2, key="bbbbbbbbbbbb", severity="BLUE"), dict(f1, no=3, key="cccccccccccc")]}],
              "building": None}
    monkeypatch.setattr(api, "CASES_DIR", tmp_path)
    monkeypatch.setattr(api, "_one", lambda sql, *a: {"svg_dir": str(rev)} if "svg_dir" in sql else
                        {"id": 3, "name": "測試案", "created_by": "amy", "created_at": "t"})
    monkeypatch.setattr(api, "_all", lambda sql, *a: [{"file_id": 7, "name": "F.dxf", "status": "done", "error": None, "result": result,
                                                       "svg_dir": str(rev), "created_at": "t", "cad_state": "done"}]
                        if "file_review" in sql else [])
    monkeypatch.setattr(api.DS, "get_context", lambda c, cid: {})
    monkeypatch.setattr(api.DS, "decisions", lambda c, cid: {"7": {"cccccccccccc": {"decision": "reject", "note": None}}})
    html = client.get("/api/cases/3/report").text
    assert "/api/cases/3/files/7/cad/1F/print.png?v=2026-10-06T00:00:00+00:00" in html
    marks = html[html.index('<svg class="marks"'):html.index("</svg>", html.index('<svg class="marks"'))]
    assert f'viewBox="0 0 {meta["width"]} {meta["height"]}"' in marks
    assert ">1</text>" in marks and ">2</text>" in marks and ">3</text>" not in marks          # 退回的不畫
    assert 'fill="none" fill-opacity="0.2" stroke="#1a73e8"' in marks                           # 「建議」只畫外框
    assert "M100.0,700.0L200.0,700.0" in marks                                                 # 公尺 → 原尺寸像素（meta.transform）
    assert "class='plan fallback'" in html and "<title>plan</title>" in html and "id='print'" in html and " disabled>" in html
    r = client.get("/api/cases/3/files/7/cad/1F/print.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert "原圖載入中，請稍候再列印" in html and "max-height: 150mm; display: block; }" in html     # 簡化圖也放得進一頁
    import os
    next((rev / "cad" / "1F" / str(meta["max_level"])).glob("*.png")).unlink()      # 圖重畫過、缺圖磚：拿不到就 404（報告退回簡化圖）
    (rev / "cad" / "1F" / "print.png").unlink()
    os.utime(rev / "cad" / "1F" / "meta.json")
    assert client.get("/api/cases/3/files/7/cad/1F/print.png").status_code == 404
    client.cookies.clear()
    assert client.get("/api/cases/3/files/7/cad/1F/print.png").status_code == 401


def test_cad_overlay_svg_lines_multipolygons_and_collections():
    from litian.review import report as RP
    meta = {"width": 1000, "height": 500, "transform": [10, 0, 0, 0, -10, 500]}
    ov = {"findings": [
        {"no": 4, "key": "d", "severity": "ORANGE", "geom": {"type": "LineString", "coordinates": [[1, 1], [5, 1]]}},
        {"no": 5, "key": "e", "severity": "YELLOW", "geom": {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]}},
        {"no": 6, "key": "f", "severity": "RED", "geom": {"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [20, 20]}, {"type": "LineString", "coordinates": [[30, 30], [31, 31]]}]}},
        {"no": 7, "key": "g", "severity": "RED", "geom": None, "anchor": None},                 # 沒有位置：不畫編號
        {"no": "8", "key": "h", "severity": "RED", "geom": None, "anchor": [1, 1]}]}            # 編號不是整數：略過
    s = RP.cad_overlay_svg(meta, ov, set())
    assert 'd="M10.0,490.0L50.0,490.0" fill="none" stroke="#e8710a"' in s                      # 線
    assert ">4</text>" in s and ">5</text>" in s and ">6</text>" in s and ">7</text>" not in s and ">8</text>" not in s
    assert s.count("<path") == 4                                                            # 線、多邊形、點＋線（同一筆分兩條）


# ---------- 檔案處理狀態的白話說明（note）與外部參考（xref_of） ----------

def _file(fid, name, status="done", kind="dwg", stats=None, error=None):
    return {"id": fid, "name": name, "kind": kind, "size": 1, "status": status, "error": error, "stats": stats, "attempts": 0}


def _summary(fid, stored, review=None, floors=0, findings=0, error=None):
    return {"id": fid, "path": f"/cases/3/{stored}", "review": review, "review_error": error, "floors": floors, "findings": findings}


def test_case_files_get_plain_notes_and_xref_host(client, monkeypatch):
    client.cookies.set("__Host-fr_session", "good-token")
    files = [
        _file(1, "F-101.dwg", stats={"sheets": 3, "xref": {"bound": ["Area_1F"], "bound_files": ["002_Area_1F.dwg"], "missing": []}}),
        _file(2, "Area_1F.dwg", stats={"sheets": 1}),
        _file(3, "Area_2F.dwg", stats={"sheets": 1}),
        _file(4, "Area_9F.dwg", stats={"sheets": 1}),
        _file(5, "G.dxf", kind="dxf", stats={"sheets": 1}),
        _file(6, "H.dwg", stats={"xref": {"bound": ["AREA_2F"], "missing": []}}),     # 舊資料：只有圖塊名
        _file(7, "x.dwl", status="skipped", kind="other", error="不支援的檔案類型（AutoCAD 暫存檔等）"),
        _file(8, "a.pdf", status="skipped", kind="pdf", error="PDF 擷取尚未支援（後續里程碑）"),
        _file(9, "bad.dwg", status="failed", error="ConvertError: 轉檔逾時"),
        _file(10, "q.dwg", status="queued"),
        _file(11, "Area_2F.dwg", stats={"sheets": 1}),                                # 同名再上傳；H 是舊規則綁的（取最早的）
        _file(12, "K.dwg", stats={"xref": {"bound": ["Area_9F"], "bound_files": [], "missing": []}}),   # 新資料以 bound_files 為準
        _file(13, "R.dwg", status="reviewing", stats={"sheets": 1}),
    ]
    summary = [_summary(1, "001_F-101.dwg", "done", floors=2, findings=5), _summary(2, "002_Area_1F.dwg"),
               _summary(3, "003_Area_2F.dwg"), _summary(4, "004_Area_9F.dwg"),
               _summary(5, "005_G.dxf", "failed", error="ValueError: 圖框讀不到"),
               _summary(6, "006_H.dwg", "done", floors=0, findings=2), _summary(7, "007_x.dwl"), _summary(8, "008_a.pdf"),
               _summary(9, "009_bad.dwg"), _summary(10, "010_q.dwg"), _summary(11, "011_Area_2F.dwg"),
               _summary(12, "012_K.dwg", "done", floors=1, findings=0), _summary(13, "013_R.dwg")]
    monkeypatch.setattr(api.DS, "case_status", lambda c, cid: [dict(f) for f in files])
    monkeypatch.setattr(api.DS, "file_reviews", lambda c, cid: summary)
    monkeypatch.setattr(api, "_all", lambda sql, *a: [])
    d = client.get("/api/cases/3").json()
    got = {f["id"]: (f["note"], f["xref_of"], f["review"]) for f in d["files"]}
    assert got[1] == ("已檢核 2 層，缺失 5 條", None, "done")
    assert got[2] == ("建築底圖（外部參考），已併入「F-101.dwg」一起檢核", "F-101.dwg", None)
    assert got[3] == ("建築底圖（外部參考），已併入「H.dwg」一起檢核", "H.dwg", None)        # 圖塊名比對不分大小寫
    assert got[4] == (api.NO_FLOOR_NOTE, None, None) and "樓層" in api.NO_FLOOR_NOTE
    assert got[5] == ("檢核失敗：ValueError: 圖框讀不到", None, "failed")
    assert got[6] == ("已檢核，但沒有認出樓層平面圖；全棟缺失 2 條", None, "done")
    assert got[7] == ("不支援的檔案類型，已略過", None, None)
    assert got[8] == ("不支援的檔案類型，已略過（PDF 尚未支援）", None, None)
    assert got[9] == ("ConvertError: 轉檔逾時", None, None)
    assert got[10] == (None, None, None) and got[13] == (None, None, None)              # 處理中不寫說明
    assert got[11] == ("較新上傳的建築底圖：「H.dwg」重新處理後改用這份", "H.dwg", None)
    assert got[12] == ("已檢核 1 層，缺失 0 條", None, "done")
    f1 = next(f for f in d["files"] if f["id"] == 1)
    assert f1["stats"]["sheets"] == 3 and f1["size"] == 1 and "path" not in f1           # 原有欄位保留，存檔路徑不外露


def test_reuploaded_base_drawing_notes():
    # 同名底圖重新上傳：主圖重新處理前／中／後的說明；舊的那份最後標成不再使用（不列成未檢核的問題）
    def notes(main_status, bound, failed=(), sha=("a", "b")):
        files = [_file(1, "M.dwg", status=main_status, stats={"xref": {"bound": ["B"], "bound_files": [bound],
                                                                          "failed": list(failed)}}),
                 _file(2, "B.dwg"), _file(3, "B.DWG"),
                 _file(4, "1F.dwg", stats={"xref": {"bound": ["1F"], "bound_files": ["006_1F.dwg"]}}),   # 與底圖同名的主圖
                 _file(6, "1F.dwg")]
        info = {1: _summary(1, "001_M.dwg", "done" if main_status == "done" else None, 1, 3),
                2: {**_summary(2, "002_B.dwg"), "sha256": sha[0]}, 3: {**_summary(3, "005_B.DWG"), "sha256": sha[1]},
                4: _summary(4, "007_1F.dwg", "done", 1, 0), 6: _summary(6, "006_1F.dwg")}
        out = {f["id"]: f for f in api._file_notes(files, info)}
        return {i: (f["note"], f["xref_of"], f["superseded"], f["xref_warn"]) for i, f in out.items()}
    busy = notes("queued", "002_B.dwg")
    assert busy[2] == ("建築底圖（外部參考），已併入「M.dwg」，但主圖沒有完成檢核（原因見主圖的說明）", "M.dwg", False, False)
    assert busy[3] == ("較新上傳的建築底圖：「M.dwg」重新處理中，完成後改用這份", "M.dwg", False, False)
    assert notes("done", "002_B.dwg")[3] == ("較新上傳的建築底圖：「M.dwg」重新處理後改用這份", "M.dwg", False, False)
    same = notes("done", "002_B.dwg", sha=("a", "a"))                                # 內容一樣：不必重跑
    assert same[3] == ("內容與「M.dwg」已併入的同名檔相同，照用原本那份", "M.dwg", False, False)
    bad = notes("done", "002_B.dwg", failed=["005_B.DWG（DXFStructureError）"])         # 綁定時試過、讀不了：要處理
    assert bad[3][1:] == (None, False, True) and "讀不了（DXFStructureError）" in bad[3][0]
    after = notes("done", "005_B.DWG")
    assert after[3] == ("建築底圖（外部參考），已併入「M.dwg」一起檢核", "M.dwg", False, False)
    assert after[2] == ("已有較新上傳的同名檔，這份不再使用", None, True, False)
    assert after[6][0] == "建築底圖（外部參考），已併入「1F.dwg」一起檢核"                # 同名主圖不算較新的底圖


def test_same_name_main_and_unreadable_base_notes():
    # 主圖和底圖同名、主圖沒認出樓層：照實說「沒有認出樓層平面圖」，不能被當成底圖淡化
    files = [_file(1, "1F.dwg", stats={"xref": {"bound": ["1F"], "bound_files": ["001_1F.dwg"]}}), _file(2, "1F.dwg"),
             _file(3, "1F.dwg")]
    info = {1: _summary(1, "002_1F.dwg"), 2: _summary(2, "001_1F.dwg"), 3: _summary(3, "003_1F.dwg")}
    out = {f["id"]: f for f in api._file_notes(files, info)}
    assert out[1]["note"] == api.NO_FLOOR_NOTE and not out[1]["superseded"] and out[1]["xref_of"] is None
    # 讀不了、主圖也沒有其他同名檔可用：一樣警告（不叫使用者去上傳主圖）
    files = [_file(1, "M.dwg", stats={"xref": {"bound": [], "bound_files": [], "missing": ["B.dwg"],
                                                "failed": ["002_B.dwg（DXFStructureError）"]}}), _file(2, "B.dwg")]
    out = {f["id"]: f for f in api._file_notes(files, {1: _summary(1, "001_M.dwg", "done", 1, 0), 2: _summary(2, "002_B.dwg")})}
    assert out[2]["xref_warn"] and out[2]["note"] == "這份讀不了（DXFStructureError），主圖沒有用到；請確認檔案後重新上傳"
    # 內容相同又剛好排隊只重跑檢核：寫「照用原本那份」，不寫「完成後改用這份」
    files = [_file(1, "M.dwg", status="queued", stats={"xref": {"bound": ["B"], "bound_files": ["002_B.dwg"]}}),
             _file(2, "B.dwg"), _file(3, "B.dwg")]
    info = {1: _summary(1, "001_M.dwg"), 2: {**_summary(2, "002_B.dwg"), "sha256": "x"}, 3: {**_summary(3, "003_B.dwg"), "sha256": "x"}}
    out = {f["id"]: f for f in api._file_notes(files, info)}
    assert out[3]["note"] == "內容與「M.dwg」已併入的同名檔相同，照用原本那份"


def test_xref_list_error_and_memory_error_notes():
    # 讀不出主圖引用的外部參考：完成了也要講清楚這次沒有併入底圖；底圖記憶體不足：檔案沒壞，不叫使用者重新上傳
    files = [_file(1, "M.dwg", stats={"sheets": 2, "xref": {"list_error": "MemoryError"}}),
             _file(2, "N.dwg", stats={"xref": {"list_error": "逾時"}}),
             _file(3, "P.dwg", stats={"xref": {"bound": [], "bound_files": [], "missing": ["B.dwg"],
                                                "failed": ["004_B.dwg（MemoryError）"]}}),
             _file(4, "B.dwg"),
             _file(5, "Q.dwg", status="queued", stats={"xref": {"list_error": "逾時"}})]   # 重新處理中：舊的說明不寫
    info = {1: _summary(1, "001_M.dwg", "done", 2, 5), 2: _summary(2, "002_N.dwg"), 3: _summary(3, "003_P.dwg", "done", 1, 0),
            4: _summary(4, "004_B.dwg"), 5: _summary(5, "005_Q.dwg")}
    out = {f["id"]: f for f in api._file_notes(files, info)}
    assert out[1]["note"] == "已檢核 2 層，缺失 5 條；建築底圖（外部參考）讀取失敗（MemoryError），這次檢核沒有併入底圖，結果可能不準；請通知系統管理者"
    assert out[2]["note"] == api.NO_FLOOR_NOTE + "；建築底圖（外部參考）讀取失敗（逾時），這次檢核沒有併入底圖，結果可能不準；請通知系統管理者"
    assert out[4]["xref_warn"] and out[4]["note"] == "這份讀取時記憶體不足（圖太大，不是檔案壞掉），主圖沒有用到；不必重新上傳，請通知系統管理者"
    assert out[3]["note"] == "已檢核 1 層，缺失 0 條" and out[5]["note"] is None


def test_queued_files_show_position_and_estimate(client, monkeypatch):
    # 排隊中：前面還有幾個（所有案件一起排，只給數字）＋照最近的處理時間估要等多久；沒有紀錄只寫個數；沒人在等不查
    client.cookies.set("__Host-fr_session", "good-token")
    files = [{**_file(1, "A.dwg", status="queued"), "ahead": 3}, {**_file(2, "B.dwg", status="queued"), "ahead": 0},
             {**_file(3, "C.dwg", status="processing"), "ahead": None}, {**_file(4, "D.dwg", stats={"sheets": 1}), "ahead": None}]
    med, calls = {"file": 240.0, "cad": 999.0}, []
    monkeypatch.setattr(api.DS, "case_status", lambda c, cid: [dict(f) for f in files])
    monkeypatch.setattr(api.DS, "file_reviews", lambda c, cid: [_summary(4, "004_D.dwg")])
    monkeypatch.setattr(api.DS, "recent_seconds", lambda c: calls.append(1) or med)
    monkeypatch.setattr(api, "_all", lambda sql, *a: [])
    notes = lambda: {f["id"]: f["note"] for f in client.get("/api/cases/3").json()["files"]}
    got = notes()
    assert got[1] == "前面還有 3 個檔（所有案件一起排隊），約 12 分鐘後開始處理（依最近的處理時間估計）"
    assert got[2] == "下一個處理" and got[3] is None and got[4] == api.NO_FLOOR_NOTE and calls == [1]
    med["file"] = None
    assert notes()[1] == "前面還有 3 個檔（所有案件一起排隊）"
    files[0]["ahead"] = 0
    calls.clear()
    assert notes()[1] == "下一個處理" and calls == []
    assert [api._about(s) for s in (20, 89, 3540, 5400, 7200)] == ["不到 1 分鐘", "約 1 分鐘", "約 59 分鐘", "約 1.5 小時", "約 2 小時"]


def test_unreviewed_list_keeps_superseded_files_muted():
    html = _html()
    js = html[html.index("function unreviewedHtml("):html.index("function renderUnreviewed(")]
    assert "!f.xref_of && !f.superseded && !f.xref_warn" in js and "f.xref_of || f.superseded" in js and "f.xref_warn" in js


def test_xref_host_lists_every_main_file():
    files = [_file(1, "A.dwg", stats={"xref": {"bound": ["Area_1F"], "bound_files": ["003_Area_1F.dwg"]}}),
             _file(2, "B.dwg", stats={"xref": {"bound": ["Area_1F"], "bound_files": ["003_area_1f.DWG"]}}),
             _file(3, "Area_1F.dwg")]
    info = {1: _summary(1, "001_A.dwg", "done", 1, 0), 2: _summary(2, "002_B.dwg", "done", 1, 0), 3: _summary(3, "003_Area_1F.dwg")}
    out = {f["id"]: f for f in api._file_notes(files, info)}
    assert out[3]["xref_of"] == "A.dwg" and out[3]["note"] == "建築底圖（外部參考），已併入「A.dwg」、「B.dwg」一起檢核"
    assert out[1]["xref_of"] is None and out[2]["xref_of"] is None


def test_xref_base_of_unreviewed_main_is_not_called_reviewed():
    # 主圖檢核失敗或沒認出樓層：底圖不能寫「一起檢核」，指回主圖的說明
    files = [_file(1, "M.dwg", stats={"xref": {"bound": ["B"], "bound_files": ["002_B.dwg"]}}), _file(2, "B.dwg")]
    for main in (_summary(1, "001_M.dwg", "failed"), _summary(1, "001_M.dwg")):
        out = {f["id"]: f for f in api._file_notes([dict(f) for f in files], {1: main, 2: _summary(2, "002_B.dwg")})}
        assert out[2]["xref_of"] == "M.dwg" and "一起檢核" not in out[2]["note"] and "主圖沒有完成檢核" in out[2]["note"]


# ---------- 工作台頁面：簡化後的版面（靜態檢查） ----------

def _html() -> str:
    return api.WEB_WORKBENCH.read_text(encoding="utf-8")


def test_workbench_files_table_shows_plain_notes_not_diagnostics():
    html = _html()
    assert "<th>說明</th>" in html and "抽取結果" not in html and "sheet_numbers" not in html
    assert "f.note" in html and "f.xref_of" in html
    # 圖紙與抽出的文字收在頁面最下方、預設收起的區塊裡（點圖紙看文字、搜尋框照舊）
    raw = html[html.index('<details id="raw"'):]
    raw = raw[:raw.index("</details>")]
    assert "圖紙與抽出的文字（查看系統從圖上讀到什麼）" in raw and " open" not in raw.split(">")[0]
    for s in ['id="sheets"', 'id="texts"', 'id="filter"', 'id="texts-title"', 'id="texts-hint"']:
        assert s in raw, s
    assert html.index('id="reviews"') < html.index('<details id="raw"')


def test_workbench_lists_unreviewed_files_with_hint():
    html = _html()
    assert 'id="unreviewed"' in html and "function unreviewedHtml(" in html
    for s in ["圖框的圖名要寫出樓層（例如「一層消防平面圖」）系統才會檢核", "請上傳引用它的消防設備圖，系統會自動併入"]:
        assert s in html, s
    # 沒有任何檢核結果時由這份清單取代一般提示；輪詢、局部更新照舊
    hint = html[html.index("function updateHint("):]
    assert "bundle.reviews.length > 0 || !!lastUnrev" in hint[:200]
    assert "renderUnreviewed()" in html[html.index("async function refreshCase("):html.index("// ---------- 檢核結果")]


def test_workbench_law_citations_use_popover_not_title():
    html = _html()
    assert "title=\"' + esc(l.text)" not in html and ".law span" not in html
    for s in ['class="lawref"', '"role", "dialog"', "aria-expanded", '"Escape"', "pointerdown", "lawBodyHtml(l)", "--pop-shadow"]:
        assert s in html, s


def test_workbench_finding_card_layout_and_locate_button():
    html = _html()
    find = html[html.index("function findingHtml("):html.index("function noteHtml(")]
    for s in ['class="f-head"', 'class="f-title"', 'class="f-body"', "<dt>說明</dt>", "<dt>建議</dt>", "<dt>要補的資料</dt><dd><ul>",
              "<dt>依據</dt>", "decisionHtml(fileId, f.key)"]:
        assert s in find, s
    cad = html[html.index("async function mountCad("):html.index("function cadMarks(")]
    assert 'li.querySelector(".f-head")' in cad and "head.append(b)" in cad and '"aria-label"' in cad and 'sv("svg"' in cad
    assert 'li.querySelector("b").after' not in cad
    assert "button.locate {" in html and "background: var(--accent-solid)" in html and "@container" in html
    # 巢狀清單（要補的資料）不能套到缺失卡的樣式
    assert "ol.findings li {" not in html and "ol.findings > li {" in html


# ---------- 法條區塊（工作台的純函式段落，用 node 跑；沒有 node 就略過） ----------

def _run_law_js(tmp_path, data, body: str):
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，略過前端純函式測試")
    html = _html()
    funcs = html[html.index("// ---------- 法條區塊 ----------"):html.index("// ---------- 法條區塊結束")]
    js = tmp_path / "law.js"
    js.write_text(funcs + "\nconst D = " + json.dumps(data, ensure_ascii=False) + ";\n"
                  "process.stdout.write(JSON.stringify((() => {" + body + "})()));\n", encoding="utf-8")
    r = subprocess.run([node, str(js)], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8"))


def test_law_buttons_escape_everything(tmp_path):
    laws = {'D/1"x': {"citation": "第 1 條<script>alert(1)</script>", "text": "t"}}
    got = _run_law_js(tmp_path, {"laws": laws}, "return [lawHtml(['D/1\"x', '<b>不存在</b>'], D.laws), lawHtml(null, D.laws)];")
    assert got[0] == ('<button type="button" class="lawref" data-law="D/1&quot;x" aria-haspopup="dialog" aria-expanded="false">'
                      "第 1 條&lt;script&gt;alert(1)&lt;/script&gt;</button>、&lt;b&gt;不存在&lt;/b&gt;")
    assert got[1] == ""


def test_law_body_table_rowspan_colspan_and_escape(tmp_path):
    law = {"citation": "第 2 條", "text": "（原文）", "blocks": [
        {"type": "text", "text": "\n第二條　下列<場所>：\n  一、甲類 & 乙類\n\n"},
        {"type": "table", "header_rows": 1, "rows": [
            [{"text": "類別", "rowspan": 1, "colspan": 2}, {"text": "面積\n（㎡）", "rowspan": 1, "colspan": 1}],
            [{"text": "甲", "rowspan": 2, "colspan": 1}, {"text": "一", "rowspan": 1, "colspan": 1}, {"text": "<300", "rowspan": 1, "colspan": 1}],
            [{"text": "二", "rowspan": "abc", "colspan": 0}, {"text": "'500'", "rowspan": -3, "colspan": 5000}],
        ]},
        {"type": "pre", "text": "┌─┐\n│<x>│\n└─┘"},
    ]}
    html = _run_law_js(tmp_path, {"law": law}, "return lawBodyHtml(D.law);")
    assert html.startswith('<div class="lawtext">第二條　下列&lt;場所&gt;：\n  一、甲類 &amp; 乙類</div>')   # 頭尾空行去掉、縮排保留
    assert ('<div class="lawtbl"><table><tbody><tr><th colspan="2">類別</th><th>面積\n（㎡）</th></tr>'
            '<tr><td rowspan="2">甲</td><td>一</td><td>&lt;300</td></tr>'
            '<tr><td>二</td><td colspan="1000">&#39;500&#39;</td></tr></tbody></table></div>') in html
    assert html.endswith('<pre class="lawpre">┌─┐\n│&lt;x&gt;│\n└─┘</pre>')
    assert 'rowspan="1"' not in html and 'colspan="1"' not in html and "（原文）" not in html     # 有 blocks 就不用 text


def test_law_body_falls_back_to_text(tmp_path):
    got = _run_law_js(tmp_path, {"plain": {"citation": "c", "text": "第三條\n  <內文>"},
                                 "box": {"citation": "c", "text": "表：\n┌──┬──┐\n│a │b │"},
                                 "empty": {"citation": "c", "text": "", "blocks": []}},
                      "return [lawBodyHtml(D.plain), lawBodyHtml(D.box), lawBodyHtml(D.empty), lawBodyHtml(undefined)];")
    assert got[0] == '<div class="lawtext">第三條\n  &lt;內文&gt;</div>'
    assert got[1] == '<pre class="lawpre">表：\n┌──┬──┐\n│a │b │</pre>'                         # 舊資料：框線原樣等寬
    assert got[2] == got[3] == '<p class="muted">（沒有條文內容）</p>'
