"""依場所判定應設設備：《各類場所消防安全設備設置標準》第 14～30-1 條。

輸入：場所類別（第 12 條代碼，審圖人員在工作台填）、各層樓地板面積（平面理解或面積計算表）、
地上層數、建築物高度、基地面積、無開口樓層。
輸出：每種設備「應設／未達門檻／無法判定」，附判定理由、法源節點、缺的資料。

只做本標準本文的門檻。下列情形不硬判，標「無法判定」並說明：
- 複合用途建築物（戊-1、戊-2）：第 6 條要各用途分開合計面積，需要各層用途面積。
- 條文限定特定機構（例：第 17 條第 9 款甲-6 的部分機構）。
- 需要圖上看不到的資料（居室有效通風面積、採光面積、倉庫樓層高度等）。
「無開口樓層」需審圖人員勾選；沒勾的樓層以非無開口樓層計（並在說明提醒）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REQUIRED, NOT_REQUIRED, UNKNOWN = "REQUIRED", "NOT_REQUIRED", "UNKNOWN"
STATUS_LABEL = {REQUIRED: "應設", NOT_REQUIRED: "未達門檻", UNKNOWN: "無法判定"}
L = "D0120029/"


@dataclass
class FloorArea:
    label: str
    level: int                  # 地上 1、2…；地下 -1、-2…
    area: float
    no_opening: bool = False


@dataclass
class Profile:
    occupancy: str | None
    floors: list[FloorArea]
    stories: int | None
    height: float | None = None
    site_area: float | None = None
    roof_area: float = 0.0      # 屋突層面積（只計入總樓地板面積）
    has_electrical: bool = False
    has_kitchen: bool = False
    rooms_over_100: list[str] = field(default_factory=list)
    ceiling_height: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def cls(self) -> str | None:
        return self.occupancy.split("-")[0] if self.occupancy else None

    @property
    def num(self) -> int | None:
        m = re.search(r"-(\d+)", self.occupancy or "")
        return int(m.group(1)) if m else None

    @property
    def total_area(self) -> float:
        return sum(f.area for f in self.floors) + self.roof_area

    @property
    def high_rise(self) -> bool | None:
        """高層建築物（建築技術規則）：高度 50 m 以上或 16 層以上。"""
        if (self.height is not None and self.height >= 50) or (self.stories or 0) >= 16:
            return True
        if self.height is None and (self.stories or 0) >= 11:
            return None                  # 11～15 層、高度未知：層高大時可能已達 50 m
        return False

    def is_(self, *codes: str) -> bool:
        """codes：「甲」（整類）或「甲-1」（單目）。"""
        return any(self.occupancy == c or (self.cls == c) for c in codes) if self.occupancy else False

    def special(self) -> list[FloorArea]:
        return [f for f in self.floors if f.level < 0 or f.no_opening]

    def max_floor(self, floors=None) -> float:
        fs = self.floors if floors is None else floors
        return max((f.area for f in fs), default=0.0)


@dataclass
class Requirement:
    key: str
    equipment: str
    kinds: tuple[str, ...]          # 對應圖面設備種類；空＝不在平面圖上檢查（例：蓄水池）
    status: str
    why: str
    law: list[str]
    floors: list[str] | None = None  # None＝全棟
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _fmt(v: float) -> str:
    return f"{v:,.0f}"


def _unknown_occ(key, name, kinds, law) -> Requirement:
    return Requirement(key, name, kinds, UNKNOWN, "尚未填寫場所類別（第 12 條）", law, missing=["場所類別（第 12 條第幾款第幾目）"])


def _composite_note() -> str:
    return "複合用途建築物依第 6 條以各目為單元分別合計樓地板面積，需各層用途面積才能判定"


def extinguisher(p: Profile) -> Requirement:
    name, kinds = "滅火器", ("extinguisher",)
    hits, whole = [], False
    if p.is_("甲", "戊-3", "乙-12", "戊-1"):
        hits.append(("甲類場所（含戊-1 中之甲類用途）、地下建築物或幼兒園", L + "14/1/1"))
        whole = True
    if p.is_("乙", "丙", "丁") and p.total_area >= 150:
        hits.append((f"乙、丙、丁類場所，總樓地板面積 {_fmt(p.total_area)} ㎡ ≥ 150 ㎡", L + "14/1/2"))
        whole = True
    sp = [f for f in p.special() if f.area >= 50]
    if sp:
        hits.append(("地下層或無開口樓層樓地板面積 ≥ 50 ㎡：" + "、".join(f.label for f in sp), L + "14/1/3"))
    if p.has_electrical:
        hits.append(("設有變壓器、配電盤等電氣設備（圖上有電氣室）", L + "14/1/4"))
        whole = True
    if p.has_kitchen:
        hits.append(("設有鍋爐房、廚房等大量使用火源處所", L + "14/1/5"))
        whole = True
    if hits:
        return Requirement("14", name, kinds, REQUIRED, "；".join(h for h, _ in hits), [l for _, l in hits],
                           floors=None if whole else [f.label for f in sp])
    if not p.occupancy:
        return _unknown_occ("14", name, kinds, [L + "14/1"])
    if p.is_("戊"):
        return Requirement("14", name, kinds, UNKNOWN, _composite_note(), [L + "14/1"], missing=["各層各用途樓地板面積"])
    return Requirement("14", name, kinds, NOT_REQUIRED, "未達第 14 條各款門檻", [L + "14/1"])


def indoor_hydrant(p: Profile) -> Requirement:
    name, kinds = "室內消防栓設備", ("hydrant",)
    if not p.occupancy:
        return _unknown_occ("15", name, kinds, [L + "15/1"])
    if p.is_("戊-1", "戊-2"):
        return Requirement("15", name, kinds, UNKNOWN, _composite_note(), [L + "15/1"], missing=["各層各用途樓地板面積"])
    hits, mx, whole = [], p.max_floor([f for f in p.floors if f.level > 0]), False
    st = p.stories or 0
    classroom_unknown = False
    if st and st <= 5:
        th = 300 if p.is_("甲-1") else 500
        if p.is_("乙-3") and th <= mx < 1400:
            classroom_unknown = True      # 學校教室 1,400 ㎡、同目其他用途（補習班等）500 ㎡
        elif p.is_("甲", "乙", "丙", "丁") and mx >= th:
            hits.append((f"五層以下建築物，最大一層樓地板面積 {_fmt(mx)} ㎡ ≥ {1400 if p.is_('乙-3') else th} ㎡", L + "15/1/1"))
            whole = True
    if st >= 6 and p.is_("甲", "乙", "丙", "丁") and mx >= 150:
        hits.append((f"六層以上建築物，最大一層樓地板面積 {_fmt(mx)} ㎡ ≥ 150 ㎡", L + "15/1/2"))
        whole = True
    if p.is_("戊-3") and p.total_area >= 150:
        hits.append((f"地下建築物總樓地板面積 {_fmt(p.total_area)} ㎡ ≥ 150 ㎡", L + "15/1/3"))
        whole = True
    th = 100 if p.is_("甲-1") else 150
    sp = [f for f in p.special() if f.area >= th]
    if sp and p.is_("甲", "乙", "丙", "丁"):
        hits.append((f"地下層或無開口樓層樓地板面積 ≥ {th} ㎡：" + "、".join(f.label for f in sp), L + "15/1/4"))
    if hits:
        return Requirement("15", name, kinds, REQUIRED, "；".join(h for h, _ in hits), [l for _, l in hits],
                           floors=None if whole else [f.label for f in sp],
                           notes=["設有自動撒水等滅火設備者，在其有效範圍內得免設（第 15 條第 2 項）"])
    if classroom_unknown:
        return Requirement("15", name, kinds, UNKNOWN, f"最大一層 {_fmt(mx)} ㎡：學校教室門檻 1,400 ㎡，補習班等其他乙-3 用途 500 ㎡",
                           [L + "15/1/1"], missing=["是否為學校教室"])
    if not st:
        return Requirement("15", name, kinds, UNKNOWN, "地上層數未知", [L + "15/1"], missing=["地上層數"])
    return Requirement("15", name, kinds, NOT_REQUIRED, "未達第 15 條各款門檻", [L + "15/1"])


def outdoor_hydrant(p: Profile) -> Requirement:
    name, kinds = "室外消防栓設備", ()          # 設於建築物外（配置圖），不在各層平面圖逐層比對
    if not p.occupancy:
        return _unknown_occ("16", name, kinds, [L + "16/1"])
    a12 = sum(f.area for f in p.floors if f.level in (1, 2))
    th = {"丁-1": (3000, "16/1/1"), "丁-2": (5000, "16/1/2"), "丁-3": (10000, "16/1/3")}.get(p.occupancy)
    if th and a12 >= th[0]:
        return Requirement("16", name, kinds, REQUIRED, f"{p.occupancy} 工作場所，第一層及第二層樓地板面積合計 {_fmt(a12)} ㎡ ≥ {_fmt(th[0])} ㎡",
                           [L + th[1]], notes=["面積含同基地儲存場所；設有自動撒水等設備者，在其有效範圍內得免設（第 16 條第 2 項）"])
    if th:
        return Requirement("16", name, kinds, NOT_REQUIRED, f"第一層及第二層合計 {_fmt(a12)} ㎡ < {_fmt(th[0])} ㎡", [L + th[1]],
                           notes=["同基地有其他建築物或儲存場所時，面積需合計（第 16 條）"])
    return Requirement("16", name, kinds, NOT_REQUIRED, "第 16 條限工作場所及木造建築群", [L + "16/1"],
                       notes=["同一基地內二棟以上木造或易燃構造建築物另依第 16 條第 1 項第 5 款判定"])


def sprinkler(p: Profile) -> Requirement:
    name, kinds = "自動撒水設備", ("sprinkler",)
    if not p.occupancy:
        return _unknown_occ("17", name, kinds, [L + "17/1"])
    st = p.stories or 0
    floors: dict[str, list[str]] = {}
    why, law, missing, notes = [], [], [], []

    def add(fs, reason, node):
        for f in fs:
            floors.setdefault(f.label, []).append(node)
        why.append(reason)
        law.append(L + node)

    above = [f for f in p.floors if f.level > 0]
    if st and st <= 10:
        if p.is_("甲-1"):
            tot = sum(f.area for f in above)
            if tot >= 300:
                add(above, f"十層以下建築物供甲-1 使用，樓地板面積合計 {_fmt(tot)} ㎡ ≥ 300 ㎡", "17/1/1")
        elif p.is_("甲", "乙-1"):
            big = [f for f in above if f.area >= 1500]
            if big:
                add(big, "十層以下建築物供甲類其他目或乙-1 使用，樓地板面積 ≥ 1,500 ㎡ 之樓層：" + "、".join(f.label for f in big), "17/1/1")
    hi = [f for f in p.floors if f.level >= 11 and f.area >= 100]
    if hi:
        add(hi, "十一層以上之樓層樓地板面積 ≥ 100 ㎡：" + "、".join(f.label for f in hi), "17/1/2")
    sp = [f for f in p.special() if f.area >= 1000]
    if sp and p.is_("甲"):
        add(sp, "地下層或無開口樓層供甲類使用，樓地板面積 ≥ 1,000 ㎡：" + "、".join(f.label for f in sp), "17/1/3")
    if st >= 11 and p.is_("甲", "戊-1"):
        add(p.floors, "十一層以上建築物供甲類或戊-1 使用", "17/1/4")
    if p.is_("戊-1"):
        notes.append("戊-1 中甲類場所面積合計達 3,000 ㎡ 時，供甲類之樓層應設（第 17 條第 1 項第 5 款），需各用途面積")
    if p.is_("乙-11"):
        big = [f for f in above if f.area >= 700]
        tall = [f for f in big if p.ceiling_height.get(f.label, 0) > 10]      # 天花板已超過 10 m，樓層高度必然超過
        if tall:
            add(tall, "高架儲存倉庫：樓層高度超過 10 m 且樓地板面積 ≥ 700 ㎡", "17/1/6")
        if len(tall) < len(big):
            missing.append("倉庫各層樓層高度（樓板至上層樓板，是否超過 10 m）")
    if p.is_("戊-3") and p.total_area >= 1000:
        add(p.floors, f"地下建築物總樓地板面積 {_fmt(p.total_area)} ㎡ ≥ 1,000 ㎡", "17/1/7")
    hr = p.high_rise
    if hr:
        add(p.floors, "高層建築物", "17/1/8")
    elif hr is None:
        missing.append("建築物高度（是否為高層建築物）")
    if p.is_("甲-6"):
        notes.append("甲-6 中長照、老福、護理等特定機構應全棟設置（第 17 條第 1 項第 9 款），請確認是否屬之")
    if floors:
        return Requirement("17", name, kinds, REQUIRED, "；".join(why), sorted(set(law)), floors=list(floors), missing=missing,
                           notes=notes + ["設有水霧、泡沫等滅火設備者，在其有效範圍內得免設（第 17 條第 2 項）"])
    if missing or p.is_("戊-1", "戊-2") or not st:
        return Requirement("17", name, kinds, UNKNOWN, "部分條件需補資料", [L + "17/1"], missing=missing + ([] if st else ["地上層數"]), notes=notes)
    return Requirement("17", name, kinds, NOT_REQUIRED, "未達第 17 條各款門檻", [L + "17/1"], notes=notes)


def fire_alarm(p: Profile) -> Requirement:
    name, kinds = "火警自動警報設備", ("detector", "flame_detector")
    if not p.occupancy:
        return _unknown_occ("19", name, kinds, [L + "19/1"])
    st = p.stories or 0
    mx = p.max_floor([f for f in p.floors if f.level > 0])
    hits = []
    if st and st <= 5:
        if p.is_("甲", "乙-12") and mx >= 300:
            hits.append((f"五層以下建築物供甲類或幼兒園使用，最大一層 {_fmt(mx)} ㎡ ≥ 300 ㎡", "19/1/1"))
        elif p.is_("乙", "丙", "丁") and not p.is_("乙-12") and mx >= 500:
            hits.append((f"五層以下建築物供乙至丁類使用，最大一層 {_fmt(mx)} ㎡ ≥ 500 ㎡", "19/1/1"))
    if 6 <= st <= 10 and mx >= 300:
        hits.append((f"六至十層建築物，最大一層 {_fmt(mx)} ㎡ ≥ 300 ㎡", "19/1/2"))
    if st >= 11:
        hits.append(("十一層以上建築物", "19/1/3"))
    th = 100 if p.is_("甲-1", "甲-5") else 300
    sp = [f for f in p.special() if f.area >= th]
    if sp:
        hits.append((f"地下層或無開口樓層樓地板面積 ≥ {th} ㎡：" + "、".join(f.label for f in sp), "19/1/4"))
    if p.is_("甲", "戊-3") and p.total_area >= 300:
        hits.append((f"甲類或地下建築物總樓地板面積 {_fmt(p.total_area)} ㎡ ≥ 300 ㎡", "19/1/6"))
    notes = []
    if p.is_("甲-6"):
        notes.append("甲-6 中長照、老福、護理等特定機構應設（第 19 條第 1 項第 7 款），請確認是否屬之")
    if hits:
        if not p.is_("甲", "戊-3") and p.high_rise is False:
            notes.append("已設密閉型撒水頭（標示溫度 75 °C 以下、動作 60 秒內）之自動撒水等設備者，在其有效範圍內得免設；"
                         "但應設置偵煙式探測器之場所不適用（第 19 條第 2 項）")
        whole = any(n != "19/1/4" for _, n in hits)
        return Requirement("19", name, kinds, REQUIRED, "；".join(h for h, _ in hits), sorted({L + n for _, n in hits}),
                           floors=None if whole else [f.label for f in sp], notes=notes)
    if p.is_("戊-1", "戊-2"):
        return Requirement("19", name, kinds, UNKNOWN, _composite_note(), [L + "19/1"], missing=["各層各用途樓地板面積"], notes=notes)
    if not st:
        return Requirement("19", name, kinds, UNKNOWN, "地上層數未知", [L + "19/1"], missing=["地上層數"])
    return Requirement("19", name, kinds, NOT_REQUIRED, "未達第 19 條各款門檻", [L + "19/1"], notes=notes)


def manual_alarm(p: Profile) -> Requirement:
    name, kinds = "手動報警設備", ("manual_alarm",)
    st = p.stories or 0
    mx = p.max_floor()
    if st >= 3 and mx >= 200:
        return Requirement("20", name, kinds, REQUIRED, f"三層以上建築物，最大一層 {_fmt(mx)} ㎡ ≥ 200 ㎡", [L + "20/1/1"])
    if p.is_("甲-3"):
        return Requirement("20", name, kinds, REQUIRED, "甲-3（旅館等）場所", [L + "20/1/2"])
    if not st:
        return Requirement("20", name, kinds, UNKNOWN, "地上層數未知", [L + "20/1"], missing=["地上層數"])
    if not p.occupancy:
        return _unknown_occ("20", name, kinds, [L + "20/1"])
    return Requirement("20", name, kinds, NOT_REQUIRED, "未達第 20 條門檻", [L + "20/1"])


def emergency_broadcast(alarm: Requirement, gas: Requirement | None = None) -> Requirement:
    name, kinds = "緊急廣播設備", ("speaker",)
    if alarm.status == REQUIRED:
        return Requirement("22", name, kinds, REQUIRED, "依第 19 條應設火警自動警報設備", [L + "22/1"])
    if gas is not None and gas.status == REQUIRED:
        return Requirement("22", name, kinds, REQUIRED, "依第 21 條應設瓦斯漏氣火警自動警報設備", [L + "22/1"])
    if alarm.status == UNKNOWN:
        return Requirement("22", name, kinds, UNKNOWN, "火警自動警報設備是否應設尚無法判定", [L + "22/1"], missing=alarm.missing)
    return Requirement("22", name, kinds, NOT_REQUIRED, "火警自動警報設備未達應設門檻（地下層瓦斯漏氣警報另依第 21 條）", [L + "22/1"])


def signs(p: Profile) -> list[Requirement]:
    out = []
    for key, name, kind, node in (("23-1", "出口標示燈", "exit_sign", "23/1/1"), ("23-2", "避難方向指示燈", "direction_light", "23/1/2")):
        if not p.occupancy:
            out.append(_unknown_occ(key, name, (kind,), [L + node]))
            continue
        if p.is_("甲", "乙-12", "戊-1", "戊-3"):
            out.append(Requirement(key, name, (kind,), REQUIRED, "甲類、幼兒園、戊-1 或地下建築物", [L + node],
                                   notes=["符合第 146 條免設條件（步行距離短、可直接看見出口等）之部分得免設"]))
            continue
        fs = [f for f in p.floors if f.level < 0 or f.no_opening or f.level >= 11]
        if fs:
            out.append(Requirement(key, name, (kind,), REQUIRED, "地下層、無開口樓層或十一層以上之樓層：" + "、".join(f.label for f in fs),
                                   [L + node], floors=[f.label for f in fs],
                                   notes=["符合第 146 條免設條件之部分得免設"]))
        else:
            out.append(Requirement(key, name, (kind,), NOT_REQUIRED, "非第 23 條所列場所或樓層（各類場所仍應設避難指標，第 23 條第 4 款）",
                                   [L + node, L + "23/1/4"]))
    return out


def emergency_lighting(p: Profile) -> Requirement:
    name, kinds = "緊急照明設備", ("emergency_light",)
    hits = []
    if p.is_("甲", "丙", "戊"):
        hits.append(("甲、丙、戊類場所之居室", "24/1/1"))
    elif p.is_("乙-1", "乙-2", "乙-4", "乙-5", "乙-6", "乙-8", "乙-9", "乙-12"):
        hits.append(("第 24 條第 2 款所列乙類場所之居室", "24/1/2"))
    if p.total_area >= 1000 and not p.is_("乙-3"):
        hits.append((f"總樓地板面積 {_fmt(p.total_area)} ㎡ ≥ 1,000 ㎡ 建築物之居室", "24/1/3"))
    if not hits and p.is_("乙-3"):
        return Requirement("24", name, kinds, UNKNOWN, "乙-3 中學校教室除外（第 24 條第 2、3 款），補習班、訓練班等仍應設",
                           [L + "24/1/2", L + "24/1/3"], missing=["是否為學校教室"])
    if not hits and p.is_("乙-7"):
        return Requirement("24", name, kinds, UNKNOWN, "乙-7 中僅住宿型精神復健機構列入第 24 條第 2 款；集合住宅之居室得免設（第 179 條）",
                           [L + "24/1/2", L + "179/1/3"], missing=["是否為住宿型精神復健機構"])
    if hits:
        return Requirement("24", name, kinds, REQUIRED, "；".join(h for h, _ in hits) + "，及自居室通達避難層之走廊、樓梯間、通道",
                           sorted({L + n for _, n in hits} | {L + "24/1/5"}),
                           notes=["洗手間、儲藏室、機械室、設有固定機械之工作場所部分、避難層 30 m 內可達屋外之居室等得免設（第 179 條）"])
    missing = ["各居室有效採光面積（未達樓地板面積 5% 者應設，第 24 條第 4 款）"]
    if not p.occupancy:
        return Requirement("24", name, kinds, UNKNOWN, "尚未填寫場所類別", [L + "24/1"], missing=["場所類別（第 12 條第幾款第幾目）"] + missing)
    return Requirement("24", name, kinds, UNKNOWN, "未達第 24 條第 1 至 3 款；第 4 款需採光資料", [L + "24/1/4"], missing=missing)


def standpipe(p: Profile) -> Requirement:
    name, kinds = "連結送水管", ("standpipe_outlet",)
    st = p.stories or 0
    if st >= 7 or (st in (5, 6) and p.total_area >= 6000):
        fl = [f.label for f in p.floors if f.level >= 3]
        why = "七層以上建築物" if st >= 7 else f"{st} 層建築物總樓地板面積 {_fmt(p.total_area)} ㎡ ≥ 6,000 ㎡"
        return Requirement("26", name, kinds, REQUIRED, why + "（出水口設於第三層以上各層，第 180 條）", [L + "26/1/1", L + "180/1/1"], floors=fl)
    if p.is_("戊-3") and p.total_area >= 1000:
        return Requirement("26", name, kinds, REQUIRED, "地下建築物總樓地板面積 ≥ 1,000 ㎡", [L + "26/1/2"])
    if not st:
        return Requirement("26", name, kinds, UNKNOWN, "地上層數未知", [L + "26/1"], missing=["地上層數"])
    return Requirement("26", name, kinds, NOT_REQUIRED, f"{st} 層建築物、總樓地板面積 {_fmt(p.total_area)} ㎡，未達門檻", [L + "26/1"])


def water_tank(p: Profile) -> Requirement:
    name = "消防專用蓄水池"
    mx = p.max_floor()
    if p.site_area is not None and p.site_area >= 20000 and mx >= 1500:
        return Requirement("27", name, (), REQUIRED, f"基地面積 {_fmt(p.site_area)} ㎡ ≥ 20,000 ㎡ 且最大一層 {_fmt(mx)} ㎡ ≥ 1,500 ㎡", [L + "27/1/1"])
    if p.height is not None and p.height > 31 and p.total_area >= 25000:
        return Requirement("27", name, (), REQUIRED, f"高度 {p.height} m > 31 m 且總樓地板面積 ≥ 25,000 ㎡", [L + "27/1/2"])
    missing = []
    if p.site_area is None and mx >= 1500:
        missing.append("基地面積")
    if p.height is None and p.total_area >= 25000:
        missing.append("建築物高度")
    notes = ["同一基地二棟以上建築物另依第 27 條第 3 款合計第一、二層面積判定"]
    if missing:
        return Requirement("27", name, (), UNKNOWN, "需補資料才能判定", [L + "27/1"], missing=missing, notes=notes)
    return Requirement("27", name, (), NOT_REQUIRED, "未達第 27 條門檻", [L + "27/1"], notes=notes)


def smoke_control(p: Profile) -> Requirement:
    name, kinds = "排煙設備", ("smoke_vent",)
    hits = []
    if p.is_("甲", "戊-3") and p.total_area >= 500:
        hits.append((f"甲類或地下建築物，樓地板面積合計 {_fmt(p.total_area)} ㎡ ≥ 500 ㎡", "28/1/1"))
    no = [f for f in p.floors if f.no_opening and f.area >= 1000]
    if no:
        hits.append(("樓地板面積 ≥ 1,000 ㎡ 之無開口樓層：" + "、".join(f.label for f in no), "28/1/3"))
    notes = ["舞臺 ≥ 500 ㎡（第 28 條第 1 項第 4 款）、特別安全梯或緊急昇降機間（第 5 款）另依建築技術規則判定",
             "樓梯間、昇降路、管道間、儲藏室、廁所等及第 190 條所列處所得免設"]
    missing = []
    if p.rooms_over_100:
        missing.append("樓地板面積 100 ㎡ 以上居室之天花板下 80 cm 內有效通風面積（未達 2% 應設）："
                       + "、".join(p.rooms_over_100[:8]) + ("…" if len(p.rooms_over_100) > 8 else ""))
    if hits:
        return Requirement("28", name, kinds, REQUIRED, "；".join(h for h, _ in hits), sorted({L + n for _, n in hits}),
                           floors=[f.label for f in no] if no and not p.is_("甲", "戊-3") else None, missing=missing, notes=notes)
    if missing:
        return Requirement("28", name, kinds, UNKNOWN, "100 ㎡ 以上居室是否應設排煙，需有效通風面積", [L + "28/1/2"], missing=missing, notes=notes)
    if not p.occupancy:
        return _unknown_occ("28", name, kinds, [L + "28/1"])
    return Requirement("28", name, kinds, NOT_REQUIRED, "未達第 28 條第 1 至 3 款", [L + "28/1"], notes=notes)


def emergency_outlet(p: Profile) -> Requirement:
    name, kinds = "緊急電源插座", ("emergency_outlet",)
    if (p.stories or 0) >= 11:
        return Requirement("29", name, kinds, REQUIRED, "十一層以上建築物之各樓層（每層任一處至插座水平距離 50 m 以下，第 191 條）",
                           [L + "29/1/1"])
    if p.is_("戊-3") and p.total_area >= 1000:
        return Requirement("29", name, kinds, REQUIRED, "地下建築物總樓地板面積 ≥ 1,000 ㎡", [L + "29/1/2"])
    return Requirement("29", name, kinds, NOT_REQUIRED, "未達第 29 條第 1、2 款（緊急昇降機間另依建築技術規則，第 3 款）", [L + "29/1"])


def gas_leak(p: Profile) -> Requirement:
    name, kinds = "瓦斯漏氣火警自動警報設備", ("gas_detector",)
    base = [f for f in p.floors if f.level < 0]
    ba = sum(f.area for f in base)
    if p.is_("甲") and ba >= 1000:
        return Requirement("21", name, kinds, REQUIRED, f"地下層供甲類使用，樓地板面積合計 {_fmt(ba)} ㎡ ≥ 1,000 ㎡",
                           [L + "21/1/1"], floors=[f.label for f in base], notes=["限使用瓦斯之場所"])
    if p.is_("戊-3") and p.total_area >= 1000:
        return Requirement("21", name, kinds, REQUIRED, "地下建築物總樓地板面積 ≥ 1,000 ㎡", [L + "21/1/3"], notes=["限使用瓦斯之場所"])
    if p.is_("戊-1") and ba >= 1000:
        return Requirement("21", name, kinds, UNKNOWN, "戊-1 地下層合計 ≥ 1,000 ㎡，需甲類用途面積是否 ≥ 500 ㎡",
                           [L + "21/1/2"], missing=["地下層甲類用途樓地板面積"])
    return Requirement("21", name, kinds, NOT_REQUIRED, "未達第 21 條門檻", [L + "21/1"])


def radio_aux(p: Profile) -> Requirement:
    name = "無線電通信輔助設備"
    base = [f for f in p.floors if f.level < 0]
    ba = sum(f.area for f in base)
    if base and p.height is not None and p.height >= 100:
        return Requirement("30", name, (), REQUIRED, f"樓高 {p.height} m ≥ 100 m 建築物之地下層", [L + "30/1/1"], floors=[f.label for f in base])
    if p.is_("戊-3") and p.total_area >= 1000:
        return Requirement("30", name, (), REQUIRED, "地下建築物總樓地板面積 ≥ 1,000 ㎡", [L + "30/1/2"])
    if len(base) >= 4 and ba >= 3000:
        return Requirement("30", name, (), REQUIRED, f"地下層 {len(base)} 層、合計 {_fmt(ba)} ㎡ ≥ 3,000 ㎡", [L + "30/1/3"],
                           floors=[f.label for f in base])
    if base and p.height is None:
        return Requirement("30", name, (), UNKNOWN, "有地下層，需建築物高度判定第 30 條第 1 款", [L + "30/1/1"], missing=["建築物高度"])
    return Requirement("30", name, (), NOT_REQUIRED, "未達第 30 條門檻", [L + "30/1"])


def evacuation_tools(p: Profile) -> Requirement:
    return Requirement("25", "避難器具", (), UNKNOWN, "依第 157 條選設表按場所與樓層判定（規則待建）", [L + "25", L + "157"],
                       notes=["十一層以上樓層及避難層除外"])


def disaster_control(p: Profile) -> Requirement:
    hr = p.high_rise
    if hr or p.total_area >= 50000 or (p.is_("戊-3") and p.total_area >= 1000):
        return Requirement("30-1", "防災監控系統綜合操作裝置", (), REQUIRED,
                           "高層建築物、總樓地板面積 ≥ 50,000 ㎡ 或地下建築物 ≥ 1,000 ㎡", [L + "30-1/1"])
    if hr is None:
        return Requirement("30-1", "防災監控系統綜合操作裝置", (), UNKNOWN, "是否為高層建築物需建築物高度", [L + "30-1/1/1"], missing=["建築物高度"])
    return Requirement("30-1", "防災監控系統綜合操作裝置", (), NOT_REQUIRED, "未達第 30-1 條門檻", [L + "30-1/1"])


def evaluate(p: Profile) -> list[Requirement]:
    alarm, gas = fire_alarm(p), gas_leak(p)
    out = [extinguisher(p), indoor_hydrant(p), outdoor_hydrant(p), sprinkler(p), special_suppression_note(), alarm, manual_alarm(p),
           gas, emergency_broadcast(alarm, gas), *signs(p), emergency_lighting(p), standpipe(p), water_tank(p), smoke_control(p),
           emergency_outlet(p), radio_aux(p), evacuation_tools(p), disaster_control(p)]
    return out


def special_suppression_note() -> Requirement:
    """第 18 條按房間判定（發電機室、廚房、電腦室、停車空間等達面積者），由逐層規則 special_suppression 檢查。"""
    return Requirement("18", "水霧、泡沫、二氧化碳、鹵化烴或乾粉滅火設備", (), UNKNOWN,
                       "依第 18 條附表按房間用途與面積判定，逐層列在各樓層缺失中", [L + "18/1"],
                       notes=["樓地板面積 300 ㎡ 以上之餐廳等，其廚房排油煙管及煙罩應設簡易自動滅火設備（第 18 條第 2 項）"])


# ── 由檢核結果組出建物資料 ─────────────────────────────────────────────

def _level(label: str) -> tuple[str, int | None]:
    """樓層代號 → (種類, 層)：('above', 3)、('mezz', 1)、('base', -2)、('roof', None)。"""
    if m := re.fullmatch(r"(\d+)F", label):
        return "above", int(m.group(1))
    if m := re.fullmatch(r"(\d+)MF", label):
        return "mezz", int(m.group(1))
    if m := re.fullmatch(r"B(\d+)", label):
        return "base", -int(m.group(1))
    return "roof", None


def build_profile(floors: list, ctx) -> Profile:
    """floors：engine 的 FloorResult 清單；ctx：checks.Context。夾層面積併入所在樓層。"""
    by_level: dict[int, FloorArea] = {}
    roof = 0.0
    notes = []
    has_elec = has_kitchen = False
    big_rooms = []
    seen = set()
    for fr in floors:
        fl = fr.floor
        lab = fl.label or ""
        if lab in seen:
            continue                     # 同一樓層分成幾張圖（各系統一張）：面積只算一次
        seen.add(lab)
        area = ctx.floor_area.get(lab, fl.area)
        kind, lv = _level(lab)
        has_elec |= any(r.kind == "electrical" for r in fl.rooms)
        has_kitchen |= any(r.kind == "kitchen" for r in fl.rooms)
        big_rooms += [f"{lab} {r.name}" for r in fl.rooms if r.kind in ("room", "kitchen") and r.area >= 100]
        if kind == "roof":
            roof += area
            continue
        main = f"{lv}F" if kind == "mezz" else lab
        fa = by_level.setdefault(lv, FloorArea(main, lv, 0.0, main in ctx.no_opening))
        fa.area += area
    fls = sorted(by_level.values(), key=lambda f: f.level)
    above = [f.level for f in fls if f.level > 0]
    stories = ctx.stories or (max(above) if above else None)
    if not ctx.stories and above:
        notes.append(f"地上層數以平面圖推定為 {stories} 層；若圖面未含全部樓層，請在檢核條件填寫")
    if not ctx.floor_area:
        notes.append("各層樓地板面積為平面圖判讀值（未標示的挑空可能被算入）；可在檢核條件以面積計算表數字覆寫")
    if not ctx.no_opening:
        notes.append("未勾選無開口樓層，以全部樓層皆非無開口樓層判定")
    return Profile(ctx.occupancy, fls, stories, ctx.height, ctx.site_area, roof, has_elec, has_kitchen,
                   big_rooms, dict(ctx.ceiling_height), notes)
