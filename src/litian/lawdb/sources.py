"""第 0 期收錄的法規清單。

pcode：全國法規資料庫代碼（法律、命令）或消防署法令查詢系統的 lsid（行政規則）。
kind：law＝法律、order＝命令（兩者來自全國法規資料庫 Open API）；nfa＝消防署行政規則（網頁抓取，見 nfa.py）。
unit：條文單位。法律命令用「條」，行政規則用「點」。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LawSource:
    pcode: str
    kind: str            # "law" | "order" | "nfa"
    short: str           # 引用時用的簡稱
    aliases: tuple[str, ...]
    unit: str = "條"


LAWS: tuple[LawSource, ...] = (
    LawSource("D0120001", "law", "消防法", ("消防法",)),
    LawSource("D0120029", "order", "設置標準",
              ("各類場所消防安全設備設置標準", "消防安全設備設置標準", "消防設備設置標準", "設置標準")),
    LawSource("D0120002", "order", "消防法施行細則", ("消防法施行細則", "施行細則")),
    LawSource("D0120054", "order", "檢修及申報辦法",
              ("消防安全設備檢修及申報辦法", "檢修及申報辦法", "檢修申報辦法")),
    LawSource("D0120075", "order", "設計監造測試及檢修作業辦法",
              ("消防安全設備設計監造測試及檢修作業辦法", "設計監造測試及檢修作業辦法", "設計監造作業辦法")),
    LawSource("FL019489", "nfa", "審查及查驗作業基準",
              ("消防機關辦理建築物消防安全設備審查及查驗作業基準", "審查及查驗作業基準", "審查查驗作業基準",
               "審查作業基準", "查驗作業基準"), unit="點"),
)

MOJ_KINDS = ("law", "order")

BY_PCODE = {s.pcode: s for s in LAWS}

# 各法規條文中「本法」指的是哪一部（設置標準第 1 條：「消防法（以下簡稱本法）」）
THIS_ACT = "D0120001"

MOJ_API = {
    "law": "https://law.moj.gov.tw/api/ch/law/xml",
    "order": "https://law.moj.gov.tw/api/ch/order/xml",
}
