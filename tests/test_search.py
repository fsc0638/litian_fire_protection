"""檢索的純函式部分：條號解析、場所代碼、查詢正規化、RRF 合併。"""

import pytest

from litian.lawdb import search as S

EXISTING = {
    "D0120029/12", "D0120029/12/1", "D0120029/12/1/1", "D0120029/12/1/1/3",
    "D0120029/17", "D0120029/17/1", "D0120029/17/1/9", "D0120029/17/3",
    "D0120001/10", "D0120001/10/1", "D0120029/18-1", "D0120001/7", "D0120001/7/1",
}
exists = EXISTING.__contains__


@pytest.mark.parametrize("q,want", [
    ("第12條第1款第3目", ["D0120029/12/1/1/3"]),          # 單一項條文省略第1項
    ("設置標準第十二條第一款第三目", ["D0120029/12/1/1/3"]),
    ("第17條第3項", ["D0120029/17/3"]),
    ("第十七條第一項第九款", ["D0120029/17/1/9"]),
    ("消防法第10條", ["D0120001/10"]),
    ("§17", ["D0120029/17"]),
    ("第18條之1", ["D0120029/18-1"]),
    ("消防法第七條第一項", ["D0120001/7/1"]),
    ("工廠要不要裝撒水", []),
])
def test_structural(q, want):
    assert S.structural(q, exists) == want


@pytest.mark.parametrize("q,want", [
    ("甲-3", ["N3"]), ("甲－3", ["N3"]), ("甲類第3目", ["N3"]), ("甲類場所第三目", ["N3"]),
    ("甲類場所 3樓", []), ("乙類12", []),
])
def test_occupancy(q, want):
    assert S.occupancy(q, {"甲-3": "N3"}) == want


@pytest.mark.parametrize("q,want", [
    ("11樓以上要裝什麼", "十一層以上要裝什麼"),
    ("300平方公尺以上", "三百平方公尺以上"),
    ("1500㎡", "一千五百㎡"),
])
def test_normalize(q, want):
    assert S.normalize_query(q) == want


def test_fuse_pins_direct_hits_first():
    hits = S.fuse({"keyword": ["A", "B", "C"]}, pinned=["C"])
    assert [h.node_id for h in hits] == ["C", "A", "B"]
    assert hits[0].routes == ["direct", "keyword"]


def test_detect_law_prefers_longest_alias():
    assert S.detect_law("消防法施行細則第3條") == "D0120002"
    assert S.detect_law("消防法第3條") == "D0120001"
    assert S.detect_law("各類場所消防安全設備設置標準") == "D0120029"


OCC_ROWS = [
    {"code": "甲-1", "text": "電影片映演場所（戲院、電影院）、歌廳、視聽歌唱場所（KTV等）、酒吧。"},
    {"code": "甲-5", "text": "餐廳、飲食店、咖啡廳、茶藝館。"},
    {"code": "乙-12", "text": "幼兒園。"},
]
CODES = ["甲-1", "甲-5", "乙-12"]


@pytest.mark.parametrize("q,want", [
    ("哪些場所應設置滅火器", "場所應設置滅火器"),
    ("KTV要不要裝自動撒水", "KTV 設置自動撒水"),
    ("大樓幾層以上要裝緊急電源插座？", "大樓 層以上設置緊急電源插座"),
    ("11樓以上需要排煙嗎", "十一層以上 排煙"),
    ("健身休閒中心的場所分類", "健身休閒中心 場所分類"),
    ("餐廳一定要放滅火器嗎", "餐廳 設置滅火器"),
    ("一一九火災通報裝置", "一一九火災通報裝置"),            # 「裝置」不可被改寫
    ("放映室要設滅火器", "放映室設置滅火器"),                # 「放映」不可被改寫；「要設」→「設置」
    ("醫院要不要裝119通報裝置", "醫院 設置一一九通報裝置"),    # 119 不可變成「一百十九」
    ("醫院療養院是甲類第幾目", "醫院療養院 甲類"),
    ("地下建築物屬於哪一類場所", "地下建築物 場所"),
    ("誰可以做消防安全設備的設計和監造", "消防安全設備 設計 監造"),
])
def test_clean_query(q, want):
    assert S.clean_query(q) == want


def test_detect_places_uses_article12_terms_and_synonyms():
    terms = S.place_terms(OCC_ROWS)
    codes, rest = S.detect_places("KTV要不要裝自動撒水", terms, CODES)
    assert codes == {"甲-1"} and "KTV" not in rest
    codes, _ = S.detect_places("卡拉OK要裝撒水嗎", terms, CODES)          # 同義詞
    assert codes == {"甲-1"}
    codes, _ = S.detect_places("幼稚園火警", terms, CODES)
    assert codes == {"乙-12"}
    codes, _ = S.detect_places("甲類場所要設滅火器", terms, CODES)          # 類別字面
    assert codes == {"甲-1", "甲-5"}


def test_occupancy_of_node():
    occ_by_node = {"D0120029/12/1/1/1": "甲-1"}
    assert S.occupancy_of("…第十二條第一款第一目…", ["D0120029/12/1/1/1"], occ_by_node, CODES) == ["甲-1"]
    assert S.occupancy_of("…第十二條第一款…", ["D0120029/12/1/1"], occ_by_node, CODES) == ["甲-1", "甲-5"]
    assert S.occupancy_of("一、甲類場所、地下建築物、幼兒園。", [], occ_by_node, CODES) == ["甲-1", "甲-5"]
    assert S.occupancy_of("二、總樓地板面積在一百五十平方公尺以上之乙、丙、丁類場所。", [], {}, CODES) == ["乙-12"]
