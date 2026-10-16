"""Direction contracts: authored rules, runtime resolution and plans are different objects.

Validation guarantees linkage/coverage, not that LLM-generated scientific judgements are true.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .research_integrity import ContractError, digest

GLOBAL_POLICY = {
    "id": "ai_cs_empirical/v2", "preprints": "include_and_label", "unknown_peer_review": "label_unknown",
    "version_binding": "required", "locator_binding": "required", "abstract_only_support": False,
    "secondary_counts_as_independent": False, "unknown_independence": "do_not_count",
    "missing_required_evidence": "insufficient_evidence", "allow_rule_relaxation": False,
}
FAMILY_FIELDS = {
    "mechanism": ["task", "method_version"],
    "memory_retention": ["task", "dataset", "model", "memory_key", "memory_value", "history_length", "retrieval_budget", "metric"],
    "compression_tradeoff": ["task", "dataset", "model", "compression_method", "compression_ratio", "context_budget", "metric"],
    "retrieval_comparison": ["task", "dataset", "model", "corpus_version", "retriever", "chunk_size", "top_k", "context_budget", "metric"],
    "cost_quality": ["task", "model", "input_tokens", "output_tokens", "latency_definition", "hardware_or_price_date", "metric"],
    "judge_agreement": ["task", "dataset", "judge_model", "judge_prompt", "human_protocol", "sample_size", "agreement_metric"],
    "judge_bias": ["task", "judge_model", "controlled_factor", "randomization", "sample_size", "metric"],
}

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

# 每种比较模式的**字段下限**：comparison_conditions 不得低于该下限。
# 只检查「比较字段 ⊂ required」不够——自由输入推导可以
# 不删 required_conditions，却把 comparison_conditions 缩到 ["task"]，
# 从而把「同设置比较」悄悄弱化成几乎不比较任何东西。
COMPARISON_FLOOR_DROP: dict[str, set[str]] = {
    "same_setting": set(),                                  # 全部必需条件都必须比较
    "cross_dataset": {"dataset"},                            # 泛化轴：允许不同数据集
    "protocol_agreement": {"human_protocol", "sample_size"},  # 人评协议与样本量允许不同
}


class ClaimRule(StrictModel):
    id: str = Field(min_length=2, max_length=80)
    family: Literal["mechanism", "memory_retention", "compression_tradeoff", "retrieval_comparison", "cost_quality", "judge_agreement", "judge_bias"]
    required_conditions: list[str] = Field(min_length=1, max_length=20)
    required_evidence_blocks: list[Literal["method", "experiment_setup", "results", "table", "appendix", "annotation_protocol"]] = Field(min_length=1)
    missing_fields: Literal["insufficient_evidence"] = "insufficient_evidence"
    # --- 比较合同（PR3 + round2）：决定「哪些条件值必须对齐才谈得上可比」 -------------
    # 条件**填没填**只表示 metadata completeness，**不表示 condition compatibility**；
    # 匹配规则必须由 claim 定义，而不是由字段数量定义——跨数据集泛化与同设置性能比较
    # 本来就需要不同的合同。
    comparison_mode: Literal["same_setting", "cross_dataset", "protocol_agreement"] = "same_setting"
    # 必须对齐的条件。**不得低于该模式的下限**（见 COMPARISON_FLOOR_DROP）：
    #   same_setting       → 全部 required_conditions
    #   cross_dataset      → 除 dataset 外的全部（含 corpus_version / context_budget 等）
    #   protocol_agreement → 除 human_protocol / sample_size 外的全部；
    #                        该模式只允许用于**显式的跨协议描述**，
    #                        **不得**据此宣称「同一人评协议下的可靠性已被验证」。
    comparison_conditions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def floor(self):
        if not set(FAMILY_FIELDS[self.family]) <= set(self.required_conditions):
            raise ValueError("claim rule cannot remove family-level required fields")
        if self.comparison_conditions and not set(self.comparison_conditions) <= set(self.required_conditions):
            raise ValueError("comparison_conditions must be a subset of required_conditions")
        floor = self.comparison_floor()
        if self.comparison_conditions and not floor <= set(self.comparison_conditions):
            raise ValueError(
                f"comparison_conditions for mode {self.comparison_mode} must cover {sorted(floor)}")
        return self

    def comparison_floor(self) -> set[str]:
        """该模式下**不能削减**的比较字段集合。"""
        return set(self.required_conditions) - COMPARISON_FLOOR_DROP[self.comparison_mode]

    def comparison_fields(self) -> list[str]:
        floor = self.comparison_floor()
        if self.comparison_conditions:
            # 校验已保证 floor ⊆ comparison_conditions；保持档案声明顺序以便可读
            return [f for f in self.required_conditions
                    if f in set(self.comparison_conditions) or f in floor]
        return [f for f in self.required_conditions if f in floor]

    def cross_protocol_only(self) -> bool:
        """该规则是否只允许**跨协议描述**（protocol_agreement 且协议字段不参与比较）。"""
        return self.comparison_mode == "protocol_agreement" and bool(
            COMPARISON_FLOOR_DROP["protocol_agreement"] - set(self.comparison_conditions or []))

class SourcePolicy(StrictModel):
    global_policy_ref: Literal["ai_cs_empirical/v2"]
    preferred_document_types: list[Literal["primary_research", "benchmark", "survey", "artifact_documentation"]]
    required_source_blocks: list[Literal["method", "experiment_setup", "results", "table", "appendix", "annotation_protocol"]] = Field(min_length=1)
    source_checks: list[str] = Field(min_length=1, max_length=8)
    # Inference cannot introduce URLs, provider keys, trust scores, or relaxed security rules.

class Facet(StrictModel):
    id: str = Field(min_length=2, max_length=60)
    question: str = Field(min_length=8, max_length=240)
    claim_type_ids: list[str] = Field(min_length=1)

class DirectionProfile(StrictModel):
    schema_version: Literal["direction-profile/2"]
    id: str = Field(min_length=3, max_length=80)
    name: str = Field(min_length=2, max_length=80)
    scope: Literal["AI_CS_empirical"]
    description: str = Field(min_length=10, max_length=500)
    seed_queries: list[str] = Field(min_length=3, max_length=5)
    claim_types: list[ClaimRule] = Field(min_length=1, max_length=6)
    source_policy: SourcePolicy
    facets: list[Facet] = Field(min_length=2, max_length=4)
    @model_validator(mode="after")
    def links(self):
        ids = [x.id for x in self.claim_types]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate claim type IDs")
        facet_ids = [x.id for x in self.facets]
        if len(facet_ids) != len(set(facet_ids)):
            raise ValueError("duplicate facet IDs")
        if any(not set(f.claim_type_ids) <= set(ids) for f in self.facets):
            raise ValueError("facet refers to an unknown claim type")
        return self

class PresetChoice(StrictModel):
    mode: Literal["preset"]
    preset_id: str = Field(min_length=3)

class CustomChoice(StrictModel):
    mode: Literal["custom"]
    text: str = Field(min_length=4, max_length=300)

DirectionChoice = Annotated[PresetChoice | CustomChoice, Field(discriminator="mode")]

class DirectionRequest(StrictModel):
    choice: DirectionChoice
    focus_angles: list[str] = Field(default_factory=list, max_length=3)
    start_year: int = Field(ge=1990, le=2100)
    end_year: int = Field(ge=1990, le=2100)
    @model_validator(mode="after")
    def years(self):
        if self.start_year > self.end_year:
            raise ValueError("start_year must not exceed end_year")
        return self

class ResolvedDirection(StrictModel):
    profile: DirectionProfile
    global_policy: dict
    rule_origin: Literal["preset", "inferred"]
    field_origins: dict[str, Literal["global", "preset", "inferred"]]
    profile_hash: str
    model: str | None = None
    prompt_version: str
    input_hash: str
    validation_status: Literal["contract_valid_not_expert_validated"]

class PlanSection(StrictModel):
    id: str
    title: str = Field(min_length=4)
    description: str = Field(min_length=10)
    facet_ids: list[str] = Field(min_length=1)
    claim_type_ids: list[str] = Field(min_length=1)
    research_questions: list[str] = Field(min_length=1)
    required_conditions: list[str] = Field(min_length=1)
    search_queries: list[str] = Field(min_length=1, max_length=3)
    focus_angle_indices: list[int] = Field(default_factory=list)
    requires_chart: bool = False

class PlanHypothesis(StrictModel):
    id: str
    content: str = Field(min_length=10)
    claim_type_id: str
    conditions: dict
    refutation_query: str = Field(min_length=4)

class ResearchPlan(StrictModel):
    schema_version: Literal["research-plan/2"]
    profile_hash: str
    sections: list[PlanSection] = Field(min_length=2, max_length=4)
    hypotheses: list[PlanHypothesis] = Field(min_length=1, max_length=3)
    explicit_unknowns: list[str]


def load_preset(preset_id: str, directory: Path | None = None) -> DirectionProfile:
    # Fixed allow-list prevents path traversal and typo fallback.
    allowed = {"agent_memory", "rag_context", "llm_evaluation"}
    if preset_id not in allowed:
        raise ContractError(f"unknown preset_id: {preset_id}; choose custom mode explicitly")
    root = directory or Path(__file__).parents[1] / "config" / "direction_profiles"
    return DirectionProfile.model_validate_json((root / f"{preset_id}.json").read_text(encoding='utf-8'))


def resolved(profile, request, origin, model=None):
    return ResolvedDirection(profile=profile, global_policy=GLOBAL_POLICY, rule_origin=origin,
        field_origins={"global_policy": "global", "profile.seed_queries": origin,
                       "profile.claim_types": origin, "profile.source_policy": origin, "profile.facets": origin},
        profile_hash=digest(profile.model_dump()), model=model, prompt_version="direction-inference/2",
        input_hash=digest(request.model_dump()), validation_status="contract_valid_not_expert_validated").model_dump()


def validate_plan(plan: dict, direction: dict, request: DirectionRequest) -> dict:
    parsed = ResearchPlan.model_validate(plan)
    profile = DirectionProfile.model_validate(direction["profile"])
    if parsed.profile_hash != direction["profile_hash"]:
        raise ContractError("plan does not bind to the resolved direction")
    facets = {f.id: f for f in profile.facets}
    rules = {r.id: r for r in profile.claim_types}
    covered, angles = set(), set()
    generic = {"研究现状", "方法对比", "结论", "未来展望", "研究背景"}
    ids = [s.id for s in parsed.sections]
    if len(ids) != len(set(ids)):
        raise ContractError("duplicate section IDs")
    for s in parsed.sections:
        if s.title.strip() in generic:
            raise ContractError("generic-only section title")
        if not set(s.facet_ids) <= set(facets) or not set(s.claim_type_ids) <= set(rules):
            raise ContractError("unknown facet/claim type in section")
        required_types = {c for f in s.facet_ids for c in facets[f].claim_type_ids}
        if not required_types <= set(s.claim_type_ids):
            raise ContractError("section does not cover its facets' claim requirements")
        required_fields = {c for r in s.claim_type_ids for c in rules[r].required_conditions}
        if not required_fields <= set(s.required_conditions):
            raise ContractError("section omitted direction-specific experimental conditions")
        if any(i < 0 or i >= len(request.focus_angles) for i in s.focus_angle_indices):
            raise ContractError("invalid focus angle index")
        covered.update(s.facet_ids)
        angles.update(s.focus_angle_indices)
    if covered != set(facets) or angles != set(range(len(request.focus_angles))):
        raise ContractError("plan does not cover all declared facets and user angles")
    for h in parsed.hypotheses:
        if h.claim_type_id not in rules:
            raise ContractError("unknown hypothesis claim type")
        if not set(rules[h.claim_type_id].required_conditions) <= set(h.conditions):
            raise ContractError("hypothesis must represent unknown conditions explicitly as null")
    return parsed.model_dump()
