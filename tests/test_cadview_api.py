"""CAD 原樣檢視：圖磚、圖磚資訊、缺失疊圖的 API（要登入、防路徑穿越），檢核結果每層的原圖狀態，工作台頁面的靜態檢查。
本機不連資料庫：檢核資料夾用暫存資料夾假造。"""

import json
import re
import struct
import zlib
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from litian import api

USER = {"id": 1, "username": "amy", "role": "reviewer"}
NAME = "1F-27"
META = {"version": 1, "width": 1200, "height": 800, "tile_size": 512, "overlap": 0, "format": "png", "max_level": 11,
        "transform": [20.0, 0.0, 100.0, 0.0, -20.0, 700.0], "source": "layout", "layout": "FE-101", "dpi": 150,
        "rendered_at": "2026-10-05T10:00:00+08:00", "renderer": "ezdxf 1.4.0"}
OVERLAY = {"version": 1, "sheet": 27, "number": "FE-101", "label": "1F",
           "findings": [{"no": 1, "key": "0123456789ab", "severity": "RED", "rule": "detector_coverage", "title": "探測器涵蓋不足",
                         "geom": {"type": "Polygon", "coordinates": [[[0, 0], [5, 0], [5, 4], [0, 4], [0, 0]]]},
                         "anchor": [2.5, 2.0]},
                        {"no": 2, "key": "ba9876543210", "severity": "BLUE", "rule": "note", "title": "建議", "geom": None,
                         "anchor": None}]}


def _png(w: int = 2, h: int = 2) -> bytes:
    """最小的白色 PNG（測試用圖磚）。"""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_review_dir(root, name=NAME, status=None, meta=True, tiles=((11, 0, 0), (11, 2, 1), (0, 0, 0)), overlay=True):
    """假造一個檢核資料夾：<root>/3/001_F.dxf.review/（cad/status.json、cad/<圖名>/meta.json＋圖磚、<圖名>.overlay.json）。"""
    rev = root / "3" / "001_F.dxf.review"
    cad = rev / "cad" / name
    cad.mkdir(parents=True, exist_ok=True)
    if meta:
        (cad / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    for lv, col, row in tiles:
        (cad / str(lv)).mkdir(exist_ok=True)
        (cad / str(lv) / f"{col}_{row}.png").write_bytes(_png())
    if overlay:
        (rev / f"{name}.overlay.json").write_text(json.dumps(OVERLAY, ensure_ascii=False), encoding="utf-8")
    if status is not None:
        (rev / "cad" / "status.json").write_text(status if isinstance(status, str) else json.dumps(status), encoding="utf-8")
    return rev


@pytest.fixture
def client(monkeypatch, tmp_path):
    @contextmanager
    def conn():
        yield NS()
    cases = tmp_path / "cases"
    cases.mkdir()
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    monkeypatch.setattr(api, "CASES_DIR", cases)
    monkeypatch.setattr(api.AU, "session_user", lambda c, token: USER if token == "good-token" else None)
    state = {"svg_dir": None, "queries": []}

    def fake_one(sql, *a):
        state["queries"].append((sql, a))
        if "FROM file_review" in sql:
            return {"svg_dir": state["svg_dir"]} if a == (7, 3) else None           # 檔案 7 屬於案件 3
        return {"id": a[0], "name": "案", "created_by": "amy", "created_at": "t"}
    monkeypatch.setattr(api, "_one", fake_one)
    c = TestClient(api.app)
    c.state, c.cases = state, cases
    return c


BASE = "/api/cases/3/files/7"
URLS = [f"{BASE}/cad/{NAME}/meta.json", f"{BASE}/cad/{NAME}/11/0_0.png", f"{BASE}/review/{NAME}.overlay.json"]


def test_cad_endpoints_require_login(client):
    client.state["svg_dir"] = str(make_review_dir(client.cases))
    for u in URLS:
        assert client.get(u).status_code == 401, u
    client.cookies.set("__Host-fr_session", "bad-token")
    for u in URLS:
        assert client.get(u).status_code == 401, u


def test_meta_tiles_and_overlay_served(client):
    client.state["svg_dir"] = str(make_review_dir(client.cases))
    client.cookies.set("__Host-fr_session", "good-token")
    r = client.get(URLS[0])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == META and "no-cache" in r.headers["cache-control"]
    for lv, col, row in ((11, 0, 0), (11, 2, 1), (0, 0, 0)):
        t = client.get(f"{BASE}/cad/{NAME}/{lv}/{col}_{row}.png?v=2026-10-05T10:00:00")      # 前端帶 ?v= 破快取
        assert t.status_code == 200 and t.headers["content-type"] == "image/png"
        assert t.headers["cache-control"] == "private, max-age=86400" and t.content.startswith(b"\x89PNG")
        assert t.headers["x-content-type-options"] == "nosniff"
    o = client.get(URLS[2])
    assert o.status_code == 200 and o.headers["content-type"].startswith("application/json")
    assert o.json()["findings"][0]["title"] == "探測器涵蓋不足" and "no-cache" in o.headers["cache-control"]
    # 只查自己案件的檔案：案件與檔案不符就沒有檢核資料夾
    assert client.get(f"/api/cases/4/files/7/cad/{NAME}/meta.json").status_code == 404


@pytest.mark.parametrize("path", [
    f"/cad/x/meta.json", f"/cad/1f-27/meta.json", f"/cad/1F-27%0A/meta.json", f"/cad/1F-27.svg/meta.json",
    f"/cad/..%2F..%2Fsecret/meta.json", f"/cad/%2E%2E/meta.json", f"/cad/1F-27/meta.json.png",
    f"/cad/{NAME}/a/0_0.png", f"/cad/{NAME}/-1/0_0.png", f"/cad/{NAME}/1.5/0_0.png", f"/cad/{NAME}/123/0_0.png",
    f"/cad/{NAME}/11/x_0.png", f"/cad/{NAME}/11/0_0.jpg", f"/cad/{NAME}/11/0_0.png.png", f"/cad/{NAME}/11/0-0.png",
    f"/cad/{NAME}/11/0_0_0.png", f"/cad/{NAME}/11/..%2F..%2Fmeta.json", f"/cad/{NAME}/%2E%2E/meta.json",
    f"/cad/{NAME}/11/%2E%2E%2F0_0.png", f"/review/x.overlay.json", f"/review/..%2F{NAME}.overlay.json",
    f"/review/{NAME}%2F..%2F{NAME}.overlay.json",
])
def test_bad_names_and_tiles_blocked(client, path):
    client.state["svg_dir"] = str(make_review_dir(client.cases))
    (client.cases / "secret").mkdir()
    (client.cases / "secret" / "meta.json").write_text("{}", encoding="utf-8")
    client.cookies.set("__Host-fr_session", "good-token")
    assert client.get(BASE + path).status_code == 404


def test_missing_files_and_dirs_outside_cases_are_404(client, tmp_path):
    client.cookies.set("__Host-fr_session", "good-token")
    assert client.get(URLS[0]).status_code == 404                         # 還沒有檢核結果（資料庫沒有資料夾）
    client.state["svg_dir"] = str(make_review_dir(client.cases, meta=False, overlay=False))
    for u in URLS[:1] + [f"{BASE}/cad/{NAME}/11/9_9.png", f"{BASE}/cad/{NAME}/12/0_0.png", URLS[2],
                         f"{BASE}/cad/2F-3/meta.json"]:
        assert client.get(u).status_code == 404, u
    # 資料庫裡的路徑不在案件資料夾內：檔案存在也不給
    client.state["svg_dir"] = str(make_review_dir(tmp_path / "elsewhere"))
    for u in URLS:
        assert client.get(u).status_code == 404, u


def _bundle(client, monkeypatch, rows):
    def fake_all(sql, *a):
        return rows if "FROM file_review" in sql else []
    monkeypatch.setattr(api, "_all", fake_all)
    monkeypatch.setattr(api.DS, "get_context", lambda c, cid: {})
    monkeypatch.setattr(api.DS, "decisions", lambda c, cid: {})
    client.cookies.set("__Host-fr_session", "good-token")
    r = client.get("/api/cases/3/reviews")
    assert r.status_code == 200
    return {(rv["file_id"], fl["svg_name"]): fl["cad"] for rv in r.json()["reviews"] for fl in rv["result"]["floors"]}


def _row(fid, svg_dir, names):
    floors = [{"label": n.split("-")[0], "svg_name": n, "findings": [], "notes": []} for n in names]
    return {"file_id": fid, "name": f"F{fid}.dxf", "status": "done", "error": None, "svg_dir": svg_dir,
            "result": {"floors": floors, "warnings": []}, "created_at": "t"}


def test_review_bundle_cad_states(client, monkeypatch, tmp_path):
    c = client.cases
    done = make_review_dir(c / "a", status={"state": "done", "sheets": {NAME: "done"}, "error": None})
    rendering = make_review_dir(c / "b", status={"state": "rendering", "sheets": {}, "error": None}, meta=False)
    failed = make_review_dir(c / "c", status={"state": "failed", "sheets": {}, "error": "轉檔逾時"}, meta=False)
    partial = make_review_dir(c / "d", status={"state": "rendering", "sheets": {NAME: "done", "2F-28": "failed"}})
    broken = make_review_dir(c / "e", status="{not json")                          # 狀態檔壞掉：看圖磚資訊在不在
    old = c / "f" / "3" / "001_F.dxf.review"                                       # 舊的檢核結果：沒有產生過原圖
    old.mkdir(parents=True)
    outside = make_review_dir(tmp_path / "elsewhere", status={"state": "done", "sheets": {NAME: "done"}})
    got = _bundle(client, monkeypatch, [
        _row(1, str(done), [NAME]), _row(2, str(rendering), [NAME]), _row(3, str(failed), [NAME]),
        _row(4, str(partial), [NAME, "2F-28", "3F-29"]), _row(5, str(broken), [NAME, "2F-28"]),
        _row(6, str(old), [NAME]), _row(7, None, [NAME]), _row(8, str(outside), [NAME])])
    assert got == {(1, NAME): "done", (2, NAME): "rendering", (3, NAME): "failed",
                   (4, NAME): "done", (4, "2F-28"): "failed", (4, "3F-29"): "rendering",
                   (5, NAME): "done", (5, "2F-28"): None, (6, NAME): None, (7, NAME): None, (8, NAME): None}


def test_review_bundle_cad_done_needs_meta(client, monkeypatch):
    # 狀態寫 done 但圖磚資訊不見了：不能叫前端去載
    rev = make_review_dir(client.cases, status={"state": "done", "sheets": {NAME: "done"}}, meta=False)
    assert _bundle(client, monkeypatch, [_row(1, str(rev), [NAME])]) == {(1, NAME): None}


# ---------- 工作台頁面（前端 JS 無法在這裡跑，只檢查必要元素） ----------

def test_workbench_loads_openseadragon_from_cdnjs_only():
    html = api.WEB_WORKBENCH.read_text(encoding="utf-8")
    srcs = re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html)
    assert srcs and all(s.startswith("https://cdnjs.cloudflare.com/ajax/libs/") for s in srcs)
    osd = [s for s in srcs if "/openseadragon/" in s]
    assert len(osd) == 1 and osd[0].endswith("/openseadragon.min.js")
    ver = re.search(r"/openseadragon/([0-9.]+)/", osd[0]).group(1)
    tag = re.search(r"<script[^>]*openseadragon\.min\.js[^>]*>", html).group(0)
    assert 'integrity="sha512-' in tag and 'crossorigin="anonymous"' in tag          # 外部程式要驗雜湊
    assert f"https://cdnjs.cloudflare.com/ajax/libs/openseadragon/{ver}/images/" in html     # 按鈕圖示同版本


def test_workbench_has_cad_viewer_pieces():
    html = api.WEB_WORKBENCH.read_text(encoding="utf-8")
    for s in ["顯示檢核標示", "原圖產生中（約數分鐘）", "原圖產生失敗，先顯示簡化圖", "/meta.json", ".overlay.json",
              "max_level", "tile_size", "transform", "rendered_at", "getTileUrl", "30000", "canvas-click"]:
        assert s in html, s
    # 疊圖不用 innerHTML 塞圖面文字
    script = html[html.index("// ---------- CAD 原樣檢視"):]
    script = script[:script.index("// ---------- CAD 原樣檢視結束")]
    assert "innerHTML" not in script
