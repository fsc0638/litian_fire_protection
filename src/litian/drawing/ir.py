"""圖面中介資料（Drawing IR）抽取：DXF → 圖紙、文字、圖塊、線段、封閉多邊形。

設計文件 §6.2。DXF 一律當不可信輸入：用 ezdxf 修復模式讀，實體數設上限。
一個 DXF 的模型空間常放好幾張圖（各有圖框），所以先找圖框、把每個實體依座標分到所屬圖紙：
- 圖紙資訊：帶屬性且有「圖號」類欄位的 INSERT（例：圖號、中文圖名、比例、單位、日期、繪圖／審核／核准）。
- 圖框：範圍內包住圖號屬性的大型圖塊（線段與文字都多）。不看圖塊名稱，因為各事務所命名不同；
  平面圖等內容圖塊也很大，但不會包住位在圖框角落的圖號屬性。同一個圖號被幾個框包住時取最小的框。
- 圖框圖塊本身的文字（格線編號等）不收。沒有圖框時整個模型空間算一張圖。

命令列：python -m litian.drawing.ir <in.dxf> <out.json>（worker 用子行程呼叫，限時限記憶體）
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

MAX_ENTITIES = 400_000
FRAME_MIN_LINES = 40          # 圖框圖塊至少要有這麼多線段
FRAME_MIN_TEXTS = 8           # 與文字（格線編號等）
BLOCK_TEXT_MAX = 200          # 每個一般圖塊最多展開的文字數
SHEET_KEY_RE = re.compile(r"圖號|DWG\.?\s*NO|SHEET\s*NO|DRAWING\s*NO", re.I)
NUM = 2                       # 座標四捨五入位數
_SURR = re.compile("[\ud800-\udfff]")


def _clean(s) -> str:
    """DXF 編碼錯誤會留下無效的 Unicode 代理字元，寫 JSON／資料庫前換成 U+FFFD。"""
    return _SURR.sub("�", str(s)) if s is not None else ""


def _r(v: float) -> float:
    return round(float(v), NUM)


def _text_of(e) -> str:
    if e.dxftype() in ("MTEXT", "TEXT", "ATTRIB"):
        return _clean(e.plain_text())
    return ""


def _block_profile(doc, name: str) -> tuple[int, int]:
    blk = doc.blocks.get(name)
    if blk is None:
        return 0, 0
    lines = texts = 0
    for e in blk:
        t = e.dxftype()
        if t in ("LINE", "LWPOLYLINE", "POLYLINE"):
            lines += 1
        elif t in ("TEXT", "MTEXT"):
            texts += 1
    return lines, texts


def _bbox(entities):
    """圖框範圍只看線條（含巢狀圖塊）：文字、標註的範圍要字型資料，伺服器容器沒有字型時 ezdxf 會整個失敗；
    圖框外框本來就是線。"""
    from ezdxf import disassemble

    from litian.plan.geometry import walk
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for e, _layer in walk(entities):
        try:
            pts = disassemble.make_primitive(e, max_flattening_distance=10).vertices()
            for v in pts:
                x0, y0, x1, y1 = min(x0, v.x), min(y0, v.y), max(x1, v.x), max(y1, v.y)
        except Exception:
            continue
    if x0 == float("inf"):
        return None
    return [_r(x0), _r(y0), _r(x1), _r(y1)]


def _place(b, insert):
    """圖塊座標的範圍 → 插入後的範圍（四角經插入點、比例、旋轉換算後取外框）。"""
    if b is None:
        return None
    from ezdxf.math import Vec3
    m = insert.matrix44()
    base = insert.block().block.dxf.base_point if insert.block() is not None else Vec3()
    pts = [m.transform(Vec3(x, y) - base) for x, y in ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]))]
    return [_r(min(p.x for p in pts)), _r(min(p.y for p in pts)), _r(max(p.x for p in pts)), _r(max(p.y for p in pts))]


DRAWING_NO = re.compile(r"[A-Z]{1,4}-?[0-9A-Z]{1,5}(?:-\d{1,2})?")
DETAIL_NAME = re.compile(r"\(\s*\d+\s*\)\s*$")
NOTE_PREFIX = re.compile(r"^(變更設計|本次|註|說明|備註|NOTE|\d+\s*[.、．])", re.I)     # 變更說明、編號條列不是圖名


def _overlap(a, b) -> bool:
    return a is not None and b is not None and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _within(a, b, tol: float = 0.02) -> bool:
    """a 是否落在 b 內（容許 b 尺寸 2% 誤差）。"""
    dx, dy = (b[2] - b[0]) * tol, (b[3] - b[1]) * tol
    return a[0] >= b[0] - dx and a[1] >= b[1] - dy and a[2] <= b[2] + dx and a[3] <= b[3] + dy


def _viewport_window(vp):
    """視埠在模型空間框出的範圍（view_center ± 視埠寬高換算；有扭轉角時取外接框）。"""
    h = float(vp.dxf.view_height)
    pw, ph = float(vp.dxf.width), float(vp.dxf.height)
    if h <= 0 or pw <= 0 or ph <= 0:
        return None
    w = h * pw / ph
    c = vp.dxf.view_center_point
    a = math.radians(float(vp.dxf.get("view_twist_angle", 0) or 0))
    hw = abs(w / 2 * math.cos(a)) + abs(h / 2 * math.sin(a))
    hh = abs(w / 2 * math.sin(a)) + abs(h / 2 * math.cos(a))
    return [_r(c.x - hw), _r(c.y - hh), _r(c.x + hw), _r(c.y + hh)]


def _layout_title(texts: list[tuple[float, float, str]]) -> str:
    """配置頁上的圖名：含中文、40 字以內、不是變更說明或註記的文字，依由上而下、由左而右串起來
    （圖名常拆成兩行，例：「一層消防滅火器避難」＋「緊急廣播設備平面圖」）。"""
    parts = [(-y, x, s) for x, y, s in texts
             if re.search(r"[一-鿿]", s) and len(s) <= 40 and not NOTE_PREFIX.match(s)]
    return "".join(s for _, _, s in sorted(parts))


def layout_sheets(doc) -> list[dict]:
    """配置頁出圖：每個有視埠的配置頁＝一張圖。role：
    main（主圖，分配模型空間內容）｜detail（細部放大，名稱帶「(1)」或範圍落在別張主圖內）｜duplicate（與主圖範圍幾乎相同，例：涵蓋檢討頁）。"""
    out = []
    for order, lay in enumerate(doc.layouts):
        if lay.name == "Model":
            continue
        vps = []
        for e in lay:
            if e.dxftype() == "VIEWPORT" and e.dxf.get("id", 2) != 1:
                w = _viewport_window(e)
                if w:
                    vps.append((float(e.dxf.width) * float(e.dxf.height), w))
        if not vps:
            continue
        window = max(vps)[1]
        meta, frame_block, texts = {}, None, []
        for e in lay:
            t = e.dxftype()
            if t == "INSERT":
                frame_block = frame_block or e.dxf.name
                if e.attribs and any(SHEET_KEY_RE.search(a.dxf.tag) for a in e.attribs):
                    meta = {_clean(a.dxf.tag): _clean(a.dxf.text) for a in e.attribs}
                    frame_block = e.dxf.name
            elif t in ("TEXT", "MTEXT"):
                s = _text_of(e).strip()
                if s:
                    p = e.dxf.insert
                    texts.append((p.x, p.y, " ".join(s.split())))
        name = _clean(lay.name)
        if not sheet_number(meta):
            base = DETAIL_NAME.sub("", name).strip()
            meta = {"圖號": name if DRAWING_NO.fullmatch(base) else "", "圖名": _layout_title(texts), **meta}
        meta["配置頁"] = name
        out.append({"layout": name, "order": order, "bbox": window, "meta": meta, "frame_block": _clean(frame_block),
                    "detail": bool(DETAIL_NAME.search(name)), "plan": "平面" in sheet_title(meta)})
    # 決定角色：先排主圖候選（圖名含「平面」者優先），再判細部與重複
    mains: list[dict] = []
    for s in sorted(out, key=lambda s: (s["detail"], not s["plan"], s["order"])):
        if s["detail"] or any(_within(s["bbox"], m["bbox"]) and _iou(s["bbox"], m["bbox"]) < 0.8 for m in mains):
            s["role"] = "detail"
        elif any(_iou(s["bbox"], m["bbox"]) >= 0.8 for m in mains):
            s["role"] = "duplicate"
        else:
            s["role"] = "main"
            mains.append(s)
    out.sort(key=lambda s: s["order"])
    for s in out:
        del s["order"], s["detail"], s["plan"]
    return out


def _contains(b, x, y) -> bool:
    return b is not None and b[0] <= x <= b[2] and b[1] <= y <= b[3]


def _poly_area(pts) -> float:
    a = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        a += x1 * y2 - x2 * y1
    return abs(a) / 2


def sheet_number(meta: dict) -> str | None:
    return next((v for k, v in meta.items() if SHEET_KEY_RE.search(k) and v), None)


def meta_field(meta: dict, word: str) -> str | None:
    """圖框欄位名稱各事務所不同（「中文圖名<一>」「圖名」「比例」「SCALE」…），取第一個含 word 且有值的欄位。"""
    keys = sorted((k for k in meta if word in k and meta[k]), key=lambda k: ("中文" not in k, len(k), k))
    return meta[keys[0]] if keys else None


def sheet_title(meta: dict) -> str:
    return meta_field(meta, "圖名") or ""


def _expand_block(e, frame_of, texts: list, inserts: list, depth: int = 0) -> None:
    """綁定進來的外部參考（建築底圖）：展開裡面的文字（不設上限，房名都在這裡）與巢狀圖塊（門、設備）。"""
    name = _clean(e.dxf.name)
    try:
        children = list(e.virtual_entities())
    except Exception:
        return
    for v in children:
        t = v.dxftype()
        if t in ("TEXT", "MTEXT"):
            s = _text_of(v).strip()
            if s:
                q = v.dxf.insert
                texts.append({"h": e.dxf.handle, "t": s, "x": _r(q.x), "y": _r(q.y),
                              "ht": _r(v.dxf.char_height if t == "MTEXT" else v.dxf.height),
                              "rot": _r(v.dxf.get("rotation", 0)), "layer": _clean(v.dxf.layer),
                              "f": frame_of(q.x, q.y), "src": f"xref:{name}"})
        elif t == "INSERT" and depth < 3:
            q = v.dxf.insert
            inserts.append({"h": e.dxf.handle, "name": _clean(v.dxf.name), "x": _r(q.x), "y": _r(q.y),
                            "rot": _r(v.dxf.get("rotation", 0)), "sx": _r(v.dxf.get("xscale", 1)),
                            "sy": _r(v.dxf.get("yscale", 1)), "layer": _clean(v.dxf.layer),
                            "f": frame_of(q.x, q.y), "attribs": {}, "src": f"xref:{name}"})
            if "$0$" in v.dxf.name:                                     # 綁定時參考檔的圖塊名稱加上「參考名$0$」前綴
                _expand_block(v, frame_of, texts, inserts, depth + 1)     # 參考檔內的巢狀圖塊（房名標籤等）


def extract(path: str | Path, expand: tuple[str, ...] | list[str] = ()) -> dict:
    """expand：已綁定的外部參考圖塊名稱（xref.bind 的 bound），其內容完整展開。"""
    from ezdxf import recover

    doc, auditor = recover.readfile(str(path))
    expand = set(expand)
    msp = doc.modelspace()
    n = len(msp)
    if n > MAX_ENTITIES:
        raise ValueError(f"實體數 {n} 超過上限 {MAX_ENTITIES}")

    profiles: dict[str, tuple[int, int]] = {}
    local: dict[str, list | None] = {}           # 圖塊定義（圖塊座標）的範圍：同一圖塊只算一次
    big, anchors = [], []
    for e in msp.query("INSERT"):
        name = e.dxf.name
        if name not in profiles:
            profiles[name] = _block_profile(doc, name)
        lines, ntexts = profiles[name]
        if lines >= FRAME_MIN_LINES and ntexts >= FRAME_MIN_TEXTS:
            if name not in local:
                blk = doc.blocks.get(name)
                local[name] = _bbox(blk) if blk is not None else None
            b = _place(local[name], e)
            if b:
                big.append({"block": name, "bbox": b, "sheet": {}})
        if e.attribs and any(SHEET_KEY_RE.search(a.dxf.tag) for a in e.attribs):
            anchors.append((e.dxf.insert.x, e.dxf.insert.y,
                            {_clean(a.dxf.tag): _clean(a.dxf.text) for a in e.attribs}))

    frames, orphan_meta = [], []
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])  # noqa: E731
    for x, y, attrs in anchors:
        inside = [f for f in big if _contains(f["bbox"], x, y) and not f["sheet"]]
        if inside:
            f = min(inside, key=lambda f: area(f["bbox"]))
            f["sheet"] = attrs
            frames.append(f)
        else:
            orphan_meta.append(attrs)
    frame_names = {f["block"] for f in frames}
    frames.sort(key=lambda f: (sheet_number(f["sheet"]) or "", f["bbox"][0]))

    # 配置頁（paper space）出圖：每個配置頁是一張圖，主視埠框出的模型空間範圍就是這張圖的內容
    lsheets = layout_sheets(doc)
    if lsheets:
        mains = [s for s in lsheets if s["role"] == "main"]
        extra = [f for f in frames if not any(_overlap(f["bbox"], s["bbox"]) for s in mains)]
        frames = [{"block": s["frame_block"], "bbox": s["bbox"], "sheet": s["meta"], "role": s["role"],
                   "layout": s["layout"]} for s in lsheets] + [{**f, "role": "main", "layout": None} for f in extra]
        frame_names |= {s["frame_block"] for s in lsheets if s["frame_block"]}
    assignable = [(i, f["bbox"]) for i, f in enumerate(frames) if f.get("role", "main") == "main"]

    def frame_of(x: float, y: float):
        """點所在的圖紙：重疊時取範圍最小者（細部放大圖、涵蓋檢討頁等不分配內容）。"""
        hits = [(area(b), i) for i, b in assignable if _contains(b, x, y)]
        return min(hits)[1] if hits else None

    texts, inserts, segments, polygons = [], [], [], []
    for e in msp:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT"):
            s = _text_of(e).strip()
            if s:
                p = e.dxf.insert
                texts.append({"h": e.dxf.handle, "t": s, "x": _r(p.x), "y": _r(p.y),
                              "ht": _r(e.dxf.char_height if t == "MTEXT" else e.dxf.height),
                              "rot": _r(e.dxf.get("rotation", 0)), "layer": _clean(e.dxf.layer),
                              "f": frame_of(p.x, p.y), "src": t})
        elif t == "INSERT":
            p = e.dxf.insert
            name = _clean(e.dxf.name)
            inserts.append({"h": e.dxf.handle, "name": name, "x": _r(p.x), "y": _r(p.y),
                            "rot": _r(e.dxf.get("rotation", 0)), "sx": _r(e.dxf.get("xscale", 1)),
                            "sy": _r(e.dxf.get("yscale", 1)), "layer": _clean(e.dxf.layer),
                            "f": frame_of(p.x, p.y),
                            "attribs": {_clean(a.dxf.tag): _clean(a.dxf.text) for a in e.attribs}})
            if e.dxf.name in expand:
                _expand_block(e, frame_of, texts, inserts)
            elif e.dxf.name not in frame_names and not e.dxf.name.startswith("*D"):   # 圖框與標註圖塊不展開
                k = 0
                for v in e.virtual_entities():
                    if k >= BLOCK_TEXT_MAX:
                        break
                    if v.dxftype() in ("TEXT", "MTEXT"):
                        s = _text_of(v).strip()
                        if s:
                            q = v.dxf.insert
                            texts.append({"h": e.dxf.handle, "t": s, "x": _r(q.x), "y": _r(q.y),
                                          "ht": _r(v.dxf.char_height if v.dxftype() == "MTEXT" else v.dxf.height),
                                          "rot": _r(v.dxf.get("rotation", 0)), "layer": _clean(v.dxf.layer),
                                          "f": frame_of(q.x, q.y), "src": f"block:{name}"})
                            k += 1
        elif t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            segments.append([_r(a.x), _r(a.y), _r(b.x), _r(b.y), _clean(e.dxf.layer),
                             frame_of((a.x + b.x) / 2, (a.y + b.y) / 2)])
        elif t in ("LWPOLYLINE", "POLYLINE"):
            try:
                if t == "LWPOLYLINE":
                    pts = [(float(x), float(y)) for x, y in e.get_points("xy")]
                    flag = e.closed
                else:
                    pts = [(float(v[0]), float(v[1])) for v in e.points()]
                    flag = e.is_closed
            except Exception:
                continue
            if len(pts) < 2:
                continue
            closed = bool(flag) or (len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-6)
            layer = _clean(e.dxf.layer)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + (pts[:1] if closed else [])):
                segments.append([_r(x1), _r(y1), _r(x2), _r(y2), layer, frame_of((x1 + x2) / 2, (y1 + y2) / 2)])
            if closed and len(pts) >= 3:
                cx = sum(x for x, _ in pts) / len(pts)
                cy = sum(y for _, y in pts) / len(pts)
                polygons.append({"h": e.dxf.handle, "layer": layer, "f": frame_of(cx, cy),
                                 "area": _r(_poly_area(pts)), "pts": [[_r(x), _r(y)] for x, y in pts]})

    if frames:
        sheets = [{"idx": i, "bbox": f["bbox"], "frame_block": _clean(f["block"]) if f["block"] else None, "meta": f["sheet"],
                   "role": f.get("role", "main"), "layout": f.get("layout")}
                  for i, f in enumerate(frames)]
    elif len(orphan_meta) <= 1:
        # 沒有圖框、最多一個圖號：整個模型空間算一張圖
        sheets = [{"idx": 0, "bbox": None, "frame_block": None, "meta": orphan_meta[0] if orphan_meta else {}}]
        orphan_meta = []
        for coll in (texts, inserts, polygons):
            for x in coll:
                x["f"] = 0
        for s in segments:
            s[5] = 0
    else:
        # 沒有圖框（例：圖框包在別的圖塊裡）卻有好幾個圖號：各列一張圖，內容不硬塞給其中一張（f 留 None＝未歸屬）
        sheets = [{"idx": i, "bbox": None, "frame_block": None, "meta": m} for i, m in enumerate(orphan_meta)]
        orphan_meta = []

    layers = [{"name": _clean(L.dxf.name), "color": L.dxf.get("color", 7), "on": L.is_on(), "frozen": L.is_frozen()}
              for L in doc.layers]
    return {
        "version": 1,
        "dxf": {"dxfversion": doc.dxfversion, "insunits": doc.header.get("$INSUNITS", 0),
                "entities": n, "audit_errors": len(auditor.errors), "audit_fixes": len(auditor.fixes)},
        "layers": layers,
        "sheets": sheets,
        "texts": texts,
        "inserts": inserts,
        "segments": segments,
        "polygons": polygons,
        "unassigned_meta": orphan_meta,
    }


def summary(ir: dict) -> dict:
    """存在 case_file.stats、給狀態頁看的摘要。"""
    return {"sheets": len(ir["sheets"]), "texts": len(ir["texts"]), "inserts": len(ir["inserts"]),
            "segments": len(ir["segments"]), "polygons": len(ir["polygons"]), "layers": len(ir["layers"]),
            "entities": ir["dxf"]["entities"], "audit_errors": ir["dxf"]["audit_errors"],
            "sheet_numbers": [sheet_number(s["meta"]) for s in ir["sheets"]],
            "unassigned_meta": len(ir["unassigned_meta"])}


def main(argv: list[str]) -> int:
    """python -m litian.drawing.ir <in.dxf> <out.json> [--expand 圖塊1,圖塊2]（--expand：已綁定的外部參考）"""
    src, dst = argv[1], argv[2]
    expand = argv[argv.index("--expand") + 1].split(",") if "--expand" in argv else ()
    ir = extract(src, expand=[x for x in expand if x])
    Path(dst).write_text(json.dumps(ir, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps(summary(ir), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
