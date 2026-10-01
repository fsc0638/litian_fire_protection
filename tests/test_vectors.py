"""向量檢索：嵌入文字組成、增量重算規劃、權重覆寫、失敗時不影響其他路線。不呼叫 OpenAI、不連資料庫。"""

from contextlib import contextmanager
from types import SimpleNamespace as NS

from litian import api
from litian.lawdb import search as S
from litian.lawdb import vectors as V

BY = {
    "D0120029/12": {"node_id": "D0120029/12", "level": "article", "text": "全文……", "parent_id": None},
    "D0120029/12/1": {"node_id": "D0120029/12/1", "level": "paragraph", "text": "各類場所按用途分類如下：",
                      "parent_id": "D0120029/12"},
    "D0120029/12/1/1": {"node_id": "D0120029/12/1/1", "level": "item", "text": "一、甲類場所：",
                        "parent_id": "D0120029/12/1"},
    "D0120029/12/1/1/1": {"node_id": "D0120029/12/1/1/1", "level": "subitem", "parent_id": "D0120029/12/1/1",
                          "text": "（一）電影片映演場所、視聽歌唱場所（KTV等）", "citation": "設置標準第12條第1款第1目",
                          "chapter": "第二編 消防設計"},
}


def test_parent_texts_excludes_article():
    assert V.parent_texts(BY["D0120029/12/1/1/1"], BY) == ["各類場所按用途分類如下：", "一、甲類場所："]


def test_embed_text_composition():
    n = BY["D0120029/12/1/1/1"]
    t = V.embed_text(n, "各類場所消防安全設備設置標準", V.parent_texts(n, BY), "表格列")
    assert t.splitlines() == ["設置標準第12條第1款第1目｜各類場所消防安全設備設置標準", "第二編 消防設計",
                              "各類場所按用途分類如下： ＞ 一、甲類場所：", n["text"], "表格列"]
    assert len(V.embed_text({**n, "text": "字" * 9000}, "x", [])) == V.MAX_CHARS


def test_plan_only_recomputes_changed_and_removes_stale():
    inputs = {"a": "甲", "b": "乙", "c": "丙"}
    existing = {"a": V.text_hash("甲"), "b": V.text_hash("舊的乙"), "z": V.text_hash("已刪")}
    todo, stale = V.plan(inputs, existing)
    assert sorted(todo) == ["b", "c"] and stale == ["z"]


def test_vec_literal():
    assert V.vec_literal([0.1, -0.25]) == "[0.1000000,-0.2500000]"


def test_fuse_weight_override():
    routes = {"keyword": ["a", "b"], "vector": ["b", "c"]}
    assert [h.node_id for h in S.fuse(routes, [])][0] == "b"                       # 兩路都有 b
    only_kw = S.fuse(routes, [], {**S.ROUTE_WEIGHT, "vector": 0.0})
    assert [h.node_id for h in only_kw][:2] == ["a", "b"]


def test_vector_route_without_key_is_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert api._vector_route("KTV", None) == []


def test_vector_route_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def boom(q):
        raise TimeoutError("OpenAI 連不上")
    monkeypatch.setattr(api, "_embed_query", boom)
    assert api._vector_route("KTV", None) == []


def test_vector_route_passes_legend_intent_and_law(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(api, "_embed_query", lambda q: (0.1, 0.2))
    calls = []

    @contextmanager
    def conn():
        yield "CONN"
    monkeypatch.setattr(api, "pool", NS(connection=conn))
    monkeypatch.setattr(api.V, "search", lambda c, vec, pcode=None, allow_legend=False, k=20:
                        calls.append((c, vec, pcode, allow_legend)) or ["X"])
    assert api._vector_route("撒水頭的圖例", None) == ["X"]
    assert api._vector_route("消防法第6條", "D0120001") == ["X"]
    assert calls == [("CONN", [0.1, 0.2], None, True), ("CONN", [0.1, 0.2], "D0120001", False)]
