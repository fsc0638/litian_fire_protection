"""外部參考（XREF）綁定：把同一案件裡上傳的參考檔內容併進主圖。

消防圖常以建築平面為外部參考（例：主圖套用 Area_1F.dwg 當底圖）：主圖裡只記「這裡套用某檔」，
牆、門、房名都在參考檔裡。外部參考記的是設計者電腦上的路徑（d:\\工程\\…\\Area_1F.dwg），
所以改在同一案件的上傳檔裡依檔名找，找到的用 ezdxf.xref.embed 併進圖塊定義，另存一份綁定後的 DXF。

參考檔若是 DWG 且還沒轉檔，由呼叫端提供的 convert 函式現轉。找不到的列在 missing，
之後該檔上傳完成時，worker 會把引用它的檔案重新排入處理。
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Callable

from .cli import safe_name

UPLOAD_PREFIX = re.compile(r"^\d{3}_")


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


def case_candidates(case_dir: Path, exclude: Path | None = None) -> dict[str, Path]:
    """案件資料夾裡的上傳檔：檔名主體 → 原始檔（DWG／DXF）。"""
    out: dict[str, Path] = {}
    for p in sorted(case_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".dwg", ".dxf") or ".converted" in p.name or ".bound" in p.name:
            continue
        if exclude is not None and p.resolve() == exclude.resolve():
            continue
        out.setdefault(UPLOAD_PREFIX.sub("", p.stem).lower(), p)
    return out


def bind(src: Path, case_dir: Path, out: Path, convert: Callable[[Path], Path] | None = None,
         original: Path | None = None) -> dict:
    """把 src（主圖 DXF）的外部參考併進來，寫到 out。回傳 {"bound": [圖塊名], "bound_files": [綁進來的上傳檔名],
    "missing": [參考檔名], "path": 使用的 DXF}。上傳檔名是案件資料夾裡的存檔名（001_Area_1F.dwg），工作台用來標出哪個檔被併入。
    沒有外部參考、或一個都綁不到時不寫檔，path 為 src。"""
    from ezdxf import recover, xref

    doc, _ = recover.readfile(str(src))
    refs = xref_blocks(doc)
    info = {"bound": [], "bound_files": [], "missing": [], "path": str(src)}
    if not refs:
        return info
    cands = case_candidates(case_dir, exclude=original)
    for name, path in refs:
        key = ref_key(path)
        cand = cands.get(key)
        if cand is None:
            info["missing"].append(PureWindowsPath(path).name or name)
            continue
        dxf = cand
        if cand.suffix.lower() == ".dwg":
            conv = cand.with_name(cand.stem + ".converted.dxf")
            if not conv.exists():
                if convert is None:
                    info["missing"].append(cand.name)
                    continue
                conv = convert(cand)
            dxf = conv
        blk = doc.blocks.get(name)
        blk.block.dxf.xref_path = str(Path(dxf).resolve())      # 指向案件裡的檔，ezdxf 才找得到
        try:
            xref.embed(blk, load_fn=lambda p: recover.readfile(p)[0])
            info["bound"].append(name)
            info["bound_files"].append(cand.name)
        except Exception as e:                                   # 版本較新、檔案損壞等：不中斷主圖處理
            info["missing"].append(f"{cand.name}（{type(e).__name__}）")
    if info["bound"]:
        doc.saveas(str(out))
        info["path"] = str(out)
    return info


def main(argv: list[str]) -> int:
    """worker 用子行程呼叫（讀不可信的 DXF，限時限記憶體）：
    python -m litian.drawing.xref list <主圖.dxf>                         → 未綁定的外部參考 [[圖塊名, 路徑, 比對鍵]]
    python -m litian.drawing.xref bind <主圖.dxf> <案件資料夾> <輸出.dxf> [原始上傳檔] → 綁定結果"""
    import json
    if argv[1] == "list":
        from ezdxf import recover
        doc, _ = recover.readfile(argv[2])
        print(json.dumps([[n, p, ref_key(p)] for n, p in xref_blocks(doc)], ensure_ascii=False))
        return 0
    if argv[1] == "bind":
        original = Path(argv[5]) if len(argv) > 5 else None
        print(json.dumps(bind(Path(argv[2]), Path(argv[3]), Path(argv[4]), original=original), ensure_ascii=False))
        return 0
    raise SystemExit("用法：list <dxf> | bind <dxf> <case_dir> <out> [original]")


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
