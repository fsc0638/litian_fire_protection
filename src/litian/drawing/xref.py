"""外部參考（XREF）綁定：把同一案件裡上傳的參考檔內容併進主圖。

消防圖常以建築平面為外部參考（例：主圖套用 Area_1F.dwg 當底圖）：主圖裡只記「這裡套用某檔」，
牆、門、房名都在參考檔裡。外部參考記的是設計者電腦上的路徑（d:\\工程\\…\\Area_1F.dwg），
所以改在同一案件的上傳檔裡依檔名找，找到的用 ezdxf.xref.embed 併進圖塊定義，另存一份綁定後的 DXF。

參考檔若是 DWG 且還沒轉檔，由呼叫端提供的 convert 函式現轉。找不到的列在 missing，
之後該檔上傳完成時，worker 會把引用它的檔案重新排入處理。
"""

from __future__ import annotations

import gc
import re
from pathlib import Path, PureWindowsPath
from typing import Callable

from .cli import safe_name

UPLOAD_PREFIX = re.compile(r"^\d{3,}_")              # 上傳序號（001_；第 1000 個起是 4 位數）


def xref_blocks(doc) -> list[tuple[str, str]]:
    """主圖中尚未綁定（沒有內容）的外部參考：[(圖塊名稱, 參考路徑)]。"""
    out = []
    for b in doc.blocks:
        blk = b.block
        if blk is not None and (blk.is_xref or blk.is_xref_overlay) and len(b) == 0:
            out.append((b.name, blk.dxf.get("xref_path", "") or b.name))
    return out


def ref_key(xref_path: str) -> str:
    """參考路徑 → 比對用的檔名主體（不分大小寫、與上傳檔名同樣清理）。"""
    name = PureWindowsPath(xref_path).name or xref_path
    return Path(safe_name(name)).stem.lower()


def upload_no(name: str) -> int:
    """存檔名的上傳序號（001_Area_1F.dwg → 1）；沒有序號的當 0（最早）。"""
    m = UPLOAD_PREFIX.match(name)
    return int(m.group(0)[:-1]) if m else 0


def strip_note(name: str) -> str:
    """去掉結尾的「（錯誤）」註記（綁定失敗時記的「001_B.dwg（DXFStructureError）」）。"""
    return re.sub(r"（[^（）]*）$", "", str(name))


def name_key(stored: str) -> str:
    """案件資料夾裡的存檔名（001_Area_1F.dwg）→ 比對用的檔名主體：去掉上傳序號與註記，不分大小寫。
    外部參考的原始路徑用 ref_key（檔名本身可能就是「20240315_」這類數字開頭，不能當序號去掉）。"""
    return UPLOAD_PREFIX.sub("", ref_key(strip_note(stored)))


class _SameNameMain(Exception):
    """候選檔自己也引用同名的外部參考：可能是同名主圖的另一個版本，先試別的候選。"""


def main_version(stored: str, xref_stats: dict | None) -> bool:
    """這個上傳檔綁進過另一份同名的上傳檔：是同名主圖的一個版本（不是底圖）。舊資料沒有 bound_files 時不判斷。"""
    key = name_key(stored)
    return any(name_key(b) == key and str(b).lower() != stored.lower() for b in (xref_stats or {}).get("bound_files") or [])


def case_candidates(case_dir: Path, exclude: Path | None = None) -> dict[str, list[Path]]:
    """案件資料夾裡的上傳檔：檔名主體 → 原始檔（DWG／DXF）清單，同名重新上傳時最新的排最前（綁定優先用最新的，
    讀不了才退回較早上傳的）。"""
    out: dict[str, list[Path]] = {}
    for p in sorted(case_dir.iterdir(), key=lambda p: (-upload_no(p.name), p.name)):
        if not p.is_file() or p.suffix.lower() not in (".dwg", ".dxf") or ".converted" in p.name or ".bound" in p.name:
            continue
        if exclude is not None and p.resolve() == exclude.resolve():
            continue
        out.setdefault(UPLOAD_PREFIX.sub("", p.stem).lower(), []).append(p)
    return out


def bind(src: Path, case_dir: Path, out: Path, convert: Callable[[Path], Path] | None = None,
         original: Path | None = None, skip=()) -> dict:
    """把 src（主圖 DXF）的外部參考併進來，寫到 out。回傳 {"bound": [圖塊名], "bound_files": [綁進來的上傳檔名],
    "missing": [沒綁到的參考檔名（原始路徑的檔名）], "failed": [試過讀不了的上傳檔名（錯誤）], "path": 使用的 DXF}。
    上傳檔名是案件資料夾裡的存檔名（001_Area_1F.dwg），工作台用來標出哪個檔被併入。skip：不用的上傳檔名（處理失敗的）。
    同名的上傳檔最新的先試；讀不了、或看起來是同名主圖的另一版（自己也引用同名參考）就試較早上傳的；
    候選全都自己也引用同名參考時（例：建築底圖 1F 又疊了結構圖 1F），照用最新的那份；
    但處理中的檔自己也叫這個名字時不這麼做（候選多半是它自己的舊版，綁進來等於疊著舊版檢核）。
    沒有外部參考、或一個都綁不到時不寫檔，path 為 src。"""
    from ezdxf import recover, xref

    doc, _ = recover.readfile(str(src))
    refs = xref_blocks(doc)
    info = {"bound": [], "bound_files": [], "missing": [], "failed": [], "path": str(src)}
    skip = {str(s).lower() for s in skip}
    if not refs:
        return info
    cands = case_candidates(case_dir, exclude=original)
    own = name_key(original.name) if original else None
    for name, path in refs:
        key = ref_key(path)
        blk = doc.blocks.get(name)

        def load(p, key=key):
            d = recover.readfile(p)[0]
            if any(ref_key(x) == key for _, x in xref_blocks(d)):
                del d
                gc.collect()                                     # 整份圖先放掉再丟例外（例外會留著這層的變數）
                raise _SameNameMain(p)
            return d

        bound, held = False, []                                  # held：自己也引用同名參考的候選
        for cand in cands.get(key, []):                          # 同名的最新上傳先試
            if cand.name.lower() in skip:
                continue
            dxf = cand
            if cand.suffix.lower() == ".dwg":
                conv = cand.with_name(cand.stem + ".converted.dxf")
                if not conv.exists():
                    if convert is None:
                        info["failed"].append(f"{cand.name}（尚未轉檔）")
                        continue
                    conv = convert(cand)
                dxf = conv
            blk.block.dxf.xref_path = str(Path(dxf).resolve())   # 指向案件裡的檔，ezdxf 才找得到
            try:
                xref.embed(blk, load_fn=load)
                info["bound"].append(name)
                info["bound_files"].append(cand.name)
                bound = True
                break
            except _SameNameMain:
                held.append((cand, dxf))
                continue
            except Exception as e:                               # 版本較新、檔案損壞等：不中斷主圖處理
                info["failed"].append(f"{cand.name}（{type(e).__name__}）")
                if len(blk):                                     # 併到一半：不再拿別的檔疊上去
                    break
        if not bound and held and not len(blk) and key != own:
            cand, dxf = held[0]
            blk.block.dxf.xref_path = str(Path(dxf).resolve())
            try:
                xref.embed(blk, load_fn=lambda p: recover.readfile(p)[0])
                info["bound"].append(name)
                info["bound_files"].append(cand.name)
                bound = True
            except Exception as e:
                info["failed"].append(f"{cand.name}（{type(e).__name__}）")
        if not bound:
            info["missing"].append(PureWindowsPath(path).name or name)
    if info["bound"]:
        doc.saveas(str(out))
        info["path"] = str(out)
    return info


def main(argv: list[str]) -> int:
    """worker 用子行程呼叫（讀不可信的 DXF，限時限記憶體）：
    python -m litian.drawing.xref list <主圖.dxf>                         → 未綁定的外部參考 [[圖塊名, 路徑, 比對鍵]]
    python -m litian.drawing.xref bind <主圖.dxf> <案件資料夾> <輸出.dxf> [原始上傳檔 [不用的上傳檔名 JSON 清單]] → 綁定結果"""
    import json
    if argv[1] == "list":
        from ezdxf import recover
        doc, _ = recover.readfile(argv[2])
        print(json.dumps([[n, p, ref_key(p)] for n, p in xref_blocks(doc)], ensure_ascii=False))
        return 0
    if argv[1] == "bind":
        original = Path(argv[5]) if len(argv) > 5 else None
        skip = json.loads(argv[6]) if len(argv) > 6 else []
        print(json.dumps(bind(Path(argv[2]), Path(argv[3]), Path(argv[4]), original=original, skip=skip), ensure_ascii=False))
        return 0
    raise SystemExit("用法：list <dxf> | bind <dxf> <case_dir> <out> [original [skip_json]]")


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
