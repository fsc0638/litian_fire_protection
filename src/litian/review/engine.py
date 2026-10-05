"""一份圖檔的檢核流程：DXF → 各樓層平面理解 → 設備辨識 → 挑空投影 → 逐條規則 → 應設判定 → 缺失。

命令列（開發與驗收用）：python -m litian.review.engine <in.dxf> <out.json> [--svg 資料夾]
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, mapping

from litian.drawing import ir as IR
from litian.plan import floor as F
from litian.plan import geometry as G
from litian.review import checks as K
from litian.review import coverage as C
from litian.review import equipment as E
from litian.review import escape as ESC
from litian.review import piping as PIPE
from litian.review import rescue as RES
from litian.review import required as RQ
from litian.review import stack as ST

EQUIP_MARGIN = 2.0      # 外框外 2 m 內的設備（送水口、壁掛）仍算這層；更遠的多半是圖例表


@dataclass
class FloorResult:
    sheet: int
    number: str | None
    title: str
    floor: F.Floor
    equipment: list[E.Equipment]
    findings: list[K.Finding]
    notes: list[K.Note]
    outside: int = 0
    projected: list[E.Equipment] = field(default_factory=list)   # 上層挑空投影來的設備：只參與檢核，不計入設備數量


@dataclass
class Result:
    floors: list[FloorResult] = field(default_factory=list)
    profile: RQ.Profile | None = None
    requirements: list[RQ.Requirement] = field(default_factory=list)
    building_findings: list[K.Finding] = field(default_factory=list)
    building_notes: list[K.Note] = field(default_factory=list)
    unknown_blocks: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


ALL_RULES = K.RULES + ESC.RULES + RES.RULES + PIPE.RULES
WALK_KINDS = {"extinguisher", "direction_light", "emergency_light"}      # 這些規則要算步行距離


def review_floor(fl: F.Floor, eq: list[E.Equipment], ctx: K.Context):
    findings, notes = [], []
    need_grid = any(k in WALK_KINDS for e in eq for k in e.kinds) and not fl.walkable.is_empty
    grid = C.WalkGrid(fl.walkable) if need_grid else None
    for rid, _title, fn in ALL_RULES:
        if fn is K.extinguisher_walk and grid is None:
            continue
        f, n = fn(fl, eq, ctx, grid=grid)
        findings.extend(f)
        notes.extend(n)
    sort_findings(findings)
    return findings, notes


def sort_findings(findings: list[K.Finding]) -> None:
    order = {K.RED: 0, K.ORANGE: 1, K.YELLOW: 2, K.BLUE: 3}
    findings.sort(key=lambda x: (order[x.severity], -(x.area or 0)))


def presence_findings(res: Result) -> None:
    """應設設備（第 14～30-1 條）vs 圖面：應設卻整層沒有該設備 → 缺失。"""
    plan_floors = [fr for fr in res.floors if RQ._level(fr.floor.label or "")[0] in ("above", "base")]
    if not plan_floors:
        return
    required = [r for r in res.requirements if r.status == RQ.REQUIRED and r.kinds]
    if not any(fr.equipment for fr in res.floors):
        if required:
            res.building_findings.append(K.Finding(
                "REQ", K.YELLOW, "資料不足", "全棟", "圖面未認出任何消防設備，無法比對應設設備",
                "依場所判定應設：" + "、".join(r.equipment for r in required) + "；但圖上沒有認得的消防設備符號"
                "（可能上傳的是建築圖，或設備圖塊名稱不在圖例字典中）",
                "上傳消防設備平面圖；若已上傳，請確認設備圖塊名稱並補進圖塊字典",
                sorted({law for r in required for law in r.law}), missing=["消防設備平面圖"]))
        return
    # 同一樓層常分成幾張圖（例：室內栓火警、滅火器避難廣播、排煙各一張）：設備合併看，缺失記在該層第一張圖
    by_label: dict[str, list[FloorResult]] = {}
    for fr in plan_floors:
        by_label.setdefault(fr.floor.label, []).append(fr)
    for r in required:
        targets = [frs for lab, frs in by_label.items() if r.floors is None or lab in r.floors]
        for frs in targets:
            fr = frs[0]
            eqs = [e for x in frs for e in x.equipment]
            if any(k in e.kinds for e in eqs for k in r.kinds):
                continue
            sev, extra, law = K.RED, "", list(r.law)
            has_spk = any("sprinkler" in e.kinds for e in eqs)
            if r.key == "15" and has_spk:
                sev, extra = K.ORANGE, "；本層設有自動撒水設備，若在其有效範圍內得免設（第 15 條第 2 項），請確認"
                law.append("D0120029/15/2")
            if r.key == "19" and has_spk and any("第 19 條第 2 項" in n for n in r.notes):
                sev, extra = K.ORANGE, "；本層設有自動撒水設備，符合條件者在其有效範圍內得免設（第 19 條第 2 項），請確認"
                law.append("D0120029/19/2")
            fr.findings.append(K.Finding(
                f"REQ-{r.key}", sev, "未設置", fr.floor.label or "", f"依規定應設{r.equipment}，本層圖上未見",
                f"判定理由：{r.why}{extra}", f"於本層配置{r.equipment}，並依相關設置規定檢討位置與數量",
                law, metrics={"equipment": r.equipment}))
    for fr in res.floors:
        sort_findings(fr.findings)


def _name(fr: FloorResult) -> str:
    return fr.number or fr.title


def project_voids(res: Result, ctx: K.Context, aligner: ST.Aligner | None = None) -> None:
    """上層挑空範圍內（屋頂板下）、屋突層房間外的探測器，投影到下層實際保護的房間（見 stack.py）。"""
    projs, warns = ST.project_detectors([fr.floor for fr in res.floors], [_name(fr) for fr in res.floors],
                                        [fr.equipment for fr in res.floors], ctx.stories, aligner)
    res.warnings.extend(warns)
    for p in projs:
        fr, src = res.floors[p.target], res.floors[p.source]
        fr.projected.extend(p.equipment)
        kinds = Counter(next(E.KIND_LABEL[k] for k in e.kinds if k in ST.DETECT_KINDS) for e in p.equipment)
        what = "、".join(f"{n} 個{k}" for k, n in kinds.items())
        where = f"{src.floor.label} 屋頂層房間外" if p.roof else f"{src.floor.label} 挑空範圍內"
        if p.levels >= 2:
            mount = (f"裝在{'屋頂板' if p.roof else '上層樓板'}下，裝置面自本層樓地板起約 {p.levels} 層樓高、高於本層天花板，"
                     "已併入本層探測器檢核；但逐房的數量、種類是依本層天花板高度條件檢討，實際應依裝置面高度確認"
                     "（第 114 條，同一室內高度不同以平均高度計），需確認者另列 DET-114")
            law = ["D0120029/114/1"]
        else:
            mount, law = "裝在本層大空間上方的屋頂板下（即本層天花板），已併入本層探測器檢核", []
        fr.notes.append(K.Note("DET-120", f"{_name(src)} 圖上 {where}的 {what}{mount}；"
                                          f"兩圖以共同的電梯、管道間、樓梯 {p.shift.anchors} 處對位（位移 dx {p.shift.dx:+.2f} m、"
                                          f"dy {p.shift.dy:+.2f} m）。這些探測器畫在上層圖，不計入本圖設備數量", law))


SKIP_HEIGHT = ("void", "outdoor", "elevator", "shaft", "stair", "toilet")   # 逐房探測器規則不檢討的房間


def void_heights(res: Result, ctx: K.Context) -> None:
    """DET-114：挑空投影來的探測器裝在上層樓板（屋頂板）下，裝置面比本層天花板高（第 114 條依裝置面高度選種類、
    有效探測範圍也隨高度變小），逐房規則以本層高度條件檢討的結果可能偏寬。裝置面兩層樓以上高的房間列需確認，
    並以 4 m 以上（有建築物高度時以估算高度）試算需設數量；該房已列不符（紅色）的只在其說明補上高度前提。
    唯一不另列的情形（高度再高也藏不住缺失）：本層未填天花板高度（逐房規則已含 4～8 m 的有效範圍）、房內全是
    偵煙式且在可能的高度都適用（一種：4～20 m 有效範圍相同、20 m 以上得免設；二種：估算裝置面未達 15 m），
    而且數量達到試算需設數。"""
    stories = ctx.stories or (res.profile.stories if res.profile else None)
    storey = ctx.height / stories if ctx.height and stories else None
    for fr in res.floors:
        fl = fr.floor
        rooms = {r.id: r for r in fl.rooms}
        groups: dict[int, list[E.Equipment]] = {}
        for e in fr.projected:
            if "detector" in e.kinds and e.spec.get("projected_levels", 1) >= 2 and (r := fl.room_near(e.x, e.y)) is not None:
                groups.setdefault(r.id, []).append(e)
        h = ctx.ceiling_height.get(fl.label or "")
        changed = False
        for rid, pe in groups.items():
            room = rooms[rid]
            if room.kind in SKIP_HEIGHT or room.area < 2:
                continue
            ids = {id(e) for e in pe}
            dets = [e for e in fr.equipment + fr.projected if "detector" in e.kinds
                    and (id(e) in ids or room.polygon.covers(Point(e.x, e.y)))]
            kinds = Counter((e.spec["detector_type"], e.spec.get("detector_class")) for e in dets if "detector_type" in e.spec)
            heat = any(t != "偵煙式" for t, _ in kinds)
            levels = sorted({e.spec["projected_levels"] for e in pe})
            est_lo = storey * levels[0] if storey else None
            # 試算：房內數量最多的種類，裝置面取估算高度（無建築物高度時以 4 m 以上計）
            need, trial = None, ""
            hdesc = (f"以建築物高度 {K._fmt(ctx.height)} m ÷ {stories} 層估算裝置面約 {K._fmt(est_lo)} m" if est_lo is not None
                     else "以裝置面 4 m 以上（至少兩層樓高）試算")
            if kinds:
                (dtype, dclass), _ = max(kinds.items(), key=lambda kv: kv[1])
                band = K.height_band(est_lo) if est_lo is not None else "4to8"
                fp = ctx.fireproof if ctx.fireproof is not None else fl.fireproof
                effs = [K._eff_area(dtype, dclass, band, f) for f in ([fp] if fp is not None else [True, False])]
                if band == "ge20":
                    trial = f"{hdesc}，達 20 m 以上：局限型探測器不適用（得免設或改設火焰式等，第 114、116 條）"
                elif None in effs:
                    trial = f"{hdesc}，{dtype}局限型{dclass}種在此高度不得使用（第 114 條）"
                else:
                    need = math.ceil(room.area / min(effs))
                    trial = (f"{hdesc}，{dtype}局限型{dclass}種有效探測範圍 {K._fmt(min(effs))} ㎡，"
                             f"約 {K._fmt(room.area)} ㎡ 至少需 {need} 個，現有 {len(dets)} 個（含投影 {len(pe)} 個）")
                if est_lo is None:
                    trial += "；裝置面達 8 m 以上時熱式探測器不得使用、達 15 m 以上時偵煙式只有一種可用"
            valid = kinds and all(t == "偵煙式" and (c == "1" or (storey is not None and storey * levels[-1] < 15))
                                  for t, c in kinds)
            if h is None and valid and need is not None and len(dets) >= need:
                continue
            src = Counter((e.spec["projected_from"], e.spec["projected_levels"]) for e in pe)
            head = (f"{room.name} 有 {len(pe)} 個探測器取自上層圖的挑空範圍（"
                    + "、".join(f"{s} 圖 {n} 個，約 {lv} 層樓高" for (s, lv), n in sorted(src.items()))
                    + "），裝在上層樓板（屋頂板）下，實際裝置面高於本層"
                    + (f"設定的天花板高度 {K._fmt(h)} m" if h is not None else "天花板（本層未填天花板高度）")
                    + "；本系統以本層高度條件檢討的數量、種類僅供參考")
            reds = [f for f in fr.findings if f.rule == "DET-120" and f.geom is room.polygon and f.severity == K.RED]
            if reds:
                for f in reds:
                    f.why += f"；另：{head}，實際所需數量可能更多" + (f"（{trial}）" if trial else "") + "，請依實際裝置面高度確認"
                    f.law = f.law + [x for x in ["D0120029/114/1"] if x not in f.law]
                continue
            count_law = "D0120029/122/1/4" if kinds and not heat else "D0120029/120/1/2"
            short = need is not None and len(dets) < need
            fr.findings.append(K.Finding(
                "DET-114", K.ORANGE, "需確認", fl.label or "",
                f"{room.name} 探測器可能不足（裝在上層樓板下，試算至少需 {need} 個，現有 {len(dets)} 個）" if short
                else f"{room.name} 的探測器裝在上層樓板下，需依實際裝置面高度確認",
                f"{head}。" + (f"{trial}。" if trial else "")
                + "請依實際裝置面高度（同一室內天花板或屋頂板高度不同時以平均高度計）確認探測器種類與數量",
                "以剖面圖確認挑空部分的裝置面高度，依第 114 條選用適用的探測器種類，並依該高度的有效探測範圍檢討數量",
                ["D0120029/114/1", count_law], rooms=[room.name], area=room.area, geom=room.polygon,
                metrics={"have": len(dets), "projected": len(pe), "need_est": need, "levels": levels}))
            changed = True
        if changed:
            sort_findings(fr.findings)


SIGNS = {"EXIT-146-3": ("23-1", "出口標示燈"), "DIR-146-3": ("23-2", "避難方向指示燈")}


def voluntary_signs(res: Result, ctx: K.Context) -> None:
    """依第 23 條非應設的出口標示燈、避難方向指示燈（自主設置）：位置、涵蓋缺失改為建議（法規解讀設定 voluntary_signs_note）。
    整棟未達門檻 → 各層都改；只有部分樓層應設（地下層、無開口樓層、十一層以上）→ 其他地上樓層改。
    說明寫明：判定前提（未勾選無開口樓層時視為全部非無開口）、另一種讀法（自主設置仍應符合第 146 條之 3），
    方向指示燈另提醒第 23 條第 4 款的避難指標義務（系統不辨識避難指標）。"""
    if not ctx.rule("voluntary_signs_note"):
        return
    req = {r.key: r for r in res.requirements}
    for fr in res.floors:
        kind, lv = RQ._level(fr.floor.label or "")
        main = f"{lv}F" if kind == "mezz" else fr.floor.label
        changed = False
        for f in fr.findings:
            if f.rule not in SIGNS or f.severity == K.BLUE or (r := req.get(SIGNS[f.rule][0])) is None:
                continue
            name = SIGNS[f.rule][1]
            if r.status == RQ.NOT_REQUIRED:
                scope = "本建物"
            elif r.status == RQ.REQUIRED and r.floors is not None and kind in ("above", "mezz") and main not in r.floors:
                scope = f"本層（應設樓層：{'、'.join(r.floors)}）"
            else:
                continue
            f.severity, changed = K.BLUE, True
            why = f"；依第 23 條{scope}非應設{name}（自主設置），檢討結果僅供參考"
            if not ctx.no_opening:
                why += "（未勾選無開口樓層，以全部樓層皆非無開口樓層判定；若本層屬無開口樓層即為應設，應依第 146 條之 3 改善）"
            law = list(r.law[:1])
            if f.rule == "DIR-146-3":
                why += ("；不在避難方向指示燈有效範圍內的走廊、通道，依第 23 條第 4 款仍應設避難指標"
                        "（走廊、通道任一點至指標步行距離 7.5 m 以下，第 153 條第 2 款；圖上未辨識避難指標，請確認）")
                law += ["D0120029/23/1/4", "D0120029/153/1/2"]
            why += "；另一種讀法：若認定自主設置者亦應符合第 146 條之 3 的位置規定，本項仍為缺失"
            f.why += why
            f.law = f.law + [x for x in law if x not in f.law]
        if changed:
            sort_findings(fr.findings)


STAIR_LETTER = re.compile(r"([A-Z])梯")
STAIR_VERTICAL = 15.0        # 第 133 條第 2 款第 5 目：樓梯垂直每 15 m 至少一個 L 級揚聲器


def _stair_name(room: F.Room) -> tuple[str, bool]:
    """樓梯配對用名稱：有「A梯」「(C梯)」「D梯_安全梯」等字母的取字母，否則取完整名稱。回傳（名稱, 是否有字母）。"""
    letters = sorted({m for s in room.labels for m in STAIR_LETTER.findall(s)})
    if letters:
        return "、".join(letters) + "梯", True
    pat = dict(F.ROOM_KINDS)["stair"]
    names = [re.sub(r"[\s()（）]", "", s) for s in room.labels if re.search(pat, s)]
    return (names[0] if names else room.name), False


def _elevation(label: str, stories: int) -> float:
    """樓層代號 → 第幾層樓板（1F＝0；夾層＝半層；屋突 R1F（RF）＝地上層數）。"""
    kind, lv = RQ._level(label)
    if kind == "above":
        return lv - 1
    if kind == "mezz":
        return lv - 0.5
    if kind == "base":
        return lv
    m = re.fullmatch(r"R(\d+)F", label)
    return stories + (int(m.group(1)) if m else 1) - 1


def _stairs(res: Result, stories: int, aligner: ST.Aligner) -> list[dict]:
    """各座樓梯（跨樓層配對）＋各層樓梯間內的揚聲器數。每個樓層代號取揚聲器最多的一張圖（同層各圖平面相同）。
    配對：字母相同（「A梯」「(A梯)」）、或完整名稱相同（同層有兩座以上同名的不配，例：屋突層好幾座「梯間」）；
    上下相鄰兩層對位得出時，位置重疊的樓梯間也算同一座（字母不同的不併）。配不起來的各自列出。"""
    pick: dict[str, int] = {}
    for i, fr in enumerate(res.floors):
        n = sum("speaker" in e.kinds for e in fr.equipment)
        lab = fr.floor.label or ""
        if lab not in pick or n > sum("speaker" in e.kinds for e in res.floors[pick[lab]].equipment):
            pick[lab] = i
    nodes = []                                      # (樓層代號, 圖索引, 房間, 揚聲器數, 名稱, 有字母)
    for lab, i in pick.items():
        fl = res.floors[i].floor
        cnt = Counter()
        for e in res.floors[i].equipment:
            if "speaker" in e.kinds and (r := fl.room_near(e.x, e.y)) is not None and r.kind == "stair":
                cnt[r.id] += 1
        nodes += [(lab, i, r, cnt[r.id], *_stair_name(r)) for r in fl.rooms if r.kind == "stair"]
    parent = list(range(len(nodes)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        la = {nodes[k][4] for k in range(len(nodes)) if find(k) == find(a) and nodes[k][5]}
        lb = {nodes[k][4] for k in range(len(nodes)) if find(k) == find(b) and nodes[k][5]}
        if not (la and lb and la != lb):            # 兩邊都有字母且不同：不同座樓梯
            parent[find(a)] = find(b)

    same_floor = Counter((n[0], n[4]) for n in nodes)
    first: dict[str, int] = {}
    for k, n in enumerate(nodes):
        if n[5] or same_floor[(n[0], n[4])] == 1:
            if n[4] in first:
                union(k, first[n[4]])
            else:
                first[n[4]] = k
    order = sorted(pick, key=lambda lab: _elevation(lab, stories))
    for lo, hi in zip(order, order[1:]):
        if _elevation(hi, stories) - _elevation(lo, stories) > 1:
            continue                                # 中間樓層沒有圖：不憑位置配對
        sh = aligner(pick[hi], pick[lo])
        if sh is None:
            continue
        for a, na in enumerate(nodes):
            if na[0] != hi:
                continue
            ca = na[2].polygon.centroid
            for b, nb in enumerate(nodes):
                if nb[0] != lo:
                    continue
                cb = nb[2].polygon.centroid
                if (nb[2].polygon.buffer(0.5).covers(Point(ca.x + sh.dx, ca.y + sh.dy))
                        or na[2].polygon.buffer(0.5).covers(Point(cb.x - sh.dx, cb.y - sh.dy))):
                    union(a, b)
    groups: dict[int, list] = {}
    for k, n in enumerate(nodes):
        groups.setdefault(find(k), []).append(n)
    out = []
    for members in groups.values():
        letters = sorted({n[4] for n in members if n[5]})
        name = "／".join(letters) if letters else Counter(n[4] for n in members).most_common(1)[0][0]
        members.sort(key=lambda n: _elevation(n[0], stories))
        floors: dict[str, int] = {}
        for n in members:
            floors[n[0]] = floors.get(n[0], 0) + n[3]
        out.append({"name": name, "floors": floors, "rooms": [f"{n[0]} {n[2].name}" for n in members],
                    "ids": "、".join(f"{n[0]} #{n[2].id}" for n in members),
                    "letters": {m for n in members for s in n[2].labels for m in STAIR_LETTER.findall(s)}})
    dup = Counter(g["name"] for g in out)
    for g in out:
        if dup[g["name"]] > 1:
            g["name"] += f"（{g['ids']}）"          # 同名配不起來的（例：屋突層幾座「梯間」）以樓層＋房間編號區分
    out.sort(key=lambda g: g["name"])
    return out


def stair_speakers(res: Result, ctx: K.Context, aligner: ST.Aligner | None = None) -> None:
    """SPKR-133-5：樓梯間的揚聲器依垂直距離每 15 m 至少一個 L 級（第 133 條第 2 款第 5 目），不套水平 10 m。
    樓高未知時只列各座樓梯各層有無揚聲器（資料不足）。法規解讀設定 stair_speaker_vertical。"""
    if not ctx.rule("stair_speaker_vertical") or not any("speaker" in e.kinds for fr in res.floors for e in fr.equipment):
        return
    stories = ctx.stories or (res.profile.stories if res.profile else None)
    stairs = _stairs(res, stories or 1, aligner or ST.Aligner([fr.floor for fr in res.floors]))
    if not stairs:
        return
    law = ["D0120029/133/1/2/5"]
    # 樓梯名稱只出現在大空間裡（樓梯沒有圍成獨立房間）：無法檢討，明白列出請人工確認
    named = {m for g in stairs for m in g["letters"]}
    loose = sorted({m for fr in res.floors for r in fr.floor.rooms if r.kind != "stair"
                    for m in STAIR_LETTER.findall(" ".join(r.labels))} - named)
    if loose:
        res.building_notes.append(K.Note("SPKR-133-5", "、".join(f"{m}梯" for m in loose) + " 只標示在其他房間內（樓梯沒有圍成獨立房間），"
                                         "未檢討其樓梯間揚聲器（垂直每 15 m 一個），請人工確認", law))
    listing = "；".join(f"{g['name']}：" + "、".join(f"{lab} {n} 個" if n else f"{lab} 無" for lab, n in g["floors"].items())
                       for g in stairs)
    metrics = {"stairs": {g["name"]: g["floors"] for g in stairs}}
    if ctx.height is None or not stories:
        res.building_findings.append(K.Finding(
            "SPKR-133-5", K.YELLOW, "資料不足", "全棟", "樓梯間揚聲器（垂直每 15 m 一個）需樓高資料才能判定",
            "揚聲器設於樓梯時，至少垂直距離每 15 m 設一個 L 級揚聲器（樓梯間不套各層水平 10 m）；"
            f"各座樓梯各層樓梯間內的揚聲器：{listing}。樓高未知，無法換算各座樓梯的垂直距離"
            + (f"；另 {'、'.join(f'{m}梯' for m in loose)} 沒有圍成樓梯間，未列入" if loose else ""),
            "補填建築物高度（或各層樓高）後重新檢核；樓梯間依垂直每 15 m 至少一個 L 級揚聲器配置",
            law, missing=["各層樓高（或建築物高度）"], metrics=metrics))
        return
    h = ctx.height / stories
    drawn = sorted({fr.floor.label or "" for fr in res.floors}, key=lambda lab: _elevation(lab, stories))
    for g in stairs:
        zs = {lab: _elevation(lab, stories) * h for lab in g["floors"]}
        span = max(zs.values()) - min(zs.values())
        need = max(1, math.ceil(span / STAIR_VERTICAL - 1e-9))
        have = sum(g["floors"].values())
        lit = sorted(zs[lab] for lab, n in g["floors"].items() if n)
        gaps = [b - a for a, b in zip([min(zs.values())] + lit, lit + [max(zs.values())])] if lit else []
        base = (f"{g['name']}（{'、'.join(g['floors'])}）垂直範圍約 {K._fmt(span)} m"
                f"（以建築物高度 {K._fmt(ctx.height)} m ÷ {stories} 層估算每層約 {K._fmt(h)} m），"
                f"每 15 m 至少一個需 {need} 個；各層樓梯間內：" + "、".join(
                    f"{lab} {n} 個" if n else f"{lab} 無" for lab, n in g["floors"].items()))
        # 整座樓梯在圖上連續兩層以上都認得出樓梯間才判不符／通過；只認得部分樓層（其他層沒圍成樓梯間或名稱、位置對不上）
        # → 不論數量一律需確認：垂直範圍只算到認得的樓層，數量「夠」也可能是少算了樓層
        idx = sorted(drawn.index(lab) for lab in g["floors"])
        whole = len(idx) >= 2 and idx[-1] - idx[0] + 1 == len(idx)
        if not whole:
            full = (_elevation(drawn[-1], stories) - _elevation(drawn[0], stories)) * h
            res.building_findings.append(K.Finding(
                "SPKR-133-5", K.ORANGE, "需確認", "全棟", f"{g['name']} 只在部分樓層認得出樓梯間，垂直配置需確認",
                base + f"；這座樓梯只在 {'、'.join(g['floors'])} 認得出樓梯間（其他樓層沒有圍成獨立的樓梯間，或名稱、位置對不上），"
                f"其他樓層的揚聲器沒算到；依認得出的樓層估算需 {need} 個、現有 {have} 個"
                + (f"，若這座樓梯通達圖上 {drawn[0]}～{drawn[-1]}（約 {K._fmt(full)} m）則需 "
                   f"{max(1, math.ceil(full / STAIR_VERTICAL - 1e-9))} 個" if full > span else "")
                + "，請人工確認整座樓梯的垂直配置",
                f"確認 {g['name']} 各層樓梯間的揚聲器，使垂直距離每 15 m 至少一個 L 級", law,
                rooms=g["rooms"], metrics={"need": need, "have": have, "span": round(span, 2), "whole": False}))
        elif have < need:
            res.building_findings.append(K.Finding(
                "SPKR-133-5", K.RED, "數量不足", "全棟", f"{g['name']} 樓梯間揚聲器不足（需 {need} 個，現有 {have} 個）", base,
                f"於 {g['name']} 樓梯間增設 L 級揚聲器，使垂直距離每 15 m 至少一個", law,
                rooms=g["rooms"], metrics={"need": need, "have": have, "span": round(span, 2)}))
        elif max(gaps) > STAIR_VERTICAL:
            res.building_findings.append(K.Finding(
                "SPKR-133-5", K.ORANGE, "需確認", "全棟", f"{g['name']} 樓梯間揚聲器垂直間距可能超過 15 m",
                base + f"；數量足夠，但有一段約 {K._fmt(max(gaps))} m 沒有揚聲器（樓高為平均估算，請以剖面圖確認）",
                f"調整 {g['name']} 樓梯間揚聲器位置，使垂直每 15 m 範圍內都有一個", law,
                rooms=g["rooms"], metrics={"need": need, "have": have, "span": round(span, 2)}))


def review_dxf(path: str | Path, *, ctx: K.Context | None = None, dictionary: E.Dictionary | None = None,
               profile: F.LayerProfile | None = None, ir: dict | None = None) -> Result:
    from ezdxf import recover

    t0 = time.time()
    ctx = ctx or K.Context()
    dictionary = dictionary or E.Dictionary.default()
    ir = ir or IR.extract(path)
    prof_all = profile or F.LayerProfile()
    floor_sheets = [s for s in ir["sheets"] if s.get("role", "main") == "main" and F.floor_label(IR.sheet_title(s["meta"]))]
    boxes = [s["bbox"] for s in floor_sheets if s["bbox"]]
    unit = F.unit_scale(floor_sheets[0]["meta"] if floor_sheets else {}, ir["dxf"].get("insunits")) or 0.01
    doc, _ = recover.readfile(str(path))
    # 只展開平面圖範圍內的牆、柱、門、窗（認房間用）；弧線轉折誤差統一約 2 cm
    prims = G.explode(doc, keep=lambda layer: prof_all.role(layer) is not None,
                      boxes=boxes if len(boxes) == len(floor_sheets) else None, flatten=0.02 / unit)
    del doc
    res = Result()
    for s in ir["sheets"]:
        if s.get("role", "main") != "main":
            continue                                   # 細部放大圖、涵蓋檢討頁：內容已在主圖
        title = IR.sheet_title(s["meta"])
        label = F.floor_label(title)
        if not label:
            continue
        number = IR.sheet_number(s["meta"])
        scale = F.unit_scale(s["meta"], ir["dxf"].get("insunits"))
        if scale is None:
            res.warnings.append(f"{number or title}：無法判斷圖面單位（圖框沒有「單位」欄、DXF 也沒設），未檢核")
            continue
        prof = profile or F.LayerProfile()
        sheet_ins = [i for i in ir["inserts"] if i["f"] == s["idx"]]
        at = lambda i: (i.get("cx", i["x"]), i.get("cy", i["y"]))  # noqa: E731   圖形中心（沒有時用插入點）
        doors = [at(i) for i in sheet_ins if prof.role(i.get("layer", "")) == "door"]
        fixtures = [at(i) for i in sheet_ins if F.is_fixture(i["name"], i.get("layer", ""))]
        try:
            fl = F.analyze(G.by_bbox(prims, s["bbox"]), [t for t in ir["texts"] if t["f"] == s["idx"]],
                           scale=scale, title=title, profile=profile, doors=doors, fixtures=fixtures)
        except ValueError as e:
            res.warnings.append(f"{number or title}：{e}")
            continue
        eq, unknown = E.recognize(sheet_ins, scale, dictionary)
        res.unknown_blocks.update(unknown)
        zone = fl.outline.buffer(EQUIP_MARGIN)
        inside = [e for e in eq if zone.covers(Point(e.x, e.y))]
        res.floors.append(FloorResult(s["idx"], number, title, fl, inside, [], [], len(eq) - len(inside)))
    del prims
    # 各層都理解完才投影（上層挑空內的探測器要併到下層），再逐層跑規則；對位結果挑空投影與樓梯配對共用
    aligner = ST.Aligner([fr.floor for fr in res.floors])
    project_voids(res, ctx, aligner)
    moved = Counter(e.spec.get("projected_from") for x in res.floors for e in x.projected if "flame_detector" in e.kinds)
    for fr in res.floors:
        findings, notes = review_floor(fr.floor, fr.equipment + fr.projected, ctx)
        flames = sum(1 for e in fr.equipment if "flame_detector" in e.kinds)
        if flames and moved[_name(fr)] >= flames:
            # 本圖的火焰式探測器都畫在挑空內、已投影到下層實際保護的房間：檢附監視範圍的提醒只列在下層
            findings = [f for f in findings if f.rule != "DET-124"]
        fr.findings = findings
        fr.notes.extend(notes)
    if res.floors:
        res.profile = RQ.build_profile(res.floors, ctx)
        res.requirements = RQ.evaluate(res.profile)
        void_heights(res, ctx)
        presence_findings(res)
        voluntary_signs(res, ctx)
        stair_speakers(res, ctx, aligner)
    pf, pn = PIPE.check_texts(ir, [e for fr in res.floors for e in fr.equipment], ctx)
    res.building_findings.extend(pf)
    res.building_notes.extend(pn)
    sort_findings(res.building_findings)
    res.seconds = round(time.time() - t0, 1)
    return res


def _round(c, nd: int):
    return round(c, nd) if isinstance(c, (int, float)) else [_round(x, nd) for x in c]


def _geo(g, nd: int = 2):
    """shapely 幾何 → GeoJSON（座標四捨五入到公分）。"""
    if g is None or g.is_empty:
        return None
    m = mapping(g)
    if "geometries" in m:                       # GeometryCollection
        return {"type": m["type"], "geometries": [x for x in (_geo(p, nd) for p in g.geoms) if x]}
    return {"type": m["type"], "coordinates": _round(m["coordinates"], nd)}


def finding_key(f: K.Finding) -> str:
    """同一條缺失在重跑檢核後的識別碼：規則＋樓層＋房間＋範圍外框（取整到公尺）；沒有範圍的用設備名或標註文字。
    缺失內的數字（需幾個、多少 ㎡）會隨檢核條件變，不放進識別碼。"""
    parts = [f.rule, f.floor or "", ",".join(sorted(f.rooms))]
    if f.geom is not None and not f.geom.is_empty:
        parts.append(",".join(str(round(v)) for v in f.geom.bounds))
    else:
        parts.append(str(f.metrics.get("equipment") or f.metrics.get("text") or f.category))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _finding(i: int, f: K.Finding, geom: bool) -> dict:
    return {"no": i, "key": finding_key(f), "rule": f.rule, "severity": f.severity, "category": f.category, "floor": f.floor,
            "title": f.title, "why": f.why, "fix": f.fix, "law": f.law, "missing": f.missing,
            "rooms": f.rooms, "area": round(f.area, 2) if f.area else None, "metrics": f.metrics,
            "bbox": [round(v, 2) for v in f.geom.bounds] if f.geom is not None and not f.geom.is_empty else None,
            **({"geom": _geo(f.geom)} if geom else {})}


def svg_name(fr: FloorResult) -> str:
    """標示圖檔名：同一樓層可能有好幾張圖（各系統一張），用「樓層-圖紙序號」區分。"""
    return f"{fr.floor.label}-{fr.sheet}"


OVERLAY_TOL = 0.05      # 疊圖用的缺失範圍簡化到約 5 cm


def overlay(fr: FloorResult) -> dict:
    """疊在 CAD 原樣圖上的缺失（<svg_name>.overlay.json）：座標＝公尺（與 Finding.geom 同一座標系），
    no 與 to_dict 的編號相同；anchor＝標號位置（與標示圖相同取範圍內代表點）。"""
    out = []
    for i, f in enumerate(fr.findings, 1):
        g = f.geom if f.geom is not None and not f.geom.is_empty else None
        s = g.simplify(OVERLAY_TOL) if g is not None else None
        if s is not None and s.is_empty:
            s = g
        out.append({"no": i, "key": finding_key(f), "severity": f.severity, "rule": f.rule, "title": f.title,
                    "geom": _geo(s), "anchor": _round(list(g.representative_point().coords[0]), 2) if g is not None else None})
    return {"version": 1, "sheet": fr.sheet, "number": fr.number, "label": fr.floor.label, "findings": out}


def to_dict(res: Result, geom: bool = True) -> dict:
    """geom=False：缺失只留外框（bbox），存資料庫用；範圍圖形已畫在標示圖上。"""
    floors = []
    for fr in res.floors:
        fl = fr.floor
        kinds = Counter(k for e in fr.equipment for k in e.kinds)
        floors.append({
            "sheet": fr.sheet, "number": fr.number, "title": fr.title, "label": fl.label, "svg_name": svg_name(fr),
            "area": round(fl.area, 2), "outline_area": round(fl.outline.area, 2), "fireproof": fl.fireproof,
            "rooms": [{"id": r.id, "name": r.name, "kind": r.kind, "conflict": r.conflict, "area": round(r.area, 2)}
                      for r in fl.rooms],
            "equipment": dict(kinds), "equipment_outside": fr.outside,
            "warnings": fl.warnings,
            "notes": [{"rule": n.rule, "text": n.text, "law": n.law} for n in fr.notes],
            "findings": [_finding(i, f, geom) for i, f in enumerate(fr.findings, 1)],
        })
    building = None
    if res.profile is not None or res.building_findings:
        p = res.profile
        building = {
            "profile": None if p is None else {
                "occupancy": p.occupancy, "stories": p.stories, "height": p.height, "site_area": p.site_area,
                "total_area": round(p.total_area, 2), "roof_area": round(p.roof_area, 2),
                "floors": [{"label": f.label, "level": f.level, "area": round(f.area, 2), "no_opening": f.no_opening}
                           for f in p.floors],
                "high_rise": p.high_rise, "notes": p.notes},
            "notes": [{"rule": n.rule, "text": n.text, "law": n.law} for n in res.building_notes],
            "requirements": [{"key": r.key, "equipment": r.equipment, "kinds": list(r.kinds), "status": r.status,
                              "why": r.why, "law": r.law, "floors": r.floors, "missing": r.missing, "notes": r.notes}
                             for r in res.requirements],
            "findings": [_finding(i, f, False) for i, f in enumerate(res.building_findings, 1)],
        }
    return {"floors": floors, "building": building, "unknown_blocks": dict(res.unknown_blocks.most_common(50)),
            "warnings": res.warnings, "seconds": res.seconds}


def main(argv: list[str]) -> int:
    """python -m litian.review.engine <in.dxf> <out.json> [--ir ir.json] [--ctx 條件.json] [--svg 資料夾] [--no-geom]
    --svg：各樓層的標示圖 <svg_name>.svg 與缺失疊圖資料 <svg_name>.overlay.json 寫到該資料夾。"""
    src, dst = argv[1], argv[2]
    ir = json.loads(Path(argv[argv.index("--ir") + 1]).read_text(encoding="utf-8")) if "--ir" in argv else None
    ctx = None
    if "--ctx" in argv:
        ctx = K.Context.from_dict(json.loads(Path(argv[argv.index("--ctx") + 1]).read_text(encoding="utf-8")))
    res = review_dxf(src, ir=ir, ctx=ctx)
    Path(dst).write_text(json.dumps(to_dict(res, geom="--no-geom" not in argv), ensure_ascii=False), encoding="utf-8")
    if "--svg" in argv:
        from litian.review import render
        out = Path(argv[argv.index("--svg") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for fr in res.floors:
            (out / f"{svg_name(fr)}.svg").write_text(render.floor_svg(fr), encoding="utf-8")
            # 缺失疊圖資料（審核工作台疊在 CAD 原樣圖上）：先寫暫存檔再改名，重跑檢核時工作台不會讀到寫一半的檔
            tmp = out / f".{svg_name(fr)}.overlay.json.tmp"
            tmp.write_text(json.dumps(overlay(fr), ensure_ascii=False), encoding="utf-8")
            tmp.replace(out / f"{svg_name(fr)}.overlay.json")
    for fr in res.floors:
        c = Counter(f.severity for f in fr.findings)
        print(f"{fr.floor.label:5} {fr.title}：設備 {len(fr.equipment)}，缺失 {dict(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
