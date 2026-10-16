# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - 总架构师 Agent (ChiefArchitect)

职责：
1. 意图解码 - 深度理解用户问题
2. 知识图谱初始化 - 识别关键实体和关系
3. 动态大纲生成 - 创建可执行的研究计划
4. 进度监控 - 根据研究进展动态调整大纲
"""
import json
from service.direction_contracts import (DirectionRequest, DirectionProfile, ResearchPlan, FAMILY_FIELDS, GLOBAL_POLICY, load_preset, resolved, validate_plan)
from service.research_integrity import ContractError
from service.research_integrity import digest

import uuid
from typing import Dict, Any, List
from datetime import datetime

from .base import BaseAgent
from ..state import ResearchState, ResearchPhase


class ChiefArchitect(BaseAgent):
    """
    总架构师 - 研究规划的大脑

    特点：
    - 动态DAG调度
    - 大局观，能看到全貌
    - 根据新发现调整计划
    """

    PLANNING_PROMPT = """研究课题：{query}

请为该课题生成研究大纲和研究假设，输出JSON格式如下：

{{
  "hypothesis_1": "关于市场/行业趋势的假设（需要验证）",
  "hypothesis_2": "关于竞争格局或技术发展的假设（需要验证）",
  "hypothesis_3": "关于政策或外部因素影响的假设（需要验证）",
  "sec_1_title": "市场概况",
  "sec_1_desc": "描述市场规模、增速",
  "sec_1_query": "搜索关键词",
  "sec_2_title": "竞争格局",
  "sec_2_desc": "描述主要企业",
  "sec_2_query": "搜索关键词",
  "sec_3_title": "技术趋势",
  "sec_3_desc": "描述核心技术",
  "sec_3_query": "搜索关键词",
  "sec_4_title": "政策环境",
  "sec_4_desc": "描述相关政策",
  "sec_4_query": "搜索关键词",
  "sec_5_title": "挑战机遇",
  "sec_5_desc": "描述挑战和机会",
  "sec_5_query": "搜索关键词",
  "sec_6_title": "未来展望",
  "sec_6_desc": "描述发展趋势",
  "sec_6_query": "搜索关键词",
  "questions": "核心问题1;核心问题2;核心问题3"
}}

研究假设示例：
- 假设市场规模将持续增长，需要用数据验证增速
- 假设某类技术会成为主流，需要找证据支持或反驳
- 假设政策变化会影响行业格局，需要分析政策走向

请根据研究课题填写具体内容，每个字段都是字符串类型。"""

    REVISION_PROMPT = """你是总架构师，需要根据研究进展动态调整大纲。

## 原始问题
{query}

## 当前大纲
{current_outline}

## 新发现的重要信息
{new_findings}

## 当前进度
- 已完成章节: {completed_sections}
- 收集的事实数量: {facts_count}
- 发现的数据点: {data_points_count}

## 任务
评估是否需要调整大纲。可能的调整包括：
1. 新增章节（发现了重要的新方向）
2. 删除章节（发现某方向信息太少）
3. 调整章节顺序或优先级
4. 细化或合并章节

输出JSON格式：
```json
{{
    "needs_revision": true或false,
    "revision_reason": "调整原因",
    "revised_outline": [...],  // 如果needs_revision为true
    "new_search_queries": ["新增的搜索关键词"]  // 如果需要补充搜索
}}
```"""

    def __init__(self, llm_api_key: str, llm_base_url: str, model: str = "qwen-max"):
        super().__init__(
            name="ChiefArchitect",
            role="总架构师",
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            model=model
        )

    def _convert_flat_to_outline(self, flat_result: Dict) -> Dict:
        """将扁平JSON格式转换为标准outline格式"""
        outline = []
        for i in range(1, 10):  # 最多支持9个章节
            title_key = f"sec_{i}_title"
            desc_key = f"sec_{i}_desc"
            query_key = f"sec_{i}_query"

            if title_key not in flat_result:
                break

            section = {
                "id": f"sec_{i}",
                "title": flat_result.get(title_key, f"章节{i}"),
                "description": flat_result.get(desc_key, ""),
                "section_type": "mixed",
                "requires_data": i <= 2,  # 前两章需要数据
                "requires_chart": i <= 2,
                "search_queries": [flat_result.get(query_key, flat_result.get(title_key, ""))]
            }
            outline.append(section)

        # 处理研究问题
        questions_str = flat_result.get("questions", "")
        if isinstance(questions_str, str):
            research_questions = [q.strip() for q in questions_str.split(";") if q.strip()]
        else:
            research_questions = []

        # 处理研究假设（假设驱动研究）
        hypotheses = []
        for i in range(1, 6):  # 最多5个假设
            h_key = f"hypothesis_{i}"
            if h_key in flat_result and flat_result[h_key]:
                hypotheses.append({
                    "id": f"h_{i}",
                    "content": flat_result[h_key],
                    "scope_id": "scope_" + digest(flat_result[h_key])[:16],
                    "status": "unverified",  # unverified, supported, refuted, partially_supported
                    "evidence_for": [],
                    "evidence_against": []
                })

        return {
            "outline": outline,
            "research_questions": research_questions,
            "hypotheses": hypotheses,
            "key_entities": []
        }

    async def process(self, state: ResearchState) -> ResearchState:
        """
        处理入口

        根据当前阶段执行不同的规划任务
        """
        if state["phase"] == ResearchPhase.INIT.value:
            return await self._initial_planning(state)
        elif state["phase"] == ResearchPhase.REVIEWING.value:
            return await self._check_revision(state)
        else:
            return state

    async def _initial_planning(self, state: ResearchState) -> ResearchState:
        """初始规划（文献产品）。

        文献运行环境**必须**有显式 direction：缺 direction 直接报错，
        绝不落回行业规划 Prompt（`ENABLE_DIRECTION_PROFILES=false`
        只影响路由层，Architect 在没有 direction_request 时仍会调用行业规划，这是真实缺口）。

        旧的行业规划实现已移到 `_legacy_industry_planning`，本轮**不可达**：
        保留仅为迁移对照与可追溯，PR5 连同行业 Prompt 常量一并清理；
        任何对它的调用都是缺陷，不是降级路径。
        注意：缺 direction 的**旧检查点**在恢复后走到 plan 阶段会在此报错，
        这是有意的——不能用缺方向的旧计划继续跑。
        """
        if state.get("direction_request") is None:
            raise ContractError(
                "literature research requires an explicit direction_request (preset or custom); "
                "the legacy industry plan is no longer a fallback")
        return await self._direction_planning(state)

    async def _legacy_industry_planning(self, state: ResearchState) -> ResearchState:
        """【已废弃·本轮不可达】原行业规划实现，保留仅作迁移对照。"""
        self.logger.info(f"Starting initial planning for: {state['query'][:50]}...")

        # 发送 research_step 开始事件
        self.add_message(state, "research_step", {
            "step_id": f"step_planning_{uuid.uuid4().hex[:8]}",
            "step_type": "planning",
            "title": "研究计划",
            "subtitle": "分析问题，制定大纲",
            "status": "running",
            "stats": {}
        })

        # 发送状态消息
        self.add_message(state, "thought", {
            "agent": self.name,
            "content": "正在分析研究问题，构建知识图谱和研究大纲..."
        })

        # 调用LLM生成规划 - 带重试机制
        prompt = self.PLANNING_PROMPT.format(query=state["query"])
        result = None
        max_retries = 2

        for attempt in range(max_retries + 1):
            response = await self.call_llm(
                system_prompt="你是一位专业的行业研究规划师。请严格按照要求的JSON格式输出，不要添加任何额外内容。",
                user_prompt=prompt,
                json_mode=True,
                temperature=0.3,
                max_tokens=16000  # 拉满到最大值
            )

            # Debug: 记录原始响应
            self.logger.info(f"Raw LLM response length: {len(response)} (attempt {attempt + 1})")
            self.logger.debug(f"Raw LLM response (first 1000 chars): {response[:1000]}")

            result = self.parse_json_response(response)

            # 检查是否是扁平格式，需要转换
            if result and result.get("sec_1_title") and not result.get("outline"):
                result = self._convert_flat_to_outline(result)

            if result and result.get("outline") and len(result.get("outline", [])) >= 3:
                self.logger.info(f"Successfully parsed outline with {len(result['outline'])} sections")
                break

            # 诊断失败原因
            if not result:
                self.logger.warning(f"Attempt {attempt + 1}: JSON parsing failed completely")
            elif not result.get("outline"):
                self.logger.warning(f"Attempt {attempt + 1}: No 'outline' key in result. Keys: {list(result.keys())}")
            elif len(result.get("outline", [])) < 3:
                self.logger.warning(f"Attempt {attempt + 1}: Outline too short: {len(result.get('outline', []))} sections")

            if attempt < max_retries:
                self.logger.warning(f"Outline generation failed or incomplete, retrying... (attempt {attempt + 1})")
                # 简化提示词重试
                prompt = f"""请为"{state['query']}"生成研究大纲。

输出JSON格式：
{{"outline": [
    {{"id": "sec_1", "title": "章节标题", "description": "描述", "section_type": "mixed", "requires_data": true, "requires_chart": false, "search_queries": ["关键词1", "关键词2"]}},
    ...更多章节(共5-8个)...
], "research_questions": ["问题1", "问题2", "问题3"], "key_entities": []}}

要求：outline必须包含5-8个章节，覆盖市场概况、企业竞争、技术趋势、政策环境、未来展望等方面。"""

        if not result:
            state["errors"].append("Failed to generate research plan after retries")
            self.logger.error(f"Raw LLM response: {response[:800]}")
            return state

        # Debug: log outline count
        self.logger.info(f"Parsed result keys: {list(result.keys())}")
        outline = result.get("outline", [])
        self.logger.info(f"Outline in result: {len(outline)} sections")
        if not outline:
            self.logger.warning(f"No outline found! Full parsed result: {str(result)[:500]}")

        # 更新状态
        state["key_entities"] = [e.get("name", "") for e in result.get("key_entities", []) if isinstance(e, dict)]
        state["mind_map"] = result.get("mind_map", {})
        state["research_questions"] = result.get("research_questions", [])
        state["hypotheses"] = result.get("hypotheses", [])  # 假设驱动研究
        state["knowledge_graph"] = {"nodes": [], "edges": []}  # 知识图谱初始化

        # 处理大纲 - 确保每个章节都有必要字段
        processed_outline = []
        for i, section in enumerate(outline):
            if not isinstance(section, dict):
                continue
            processed_section = {
                "id": section.get("id", f"sec_{i+1}"),
                "title": section.get("title", f"章节{i+1}"),
                "description": section.get("description", ""),
                "section_type": section.get("section_type", "mixed"),
                "requires_data": section.get("requires_data", False),
                "requires_chart": section.get("requires_chart", False),
                "priority": section.get("priority", i+1),
                "search_queries": section.get("search_queries", [section.get("title", "")]),
                "status": "pending"
            }
            # 确保 search_queries 是列表且非空
            if not isinstance(processed_section["search_queries"], list):
                processed_section["search_queries"] = [str(processed_section["search_queries"])]
            # 过滤空字符串，如果结果为空则使用章节标题
            processed_section["search_queries"] = [q for q in processed_section["search_queries"] if q and q.strip()]
            if not processed_section["search_queries"]:
                processed_section["search_queries"] = [processed_section["title"]]
            processed_outline.append(processed_section)

        state["outline"] = processed_outline
        self.logger.info(f"Processed outline: {len(processed_outline)} sections")

        # 发送大纲事件
        self.add_message(state, "outline", {
            "understanding": result.get("understanding", {}),
            "key_entities": result.get("key_entities", []),
            "outline": outline,
            "research_questions": state["research_questions"]
        })

        # 更新阶段
        state["phase"] = ResearchPhase.PLANNING.value

        # 发送 research_step 完成事件
        self.add_message(state, "research_step", {
            "step_type": "planning",
            "title": "研究计划",
            "subtitle": "分析问题，制定大纲",
            "status": "completed",
            "stats": {
                "sections_count": len(processed_outline),
                "questions_count": len(state["research_questions"])
            }
        })

        self.logger.info(f"Planning completed. Generated {len(outline)} sections.")

        return state

    async def _check_revision(self, state: ResearchState) -> ResearchState:
        """检查是否需要修订大纲"""
        # 收集新发现
        new_findings = []
        for fact in state["facts"][-10:]:  # 最近10条事实
            new_findings.append(f"- {fact.get('content', '')[:100]}")

        if not new_findings:
            return state

        # 统计进度
        completed = [s for s in state["outline"] if s.get("status") == "final"]

        prompt = self.REVISION_PROMPT.format(
            query=state["query"],
            current_outline=state["outline"],
            new_findings="\n".join(new_findings),
            completed_sections=len(completed),
            facts_count=len(state["facts"]),
            data_points_count=len(state["data_points"])
        )

        response = await self.call_llm(
            system_prompt="你是总架构师，需要判断是否需要调整研究计划。",
            user_prompt=prompt,
            json_mode=True
        )

        result = self.parse_json_response(response)

        if result.get("needs_revision") and result.get("revised_outline"):
            state["outline"] = result["revised_outline"]
            self.add_message(state, "outline_revision", {
                "reason": result.get("revision_reason"),
                "new_outline": result["revised_outline"]
            })
            self.logger.info(f"Outline revised: {result.get('revision_reason')}")

        return state

    async def _direction_planning(self, state: ResearchState) -> ResearchState:
        request = DirectionRequest.model_validate(state["direction_request"])
        if request.choice.mode == "preset":
            profile = load_preset(request.choice.preset_id)
            direction = resolved(profile, request, "preset")
        else:
            error = None
            derivation_attempts = []
            profile = None
            system_prompt = (
                "解析AI/CS实证研究方向。必须在给定论断规则族(supported_claim_families)内推导档案。\n"
                "不支持的方向输出JSON: {\"supported\": false, \"reason\": \"...\"}。\n"
                "支持则输出JSON: {\"supported\": true, \"profile\": <符合DirectionProfile schema的JSON对象>}。\n"
                "严格要求：\n"
                "1. 顶层 schema_version 必须为 'direction-profile/2'，scope 必须为 'AI_CS_empirical'，source_policy.global_policy_ref 必须为 'ai_cs_empirical/v2'。\n"
                "2. 每个 claim_type 的 family 必须是 supported_claim_families 的 key 之一。\n"
                "3. 【关键】每个 claim_type 的 required_conditions 必须是纯英文标识符字符串列表，且必须包含该 family 在 supported_claim_families 中定义的所有字段名（例如 mechanism 必须包含 'task' 和 'method_version'；memory_retention 必须包含 'task', 'dataset', 'model', 'memory_key', 'memory_value', 'history_length', 'retrieval_budget', 'metric'）。绝对不能写成中文描述句子，不能删减必要字段。\n"
                "4. 每个 claim_type 的 required_evidence_blocks 必须来自 ['method', 'experiment_setup', 'results', 'table', 'appendix', 'annotation_protocol']。\n"
                "5. comparison_mode 默认为 'same_setting'，missing_fields 默认为 'insufficient_evidence'。\n"
                "6. facets 包含 2~4 个待核查问题，每个 facet 的 claim_type_ids 必须引用上面定义的 claim_types 的 id。\n"
                "7. 不能生成供应商密钥/权限/URL/信源信用分，档案不是章节模板，facets 写待核查科学问题。"
            )
            for attempt in range(2):
                user_payload = {
                    "direction_text": request.choice.text,
                    "supported_claim_families": FAMILY_FIELDS,
                    "global_policy": GLOBAL_POLICY,
                    "profile_schema": DirectionProfile.model_json_schema()
                }
                if error:
                    user_payload["previous_validation_error"] = error
                    user_payload["instruction"] = "上一轮生成的 profile 未能通过合同校验，请根据 previous_validation_error 严格修正并重新输出完整合法 JSON。"
                response = await self.call_llm(
                    system_prompt=system_prompt,
                    user_prompt=json.dumps(user_payload, ensure_ascii=False),
                    json_mode=True,
                    temperature=0.1
                )
                data = self.parse_json_response(response)
                derivation_attempts.append({
                    "attempt": attempt + 1,
                    "raw_response": data,
                    "validation_error": None
                })
                state["derivation_history"] = derivation_attempts
                if not isinstance(data, dict):
                    raise ContractError("方向不在已实现的AI/CS实证规则范围，不能静默回退: 输出格式错误")
                if data.get("supported") is False:
                    reason = data.get("reason", "方向不在已实现的AI/CS实证规则范围")
                    raise ContractError(f"方向不在已实现的AI/CS实证规则范围，不能静默回退: {reason}")
                elif data.get("supported") is True and isinstance(data.get("profile"), dict):
                    profile_data = data.get("profile")
                elif data.get("schema_version") == "direction-profile/2" and "claim_types" in data:
                    profile_data = data
                else:
                    reason = data.get("reason", "输出未包含合法的 direction profile")
                    raise ContractError(f"方向不在已实现的AI/CS实证规则范围，不能静默回退: {reason}")
                try:
                    profile = DirectionProfile.model_validate(profile_data)
                    break
                except Exception as exc:
                    error = str(exc)
                    derivation_attempts[-1]["validation_error"] = error
            else:
                raise ContractError("推导方向档案两次未通过合同校验，停止而非静默回退: " + str(error))
            direction = resolved(profile, request, "inferred", self.model)
            state["derivation_history"] = derivation_attempts
        state["resolved_direction"] = direction
        self.add_message(state, "direction_resolved", {"name": profile.name,
            "rule_origin": direction["rule_origin"], "profile_hash": direction["profile_hash"],
            "validation_status": direction["validation_status"]})
        payload = {"question": state["query"], "request": request.model_dump(), "resolved_direction": direction,
                   "plan_schema": ResearchPlan.model_json_schema()}
        error = None
        for attempt in range(2):
            response = await self.call_llm(
                system_prompt="制定方向特异的文献核验计划。章节不是固定模板：每章必须绑定facet_ids、claim_type_ids及这些论断的全部必要条件；覆盖全部facet和用户角度。不要用研究现状/方法对比/结论作为唯一标题。假设必须可证伪，未知条件保留null，不得编造论文或实验。不要预设必须有冲突或图表。严格输出schema JSON。",
                user_prompt=json.dumps(dict(payload, previous_validation_error=error), ensure_ascii=False),
                json_mode=True, temperature=0.1)
            try:
                plan = validate_plan(self.parse_json_response(response), direction, request)
                break
            except (ValueError, TypeError) as exc:
                error = str(exc)
        else:
            raise ContractError("方向计划两次未通过合同校验，停止而非回退行业模板: " + str(error))
        state["research_plan"] = plan
        state["outline"] = [dict(s, status="pending", section_type="mixed", requires_data=True,
                                 priority=i + 1) for i, s in enumerate(plan["sections"])]
        # Every seed is actually consumed and its origin recorded; not a dead field in the UI.
        state["search_query_origins"] = []
        for i, seed in enumerate(profile.seed_queries):
            section = state["outline"][i % len(state["outline"])]
            query = f"{seed} {request.start_year}-{request.end_year} {state['query']}"
            if query not in section["search_queries"]:
                section["search_queries"].append(query)
            state["search_query_origins"].append({"query": query, "origin": "profile.seed_queries", "section_id": section["id"]})
        state["hypotheses"] = [dict(h, scope_id="scope_" + digest([h["content"], h["conditions"]])[:16],
            status="unverified", evidence_for=[], evidence_against=[]) for h in plan["hypotheses"]]
        state["research_questions"] = [q for s in plan["sections"] for q in s["research_questions"]]
        state["phase"] = ResearchPhase.PLANNING.value
        self.add_message(state, "outline", {"outline": state["outline"], "research_questions": state["research_questions"],
            "profile_hash": direction["profile_hash"]})
        return state
