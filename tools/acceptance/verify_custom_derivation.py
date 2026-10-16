#!/usr/bin/env python3
"""C-03：真实 custom 推导，并在冻结小语料上执行抽取与非空写作。

Architect 实际调用模型推导 DirectionProfile 和 ResearchPlan；随后 Scout 使用一条
已经交付、版本明确的冻结正文做证据抽取，Writer 根据绑定事实写短报告。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))
os.environ["ENABLE_DIRECTION_PROFILES"] = "true"

from dotenv import load_dotenv

load_dotenv(BASE / ".env")

from core.runtime_settings import sanitize_no_proxy

sanitize_no_proxy()

from config.llm_config import get_config
from service.deep_research_v2.agents.architect import ChiefArchitect
from service.deep_research_v2.agents.scout import DeepScout
from service.deep_research_v2.agents.writer import LeadWriter
from service.deep_research_v2.state import ResearchPhase, create_initial_state
from service.research_integrity import ContractError, RUN_STATE, ingest_analysis, reduce_hypotheses


QUERY = "反馈协议中的无关格式约束会如何改变大语言模型评审的偏好排序与评分稳定性？"
SOURCE_URL = "https://arxiv.org/abs/2504.14716"


def contract_error_text(exc: ContractError, limit: int = 120) -> str:
    """ContractError 不是可切片对象；先转字符串再截断。"""
    return str(exc)[:limit]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_state(query: str, session_id: str) -> dict:
    state = create_initial_state(query=query, session_id=session_id, search_web=False, search_local=True)
    state["direction_request"] = {
        "choice": {"mode": "custom", "text": query},
        "start_year": 2022,
        "end_year": 2025,
        "focus_angles": ["偏好排序", "评分稳定性", "协议扰动"],
    }
    return state


async def run(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = get_config()
    architect = ChiefArchitect(config.api_key, config.base_url, config.agents.architect.model)

    print("=== [C-03] 1. 负例：非 AI/CS 领域必须由目标 ContractError 拒绝 ===")
    state_neg = build_state("西红柿炒鸡蛋的最佳火候与调味配比研究", "c03-r2-neg")
    neg_rejected = False
    neg_reason = None
    try:
        await architect._direction_planning(state_neg)
    except ContractError as exc:
        neg_rejected = True
        neg_reason = str(exc)
        print(f"  [PASS] ContractError: {contract_error_text(exc)}...")

    print("\n=== [C-03] 2. 正例：实际推导 custom 规则与计划 ===")
    state = build_state(QUERY, "c03-r2-positive")
    token = RUN_STATE.set(state)
    try:
        await architect._direction_planning(state)
    finally:
        RUN_STATE.reset(token)
    direction = state["resolved_direction"]
    assert direction["rule_origin"] == "inferred"
    assert state.get("derivation_history")
    assert state.get("research_plan")
    print(f"  [PASS] inferred profile={direction['profile_hash']} sections={len(state['outline'])}")

    print("\n=== [C-03] 3. 冻结小语料：实际抽取并绑定正文 ===")
    sources = read_jsonl(args.bundle / "delivered_sources.jsonl")
    source = next((item for item in sources if item.get("url") == args.source_url), None)
    if source is None:
        raise ContractError(f"frozen source not found: {args.source_url}")
    content = str(source.get("content") or "")
    if len(content) < 1000 or not source.get("version_id"):
        raise ContractError("frozen source must contain substantive text and an explicit version_id")

    full_outline = list(state["outline"])
    section = dict(full_outline[0])
    state["outline"] = [section]
    state["phase"] = ResearchPhase.RESEARCHING.value
    scout = DeepScout(config.api_key, config.base_url, config.search_api_key, config.agents.scout.model)
    subquestion = (section.get("research_questions") or [section.get("title")])[0]
    token = RUN_STATE.set(state)
    try:
        analysis = await scout._analyze_bound_results(QUERY, subquestion, [source], state["hypotheses"])
    finally:
        RUN_STATE.reset(token)
    ingest_analysis(state, analysis, [source], [section["id"]])
    reduce_hypotheses(state)
    if not state["facts"]:
        raise ContractError("small-corpus research produced no bound facts")
    print(f"  [PASS] facts={len(state['facts'])} edges={len(state['evidence_links'])}")

    print("\n=== [C-03] 4. 生产 Writer：生成非空、可引用短报告 ===")
    section["status"] = "pending"
    state["phase"] = ResearchPhase.WRITING.value
    writer = LeadWriter(config.api_key, config.base_url, config.agents.writer.model)
    token = RUN_STATE.set(state)
    try:
        await writer.process(state)
    finally:
        RUN_STATE.reset(token)
    report = str(state.get("final_report") or "")
    if len(report.strip()) < 200:
        raise ContractError("Writer did not produce a substantive report")
    if "[E:" not in report:
        raise ContractError("Writer report has no evidence citation token")

    source_record = {
        "url": source.get("url"),
        "version_id": source.get("version_id"),
        "publication_first_date": source.get("publication_first_date"),
        "version_date": source.get("version_date"),
        "content_sha256": sha256_text(content),
        "content_chars": len(content),
        "content": content,
    }
    (out / "frozen_source.json").write_text(
        json.dumps(source_record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    (out / "final_report.md").write_text(report, encoding="utf-8", newline="\n")
    summary = {
        "case_id": "c03_custom_small_corpus",
        "mode": "live_model_with_frozen_corpus",
        "execution_scope_note": ("完整保存实际推导的三节计划；本次 C-03 小语料验收只执行首个计划章节的"
                                 "生产 Scout 抽取与 Writer 写作，不声称完整 UI 或全计划执行"),
        "negative_control": {
            "rejected_as_expected": neg_rejected,
            "error_type": "ContractError" if neg_rejected else None,
            "contract_error": neg_reason,
        },
        "positive_control": {
            "query": QUERY,
            "rule_origin": direction["rule_origin"],
            "profile_hash": direction["profile_hash"],
            "derivation_history": state.get("derivation_history"),
            "research_plan": state.get("research_plan"),
            "full_plan_section_count": len(full_outline),
            "executed_section_id": section["id"],
            "source": {k: source_record[k] for k in source_record if k != "content"},
            "facts_count": len(state["facts"]),
            "evidence_links_count": len(state["evidence_links"]),
            "report_chars": len(report),
            "report_sha256": sha256_text(report),
            "llm_calls": state.get("llm_calls", []),
            "usage": state.get("usage", {}),
        },
        "assertions": {
            "negative_target_branch_reached": neg_rejected,
            "inferred_not_preset": direction["rule_origin"] == "inferred",
            "raw_derivation_saved": bool(state.get("derivation_history")),
            "small_corpus_bound_facts_nonempty": bool(state["facts"]),
            "writer_report_nonempty": len(report.strip()) >= 200,
            "writer_report_has_evidence_token": "[E:" in report,
        },
    }
    summary["passed"] = all(summary["assertions"].values())
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    if not summary["passed"]:
        raise AssertionError(json.dumps(summary["assertions"], ensure_ascii=False))
    print(f"\n[ALL PASS] evidence={out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True, help="含 delivered_sources.jsonl 的冻结运行目录")
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
