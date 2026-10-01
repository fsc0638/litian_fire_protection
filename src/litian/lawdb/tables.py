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


def validate(t: dict) -> None:
    for k in ("node_id", "citation", "source", "law_version", "status", "columns", "rows"):
        if k not in t:
            raise ValueError(f"{t.get('node_id')} 缺欄位 {k}")
    if t["status"] not in ("draft", "verified"):
        raise ValueError(f"{t['node_id']} status 只能是 draft 或 verified")
    if t["status"] == "verified" and not (t.get("verified_by") and t.get("verified_at")):
        raise ValueError(f"{t['node_id']} 標 verified 必須填 verified_by 與 verified_at")
    cols = set(t["columns"])
    for r in t["rows"]:
        for c in r.get("marks", []):
            if c not in cols:
                raise ValueError(f"{t['node_id']} 第 {r['no']} 列標記了不存在的欄：{c}")
        for c in r.get("cells", {}):
            if c not in cols:
                raise ValueError(f"{t['node_id']} 第 {r['no']} 列有不存在的欄：{c}")


def rows_text(t: dict) -> str:
    """供檢索用的表格文字（每列一行）。"""
    lines = []
    for r in t["rows"]:
        if "marks" in r:
            lines.append(f"{r['place']} 可選設：{'、'.join(r['marks'])}")
        else:
            parts = [f"{c}：{'、'.join(v['devices']) or '（不適用）'}" for c, v in r["cells"].items()]
            lines.append(f"{r['place']} " + "；".join(parts))
    return "\n".join(lines + t.get("notes", []))
