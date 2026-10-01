"""從全國法規資料庫官方 Open API 下載法規 ZIP，抽出收錄清單內的法規存成單檔 XML。

已知坑（2026-09-30 查證）：Python 3.13 起預設開啟 VERIFY_X509_STRICT，法務部憑證缺 Subject Key Identifier
會被拒。這裡只關掉這一個嚴格旗標，憑證鏈與主機名稱照常驗證。
"""

from __future__ import annotations

import re
import ssl
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx

from .sources import LAWS, MOJ_API, MOJ_KINDS


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def zip_path(raw_dir: Path, kind: str) -> Path:
    return raw_dir / f"moj_ch_{kind}_xml.zip"


def download(kind: str, raw_dir: Path) -> Path:
    dest = zip_path(raw_dir, kind)
    tmp = dest.with_suffix(".part")
    raw_dir.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", MOJ_API[kind], verify=ssl_context(), timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink()
        raise RuntimeError(f"{MOJ_API[kind]} 回傳的不是 ZIP")
    tmp.replace(dest)
    return dest


def extract(zip_file: Path, pcodes: set[str], raw_dir: Path) -> tuple[dict[str, Path], str]:
    """回傳 ({pcode: 單檔 XML 路徑}, 資料更新時間)。"""
    out: dict[str, Path] = {}
    update_date = ""
    with zipfile.ZipFile(zip_file) as z:
        inner = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        with z.open(inner) as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                if event == "start" and el.tag == "Laws":
                    update_date = el.get("UpdateDate", "")
                elif event == "end" and el.tag == "Law":
                    m = re.search(r"pcode=([A-Z0-9]+)", el.findtext("LawURL") or "")
                    if m and m.group(1) in pcodes:
                        p = raw_dir / f"{m.group(1)}.xml"
                        p.write_text(ET.tostring(el, encoding="unicode"), encoding="utf-8")
                        out[m.group(1)] = p
                    el.clear()
    return out, update_date


def fetch_all(raw_dir: Path, refresh: bool = False) -> dict:
    """下載（若需要）並抽出所有收錄法規。回傳 {pcode: path, '_update': {kind: date}}。"""
    result: dict = {"_update": {}}
    for kind in MOJ_KINDS:
        z = zip_path(raw_dir, kind)
        if refresh or not z.exists():
            download(kind, raw_dir)
        files, date = extract(z, {s.pcode for s in LAWS if s.kind == kind}, raw_dir)
        result.update(files)
        result["_update"][kind] = date
    missing = [s.pcode for s in LAWS if s.kind in MOJ_KINDS and s.pcode not in result]
    if missing:
        raise RuntimeError(f"官方資料包中找不到：{missing}")
    return result
