#!/usr/bin/env python3
"""诊断：正文与「发表/版本日期」为什么在真实运行里互斥（round2-B1 联调发现）。

背景（真实运行，2026-09-21）：
  两个方向各跑一次真实纵向联调（严格时间窗口 2022–2025），**交付材料均为 0 条**，
  证据审计显示全部来源 `date_unknown_deferred`。追问「日期到底在哪个页面」时发现：

    - `https://arxiv.org/abs/<id>`  **有** `Submitted on …`（可解析出发表日期）；
    - `https://arxiv.org/html/<id>` **没有**任何提交/发布日期，也没有 citation_* 元数据。

  而 `candidate_fulltext_urls()` 有意把 `/html/` 排在第一位（正文最完整），
  于是抓取层拿到的是**没有日期的正文页**，严格窗口只能把它判为「日期未知」并延后；
  若回退到 `/abs/` 摘要页则相反：日期有了，正文没有（3k 字符摘要）。
  两者目前不可能同时满足，这不是判定标准的问题，而是**证据来源的结构问题**。

本脚本只做**观测**，不修改任何判定：对给定 arXiv 条目分别打印
  (a) 原始 HTTP：/abs/ 与 /html/ 是否含提交日期、字符数；
  (b) 生产路径 `enrich_result()` 实际抓到什么、判成什么范围、窗口三态结论。
用法：
    python tools/acceptance/probe_material_date_provenance.py --ids 2005.11401 2407.16833
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))


def load_service():
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    from core.runtime_settings import sanitize_no_proxy
    sanitize_no_proxy()
    from service import research_sources as S
    return S


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="+", default=["2005.11401", "2212.10496", "2407.16833"])
    ap.add_argument("--start-year", type=int, default=2022)
    ap.add_argument("--end-year", type=int, default=2025)
    args = ap.parse_args()

    S = load_service()
    import requests

    rows = []
    for ident in args.ids:
        row: dict = {"id": ident}
        for kind in ("abs", "html"):
            url = f"https://arxiv.org/{kind}/{ident}"
            try:
                resp = requests.get(url, timeout=30, headers={"User-Agent": S._UA}, allow_redirects=True)
                body = resp.text
                row[kind] = {
                    "status": resp.status_code,
                    "chars": len(body),
                    "has_submitted_on": "Submitted on" in body,
                    "has_citation_date_meta": "citation_publication_date" in body,
                    "submitted_line": (re.search(r"Submitted on[^<]*", body).group(0)[:60]
                                       if "Submitted on" in body else None),
                }
            except Exception as exc:  # noqa: BLE001 — 诊断脚本：如实记录失败
                row[kind] = {"error": f"{type(exc).__name__}: {exc}"}
        enriched = S.enrich_result({"url": f"https://arxiv.org/abs/{ident}"}, allow_fetch=True)
        row["enrich_result"] = {
            "candidates": S.candidate_fulltext_urls(f"https://arxiv.org/abs/{ident}"),
            "fulltext_url": enriched.get("fulltext_url"),
            "fulltext_chars": enriched.get("fulltext_chars"),
            "material_status": enriched.get("material_status"),
            "material_scope": enriched.get("material_scope"),
            "material_reasons": enriched.get("material_reasons"),
            "publication_first_date": enriched.get("publication_first_date"),
            "version_date": enriched.get("version_date"),
            "date_source": enriched.get("date_source"),
        }
        row["within_window"] = list(S.within_window(enriched, args.start_year, args.end_year))
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=1))
        print("-" * 78)

    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
