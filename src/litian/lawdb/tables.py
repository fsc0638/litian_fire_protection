"""法定表格的結構化檔（data/tables/*.yaml）。

為什麼要結構化：設置標準有些「應設什麼」的判斷寫在表格裡（第 18 條選設表、第 157 條避難器具表），
網頁與 API 文字裡不是缺表（第 18 條只在 PDF）、就是方框字元排版（第 157 條），規則引擎與檢索都無法直接用。

每張表一個 YAML，status 為 draft（開發者轉錄，未經消防設備師校對）或 verified（已校對簽名）。
draft 的表 API 一律附警告，規則引擎不得當作已確認的法源。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

TABLE_DIR = Path("data/tables")
BOX = "┌┐└┘├┤┬┴┼─│"


# ---------- 方框字元表格（第 157 條等） ----------

def box_rows(text: str) -> list[list[list[str]]]:
    """把方框字元表格切成「列」，每列是「儲存格片段」：rows[i][c] = 該儲存格各行文字。

    以「├」開頭的分隔線切列；每一行以「│」切儲存格（不靠字元位置，因為半形與全形字寬不同會錯位）。
    """
    rows: list[list[list[str]]] = []
    cur: list[list[str]] | None = None
    for line in text.split("\n"):
        s = line.strip()
        if not s or s[0] not in BOX:
            continue
        if s[0] in "┌├└":
            if cur:
                rows.append(cur)
            cur = None
            continue
        parts = [p.strip() for p in s.split("│")[1:-1]]
        if cur is None:
            cur = [[] for _ in parts]
        if len(parts) != len(cur):                     # 同一列內欄數不變，否則表示切錯
            raise ValueError(f"欄數不一致：{s[:30]}")
        for i, p in enumerate(parts):
            if p:
                cur[i].append(p)
    if cur:
        rows.append(cur)
    return rows


def _join(frags: list[str]) -> str:
    return "".join(frags).strip()


def parse_157(article_text: str) -> dict:
    """第 157 條避難器具表 → {columns, rows, notes}。「同上」解析為往上最近一個非空、非「同上」的同欄儲存格。"""
    rows = box_rows(article_text)
    header, data, notes = rows[0], rows[1:-1], rows[-1]
    columns = [_join(c) for c in header[1:]]
    out_rows = []
    resolved_above: list[dict | None] = [None] * len(columns)
    raw_above: list[str | None] = [None] * len(columns)      # 正上方那一格的原文
    for r in data:
        no, place, cells = _join(r[0]), _join(r[1]), r[2:]
        row = {"no": int(no), "place": place, "cells": {}}
        for ci, frag in enumerate(cells):
            raw = _join(frag)
            cell: dict = {"raw": raw}
            if raw == "同上":
                src = resolved_above[ci]
                cell["devices"] = list(src["devices"]) if src else []
                cell["same_as_row"] = src["row"] if src else None
                if raw_above[ci] == "":                       # 正上方是空白格：「同上」指誰有疑義
                    cell["review"] = (f"「同上」正上方第 {out_rows[-1]['no']} 列此欄為空白，"
                                      f"暫依往上最近非空格（第 {src['row']} 列）解讀，請消防設備師確認")
            elif raw:
                cell["devices"] = [d for d in re.split(r"[、，]", raw) if d]
                resolved_above[ci] = {"row": int(no), "devices": cell["devices"]}
            else:
                cell["devices"] = []
                cell["blank"] = True
            raw_above[ci] = raw
            row["cells"][columns[ci]] = cell
        out_rows.append(row)
    note = _join(notes[0])
    return {"columns": columns, "rows": out_rows, "notes": [note]}


# ---------- 載入與檢查 ----------

def load_tables(table_dir: Path = TABLE_DIR) -> list[dict]:
    tables = []
    for p in sorted(table_dir.glob("*.yaml")):
        t = yaml.safe_load(p.read_text(encoding="utf-8"))
        validate(t)
        tables.append(t)
    return tables


PART_KINDS = ("table", "diagram", "formula")


def parts_of(t: dict) -> list[dict]:
    """一條的表格部分。新格式用 parts（可含多張表、公式、配線圖）；舊格式（第 18、157 條）整份就是一張表。"""
    return t["parts"] if "parts" in t else [t]


def _validate_part(nid: str, p: dict, label: str) -> None:
    kind = p.get("kind", "table")
    if kind not in PART_KINDS:
        raise ValueError(f"{nid} {label} kind 只能是 {'/'.join(PART_KINDS)}")
    if kind == "formula":
        if not p.get("formula"):
            raise ValueError(f"{nid} {label} 公式部分缺 formula")
        return
    for k in ("columns", "rows"):
        if k not in p:
            raise ValueError(f"{nid} {label} 缺欄位 {k}")
    cols = set(p["columns"])
    for r in p["rows"]:
        for c in r.get("marks", []):
            if c not in cols:
                raise ValueError(f"{nid} {label}第 {r['no']} 列標記了不存在的欄：{c}")
        for c in r.get("cells", {}):
            if c not in cols:
                raise ValueError(f"{nid} {label}第 {r['no']} 列有不存在的欄：{c}")


def validate(t: dict) -> None:
    for k in ("node_id", "citation", "source", "law_version", "status"):
        if k not in t:
            raise ValueError(f"{t.get('node_id')} 缺欄位 {k}")
    if "parts" not in t and not ("columns" in t and "rows" in t):
        raise ValueError(f"{t['node_id']} 缺欄位 columns／rows（或改用 parts）")
    if t["status"] not in ("draft", "verified"):
        raise ValueError(f"{t['node_id']} status 只能是 draft 或 verified")
    if t["status"] == "verified" and not (t.get("verified_by") and t.get("verified_at")):
        raise ValueError(f"{t['node_id']} 標 verified 必須填 verified_by 與 verified_at")
    multi = "parts" in t
    for i, p in enumerate(parts_of(t), 1):
        _validate_part(t["node_id"], p, f"第 {i} 部分" if multi else "")


def cell_text(v: dict) -> str:
    """儲存格文字：第 157 條有拆好的器具清單（devices）就用清單，其他用原文（raw）。"""
    if "devices" in v:
        return "、".join(v["devices"]) or "（不適用）"
    return str(v.get("raw", ""))


ROW_FOCUS_MIN = 4
_FOCUS_SPLIT = re.compile(r"[／、，；：（）()\s]+")


def _row_matches(place: str, focus: str) -> bool:
    return any(len(x) >= ROW_FOCUS_MIN and x in focus for x in _FOCUS_SPLIT.split(str(place)))


def rows_text(t: dict, focus: str | None = None, limit: int | None = None) -> str:
    """供檢索與 AI 問答用的表格文字（每列一行；公式寫出變數定義）。

    給 focus（使用者問題）與 limit 時：全文超過 limit 字，就只留列名對上問題的列（表名、公式、備註全留），
    避免大表（例：第 198 條 5 千字）被截斷、剛好切掉問題要的那一列與說明符號的備註（2026-10-01 實際發生）。
    沒有任何列對上時用全文。
    """
    full = _rows_text(t, None)
    if not focus or limit is None or len(full) <= limit:
        return full
    focused, kept = _rows_text(t, focus, count=True)
    return focused if kept else full


def _rows_text(t: dict, focus: str | None, count: bool = False):
    multi = "parts" in t
    lines: list[str] = []
    skipped = kept = 0
    for p in parts_of(t):
        if multi and p.get("title"):
            lines.append(f"【{p['title']}】")
        if p.get("kind") == "formula":
            lines.append(f"公式：{p['formula']}")
            lines += [f"{k}：{v}" for k, v in (p.get("variables") or {}).items()]
        else:
            for r in p["rows"]:
                if focus is not None and not _row_matches(r.get("place", ""), focus):
                    skipped += 1
                    continue
                kept += 1
                if "marks" in r:
                    lines.append(f"{r['place']} 可選設：{'、'.join(r['marks'])}")
                else:
                    cells = [f"{c}：{cell_text(v)}" for c, v in r.get("cells", {}).items()]
                    lines.append(f"{r['place']} " + "；".join(cells))
        if multi:
            lines += p.get("notes", [])
    if skipped:
        lines.append(f"（本表另有 {skipped} 列與本題無關，未列出；完整表格請看官方原文）")
    text = "\n".join(lines + t.get("notes", []))
    return (text, kept) if count else text
