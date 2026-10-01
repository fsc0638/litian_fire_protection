"""法規檢索評測：對 API 跑測試集，算前 3 名命中率。

用法：python -m litian.lawdb.evaluate --base http://127.0.0.1:18100 [--questions eval/law_questions.yaml] [--out eval/report.md]
驗收門檻（設計文件 §5.3）：前 3 名命中率 ≥ 95%。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx
import yaml


def run(base: str, questions: list[dict], k: int = 3) -> list[dict]:
    rows = []
    with httpx.Client(base_url=base, timeout=30) as c:
        for qd in questions:
            r = c.get("/api/law/search", params={"q": qd["q"], "limit": 5})
            r.raise_for_status()
            got = [x["node_id"] for x in r.json()["results"]]
            rank = next((i + 1 for i, nid in enumerate(got) if nid in qd["expect"]), None)
            rows.append({**qd, "got": got, "rank": rank, "hit": rank is not None and rank <= k})
    return rows


def report(rows: list[dict], k: int = 3) -> str:
    by = defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)
    total = sum(r["hit"] for r in rows)
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows)
    lines = [f"# 法規檢索評測（{datetime.now():%Y-%m-%d %H:%M}）", "",
             f"- 前 {k} 名命中：**{total}/{len(rows)}（{total / len(rows):.0%}）**，驗收門檻 95%",
             f"- 第 1 名命中：{sum(r['rank'] == 1 for r in rows)}/{len(rows)}；MRR {mrr:.3f}", "",
             "| 題型 | 題數 | 前 3 名命中 |", "|---|---|---|"]
    for t, rs in by.items():
        h = sum(r["hit"] for r in rs)
        lines.append(f"| {t} | {len(rs)} | {h}（{h / len(rs):.0%}） |")
    misses = [r for r in rows if not r["hit"]]
    lines += ["", f"## 未命中（{len(misses)} 題）", "", "| 編號 | 問題 | 正解 | 實際前 3 名 |", "|---|---|---|---|"]
    for r in misses:
        lines.append(f"| {r['id']} | {r['q']} | {'、'.join(r['expect'])} | {'、'.join(r['got'][:3])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18100")
    ap.add_argument("--questions", type=Path, default=Path("eval/law_questions.yaml"))
    ap.add_argument("--out", type=Path, default=Path("eval/report.md"))
    a = ap.parse_args()
    qs = yaml.safe_load(a.questions.read_text(encoding="utf-8"))
    md = report(run(a.base, qs))
    a.out.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
