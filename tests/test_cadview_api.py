"""CAD 原樣檢視：圖磚、圖磚資訊、缺失疊圖的 API（要登入、防路徑穿越），檢核結果每層的原圖狀態，工作台頁面的靜態檢查。
本機不連資料庫：檢核資料夾用暫存資料夾假造。"""

import json
import re
import shutil
import struct
import subprocess
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


def _bundle(client, monkeypatch, rows, field="cad"):
    def fake_all(sql, *a):
        if "FROM file_review" in sql:
            assert "ORDER BY f.name, f.id" in sql                    # 同名檔的順序固定：重查不會整頁重畫
            return rows
        return []
    monkeypatch.setattr(api, "_all", fake_all)
    monkeypatch.setattr(api.DS, "get_context", lambda c, cid: {})
    monkeypatch.setattr(api.DS, "decisions", lambda c, cid: {})
    client.cookies.set("__Host-fr_session", "good-token")
    r = client.get("/api/cases/3/reviews")
    assert r.status_code == 200
    assert not any("svg_dir" in rv or "cad_state" in rv or "cad_ahead" in rv for rv in r.json()["reviews"])   # 內部欄位不外露
    return {(rv["file_id"], fl["svg_name"]): fl.get(field) for rv in r.json()["reviews"] for fl in rv["result"]["floors"]}


def _row(fid, svg_dir, names, cad_state=None):
    floors = [{"label": n.split("-")[0], "svg_name": n, "findings": [], "notes": []} for n in names]
    return {"file_id": fid, "name": f"F{fid}.dxf", "status": "done", "error": None, "svg_dir": svg_dir,
            "result": {"floors": floors, "warnings": []}, "created_at": "t", "cad_state": cad_state}


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
    requeued = make_review_dir(c / "g", status={"state": "pending", "sheets": {}, "error": None})   # 重新處理後排隊：舊圖磚還在
    stale = make_review_dir(c / "h", status={"state": "done", "sheets": {NAME: "done"}, "error": None})
    leftover = make_review_dir(c / "i", status={"state": "rendering", "sheets": {}, "error": None}, meta=False)
    reprocessing = make_review_dir(c / "j", status={"state": "pending", "sheets": {}, "error": None})   # 舊圖磚還在
    got = _bundle(client, monkeypatch, [
        _row(1, str(done), [NAME], "done"), _row(2, str(rendering), [NAME], "rendering"), _row(3, str(failed), [NAME], "failed"),
        _row(4, str(partial), [NAME, "2F-28", "3F-29"], "rendering"), _row(5, str(broken), [NAME, "2F-28"], "done"),
        _row(6, str(old), [NAME], "pending"), _row(7, None, [NAME]), _row(8, str(outside), [NAME], "done"),
        _row(9, str(requeued), [NAME], "pending"), _row(10, str(stale), [NAME], "pending"),
        _row(11, str(leftover), [NAME]), _row(12, str(old), [NAME]), _row(13, str(reprocessing), [NAME])])
    assert got == {(1, NAME): "done", (2, NAME): "rendering", (3, NAME): "failed",
                   (4, NAME): "done", (4, "2F-28"): "failed", (4, "3F-29"): "rendering",
                   (5, NAME): "done", (5, "2F-28"): None,
                   (6, NAME): "pending",                  # 上線前的舊檔補排隊（還沒有狀態檔）
                   (7, NAME): None, (8, NAME): None,
                   (9, NAME): "pending", (10, NAME): "pending",   # 排隊中不拿舊圖磚（狀態檔沒清到也一樣）
                   (11, NAME): None,                      # 資料庫沒在畫、狀態檔停在畫圖中：過時的
                   (12, NAME): None,
                   (13, NAME): None}                      # 整個重新處理中（資料庫取消排隊）：不拿上一輪的圖磚


def test_review_bundle_cad_done_needs_meta(client, monkeypatch):
    # 狀態寫 done 但圖磚資訊不見了：不能叫前端去載
    rev = make_review_dir(client.cases, status={"state": "done", "sheets": {NAME: "done"}}, meta=False)
    assert _bundle(client, monkeypatch, [_row(1, str(rev), [NAME])]) == {(1, NAME): None}


def test_review_bundle_cad_queue_position(client, monkeypatch):
    # 原圖排隊中：前面還有幾個檔的原圖（只給數字）＋照最近畫圖的時間估多久；前面沒有別的就照一般說明；沒人在等不查
    rev = make_review_dir(client.cases, status={"state": "pending", "sheets": {}, "error": None})
    med, calls = {"file": 1.0, "cad": 300.0}, []
    monkeypatch.setattr(api.DS, "recent_seconds", lambda c: calls.append(1) or med)
    rows = lambda: [{**_row(1, str(rev), [NAME, "2F-28"], "pending"), "cad_ahead": 2},
                    {**_row(2, str(rev), [NAME], "pending"), "cad_ahead": 0}, {**_row(3, str(rev), [NAME], "done"), "cad_ahead": None}]
    wait = "前面還有 2 個檔的原圖要產生，約 10 分鐘後開始（依最近的產生時間估計；有檔案在處理時會再晚一些）"
    assert _bundle(client, monkeypatch, rows(), "cad_wait") == {(1, NAME): wait, (1, "2F-28"): wait, (2, NAME): None, (3, NAME): None}
    assert calls == [1]
    med["cad"] = None
    assert _bundle(client, monkeypatch, rows(), "cad_wait")[(1, NAME)] == "前面還有 2 個檔的原圖要產生；有檔案在處理時會再晚一些"
    calls.clear()
    assert set(_bundle(client, monkeypatch, [{**_row(2, str(rev), [NAME], "pending"), "cad_ahead": 0}], "cad_wait").values()) == {None}
    assert calls == []


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
    for s in ["顯示檢核標示", "原圖排隊中", "原圖產生中（大型圖", "原圖產生失敗，先顯示簡化圖", "/meta.json", ".overlay.json",
              "max_level", "tile_size", "transform", "rendered_at", "getTileUrl", "30000", "canvas-click"]:
        assert s in html, s
    # 疊圖不用 innerHTML 塞圖面文字
    script = html[html.index("// ---------- CAD 原樣檢視"):]
    script = script[:script.index("// ---------- CAD 原樣檢視結束")]
    assert "innerHTML" not in script


def test_workbench_cad_poll_survives_failures():
    # 30 秒重查失敗（網路斷、主機重啟）時要照排下一次，不能吞掉例外就停了
    html = api.WEB_WORKBENCH.read_text(encoding="utf-8")
    assert "loadReviews().catch(() => {})" not in html
    sched = html[html.index("function scheduleCad("):html.index("async function loadReviews(")]
    assert "30000" in sched and re.search(r"\.catch\(\(\) => \{[^}]*scheduleCad\(true\)", sched)
    # 重查結果一樣時走局部更新，不整頁重畫
    load = html[html.index("async function loadReviews("):html.index("function planHtml(")]
    assert "reviewsKey(d)" in load and "patchReviews(d)" in load and "JSON.stringify(d)" not in load


# ---------- 重查比對（工作台的純函式段落，用 node 跑；沒有 node 就略過） ----------

def _run_js(tmp_path, data: dict, body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，略過前端純函式測試")
    html = api.WEB_WORKBENCH.read_text(encoding="utf-8")
    funcs = html[html.index("// ---------- 重查比對（"):html.index("// ---------- 重查比對結束")]
    js = tmp_path / "t.js"
    js.write_text(funcs + "\nconst D = " + json.dumps(data, ensure_ascii=False) + ";\n"
                  "process.stdout.write(JSON.stringify((() => {" + body + "})()));\n", encoding="utf-8")
    r = subprocess.run([node, str(js)], capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8"))


def _rv(cad2="rendering", decisions=None, title="探測器涵蓋不足", laws=None):
    floors = [{"label": "1F", "svg_name": "1F-1", "cad": "done", "findings": [{"no": 1, "key": "k1", "title": title}]},
              {"label": "2F", "svg_name": "2F-2", "cad": cad2, "findings": [{"no": 1, "key": "k2", "title": "滅火器"}]}]
    return {"reviews": [{"file_id": 7, "name": "F.dxf", "status": "done", "error": None, "created_at": "t",
                         "result": {"building": None, "floors": floors, "warnings": []}},
                        {"file_id": 8, "name": "G.dxf", "status": "failed", "error": "轉檔失敗", "result": None, "created_at": "t"}],
            "laws": laws or {"L1": {"citation": "第 1 條", "text": "條文"}}, "context": {}, "decisions": decisions or {}}


def test_poll_key_ignores_decisions_and_cad(tmp_path):
    local = {"7": {"k1": {"decision": "accept", "note": None}}}                         # 按下接受後本機存的
    server = {"7": {"k1": {"decision": "accept", "note": "", "by": "amy", "at": "2026-10-05T10:00:00+08:00"}}}
    got = _run_js(tmp_path, {"a": _rv(decisions=local), "b": _rv("done", server), "c": _rv(title="改了"),
                             "d": _rv(laws={"L2": {"citation": "第 2 條", "text": ""}}), "e": _rv("failed")}, """
        const k = reviewsKey(D.a);
        return { same: k === reviewsKey(D.b), title: k === reviewsKey(D.c), laws: k === reviewsKey(D.d),
                 cad: cadChanges(D.a, D.b), cadFail: cadChanges(D.a, D.e), cadNone: cadChanges(D.a, D.a),
                 dec: decisionChanges(D.a.decisions, D.b.decisions), kept: D.a.reviews[0].result.floors[1].cad };""")
    # 只有審核結果（伺服器多帶 by、at）和原圖狀態不同：key 一樣 → 局部更新
    assert got["same"] is True and got["dec"] == []
    # 缺失內容或引用條文變了才整頁重畫
    assert got["title"] is False and got["laws"] is False
    # 原圖狀態變了的只有第 1 個檔案的第 2 層；比對不會改到原資料
    assert got["cad"] == [[0, 1]] and got["cadFail"] == [[0, 1]] and got["cadNone"] == [] and got["kept"] == "rendering"


def test_decision_changes_only_visible_ones(tmp_path):
    base = {"7": {"k1": {"decision": "accept", "note": "看過"}}}
    got = _run_js(tmp_path, {"base": base, "cases": {
        "same": {"7": {"k1": {"decision": "accept", "note": "看過", "by": "bob", "at": "x"}}},
        "other": {"7": {"k1": {"decision": "accept", "note": "看過"}, "k2": {"decision": "reject", "note": None}}},
        "undo": {},
        "note": {"7": {"k1": {"decision": "accept", "note": "改備註"}}},
        "flip": {"7": {"k1": {"decision": "reject", "note": "看過"}}},
        "file": {"7": {"k1": {"decision": "accept", "note": "看過"}}, "9": {"k9": {"decision": "accept", "note": ""}}},
    }}, "return Object.fromEntries(Object.entries(D.cases).map(([n, c]) => [n, decisionChanges(D.base, c)]));")
    assert got == {"same": [], "other": [["7", "k2"]], "undo": [["7", "k1"]], "note": [["7", "k1"]],
                   "flip": [["7", "k1"]], "file": [["9", "k9"]]}


def test_poll_key_ignores_cad_queue_text_but_patches_it(tmp_path):
    # 原圖排隊說明（前面幾個、約多久）變了：不整頁重畫（檢視器、打到一半的備註不動），只換那一層的圖區
    a, b = _rv("pending"), _rv("pending")
    a["reviews"][0]["result"]["floors"][1]["cad_wait"] = "前面還有 2 個檔的原圖要產生"
    b["reviews"][0]["result"]["floors"][1]["cad_wait"] = "前面還有 1 個檔的原圖要產生"
    got = _run_js(tmp_path, {"a": a, "b": b, "c": _rv("pending")}, """
        return { same: reviewsKey(D.a) === reviewsKey(D.b) && reviewsKey(D.a) === reviewsKey(D.c),
                 wait: cadChanges(D.a, D.b), gone: cadChanges(D.a, D.c), none: cadChanges(D.a, D.a) };""")
    assert got == {"same": True, "wait": [[0, 1]], "gone": [[0, 1]], "none": []}
    html = api.WEB_WORKBENCH.read_text(encoding="utf-8")
    note = html[html.index("function cadNote("):html.index("function setPlan(")]
    assert '"原圖排隊中：" + fl.cad_wait' in note and "esc(note)" in html[html.index("function planHtml("):]
