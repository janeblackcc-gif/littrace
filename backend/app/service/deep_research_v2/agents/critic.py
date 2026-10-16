# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - 毒舌评论家 Agent (CriticMaster)

职责：
1. 对抗式质检 - 永远不满意，找出问题
2. 逻辑漏洞检测 - 检查推理链条
3. 幻觉查杀 - 识别无来源或错误的信息
4. 偏见识别 - 发现观点偏颇
"""
import json
import math
from service.research_integrity import (ContractError, facts_text, validate_citations, chunks_covering, report_hash, validate_research_config)

import uuid
from typing import Dict, Any, List

# 严重度归一化。
# 为什么必须归一而不是直接拒绝：severity 由 LLM 自由生成，实测会返回
# critical/major/minor 之外的词（如 high/medium/low），旧实现直接抛
# ContractError("Invalid issue severity") 会让整轮研究失败。
# severity 参与「是否需要重新检索」「unresolved_issues 计数」「是否 pass」的判定，
# 故按语义映射到最近档位（而非一律降级），并把原始值记进 severity_coerced_from 便于复核。
_SEVERITY_HIGH = {"critical", "blocker", "fatal", "severe", "high", "p0", "p1"}
_SEVERITY_LOW = {"minor", "low", "trivial", "nit", "info", "informational", "suggestion", "p4", "p5"}


def normalize_severity(value: Any) -> tuple[str, str | None]:
    """返回 (归一化severity, 原值或None)。

    未知词按「宁可当严重」处理，避免把潜在阻断问题静默降级为 minor。
    """
    if not isinstance(value, str):
        return "major", repr(value)
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    if key in {"critical", "major", "minor"}:
        return key, None
    if key in _SEVERITY_HIGH:
        return "critical", value
    if key in _SEVERITY_LOW:
        return "minor", value
    if "critical" in key or "block" in key or "fatal" in key:
        return "critical", value
    if "major" in key or "high" in key or "medium" in key:
        return "major", value
    if "minor" in key or "low" in key:
        return "minor", value
    return "major", value
from datetime import datetime

from .base import BaseAgent
from ..state import ResearchState, ResearchPhase


class CriticMaster(BaseAgent):
    """
    毒舌评论家 - 质量守门人

    特点：
    - 对抗式思维：假设一切都有问题
    - 严格的证据要求
    - 逻辑一致性检查
    - 有权打回重写
    """

    REVIEW_PROMPT = """你是一位极其严苛的学术审稿人和事实核查专家。你的任务是找出研究报告中的所有问题。

## 审核原则（必须严格执行）
1. **零容忍幻觉**：任何没有明确来源的数据或事实，都是问题
2. **逻辑闭环**：论点必须有论据支撑，论据必须有来源
3. **偏见警惕**：单方面观点、情绪化表达都是问题
4. **时效性**：过时的数据（超过2年）必须标注
5. **完整性**：是否遗漏重要方面

## 研究问题
{query}

## 研究大纲
{outline}

## 待审核内容

### 章节草稿
{draft_content}

### 引用的事实
{facts}

### 使用的数据点
{data_points}

## 任务
逐条审核上述内容，找出所有问题。你必须扮演一个"找茬专家"的角色。

## 输出格式
```json
{{
    "overall_assessment": {{
        "quality_score": 1-10,
        "verdict": "pass/needs_revision/major_issues",
        "summary": "整体评估摘要"
    }},
    "issues": [
        {{
            "id": "issue_1",
            "target_section": "章节ID或'全局'",
            "issue_type": "missing_source/logic_error/bias/hallucination/outdated/incomplete",
            "severity": "critical/major/minor",
            "location": "具体位置描述",
            "description": "问题详细描述",
            "evidence": "为什么这是问题的证据",
            "suggestion": "具体的修改建议",
            "requires_new_search": true或false,
            "search_query": "如果需要补充搜索，建议的关键词"
        }}
    ],
    "fact_check_results": [
        {{
            "fact_id": "事实ID",
            "status": "verified/unverified/suspicious/false",
            "reason": "判断理由"
        }}
    ],
    "missing_aspects": ["报告中遗漏的重要方面"],
    "strength_points": ["报告中做得好的地方"]
}}
```

## 严重程度说明
- critical: 必须修复，否则报告不可用（如：核心数据错误、严重幻觉）
- major: 强烈建议修复，影响报告质量（如：缺少来源、逻辑漏洞）
- minor: 建议修复，提升报告质量（如：表述不够精确）

## 评分标准（1-10分制）
- 9-10分：优秀，几乎无问题，可直接发布
- 7-8分：良好，有小问题但不影响整体质量，审核通过（verdict=pass）
- 5-6分：一般，有明显问题需要修订
- 3-4分：较差，问题较多，需要大幅修改
- 1-2分：很差，存在严重问题或大量错误

注意：quality_score >= 7 时才能设置 verdict 为 "pass"

开始你的审核："""

    FINAL_CHECK_PROMPT = """你是最终质量把关人。这是修订后的研究报告。

## 原始问题
{query}

## 之前的问题
{previous_issues}

## 修订后的内容
{revised_content}

## 任务
检查之前的问题是否已解决，是否有新问题产生。

输出JSON：
```json
{{
    "resolved_issues": ["已解决的问题ID列表"],
    "unresolved_issues": ["未解决的问题ID列表"],
    "new_issues": [{{
        "description": "新发现的问题",
        "severity": "critical/major/minor"
    }}],
    "final_verdict": "approved/needs_more_work",
    "final_score": 1-10,
    "publication_readiness": "ready/almost_ready/not_ready",
    "final_comments": "最终评语"
}}
```"""

    def __init__(self, llm_api_key: str, llm_base_url: str, model: str = "qwen-max"):
        super().__init__(
            name="CriticMaster",
            role="毒舌评论家",
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            model=model
        )

    async def process(self, state: ResearchState) -> ResearchState:
        if state["phase"] != ResearchPhase.REVIEWING.value:
            return state
        result = await self._review_content(state)
        state.setdefault("review_history", []).append({"report_hash": state.get("reviewed_report_hash"),
            "feedback": state.get("critic_feedback", []), "quality_score": state.get("quality_score")})
        state["critic_feedback"] = []
        for issue in result.get("issues", []):
            state["critic_feedback"].append(dict(issue, id="issue_" + uuid.uuid4().hex[:8], resolved=False))
        assessment = result["overall_assessment"]
        state["quality_score"] = assessment["quality_score"]
        state["unresolved_issues"] = sum(i.get("severity") in {"critical", "major"} for i in state["critic_feedback"])
        state["review_verdict"] = assessment["verdict"]
        state["review_complete"] = result["review_complete"]
        state["review_coverage"] = result["review_coverage"]
        state["reviewed_report_hash"] = report_hash(state["final_report"])
        routing = self._analyze_issues_for_routing(result)
        state["review_route"] = "research" if routing["should_research"] else "revise"
        state["pending_search_queries"] = routing["search_queries"] if routing["should_research"] else []
        self.add_message(state, "review", {"agent": self.name, "verdict": state["review_verdict"],
            "quality_score": state["quality_score"], "issues_count": len(state["critic_feedback"]),
            "review_complete": state["review_complete"], "reviewed_report_hash": state["reviewed_report_hash"]})
        # The executor, not Critic, owns repair budgets and terminal run state.
        return state

    def _analyze_issues_for_routing(self, review_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析问题类型，决定路由方向

        Returns:
            {
                "should_research": bool,  # 是否需要重新搜索
                "search_queries": List[str]  # 建议的搜索查询
            }
        """
        issues = review_result.get("issues", [])
        missing_aspects = review_result.get("missing_aspects", [])

        # 需要补充搜索的问题类型
        research_needed_types = {"missing_source", "incomplete", "outdated"}

        search_queries = []
        research_issues_count = 0

        for issue in issues:
            issue_type = issue.get("issue_type", "")
            severity = issue.get("severity", "minor")

            # 检查是否是需要搜索的问题类型
            if issue_type in research_needed_types and severity in ["critical", "major"]:
                research_issues_count += 1

                # 收集搜索建议
                if issue.get("requires_new_search") and issue.get("search_query"):
                    search_queries.append(issue["search_query"])

        # 添加遗漏方面的搜索查询
        for aspect in missing_aspects[:3]:
            search_queries.append(aspect)

        # 决策：如果有超过30%的严重问题需要搜索，或者有明确的搜索建议，则回到搜索阶段
        total_critical_major = len([i for i in issues if i.get("severity") in ["critical", "major"]])
        should_research = (
            len(search_queries) > 0 and
            (research_issues_count > 0 or len(missing_aspects) > 0) and
            (total_critical_major == 0 or research_issues_count / max(total_critical_major, 1) > 0.3)
        )

        return {
            "should_research": should_research,
            "search_queries": sorted(set(search_queries))[:5]  # 去重，最多5个查询
        }

    async def _review_content(self, state: ResearchState) -> Dict[str, Any]:
        text = state.get("final_report", "")
        config = validate_research_config(state.get("research_config", {}))
        chunk_size = config.get("review_chunk_chars", 6000)
        max_chunks = config.get("max_review_chunks", 12)
        pieces = chunks_covering(text, limit=chunk_size, max_chunks=max_chunks)
        issues, scores, verdicts, missing = [], [], [], []
        for piece in pieces:
            start, end, chunk = piece["start"], piece["end"], piece["text"]
            cited = validate_citations(chunk, state.get("facts", []))
            evidence = [f for f in state.get("facts", []) if f["id"] in cited]
            result = self.parse_json_response(await self.call_llm(
                system_prompt="审核当前最终报告片段，不是旧草稿。检查每条经验性论断是否有证据ID、片段是否真正支持、实验条件是否匹配、是否转述/未知独立性。证据不足必须指出，不能因文风流畅而通过。输出JSON：overall_assessment:{quality_score:0到10,verdict:pass或needs_revision},issues:[{issue_type,severity,description,requires_new_search,search_query}],missing_aspects:[]。",
                user_prompt=json.dumps({"question": state["query"], "outline": state.get("outline", []),
                    "range": [start, end], "current_final_report_chunk": chunk,
                    "evidence": facts_text(evidence), "direction_rules": state.get("resolved_direction"),
                "quality_threshold": config["quality_threshold"]}, ensure_ascii=False),
                json_mode=True, temperature=0.1))
            assessment = result.get("overall_assessment", {}) if isinstance(result, dict) else {}
            score = assessment.get("quality_score")
            if not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 10:
                raise ContractError("Invalid review score")
            if assessment.get("verdict") not in {"pass", "needs_revision"}:
                raise ContractError("Invalid review verdict")
            for issue in result.get("issues", []):
                severity, coerced_from = normalize_severity(issue.get("severity"))
                issues.append(dict(issue, severity=severity, text_range=[start, end],
                                   **({"severity_coerced_from": coerced_from} if coerced_from else {})))
            scores.append(score)
            verdicts.append(assessment["verdict"])
            missing.extend(result.get("missing_aspects", []))
        score = min(scores)
        serious = any(i.get("severity") in {"critical", "major"} for i in issues)
        passed = all(v == "pass" for v in verdicts) and score >= config["quality_threshold"] and not serious
        return {"overall_assessment": {"quality_score": score, "verdict": "pass" if passed else "needs_revision"},
            "issues": issues, "missing_aspects": list(dict.fromkeys(missing)), "review_complete": True,
            "review_coverage": {"characters": len(text), "ranges": [[p["start"], p["end"]] for p in pieces],
                                "mode": "all_chunks_not_global_proof"}}

    async def final_check(self, state: ResearchState) -> Dict[str, Any]:
        return await self._review_content(state)
