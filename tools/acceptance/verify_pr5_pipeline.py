#!/usr/bin/env python3
"""PR5 验收：证据块生产路径（table / annotation_protocol）+ DataAnalyst 实验记录化。

对应代码评审：
  - R10：每个对外宣称的 preset 所需证据块都要有产出路径（**不靠删要求**变绿）；
  - P1-6：DataAnalyst 仍在跑行业分析（市场规模/增长率/市场份额/企业排名），必须改成
    PaperVersion/Experiment/Method/Dataset/Model/Metric/Value/Unit/条件/SourceLocator；
    **无可比数据时返回不足，不得为了"有产出"编造图表或数值**；
  - 概念图不能代替 Claim–Evidence 图；暂时关闭优于错误图。

全部离线（不发网络请求、不调用模型）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
APP = BASE / "backend" / "app"
sys.path.insert(0, str(APP))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TABLE_DOC = """# A paper

## 2 Method
We propose X.

## 4 Results
| Model | Accuracy | F1 |
|---|---|---|
| Baseline | 58.1 | 55.0 |
| Ours | 62.5 | 60.2 |

## 5 Annotation Protocol
Three annotators labelled 500 samples; inter-annotator agreement (Cohen's kappa) was 0.71.
Sample size was chosen to reach a 95% confidence interval of +/- 3 points.
"""

REAL_HEADING_VARIANT = """2.3 Error Annotation
Annotation Process
Three in-house annotators independently labelled every sample using four labels.
The final label was determined by majority vote, and agreement was reported for the sample.
"""

KEYWORD_ONLY = ("Our contribution is a survey that compares method selection, dataset choice, "
                "table formatting and results reporting across papers. No experiments are run.")


def main() -> int:
    import os
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    from core.runtime_settings import sanitize_no_proxy
    sanitize_no_proxy()

    import service.research_sources as sources
    from service.direction_contracts import load_preset

    # ---------- [1] 证据块生产路径（R10） ----------
    print("\n[1] 证据块生产路径：table / annotation_protocol 必须是**结构**判定")
    blocks = set(sources._BLOCK_SIGNALS)
    check("生产者词汇表包含 table 与 annotation_protocol",
          {"table", "annotation_protocol"} <= blocks, json.dumps(sorted(blocks)))
    found = sources.detect_blocks(TABLE_DOC)
    check("真实表格（表头+分隔行+数据行）能被识别为 table",
          "table" in found, json.dumps(found))
    check("真实标注协议（标题+标注者/一致性/样本量）能被识别为 annotation_protocol",
          "annotation_protocol" in found, json.dumps(found))
    variant_found = sources.detect_blocks(REAL_HEADING_VARIANT)
    check("真实材料使用 Annotation Process 标题时仍识别协议正文",
          "annotation_protocol" in variant_found, json.dumps(variant_found))
    keyword_blocks = sources.detect_blocks(KEYWORD_ONLY)
    check("只提到 table/annotation 词汇但没有结构 → 不算任何块",
          not ({"table", "annotation_protocol"} & set(keyword_blocks)), json.dumps(keyword_blocks))
    header_only = "| Model | Accuracy |\n|---|---|\n"
    check("只有表头没有数据行 → 不算 table",
          "table" not in sources.detect_blocks(header_only),
          json.dumps(sources.detect_blocks(header_only)))
    empty_protocol = "## 5 Annotation Protocol\n\n(see supplementary material)\n"
    check("只有协议标题、没有协议内容 → 不算 annotation_protocol",
          "annotation_protocol" not in sources.detect_blocks(empty_protocol),
          json.dumps(sources.detect_blocks(empty_protocol)))
    # R10 的口径：每个预置档案要求的块都必须能被生产者产出（要求一条都没删）
    for preset in ("agent_memory", "rag_context", "llm_evaluation"):
        profile = load_preset(preset)
        required = set(profile.source_policy.required_source_blocks)
        for ct in profile.claim_types:
            required.update(ct.required_evidence_blocks)
        missing = sorted(required - blocks)
        check(f"{preset} 的全部必需证据块都有生产路径", not missing, json.dumps(missing))
    rag = load_preset("rag_context")
    check("rag_context 仍要求 table（要求未被删除）",
          any("table" in ct.required_evidence_blocks for ct in rag.claim_types)
          or "table" in rag.source_policy.required_source_blocks,
          json.dumps([ct.required_evidence_blocks for ct in rag.claim_types]))
    llm = load_preset("llm_evaluation")
    check("llm_evaluation 仍要求 annotation_protocol（要求未被删除）",
          any("annotation_protocol" in ct.required_evidence_blocks for ct in llm.claim_types)
          or "annotation_protocol" in llm.source_policy.required_source_blocks,
          json.dumps([ct.required_evidence_blocks for ct in llm.claim_types]))

    # ---------- [2] DataAnalyst 实验记录化 ----------
    print("\n[2] DataAnalyst：行业分析 → 实验记录；无可比数据返回不足")
    from service.deep_research_v2.agents.data_analyst import DataAnalyst
    for prompt_name in ("DATA_EXTRACTION_PROMPT", "CHART_GENERATION_PROMPT"):
        text = getattr(DataAnalyst, prompt_name)
        banned = [w for w in ("市场规模", "市场份额", "增长率", "企业排名") if w in text]
        check(f"{prompt_name} 不再要求行业数据", not banned, json.dumps(banned, ensure_ascii=False))
    check("抽取 Prompt 要求 paper_version/experiment_id/metric/value/unit/conditions/source_locator",
          all(k in DataAnalyst.DATA_EXTRACTION_PROMPT for k in
              ("paper_version", "experiment_id", "metric", "value", "unit", "conditions",
               "source_locator", "quote")), "")
    check("抽取 Prompt 明确「没有可比记录就返回不足、不得编造数值」",
          "不得" in DataAnalyst.DATA_EXTRACTION_PROMPT and "reason_if_none" in DataAnalyst.DATA_EXTRACTION_PROMPT)

    analyst = object.__new__(DataAnalyst)
    analyst.name, analyst.model = "DataAnalyst", "probe-model"
    analyst.logger = __import__("logging").getLogger("pr5")
    analyst.research_config = {"max_charts": 2}
    # 记录必须能在**实际交付的材料**里逐字核对，因此 fixture
    # 提供材料原文与一致的定位（合成前置数据，公开在脚本里）。
    MATERIAL = "Our method reaches 60.0% accuracy on NarrativeQA."
    state = {"query": "q", "facts": [{"id": "fact_1", "content": "accuracy 60.0 on NarrativeQA",
                                      "source_name": "s", "source_version": "v1",
                                      "source_quote": MATERIAL,
                                      "source_locator": {"kind": "section", "id": "5"}}],
             "data_points": [], "charts": [], "evidence_audit": [], "experiment_records": [],
             "messages": [], "phase": "analyzing"}
    calls = {"n": 0}

    async def fake_llm(**kwargs):
        calls["n"] += 1
        return json.dumps({"experiment_records": [
            # ① 无 quote / 无定位 → 必须被拒绝（宁缺勿造）
            {"paper_version": "arxiv:1", "metric": "accuracy", "value": 999, "unit": "%"},
            # ② 引用/定位/数值都能在交付材料里核对 → 唯一应被接纳的记录
            {"paper_version": "arxiv:2", "metric": "accuracy", "value": 60.0, "unit": "%",
             "quote": MATERIAL, "source_locator": {"kind": "section", "id": "5"},
             "conditions": {"task": "QA"}},
        ], "comparability": {"comparable_groups": [["arxiv:2", "missing-record"]],
                             "reason_if_none": "条件不同"}})

    analyst.call_llm = fake_llm
    analyst.parse_json_response = json.loads
    extracted = asyncio.run(analyst._extract_data(state))
    check("无法在交付材料里核对的实验记录被拒绝（只保留可核对的）",
          len(extracted["experiment_records"]) == 1
          and extracted["experiment_records"][0]["source_locator"] == {"kind": "section", "id": "5"}
          and extracted["experiment_records"][0].get("verified_against") == "supplied_material",
          json.dumps(extracted["experiment_records"], ensure_ascii=False)[:220])
    check("被拒记录留下审计条目（含逐条原因）",
          any(a.get("reason") == "experiment_records_rejected" and a.get("detail")
              for a in state["evidence_audit"]), json.dumps(state["evidence_audit"], ensure_ascii=False)[:180])
    check("comparable_groups 确定性校验：成员不存在/条件不一致的组被丢弃",
          extracted["comparability"]["comparable_groups"] == [],
          json.dumps(extracted["comparability"], ensure_ascii=False)[:200])
    charts = asyncio.run(analyst._generate_charts(state, extracted))
    check("没有声明的可比组 → 不产图，并记录不足原因",
          charts == [] and any(a.get("reason") == "no_comparable_groups"
                               for a in state.get("analysis_insufficient", [])),
          json.dumps(state.get("analysis_insufficient"), ensure_ascii=False))
    check("无实验记录时 _generate_charts 也不产图",
          asyncio.run(analyst._generate_charts(state, {"experiment_records": []})) == [])
    graph = asyncio.run(analyst._build_knowledge_graph(state))
    check("概念图已关闭并给出原因（不产出错误图）",
          graph.get("nodes") == [] and graph.get("edges") == []
          and graph.get("disabled_reason", "").startswith("concept_graph_disabled"),
          json.dumps(graph, ensure_ascii=False)[:160])
    # 无事实材料时也要如实返回不足，而不是空转
    empty_state = dict(state, facts=[])
    empty = asyncio.run(analyst._extract_data(empty_state))
    check("没有任何已入证材料 → 明确返回不足",
          empty.get("insufficient") is True and not empty.get("experiment_records"),
          json.dumps(empty, ensure_ascii=False)[:120])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nPR5（证据块 + DataAnalyst）验收: {passed}/{len(RESULTS)} PASS")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
