# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DeepResearch V2.0 - 首席笔杆 Agent (LeadWriter)

职责：
1. 深度写作 - 将零散信息串联成逻辑严密的报告
2. Markdown排版 - 专业的格式排版
3. 图文混排 - 整合文字、图表、数据
4. 参考文献 - 规范的引用格式
"""
import json
from service.research_integrity import (ContractError, facts_text, validate_citations, references_for, chunks_covering, apply_exact_edits, report_hash)

import uuid
from typing import Dict, Any, List
from datetime import datetime

from .base import BaseAgent
from ..state import ResearchState, ResearchPhase


class LeadWriter(BaseAgent):
    """
    首席笔杆 - 最终输出的打磨者

    特点：
    - 深度写作能力
    - 专业的行业研究报告风格
    - 逻辑严密的叙述结构
    - 规范的引用和排版
    """

    SECTION_WRITING_PROMPT = """你是一位顶级投行研究部的首席分析师，擅长撰写深度行业研究报告。

## 研究主题
{query}

## 当前章节信息
标题: {section_title}
描述: {section_description}
类型: {section_type}

## 可用素材

### 相关事实
{facts}

### 数据点
{data_points}

### 已有洞察
{insights}

### 相关图表
{charts_info}

## 写作要求
1. **专业性**：使用行业术语，体现专业深度
2. **逻辑性**：论点清晰，论据充分，层层递进
3. **数据支撑**：关键观点必须有数据或事实支撑
4. **引用规范**：使用可点击链接格式 [来源名称](URL)，如 [艾瑞咨询](https://www.iresearch.cn)
5. **图表整合**：在合适位置插入图表引用 ![图表标题](chart_id)
6. **字数控制**：本章节 500-1000 字
7. **不要重复标题**：正文开头不要再写章节标题

## 输出格式
```json
{{
    "content": "章节正文内容（Markdown格式，不包含章节标题）",
    "key_points": ["本章节的核心要点"],
    "citations": [
        {{"source": "来源名称", "url": "完整URL"}}
    ],
    "suggested_improvements": ["如果有更多信息可以改进的地方"]
}}
```

## 写作风格示例
- 好的开头："2024年，中国AI芯片市场正经历深刻变革。根据[IDC数据](https://www.idc.com)，市场规模达到..."
- 避免的开头："关于AI芯片，首先我们来看..."
- 数据引用示例："市场规模达5000亿元（[艾瑞咨询报告](https://www.iresearch.cn/report)）"

开始撰写："""

    SYNTHESIS_PROMPT = """你是首席笔杆，需要将各章节整合成完整的研究报告。

## 研究主题
{query}

## 各章节内容
{sections_content}

## 收集的所有引用来源
{all_sources}

## 任务
1. 撰写报告摘要（Executive Summary）
2. 整合各章节，确保逻辑连贯，使用层级编号
3. 撰写结论与展望
4. 整理参考文献列表（确保链接可点击）

## 关键要求

### 1. 标题编号规则（必须严格遵守）
- 一级标题：1、2、3...（如：1 市场概况）
- 二级标题：1.1、1.2、2.1...（如：1.1 市场规模）
- 三级标题：1.1.1、1.1.2...（如：1.1.1 全球市场）
- **禁止标题重复**：每个标题必须唯一，不要在正文中重复章节标题

### 2. 引用格式规则（确保可点击）
- 行内引用：使用 [来源名称](URL) 格式，如 [艾瑞咨询](https://www.iresearch.cn)
- 数据引用：在数据后标注来源，如"市场规模达5000亿元（[IDC报告](https://www.idc.com)）"
- 文末参考文献：使用有序列表 + 可点击链接格式

### 3. 报告结构规范
- 不要在报告开头使用 # 一级标题
- 直接从"执行摘要"开始
- 各章节使用 ## 二级标题
- 子章节使用 ### 三级标题

## 输出格式
```json
{{
    "executive_summary": "执行摘要（300-500字）",
    "full_report": "完整报告（Markdown格式，按下方结构生成）",
    "conclusions": ["核心结论1", "核心结论2"],
    "outlook": "未来展望",
    "references": [
        {{"id": 1, "title": "来源标题", "url": "完整URL", "author": "作者/机构", "date": "日期"}}
    ]
}}
```

## 报告结构模板
```markdown
## 执行摘要

[300-500字的研究摘要]

---

## 1 [第一章标题]

[章节引言段落]

### 1.1 [子章节标题]

[内容，包含数据引用如：根据[来源名](URL)，...]

### 1.2 [子章节标题]

#### 1.2.1 [三级标题]

[更详细的内容]

---

## 2 [第二章标题]

### 2.1 [子章节标题]

...

---

## 结论与展望

### 核心结论
1. [结论1]
2. [结论2]

### 未来展望
[展望内容]

---

## 参考文献

1. [来源标题1](URL1) - 作者/机构, 日期
2. [来源标题2](URL2) - 作者/机构, 日期
...
```"""

    REVISION_PROMPT = """你是首席笔杆，需要根据审核反馈修订报告。

## 原始报告
{original_content}

## 审核反馈
{feedback}

## 补充的新信息
{new_info}

## 任务
根据反馈修订报告，解决指出的问题。

## 修订原则
1. 针对性修改：只修改有问题的部分
2. 补充来源：对缺少来源的观点补充引用
3. 修正错误：纠正事实错误或逻辑漏洞
4. 保持风格：修订后保持报告整体风格一致

输出JSON：
```json
{{
    "revised_content": "修订后的内容",
    "changes_made": ["修改1", "修改2"],
    "addressed_issues": ["已解决的问题ID"],
    "unable_to_address": ["无法解决的问题及原因"]
}}
```"""

    def __init__(self, llm_api_key: str, llm_base_url: str, model: str = "qwen-max"):
        super().__init__(
            name="LeadWriter",
            role="首席笔杆",
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            model=model
        )

    async def process(self, state: ResearchState) -> ResearchState:
        """处理入口"""
        if state["phase"] == ResearchPhase.WRITING.value:
            return await self._write_report(state)
        elif state["phase"] == ResearchPhase.REVISING.value:
            return await self._revise_report(state)
        else:
            return state

    async def _write_report(self, state: ResearchState) -> ResearchState:
        """撰写报告"""
        # 发送 research_step 开始事件
        # 注意: step_type 必须是 "writing" 以匹配 graph.py 发送的 phase 事件
        self.add_message(state, "research_step", {
            "step_id": f"step_writing_{uuid.uuid4().hex[:8]}",
            "step_type": "writing",
            "title": "内容生成",
            "subtitle": "撰写研究报告",
            "status": "running",
            "stats": {"sections_count": len(state["outline"]), "word_count": 0}
        })

        self.add_message(state, "thought", {
            "agent": self.name,
            "content": "开始撰写深度研究报告..."
        })

        # 逐章节撰写
        # 真实联调缺陷（round2-B1）：**本轮一条材料都没交付**时，不该再让模型写正文——
        # 证据集为空时「经验性论断必须带引用」不可能满足，模型只能编造引用或照抄提示词里的
        # 示例 ID（实测两次失败：第一次抄成 `[E:事实ID]`，第二次抄成 `[E:fact_...]`）。
        # 这里改为**确定性**产出：报告直接陈述材料审计结果，不调用 LLM、不产生任何引用标记；
        # 也不再走 `_synthesize_report`（它按章节拼接，会把同一段审计文字复制 N 遍）。
        if not state["facts"]:
            self._write_empty_material_report(state)
        else:
            for section in state["outline"]:
                if section.get("status") not in ["final", "drafted"]:
                    await self._write_section(state, section)

            # 整合报告
            await self._synthesize_report(state)

        # 发送 research_step 完成事件
        word_count = len(state.get("final_report", ""))
        self.add_message(state, "research_step", {
            "step_type": "writing",
            "title": "内容生成",
            "subtitle": "撰写研究报告",
            "status": "completed",
            "stats": {
                "sections_count": len(state["outline"]),
                "word_count": word_count,
                "references_count": len(state.get("references", []))
            }
        })

        # 更新阶段
        state["phase"] = ResearchPhase.REVIEWING.value

        return state

    def _write_empty_material_report(self, state: ResearchState) -> None:
        """没有有效事实时，用**确定性文本**说明实际发生了什么（不调用 LLM、不写引用）。

        为什么不用模型写：证据集为空时任何 `[E:...]` 都不可能合法，模型被要求「必须带引用」
        就只能编造；实测两次分别抄出 `[E:事实ID]` 与 `[E:fact_...]`，整轮研究以
        「unknown evidence tokens」失败——而真实原因只是「本轮没有可核查材料」。

        `facts == 0` **不等于**「材料为 0」可能已经交付了 1 条摘要材料、
        只是没提取到可核查事实。因此报告按**实际阶段**分支，并保留具体的延后原因
        （「首发日期未知」与「版本时间未确认」不能压成同一句话，）。

        实现上刻意**自包含**：不依赖本类的其他方法或模块级常量。审查方会按 AST 单独编译
        本方法执行，任何外部依赖都会让那条探针以 AttributeError/NameError 假失败。
        """
        sources = [s for s in (state.get("raw_sources") or []) if isinstance(s, dict)]
        fulltext_sources = [s for s in sources
                            if s.get("material_status") == "fetched"
                            and s.get("material_scope") in ("fulltext_page", "pdf")]
        delivered = len(sources)
        delivered_fulltext = len(fulltext_sources)
        facts = len(state.get("facts") or [])
        links = len(state.get("evidence_links") or [])

        labels = {"publication_date_unknown": "首发日期未知（未取得可确认的首次公开时间）",
                  "version_time_unconfirmed": "版本时间未确认（首发已知、但本次读到的版本日期未确认）",
                  "date_unparsable": "日期无法解析"}
        counts: dict = {}
        for item in (state.get("date_deferred_sources") or []):
            if isinstance(item, dict):
                key = str(item.get("reason") or "unknown")
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            for audit in (state.get("evidence_audit") or []):
                if isinstance(audit, dict):
                    for item in (audit.get("items") or []):
                        if isinstance(item, dict) and item.get("reason"):
                            key = str(item["reason"])
                            counts[key] = counts.get(key, 0) + 1
        reasons_line = "；".join(f"{labels.get(k, k)} ×{v}" for k, v in sorted(counts.items())) \
            or "（没有延后记录）"

        lines = ["# 本轮研究未产出可核查结论", ""]
        if delivered == 0:
            lines.append("没有任何来源通过入证前置门（时间窗口 / 材料状态），因此本轮没有材料"
                         "进入证据抽取，报告中不存在任何经验性结论，也不存在引用。")
        elif delivered_fulltext == 0:
            lines.append(f"本轮交付了 **{delivered} 条材料**，其中**正文级材料 0 条**"
                         f"（非正文级 {delivered - delivered_fulltext} 条，例如摘要页/短页），"
                         "因此没有提取到可核查事实，报告中不存在任何经验性结论，也不存在引用。")
        elif facts == 0:
            lines.append(f"本轮交付了 {delivered} 条材料（其中正文级 {delivered_fulltext} 条），"
                         "但**没有产生有效事实**（抽取结果或原文绑定未通过），"
                         "报告中不存在任何经验性结论，也不存在引用。")
        else:
            lines.append(f"本轮有 {facts} 条事实但写作阶段未产出结论，"
                         "请以运行状态为准检查写作阶段。")

        lines += ["", "## 材料处理记录（按具体原因）", f"- {reasons_line}",
                  f"- 交付来源 {delivered} 条（正文级 {delivered_fulltext} 条）；"
                  f"有效事实 {facts} 条；证据关系 {links} 条。"]
        # 抽取/引用失败必须**单独列出**它和时间窗口原因不同
        # ——材料已经交付，是抽取绑定这一步没过；混在一起会让读者以为「没材料」。
        rejection_counts: dict = {}
        for item in (state.get("provenance_rejections") or []):
            if isinstance(item, dict):
                key = str(item.get("reason") or "unknown")
                rejection_counts[key] = rejection_counts.get(key, 0) + 1
        exclusion_counts: dict = {}
        for hypothesis in (state.get("hypotheses") or []):
            if isinstance(hypothesis, dict):
                for excluded in ((hypothesis.get("adopted_evidence") or {}).get("excluded") or []):
                    if isinstance(excluded, dict):
                        key = str(excluded.get("reason") or "unknown")
                        exclusion_counts[key] = exclusion_counts.get(key, 0) + 1
        if rejection_counts:
            lines.append("- 抽取结果被拒（未绑定到交付材料）："
                         + "；".join(f"`{k}` ×{v}" for k, v in sorted(rejection_counts.items())))
        if exclusion_counts:
            lines.append("- 已绑定但未被论断采用（排除理由）："
                         + "；".join(f"`{k}` ×{v}" for k, v in sorted(exclusion_counts.items())))
        if state.get("analysis_insufficient"):
            lines.append("- 分析阶段自报材料不足（`analysis_insufficient`）。")
        audit = [a for a in (state.get("evidence_audit") or []) if isinstance(a, dict)]
        if audit:
            lines.append("- 入证审计汇总（原始记录码，未经改写）：")
            for item in audit:
                lines.append(f"  - `{item.get('reason')}`：{item.get('count')} 条")
        lines += ["", "## 这意味着什么", "",
                  "- 上面是**材料与流程状态**，不是「论文没有报告某个结论」——"
                  "后者必须先取得正文并逐条核对；",
                  "- 时间窗口只被「已确认的发表/版本日期」满足；未确认的来源一律延后并在此列出"
                  "（这些是**处理记录条数**，不是去重后的论文数）；",
                  "- 需要完整证据链时应先解决材料可得性，而不是放宽判定标准。"]
        report = "\n".join(lines)
        state["final_report"] = report
        state["references"] = []
        state["report_hash"] = report_hash(report)
        self.add_message(state, "section_content", {"agent": self.name,
                                                    "section_id": "material_audit",
                                                    "section_title": "材料审计",
                                                    "content": report,
                                                    "word_count": len(report)})
        self.add_message(state, "report_ready", {"content": report, "references": []})

    async def _write_section(self, state: ResearchState, section: Dict) -> None:
        related = [f for f in state["facts"] if section["id"] in f.get("related_sections", [])]
        if not related:
            related = state["facts"]
        # 显式给出「本节可用证据 ID」，并附全量合法 ID 供跨节引用。
        # 原实现只把 related 的正文交给模型、却不告诉它有哪些 ID 可用，
        # 且只用 related 校验引用——当模型引用【其他章节的合法证据】时会被误判为
        # 「unknown evidence tokens」并让整条链路失败（实测踩到）。
        section_ids = [f["id"] for f in related]
        all_ids = [f["id"] for f in state["facts"]]
        # 引用格式的示例必须是**一个真实存在的 id**（实测教训，round2-B1 复跑）：
        # 只要提示词里出现任何字面假 ID（`事实ID` / `fact_xxxxxxxx`），模型就可能照抄，
        # 于是 `validate_citations` 抛「unknown evidence tokens」并让整轮研究失败。
        # 用真 id 做示例：即使被照抄，也**不会因照抄该示例而产生不存在的证据 ID**。
        # 注意这**只**保证 ID 存在性——真实 ID 仍可能被贴到它并不支持的论断后面，
        # 「引用支持该论断」不是 ID 检查能证明的。
        example_id = (section_ids or all_ids)[0]
        citation_instruction = (
            "输出JSON {content,key_points}。经验性论断必须带引用标记，格式为 [E:" + example_id + "]，"
            "其中 id 部分必须逐字复制自 all_valid_evidence_ids（含 fact_ 前缀）：示例给的是本节真实可用的"
            "id，若它并不支持你要写的论断，必须换成真正支持该论断的 id；"
            "**不得**只写前缀、省略号或任何占位符，也不得改写或拼接 id；"
            "不要生成URL或参考文献表。"
            "必须按 hypothesis_status_guide 区分结论强度："
            "不得把作品数量写成「独立研究数量」——作品去重不等于研究独立性，"
            "独立性未知时只能写「N 篇作品报告了…」；"
            "条件不可比（task/metric/model 等取值不同）的材料只能作为「别的设置下的作者报告」，"
            "不得计入本命题的支持或反驳；"
            "conflicted 只能写成「发现相反表述、可比性未知」；"
            "材料不足就明确说明，source_only不是原文验证；"
            "结论措辞不得超过证据实际能证明的范围。")
        prompt = json.dumps({"question": state["query"], "section": section,
            "evidence": facts_text(related), "hypotheses": state.get("hypotheses", []),
            "available_evidence_ids": section_ids,
            "all_valid_evidence_ids": all_ids,
            # 措辞收窄（PR1）：状态说明必须与核验链路的**实际**能力一致。
            # 旧版把 supported_in_reported_scope 写成「>=2 篇独立研究同向支持」——
            # (a) 独立性当时由 work_id 直接置 True，而作品去重 ≠ 研究独立性；
            # (b) 该状态由「字段名共同出现」判定，条件可比性并未核实。
            # 于是报告会宣称一个链路并未证明的结论。这里如实降级表述。
            "hypothesis_status_guide": {
                "supported_in_scope": "全部必要条件齐备且 >=2 篇作品同向支持。独立性在本轮一律为 unknown"
                                      "（work_id 只表示「哪篇作品」），故只能写「本批材料中有 N 篇作品报告了同向结果」；"
                                      "不得写「多篇独立研究已证明」，也不得写「同一设置下」——"
                                      "条件齐备不等于实验设置相同",
                "refuted_in_scope": "全部必要条件齐备且 >=2 篇作品反向支持；同样不得称为「独立研究」",
                # PR3：旧规则（字段名共同出现即判支持/反驳）已从归约器删除，不再产出这两个状态；
                # 若历史数据里出现，一律按「未核查」处理。
                "supported_in_reported_scope": "【旧规则已删除】仅在历史数据中出现；条件可比性未核查，"
                                               "**不构成支持结论**",
                "refuted_in_reported_scope": "【旧规则已删除】仅在历史数据中出现；**不构成推翻结论**",
                "conflicted": "发现相反表述，但可比性未核实——**不是已核实的科学冲突**；"
                              "必须写明「发现相反表述、可比性未知」，且不得据此下肯定或否定结论",
                "evidence_limited": "有证据但未达到「已证实」所需条件（独立性未知 / 条件不可比 / 可比条件未报告，"
                                    "**不一定是作品数量不足**）；必须按 `limitations` 写明卡在哪一项，只能作为线索",
                "insufficient_evidence": "材料不满足必要条件，不得下结论；属技术过程状态还是已核对材料后的"
                                         "科学判断，以审查记录的 reason_codes 分类为准（技术失败不得写成"
                                         "「论文没有报告」）",
                "unverified": "没有任何证据",
                "classification_conflict": "同一证据被相反分类，须指出该冲突"},
            "child_claim_guide": "若 state 里给了 child_claims，它们是**新的窄命题**（pinned_conditions 为实际"
                                 "条件值）。引用时必须写出这些条件值，并明确说明「父命题不因此成立」；"
                                 "不得把子命题当作父命题已获证明。",
            # 材料审计摘要（只读、已脱敏为计数）：没有它，模型无法写出与**材料原因一致**的
            # 说明——真实联调里 184 条来源全部因「发表日期未知」被延后，若报告不说这件事，
            # 读者会把「没有结论」误读成「研究做过了、结论是不足」。
            "material_audit": [{"reason": str(a.get("reason")), "count": int(a.get("count") or 0)}
                               for a in (state.get("evidence_audit") or []) if isinstance(a, dict)],
            "delivered_material_count": len(state.get("raw_sources") or []),
            "delivered_fulltext_count": sum(
                1 for s in (state.get("raw_sources") or [])
                if s.get("material_status") == "fetched"
                and s.get("material_scope") in {"fulltext_page", "pdf"}),
            # 真实联调暴露（round2-B1 复跑）：这里原来写的是「使用[E:事实ID]」——
            # 中文占位符会被模型**逐字复制**进正文，于是 `validate_citations` 抛
            # 「unknown evidence tokens: 事实ID」，整轮研究在 write 阶段失败。
            # 提示词里的引用示例现在只用**真实 id**（见上方 citation_instruction）。
            # 空证据集这一分支在 `_write_report` 已改为确定性产出，不进入本函数。
            "instruction": citation_instruction}, ensure_ascii=False)
        result = self.parse_json_response(await self.call_llm(
            system_prompt="撰写有证据边界的研究报告；不把来源链接、转述或两条相似文本当成独立验证。",
            user_prompt=prompt, json_mode=True, temperature=0.2))
        content = result.get("content", "") if isinstance(result, dict) else ""
        if not content.strip():
            raise ContractError("Empty section")
        # 用【全量 facts】校验：最终报告是各节拼接后按全量校验的（见 _synthesize_report），
        # 故本节也必须按同一口径，否则跨节引用合法证据会被误杀。
        validate_citations(content, state["facts"])
        state["draft_sections"][section["id"]] = content
        section["status"] = "drafted"
        self.add_message(state, "section_content", {"agent": self.name, "section_id": section["id"],
            "section_title": section.get("title"), "content": content, "word_count": len(content)})

    async def _synthesize_report(self, state: ResearchState) -> None:
        # Deterministic assembly: avoids a second unconstrained rewrite losing IDs or whole sections.
        pieces = []
        for section in state["outline"]:
            text = state["draft_sections"].get(section["id"], "")
            if not text.strip():
                raise ContractError(f"Missing draft for {section['id']}")
            pieces.append(f"## {section['title']}\n\n{text}")
        report = "\n\n".join(pieces)
        validate_citations(report, state["facts"])
        state["final_report"] = report
        state["references"] = references_for(report, state["facts"])
        state["report_hash"] = report_hash(report)
        self.add_message(state, "report_ready", {"content": report, "references": state["references"]})

    async def _revise_report(self, state: ResearchState) -> ResearchState:
        original = state.get("final_report", "")
        config = state.get("research_config", {})
        chunks = chunks_covering(original, limit=config.get("review_chunk_chars", 6000),
                                 max_chunks=config.get("max_review_chunks", 12))
        edits = []
        for chunk in chunks:
            start, end, text = chunk["start"], chunk["end"], chunk["text"]
            ids = validate_citations(text, state["facts"])
            evidence = [f for f in state["facts"] if f["id"] in ids]
            # New facts are candidates, not permission to fabricate a URL.
            evidence += [f for f in state["facts"] if f.get("is_supplementary") and f not in evidence]
            result = self.parse_json_response(await self.call_llm(
                system_prompt="修订报告。只返回JSON {edits:[{old_text,new_text}]}；old_text必须是本段唯一的完整原文，不能覆盖本段以外文字。没有必要修改则edits=[]。必须**原样保留**正文里已有的证据引用标记（形如方括号 E 冒号加真实 fact id），不得新增引用、不得改写引用、也不得把示例文字写进正文；不能自行宣告质检通过。",
                user_prompt=json.dumps({"range": [start, end], "text": text,
                    "feedback": state.get("critic_feedback", []), "evidence": facts_text(evidence)}, ensure_ascii=False),
                json_mode=True, temperature=0.1))
            part_edits = result.get("edits") if isinstance(result, dict) else None
            if not isinstance(part_edits, list):
                raise ContractError("Revision must provide an edits array")
            for edit in part_edits:
                if not isinstance(edit, dict) or not isinstance(edit.get("old_text"), str) or edit["old_text"] not in text:
                    raise ContractError("Revision anchor lies outside reviewed chunk")
            edits.extend(part_edits)
        revised = apply_exact_edits(original, edits)
        validate_citations(revised, state["facts"])
        state["final_report"] = revised
        state["references"] = references_for(revised, state["facts"])
        state["report_hash"] = report_hash(revised)
        state["review_complete"] = False
        state["quality_status"] = "not_reviewed"
        # Feedback is archived/replaced only by Critic, not marked resolved by its author.
        state["phase"] = ResearchPhase.REVIEWING.value
        return state
