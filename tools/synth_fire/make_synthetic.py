"""合成「測試用消防設備圖」：在真實建築平面圖上自動配置消防設備，並刻意埋入已知缺失。

用途：手上只有建築圖、沒有消防設備圖時，讓「認房間 → 認設備 → 逐條檢核 → 缺失」整條流程
有標準答案可驗。合成圖只當測試資料，不當驗收依據；真實消防設備圖到手後要用真圖校正。

注意：輸入是客戶的真實圖說，輸出檔與標準答案只能放私人資料夾，不可放進版控。

用法：
  python tools/synth_fire/make_synthetic.py <建築平面圖.dxf> <輸出資料夾> [--floors 1F,2F]
輸出：<資料夾>/synthetic_fire.dxf、answer_key.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import Point, box
from shapely.ops import unary_union

from litian.drawing import ir as IR
from litian.plan import floor as F
from litian.plan import geometry as G
from litian.review import checks as K
from litian.review import coverage as C
from litian.review import equipment as E
from litian.review import escape as ESC
from litian.review import rescue as RES
from litian.review.required import _level

SPK = "密閉式撒水頭（向下型）"
DET = "差動式局限型探測器（1種）"
EXT = "乾粉滅火器"
HYD = "室內消防栓"
SPKR = "揚聲器（嵌頂式）"
EXIT = "出口標示燈"
DIRL = "避難方向指示燈（單面雙向）"
EML = "緊急照明燈（吸頂式）"
VENT = "排煙口（天花板型）"
OUTLET = "連結送水管出水口箱（雙口型）"
MAN = "手動警報機"
TESTV = "末端查驗閥"
LAYER = {SPK: "F-SPK", DET: "F-DET", EXT: "F-EXT", HYD: "F-HYD", SPKR: "F-SPKR", EXIT: "F-EXIT", DIRL: "F-EXIT",
         EML: "F-EML", VENT: "F-SMOKE", OUTLET: "F-SDP", MAN: "F-ALM", TESTV: "F-SPK"}
ATTRS = {SPK: {"型式": "一般反應型"}, EXT: {"效能值": "A-3,B-10,C"}, DET: {}, HYD: {"種類": "第一種"}, SPKR: {"等級": "L"},
         EXIT: {"等級": "B"}, DIRL: {"等級": "B"}, EML: {}, VENT: {"尺寸": "600x600"}, OUTLET: {}, MAN: {}, TESTV: {}}
PIPE_TEXTS = ["消防栓立管 Ø50", "連結送水管立管 100A", "撒水末端查驗閥 25mm", "消防配管材質：CNS 6445 配管用碳鋼鋼管"]
SPK_R = 2.3          # 一般反應型、防火構造
DET_EFF = 90         # 差動式 1 種、防火構造、未滿 4 m


def grid_in(poly, step: float, inset: float = 0.0):
    if poly.is_empty:
        return []
    x0, y0, x1, y1 = poly.bounds
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    target = poly.buffer(-inset) if inset else poly
    if target.is_empty:
        return []
    return [(float(x), float(y)) for x in xs for y in ys if target.covers(Point(x, y))]


def fill_horizontal(region, pts, radius, guard=200):
    """補點直到 region 每一處都在 radius 內（用檢核本身的計算）。"""
    for _ in range(guard):
        miss = C.uncovered(region, pts, radius)
        if not miss:
            return pts
        p = max(miss, key=lambda g: g.area).representative_point()
        pts.append((p.x, p.y))
    raise RuntimeError("補點未收斂")


def room_named(fl: F.Floor, word: str, largest=True):
    rs = [r for r in fl.rooms if any(word in s for s in r.labels) and not r.conflict]
    return (max(rs, key=lambda r: r.area) if largest else min(rs, key=lambda r: r.area)) if rs else None


def place_floor(fl: F.Floor, plant: bool):
    """回傳 {圖例名稱: [(x, y), ...]}（公尺）與埋入的缺失清單。"""
    placed: dict[str, list] = {k: [] for k in LAYER}
    planted = []
    fp = True
    hall = next(r for r in fl.rooms if r.kind not in ("void", "outdoor", "unknown"))   # 最大的實際使用空間

    # 撒水頭：每個非免設房間 3.2 m 方格，再補到全涵蓋
    spk_rooms = [r for r in fl.rooms if r.kind not in ("void", "outdoor") and not K._sprinkler_exempt(r, fp)]
    for r in spk_rooms:
        area = r.polygon.intersection(fl.region)
        if area.area < C.MIN_PIECE:
            continue
        pts = grid_in(area, 3.2) or [(area.representative_point().x, area.representative_point().y)]
        placed[SPK].extend(fill_horizontal(area, pts, SPK_R))
    # 探測器：每間（廁所、樓梯、走廊、管道間等以外）依 90 ㎡ 一個
    det_rooms = [r for r in fl.rooms if r.kind not in ("void", "outdoor", "elevator", "shaft", "stair", "corridor", "toilet")
                 and r.area >= 2]
    det_by_room = {}
    for r in det_rooms:
        n = math.ceil(r.area / DET_EFF)
        cand = grid_in(r.polygon, max(1.0, math.sqrt(r.area / n)), inset=0.3) or [(r.polygon.representative_point().x,
                                                                                     r.polygon.representative_point().y)]
        while len(cand) < n:
            cand = cand + cand[: n - len(cand)]
        det_by_room[r.id] = cand[:n] if len(cand) >= n else cand
        if len(cand) > n:   # 平均挑
            idx = np.linspace(0, len(cand) - 1, n).round().astype(int)
            det_by_room[r.id] = [cand[i] for i in idx]
    # 消防栓：25 m 貪婪覆蓋
    samples = np.array(grid_in(fl.region, 3.0))
    cands = np.array(grid_in(fl.walkable, 6.0))
    hyd = []
    if len(samples) and len(cands):
        D = np.hypot(samples[:, None, 0] - cands[None, :, 0], samples[:, None, 1] - cands[None, :, 1]) <= 24.0
        left = np.ones(len(samples), bool)
        while left.any():
            j = int(np.argmax(D[left].sum(axis=0)))
            if not D[left, j].any():
                break
            hyd.append(tuple(cands[j]))
            left &= ~D[:, j]
    placed[HYD] = fill_horizontal(fl.region, hyd, 25.0)
    # 揚聲器：14 m 方格＋補點（10 m）
    placed[SPKR] = fill_horizontal(fl.region, grid_in(fl.walkable, 13.5), 10.0)
    # 滅火器：12 m 方格，再沿步行距離補到 19 m 內
    grid = C.WalkGrid(fl.walkable)
    cells = grid.region_cells(fl.region.difference(unary_union([r.polygon for r in fl.rooms if r.kind in ("elevator", "shaft")])))
    ext = list(grid_in(fl.walkable, 12.0))
    for _ in range(300):
        d = grid.distances(ext)
        bad = cells & np.isfinite(d) & (d > 19.0)
        if not bad.any():
            break
        r, c = np.unravel_index(np.nanargmax(np.where(bad, d, -1)), d.shape)
        ext.append((float(grid.gx[r, c]), float(grid.gy[r, c])))
    placed[EXT] = ext
    elec = [r for r in fl.rooms if r.kind == "electrical"]

    if plant:
        # D1：員工餐廳東半部拿掉撒水頭；D1b：大空間中央 6 m 方塊拿掉
        dining = room_named(fl, "餐廳")
        if dining:
            x0, y0, x1, y1 = dining.polygon.bounds
            cut = box((x0 + x1) / 2, y0, x1, y1)
            placed[SPK] = [p for p in placed[SPK] if not cut.covers(Point(p))]
            planted.append({"id": "D1", "rule": "SPK-46", "room": dining.name, "where": list(cut.centroid.coords[0]),
                            "desc": "員工餐廳東半部沒有撒水頭"})
        c = hall.polygon.representative_point()
        cut = box(c.x - 3, c.y - 3, c.x + 3, c.y + 3)
        placed[SPK] = [p for p in placed[SPK] if not cut.covers(Point(p))]
        planted.append({"id": "D1b", "rule": "SPK-46", "room": hall.name, "where": [c.x, c.y], "desc": "大空間中央 6 m 方塊沒有撒水頭"})
        # D2：會議室不設探測器；D3：員工餐廳只設 1 個（需 2）
        meet = room_named(fl, "會議室")
        if meet and meet.id in det_by_room:
            det_by_room[meet.id] = []
            planted.append({"id": "D2", "rule": "DET-120", "room": meet.name, "where": list(meet.polygon.representative_point().coords[0]),
                            "desc": "會議室未設探測器"})
        if dining and len(det_by_room.get(dining.id, [])) >= 2:
            det_by_room[dining.id] = det_by_room[dining.id][:1]
            planted.append({"id": "D3", "rule": "DET-120", "room": dining.name,
                            "where": list(dining.polygon.representative_point().coords[0]), "desc": "員工餐廳探測器 1 個（需 2 個）"})
        # D4：拿掉一支消防栓（造成最大未涵蓋面積的那支）
        best, best_area = None, 0.0
        for i in range(len(placed[HYD])):
            rest = placed[HYD][:i] + placed[HYD][i + 1:]
            a = sum(p.area for p in C.uncovered(fl.region, rest, 25.0))
            if a > best_area:
                best, best_area = i, a
        if best is not None:
            p = placed[HYD].pop(best)
            planted.append({"id": "D4", "rule": "HYD-34", "room": "", "where": list(p), "desc": f"拿掉一支消防栓（約 {best_area:.0f} ㎡ 超出 25 m）"})
        # D5：大空間中央 24 m 內的滅火器全拿掉（中心點一定走超過 20 m）
        cx, cy = c.x + 8, c.y
        placed[EXT] = [p for p in placed[EXT] if math.dist(p, (cx, cy)) > 24]
        planted.append({"id": "D5", "rule": "EXT-31-3", "room": hall.name, "where": [cx, cy], "desc": "大空間中央 24 m 內沒有滅火器"})
        # D6：一間電氣室不另設滅火器
        if elec:
            zone = elec[0].polygon.buffer(3.0)
            placed[EXT] = [p for p in placed[EXT] if not zone.covers(Point(p))]
            planted.append({"id": "D6", "rule": "EXT-31-2", "room": elec[0].name,
                            "where": list(elec[0].polygon.representative_point().coords[0]), "desc": "電氣室未另設滅火器"})
            elec = elec[1:]
        # D7：拿掉離大空間中心最近的揚聲器
        if placed[SPKR]:
            j = min(range(len(placed[SPKR])), key=lambda i: math.dist(placed[SPKR][i], (c.x - 10, c.y)))
            p = placed[SPKR].pop(j)
            planted.append({"id": "D7", "rule": "SPKR-133", "room": hall.name, "where": list(p), "desc": "大空間拿掉一個揚聲器"})
    for r in elec:
        for k in range(math.ceil(r.area / 100)):
            q = r.polygon.representative_point()
            placed[EXT].append((q.x + 0.3 * k, q.y))
    placed[DET] = [p for pts in det_by_room.values() for p in pts]
    return placed, planted


def fill_walk(fl, lights, check, guard=60):
    """用檢核本身的步行距離規則補點，直到不再有不符（紅、橙）的範圍。"""
    for _ in range(guard):
        f, _n = check(fl, [E.Equipment("", DIRL, DIRL, E.kinds_of(DIRL), x, y, "", {"grade": "B"}) for x, y in lights], K.Context())
        bad = [x for x in f if x.severity in (K.RED, K.ORANGE) and x.geom is not None]
        if not bad:
            return lights
        q = max(bad, key=lambda x: x.area or 0).geom.representative_point()
        lights.append((q.x, q.y))
    return lights


def place_batch2(fl: F.Floor, plant: bool):
    """第二批設備：出口標示燈、避難方向指示燈、緊急照明、排煙口、出水口、手動警報機、末端查驗閥。"""
    placed: dict[str, list] = {k: [] for k in (EXIT, DIRL, EML, VENT, OUTLET, MAN, TESTV)}
    planted = []
    lab = fl.label or ""
    kind, lv = _level(lab)
    # 出口標示燈：每個通往戶外、直通樓梯的出入口
    doors = ESC._dedupe(fl.exterior_doors()) + [d for d in ESC._dedupe(fl.stair_doors())]
    doors = ESC._dedupe(doors)
    stair_doors = [d for d in doors if d in fl.stair_doors()]
    if plant and lab == "1F" and stair_doors:
        skip = stair_doors[0]
        doors = [d for d in doors if d != skip]
        planted.append({"id": "D8", "rule": "EXIT-146-3", "room": "", "where": list(skip), "desc": "一處樓梯出入口未設出口標示燈"})
    placed[EXIT] = [(x, y) for x, y in doors]
    # 避難方向指示燈：走廊補到 B 級 15 m 全涵蓋
    corridors = [r for r in fl.rooms if r.kind == "corridor"]
    if corridors:
        pts = [(r.polygon.representative_point().x, r.polygon.representative_point().y) for r in corridors]
        placed[DIRL] = fill_walk(fl, pts, ESC.direction_lights)
        if plant and lab == "2F":
            big = max(corridors, key=lambda r: r.area)
            placed[DIRL] = [p for p in placed[DIRL] if not big.polygon.buffer(0.5).covers(Point(p))]
            planted.append({"id": "D9", "rule": "DIR-146-3", "room": big.name, "where": list(big.polygon.representative_point().coords[0]),
                            "desc": "最大一段走廊的避難方向指示燈全拿掉"})
    # 緊急照明：每個居室、走廊、樓梯間一盞（依面積每 80 ㎡ 一盞）
    need = [r for r in fl.rooms if r.kind not in ("void", "outdoor", "shaft", "elevator", "toilet", "machine") and r.area >= 2
            and not re.search(ESC.EML_EXEMPT_LABEL, " ".join(r.labels))]
    victim = None
    if plant and lab == "2F":
        cand = [r for r in need if r.kind == "room" and not r.conflict and r.labels]
        victim = max(cand, key=lambda r: r.area) if cand else None
        if victim:
            planted.append({"id": "D10", "rule": "EML-24", "room": victim.name, "where": list(victim.polygon.representative_point().coords[0]),
                            "desc": "一間居室未設緊急照明"})
    for r in need:
        if r is victim:
            continue
        n = max(1, math.ceil(r.area / 80))
        pts = grid_in(r.polygon, max(1.0, math.sqrt(r.area / n)), inset=0.3) or [(r.polygon.representative_point().x, r.polygon.representative_point().y)]
        placed[EML] += pts[:max(n, 1)]
    # 排煙口：每個非免設房間 20 m 方格（30 m 涵蓋），尺寸使開口合計達 2%
    vent_rooms = [r for r in fl.rooms if r.kind not in ("void", "outdoor", "stair", "elevator", "shaft", "toilet") and r.area >= 2
                  and not re.search(RES.SMOKE_EXEMPT, " ".join(r.labels))]
    small_victim = None
    if plant and lab == "3F":
        cand = [r for r in vent_rooms if r.kind == "room" and not r.conflict and 20 < r.area < 400]
        small_victim = max(cand, key=lambda r: r.area) if cand else None
        if small_victim:
            planted.append({"id": "D11", "rule": "SMK-188", "room": small_victim.name,
                            "where": list(small_victim.polygon.representative_point().coords[0]), "desc": "排煙口開口面積不足 2%"})
    for r in vent_rooms:
        pts = grid_in(r.polygon, 20.0, inset=0.3) or [(r.polygon.representative_point().x, r.polygon.representative_point().y)]
        pts = fill_horizontal(r.polygon, pts, 29.0)
        side = 300 if r is small_victim else math.ceil(math.sqrt(r.area * 0.021 / len(pts)) * 1000 / 50) * 50
        placed[VENT] += [(x, y, {"尺寸": f"{side}x{side}"}) for x, y in pts]
    # 出水口：三層以上，每座樓梯間一處，補到 50 m
    if kind == "above" and lv >= 3:
        stairs = [r for r in fl.rooms if r.kind == "stair"]
        outs = [(r.polygon.representative_point().x, r.polygon.representative_point().y) for r in stairs]
        outs = fill_horizontal(fl.region, outs, 49.0)
        if plant and len(outs) > 1:
            best, best_area = None, 0.0
            for i in range(len(outs)):
                a = sum(p.area for p in C.uncovered(fl.region, outs[:i] + outs[i + 1:], 50.0))
                if a > best_area:
                    best, best_area = i, a
            if best is not None:
                p = outs.pop(best)
                planted.append({"id": "D13", "rule": "SDP-180", "room": "", "where": list(p), "desc": f"拿掉一處出水口（約 {best_area:.0f} ㎡ 超出 50 m）"})
        placed[OUTLET] = outs
    # 手動警報機：每座樓梯旁一處；末端查驗閥：每層一個（2F 刻意不設）
    placed[MAN] = [(r.polygon.representative_point().x, r.polygon.representative_point().y) for r in fl.rooms if r.kind == "stair"][:4] \
        or [(fl.region.representative_point().x, fl.region.representative_point().y)]
    if plant and lab == "2F":
        planted.append({"id": "D14", "rule": "PIPE-56", "room": "", "where": [0, 0], "desc": "設有撒水頭但未設末端查驗閥"})
    else:
        q = fl.region.representative_point()
        placed[TESTV] = [(q.x, q.y)]
    return placed, planted


def write_dxf(src: Path, dst: Path, sheets: list[tuple[dict, float, dict]]):
    from ezdxf import recover

    doc, _ = recover.readfile(str(src))
    for name, layer in LAYER.items():
        if layer not in doc.layers:
            doc.layers.add(layer)
        if name not in doc.blocks:
            b = doc.blocks.new(name)
            b.add_circle((0, 0), 15)
            for i, tag in enumerate(ATTRS[name]):
                b.add_attdef(tag, (20, -12 * i), dxfattribs={"height": 8})
    msp = doc.modelspace()
    n = 0
    for sheet, scale, placed in sheets:
        for name, pts in placed.items():
            for pt in pts:
                x, y = pt[0], pt[1]
                attrs = pt[2] if len(pt) > 2 else ATTRS[name]
                ref = msp.add_blockref(name, (x / scale, y / scale), dxfattribs={"layer": LAYER[name]})
                if attrs:
                    ref.add_auto_attribs(attrs)
                n += 1
        if sheet.get("pipe_texts"):
            x0, y0 = sheet["bbox"][0], sheet["bbox"][1]
            for i, t in enumerate(sheet["pipe_texts"]):
                msp.add_text(t, dxfattribs={"insert": (x0 + 500, y0 + 800 + 120 * i), "height": 60, "layer": "F-PIPE-TXT"})
    doc.saveas(str(dst))
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--floors", default="1F,2F,3F")
    a = ap.parse_args(argv)
    from ezdxf import recover

    want = a.floors.split(",")
    ir = IR.extract(a.src)
    doc, _ = recover.readfile(str(a.src))
    prims = G.explode(doc)
    jobs, key = [], {"source": a.src.name, "note": "合成測試資料：設備由程式配置，缺失為刻意埋入", "floors": {}}
    for s in ir["sheets"]:
        title = IR.sheet_title(s["meta"])
        lab = F.floor_label(title)
        if lab not in want:
            continue
        scale = F.unit_scale(s["meta"], ir["dxf"].get("insunits"))
        prof = F.LayerProfile()
        doors = [(i["x"], i["y"]) for i in ir["inserts"] if i["f"] == s["idx"] and prof.role(i.get("layer", "")) == "door"]
        fl = F.analyze(G.by_bbox(prims, s["bbox"]), [t for t in ir["texts"] if t["f"] == s["idx"]], scale=scale, title=title,
                       doors=doors)
        placed, planted = place_floor(fl, plant=True)
        more, planted2 = place_batch2(fl, plant=True)
        placed.update(more)
        planted += planted2
        sheet = {"bbox": s["bbox"], "pipe_texts": PIPE_TEXTS if lab == "1F" else []}
        if lab == "1F":
            planted.append({"id": "D15", "rule": "PIPE-32", "room": "", "where": [0, 0], "desc": "第一種消防栓立管標註 Ø50（應 63 mm 以上）"})
        jobs.append((sheet, scale, placed))
        key["floors"][lab] = {"planted": planted, "counts": {k: len(v) for k, v in placed.items()}}
        print(lab, {k: len(v) for k, v in placed.items()}, [p["id"] for p in planted])
    a.out.mkdir(parents=True, exist_ok=True)
    n = write_dxf(a.src, a.out / "synthetic_fire.dxf", jobs)
    (a.out / "answer_key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print("inserted", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
