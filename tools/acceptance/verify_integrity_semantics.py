"""PR1 / PR1.1 可信度收口的离线断言（代码评审验收门，含新增反例 P01–P13 的同口径断言）。

覆盖六组，每件都是**可失败**的断言，不看结论只看断言本身：
  [1] 质量门：正确反驳不被拒绝；空 hypotheses 不通过；每条论断都要有处理结果或显式待定理由。
  [2] 旧规则隔离：两个 `*_in_reported_scope` 出口对称隔离；未核实可比性的相反表述不算已核实冲突。
  [3] 不足依据：只有**可回查的审查记录**（科学域理由 + 引用材料真实存在）才构成「有依据的不足」。
  [4] 独立性与输入边界：作品身份不等于独立性；来源对象携带的旧独立性断言必须被清除。
  [5] 方向强制：Architect 缺 direction_request 直接报错；旧证据语义检查点拒绝续跑。
  [6] 配置指纹：覆盖影响行为的开关、记录有效值、随任务写入。

**网络在有守卫的情况下被禁用**（`requests.get` 被替换为抛错）：
本脚本会调用 `enrich_result()`，它在生产中会抓全文；不装守卫就会真的发请求
（代码评审实测到两次 HTTPS 尝试）。全文抓取在本脚本中一律 mock。

用法：
    python tools/acceptance/verify_integrity_semantics.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from unittest.mock import patch

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

# 应用靠 .env 启动（不得用命令行注入密钥）。本脚本导入 service.deep_research_v2
# 会连带加载 core.database，那里的 required_secret 要求 POSTGRES_PASSWORD 已在环境里。
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))

# 本机 NO_PROXY 含 "[::1]" 会让 httpx 构造请求时抛 InvalidURL；
# 按项目规则在构造任何客户端之前统一规范化。
from core.runtime_settings import sanitize_no_proxy  # noqa: E402
sanitize_no_proxy()

import service.research_integrity as R  # noqa: E402
import service.research_sources as S  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def gate_state(status: str | None, **hypothesis_extra) -> dict:
    """构造质量门输入：报告与审核**全部合格**，只让「论断判断」这一维变化。

    run_status 用 "running"：本门不判断「执行是否已完成」（那由 runtime 终态提交保证），
    只排除已失败/暂停的执行。
    """
    report = "A scoped claim was responsibly evaluated."
    state = {"final_report": report, "reviewed_report_hash": R.report_hash(report),
             "review_complete": True, "review_verdict": "pass", "quality_score": 9,
             "unresolved_issues": 0, "research_config": {"quality_threshold": 6},
             "run_status": "running", "facts": []}
    if status is None:
        state["hypotheses"] = []
    elif isinstance(status, list):
        state["hypotheses"] = status
    else:
        state["hypotheses"] = [{"id": "H", "status": status, **hypothesis_extra}]
    return state


def qualifying_assessment(state: dict, facts: list[dict], claim_id: str = "H") -> None:
    """给论断挂一条**合格**的审查记录：引用的材料真实存在 + 已确认正文 + 科学域理由。

    消费端会**重验**引用材料的当前状态与范围，
    所以这里的合成材料必须显式声明「已确认取得正文」，否则记录会被正确拒绝。
    """
    state["facts"] = [{"source_material_status": "fetched", "source_material_scope": "fulltext_page",
                       **f} for f in facts]
    state["hypotheses"][0]["assessment"] = {
        "claim_id": claim_id,
        "assessment_status": "completed",
        "source_or_attempt_ids": [f["id"] for f in state["facts"]],
        "reason_codes": ["required_condition_not_reported"],
        "scope_of_review": {"claim_id": claim_id},
        "limitations": [],
        "assessment_version": R.ASSESSMENT_VERSION,
    }


def main() -> int:
    # ---------- [1] 质量门 ----------
    print("\n[1] 质量门：判断的是「论断是否已有证据上的结论」，不是「结论是不是正面的」")
    supported = R.passed_quality(gate_state("supported_in_scope"))
    refuted = R.passed_quality(gate_state("refuted_in_scope"))
    check("全部条件与审核合格时，正向支持可以通过", supported is True)
    check("正确反驳不被拒绝（与正向支持同等对待）", refuted is True and refuted == supported,
          f"supported={supported} refuted={refuted}")
    empty = R.passed_quality(gate_state(None))
    check("空 hypotheses 不通过（不得成为绕过核验门的路径）", empty is False)
    pending = R.passed_quality(gate_state("evidence_limited", evidence_considered=2))
    check("未达到「已证实」条件的 evidence_limited 不通过", pending is False)
    mixed = R.quality_verdict(gate_state([{"id": "H1", "status": "refuted_in_scope"},
                                          {"id": "H2", "status": "made_up_future_status"}]))
    check("未知状态的论断不能被另一条已决论断掩盖",
          mixed["ok"] is False and any("undisclosed" in r for r in mixed["reasons"]),
          json.dumps(mixed["reasons"], ensure_ascii=False))
    disclosed = R.quality_verdict(gate_state([{"id": "H1", "status": "refuted_in_scope"},
                                              {"id": "H2", "status": "made_up_future_status",
                                               "pending_reason": "该规则尚未实现，待 PR3 处理"}]))
    check("未知状态但有显式待定理由时，不因该条阻断",
          disclosed["ok"] is True, json.dumps(disclosed["reasons"], ensure_ascii=False))
    failed = gate_state("refuted_in_scope")
    failed["run_status"] = "failed"
    check("已失败的执行不能标记通过（有报告 ≠ 执行完成）", R.passed_quality(failed) is False)

    # 审核自身的完整性仍逐项要求（移除正向支持门槛 ≠ 一律自动通过）
    mismatch = gate_state("refuted_in_scope")
    mismatch["reviewed_report_hash"] = R.report_hash("another report")
    check("审核哈希未绑定当前稿 → 不通过", R.passed_quality(mismatch) is False)
    unresolved = gate_state("refuted_in_scope")
    unresolved["unresolved_issues"] = 1
    check("有未决问题 → 不通过", R.passed_quality(unresolved) is False)
    verdict = gate_state("refuted_in_scope")
    verdict["review_verdict"] = "needs_revision"
    check("审核未通过 → 不通过", R.passed_quality(verdict) is False)
    empty_report = gate_state("refuted_in_scope")
    empty_report["final_report"] = ""
    empty_report["reviewed_report_hash"] = R.report_hash("")
    check("空报告即使哈希自洽也不通过", R.passed_quality(empty_report) is False)
    verdict_detail = R.quality_verdict(gate_state(None))
    check("quality_verdict 给出可读理由（含 no_hypotheses）",
          verdict_detail["ok"] is False and any("no_hypotheses" in r for r in verdict_detail["reasons"]),
          json.dumps(verdict_detail["reasons"], ensure_ascii=False))

    # ---------- [2] 旧规则隔离 + 未核实分歧 ----------
    print("\n[2] 旧规则两个出口对称隔离；未核实可比性的相反表述不算已核实冲突")
    positive_legacy = R.passed_quality(gate_state("supported_in_reported_scope", evidence_considered=2))
    negative_legacy = R.passed_quality(gate_state("refuted_in_reported_scope", evidence_considered=2))
    check("旧规则的窄支持不算已证实", positive_legacy is False)
    check("旧规则的窄反驳同样不算已证实（对称隔离）", negative_legacy is False,
          f"positive={positive_legacy} negative={negative_legacy}")
    conflict_state = gate_state([{"id": "H", "scope_id": "S"}])
    conflict_state["evidence_links"] = [
        {"id": f"e{i}", "hypothesis_id": "H", "scope_id": "S", "fact_id": f"f{i}", "stance": stance,
         "quote_bound": True, "contract_complete": True, "study_id": None,
         "independence_verified": False, "evidence_role": "unknown"}
        for i, stance in enumerate(["supports", "refutes"])]
    R.reduce_hypotheses(conflict_state)
    check("归约仍如实输出 conflicted（排列不变量依赖该状态）",
          conflict_state["hypotheses"][0]["status"] == "conflicted",
          str(conflict_state["hypotheses"][0]["status"]))
    check("仅凭两个相反标签不构成已核实的科学冲突（不单独满足论断判断）",
          R.passed_quality(conflict_state) is False)
    # 排列不变量必须覆盖**新增字段**（adopted_evidence / assessment）：它们若保持插入顺序，
    # 同一批证据的不同排列会产出不同的论断字典，17 项套件的 A2 会直接失败。
    import itertools

    def perm_edge(i, stance, study=None):
        return {"id": str(i), "hypothesis_id": "h", "fact_id": f"f{i}", "scope_id": "scope",
                "stance": stance, "quote_bound": True, "contract_complete": True, "study_id": study,
                "independence_verified": bool(study), "evidence_role": "original"}
    perm_evidence = [perm_edge(1, "supports", "study-a"), perm_edge(2, "supports", "study-b"),
                     perm_edge(3, "refutes", "study-c"), perm_edge(4, "refutes", "study-d")]
    shapes = set()
    statuses = set()
    for ordering in itertools.permutations(perm_evidence):
        perm_state = gate_state([{"id": "h", "scope_id": "scope"}])
        perm_state["evidence_links"] = list(ordering)
        R.reduce_hypotheses(perm_state)
        shapes.add(json.dumps(perm_state["hypotheses"][0], sort_keys=True, ensure_ascii=False))
        statuses.add(perm_state["hypotheses"][0]["status"])
    check("排列不变量：不同排列产出完全相同的论断字典（含 adopted_evidence/assessment）",
          len(shapes) == 1 and statuses == {"conflicted"},
          f"shape_variants={len(shapes)} statuses={sorted(statuses)}")

    # ---------- [3] 不足依据必须是可回查的审查记录 ----------
    print("\n[3] 「有依据的不足」= 可回查审查记录（科学域理由 + 引用材料真实存在）")
    invalid_values = {repr(v): R.passed_quality(gate_state("insufficient_evidence", evidence_considered=v))
                      for v in [-1, "0", True, {"not": "a count"}]}
    check("非法的 evidence_considered 一律不能支撑「不足」结论",
          not any(invalid_values.values()), json.dumps(invalid_values, ensure_ascii=False))
    dangling = gate_state("insufficient_evidence", evidence_considered=0, evidence_for=["nonexistent"])
    check("悬空引用（材料不存在）不能支撑「不足」结论", R.passed_quality(dangling) is False)
    no_record = gate_state("insufficient_evidence", evidence_considered=3)
    check("只有计数、没有审查记录 → 不通过", R.passed_quality(no_record) is False)
    # 技术性理由（引用未绑原文 / 解析失败）不得冒充科学判断
    for technical in ("quote_mismatch", "parser_failure", "retrieval_failure", "material_not_delivered"):
        state = gate_state("insufficient_evidence")
        qualifying_assessment(state, [{"id": "fact_1"}])
        state["hypotheses"][0]["assessment"]["reason_codes"] = [technical]
        check(f"纯技术理由（{technical}）不构成科学结论", R.passed_quality(state) is False)
    # 引用不存在的材料也无效
    bad_ref = gate_state("insufficient_evidence")
    qualifying_assessment(bad_ref, [{"id": "fact_1"}])
    bad_ref["hypotheses"][0]["assessment"]["source_or_attempt_ids"] = ["fact_missing"]
    check("审查记录引用的材料必须真实存在", R.passed_quality(bad_ref) is False)
    # 合格记录：材料存在 + 科学域理由 → 可以是通过的结论
    good_record = gate_state("insufficient_evidence")
    qualifying_assessment(good_record, [{"id": "fact_1"}, {"id": "fact_2"}])
    check("核对过材料、理由属科学域的「不足」可以是通过的结论",
          R.passed_quality(good_record) is True, json.dumps(R.quality_verdict(good_record)["reasons"],
                                                            ensure_ascii=False))
    # 消费端重验：材料「后来」被标成摘要时，旧 completed 记录不得继续当不足依据
    stale = gate_state("insufficient_evidence")
    qualifying_assessment(stale, [{"id": "fact_1"}])
    stale["facts"][0]["source_material_scope"] = "abstract_page"
    check("消费端按材料**当前**状态重验：摘要材料让旧 completed 记录失效",
          R.assessment_qualifies(stale, stale["hypotheses"][0]) is False
          and R.passed_quality(stale) is False)
    # 缺失状态不豁免
    missing_status = gate_state("insufficient_evidence")
    missing_status["facts"] = [{"id": "fact_1"}]
    missing_status["hypotheses"][0]["claim_type_id"] = "ct"
    missing_status["resolved_direction"] = {"profile": {
        "claim_types": [{"id": "ct", "required_conditions": ["task"],
                         "required_evidence_blocks": ["results"]}],
        "source_policy": {"required_source_blocks": []}}}
    missing_status["evidence_links"] = [{"id": "e1", "hypothesis_id": "H", "fact_id": "fact_1",
                                         "quote_bound": True, "missing_source_blocks": ["results"],
                                         "required_conditions": ["task"], "missing_conditions": ["task"]}]
    R.reduce_hypotheses(missing_status)
    missing_record = missing_status["hypotheses"][0]["assessment"]
    check("材料状态缺失不豁免：记技术理由，不产出科学域「不足」",
          missing_record["assessment_status"] == "blocked"
          and "material_status_unknown" in missing_record["reason_codes"]
          and not (set(missing_record["reason_codes"]) & R.DOMAIN_REASONS),
          json.dumps(missing_record, ensure_ascii=False)[:220])
    # 被裁剪的交付片段：未交付的部分没核查过，不能升级为「论文没报告」
    truncated = gate_state("insufficient_evidence")
    truncated["facts"] = [{"id": "fact_1", "source_material_status": "fetched",
                           "source_material_scope": "fulltext_page"}]
    truncated["hypotheses"][0]["claim_type_id"] = "ct"
    truncated["resolved_direction"] = dict(missing_status["resolved_direction"])
    truncated["evidence_links"] = [dict(missing_status["evidence_links"][0],
                                        material_status="fetched", material_scope="fulltext_page",
                                        excerpt_truncated=True)]
    R.reduce_hypotheses(truncated)
    truncated_record = truncated["hypotheses"][0]["assessment"]
    check("交付片段被裁剪时停在「未交付／未核查」，不产出科学域缺失结论",
          truncated_record["assessment_status"] == "blocked"
          and "material_excerpt_truncated" in truncated_record["reason_codes"]
          and "excerpt_truncated" in truncated_record["limitations"],
          json.dumps(truncated_record, ensure_ascii=False)[:220])
    # 归约器必须真的产出这种记录（不是靠测试手工塞）
    produced = gate_state([{"id": "H", "scope_id": "S"}])
    produced["facts"] = [{"id": "fact_1", "source_material_status": "fetched",
                          "source_material_scope": "fulltext_page"}]
    produced["resolved_direction"] = {"profile": {
        "claim_types": [{"id": "ct", "required_conditions": ["task", "metric"],
                         "required_evidence_blocks": ["method", "results"]}],
        "source_policy": {"required_source_blocks": ["experiment_setup"]}}}
    produced["hypotheses"][0]["claim_type_id"] = "ct"
    produced["evidence_links"] = [{"id": "e1", "hypothesis_id": "H", "scope_id": "S", "fact_id": "fact_1",
                                   "stance": "inconclusive", "quote_bound": True, "contract_complete": True,
                                   "study_id": None, "independence_verified": False,
                                   "evidence_role": "unknown", "conditions": {}, "reported_conditions": {},
                                   "required_conditions": ["task", "metric"],
                                   "missing_conditions": ["task", "metric"],
                                   "missing_source_blocks": ["experiment_setup", "method", "results"],
                                   # round2-B1：自称 fetched 就必须说清抓到的是正文（合成前置数据）
                                   "material_status": "fetched", "material_scope": "fulltext_page"}]
    R.reduce_hypotheses(produced)
    record = produced["hypotheses"][0]["assessment"]
    check("归约器为「引用已绑定、但缺必要条件/证据块」的材料产出合格审查记录",
          record["assessment_status"] == "completed" and "required_condition_not_reported" in record["reason_codes"]
          and record["source_or_attempt_ids"] == ["fact_1"], json.dumps(record, ensure_ascii=False))
    unbound = gate_state([{"id": "H", "scope_id": "S"}])
    unbound["facts"] = [{"id": "fact_1", "source_material_status": "fetched",
                         "source_material_scope": "fulltext_page"}]
    unbound["hypotheses"][0]["claim_type_id"] = "ct"
    unbound["evidence_links"] = [dict(produced["evidence_links"][0], quote_bound=False)]
    R.reduce_hypotheses(unbound)
    check("抽取未绑定原文的证据只记技术理由，不产出「已核对材料后不足」",
          unbound["hypotheses"][0]["assessment"]["assessment_status"] == "blocked",
          json.dumps(unbound["hypotheses"][0]["assessment"], ensure_ascii=False))
    # round2-B1：真实联调暴露——arXiv 回退到 /abs/ 摘要页（约 3k 字符、无 method/results），
    # 却因 material_status="fetched" 被当成「已取得材料」，于是「没有证据块」被报成
    # 「论文没有报告」，质量门判 passed。摘要页/短页/范围未知一律只记技术受限。
    for scope, label in (("abstract_page", "摘要页"), ("short_page", "短页"), (None, "范围未知")):
        abstract_only = gate_state([{"id": "H", "scope_id": "S"}])
        abstract_only["facts"] = [{"id": "fact_1"}]
        abstract_only["resolved_direction"] = dict(produced["resolved_direction"])
        abstract_only["hypotheses"][0]["claim_type_id"] = "ct"
        abstract_only["evidence_links"] = [dict(produced["evidence_links"][0], material_scope=scope)]
        R.reduce_hypotheses(abstract_only)
        rec = abstract_only["hypotheses"][0]["assessment"]
        verdict = R.quality_verdict(abstract_only)
        check(f"只抓到{label}（自称 fetched）不得产出「读后不足」的科学判断，也不得判 passed",
              rec["assessment_status"] == "blocked"
              and "fulltext_not_retrieved" in rec["reason_codes"]
              and not (set(rec["reason_codes"]) & R.DOMAIN_REASONS)
              and any(str(x).startswith("material_scope:") for x in rec["limitations"])
              and verdict["ok"] is False and R.passed_quality(abstract_only) is False
              and any("no_decided_claim" in r for r in verdict["reasons"]),
              json.dumps({"scope": scope, "assessment": rec, "verdict": verdict}, ensure_ascii=False)[:300])

    # ---------- [4] 独立性与输入边界 ----------
    print("\n[4] 作品身份 ≠ 研究独立性；不可信输入携带的旧断言必须清除")
    with patch.object(S, "fetch_fulltext", return_value=("", "", None)), \
         patch.object(S.requests, "get", side_effect=AssertionError("network disabled in this probe")):
        enriched = S.enrich_result({"url": "https://arxiv.org/abs/2501.12345v1",
                                    "title": "Synthetic bibliographic item"})
        dirty = S.enrich_result({"url": "https://example.org/post", "study_id": "stale-study",
                                 "work_id": "stale-work", "independence_verified": True})
        blog = S.enrich_result({"url": "https://example.org/post",
                                "summary": "We discuss earlier work DOI 10.1234/other-paper"})
    check("作品身份仍会被记录（作品级去重保留）", enriched.get("work_id") == "arxiv:2501.12345",
          str(enriched.get("work_id")))
    check("独立性不自动升级为已核查", enriched.get("independence_verified") is not True
          and enriched.get("independence_status") == "unknown",
          f"verified={enriched.get('independence_verified')} status={enriched.get('independence_status')}")
    check("未拿到实验级证据就不写 study_id", not enriched.get("study_id"), str(enriched.get("study_id")))
    check("来源对象自带的旧独立性断言被清除（不可信输入边界）",
          dirty.get("independence_verified") is not True and not dirty.get("study_id")
          and dirty.get("work_id") != "stale-work", json.dumps(dirty, ensure_ascii=False))
    check("博客 snippet 里引用的 DOI 不得绑给该网页", not blog.get("work_id"), str(blog.get("work_id")))

    # ---------- [5] 方向强制 + 旧证据语义检查点 ----------
    print("\n[5] 方向强制：缺 direction_request 直接报错；旧证据语义检查点拒绝续跑")
    from service.deep_research_v2.agents.architect import ChiefArchitect
    from service.deep_research_v2.state import create_initial_state

    class _Probe(ChiefArchitect):
        def __init__(self):        # 不构造 LLM 客户端：本脚本不联网
            self.called = None
            self.logger = __import__("logging").getLogger("probe")
            self.name, self.model = "ChiefArchitect", "probe-model"

        async def _direction_planning(self, state):
            self.called = "direction"
            state["outline"] = [{"id": "sec1"}]
            return state

    probe = _Probe()
    bare = create_initial_state("q", "s")
    try:
        asyncio.run(probe._initial_planning(bare))
        check("缺 direction 必须报错（不得调用规划）", False, "竟然返回了计划")
    except R.ContractError as exc:
        check("缺 direction 必须报错（不得调用规划）", True, str(exc)[:70])
    except Exception as exc:  # noqa: BLE001
        check("缺 direction 必须报错（不得调用规划）", False, f"{type(exc).__name__}: {exc}")
    with_direction = create_initial_state("q", "s")
    with_direction["direction_request"] = {"choice": {"mode": "preset", "preset_id": "agent_memory"},
                                           "start_year": 2022, "end_year": 2025}
    asyncio.run(probe._initial_planning(with_direction))
    check("有 direction 时仍走方向规划（正向路径未被切断）", probe.called == "direction", str(probe.called))

    fresh = create_initial_state("q", "sid")
    fresh.update(user_id="uid", next_stage="write")
    try:
        R.validate_resume(fresh, "sid", "uid")
        check("当前证据语义版本的新检查点可以续跑", True, R.EVIDENCE_SEMANTICS_VERSION)
    except R.ContractError as exc:
        check("当前证据语义版本的新检查点可以续跑", False, str(exc)[:80])
    stale = {"schema_version": R.STATE_VERSION, "pipeline_version": R.PIPELINE_VERSION,
             "session_id": "sid", "user_id": "uid", "next_stage": "write", "search_local": False,
             "hypotheses": [{"id": "H", "status": "supported_in_reported_scope"}],
             "evidence_links": [{"study_id": "arxiv:old-work", "independence_verified": True}]}
    try:
        R.validate_resume(stale, "sid", "uid")
        check("旧证据语义检查点必须拒绝续跑", False, "竟然被接受")
    except R.ContractError as exc:
        check("旧证据语义检查点必须拒绝续跑", True, str(exc)[:80])

    # ---------- [6] 配置指纹 ----------    print("\n[6] 配置指纹：覆盖影响行为的开关、记录有效值、随任务写入")
    FINGERPRINT_CHECKS = ("同一配置 → 同一指纹", "方向不同 → 指纹不同", "research_config 不同 → 指纹不同",
                          "运行开关不同 → 指纹不同", "模型不同 → 指纹不同",
                          "解析后的方向档案进入指纹（plan 之后才可得）",
                          "clean_snapshot 保留该字段（真实落库由 17 项套件的 A5/A6-I 覆盖）",
                          "开关名与取值都不含密钥形态")
    if not hasattr(R, "record_run_config"):
        for name in FINGERPRINT_CHECKS:
            check(name, False, "pre-PR1: research_integrity has no run_config_digest/record_run_config")
    else:
        base_state = {"research_config": {"quality_threshold": 6}, "direction_request": {"a": 1},
                      "resolved_direction": None, "run_models": {"writer": "m1"},
                      "run_flags": R.run_flags({"F": "1"})}
        first = R.record_run_config(dict(base_state))
        again = R.record_run_config(dict(base_state))
        check("同一配置 → 同一指纹", first == again and bool(re.fullmatch(r"[0-9a-f]{64}", first)), first[:12])
        check("方向不同 → 指纹不同",
              R.record_run_config(dict(base_state, direction_request={"a": 2})) != first)
        check("research_config 不同 → 指纹不同",
              R.record_run_config(dict(base_state, research_config={"quality_threshold": 7})) != first)
        check("运行开关不同 → 指纹不同",
              R.record_run_config(dict(base_state, run_flags=R.run_flags({"ENABLE_FULLTEXT_FETCH": "false"}))) != first)
        check("模型不同 → 指纹不同",
              R.record_run_config(dict(base_state, run_models={"writer": "m2"})) != first)
        check("解析后的方向档案进入指纹（plan 之后才可得）",
              R.record_run_config(dict(base_state, resolved_direction={"profile_hash": "abc"})) != first)
        target = dict(base_state)
        R.record_run_config(target)          # 写入状态，模拟阶段提交前刷新
        persisted = R.clean_snapshot(target)
        check("clean_snapshot 保留该字段（真实落库由 17 项套件的 A5/A6-I 覆盖）",
              persisted.get("run_config_hash") == first, str(persisted.get("run_config_hash"))[:12])
        values = R.run_flags({"ENABLE_FULLTEXT_FETCH": "false", "REPORTED_SCOPE_MIN_FIELDS": "3"})
        secret_like = re.compile(r"(sk-[A-Za-z0-9]{20,}|AKID[A-Za-z0-9]{10,}|LTAI[A-Za-z0-9]{10,})")
        check("开关名与取值都不含密钥形态",
              not any(k in name.upper() for name in R.RUN_CONFIG_FLAGS
                      for k in ("KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL", "ACCESS_ID"))
              and not any(secret_like.search(str(v)) for v in values.values())
              and values["ENABLE_FULLTEXT_FETCH"] is False and values["REPORTED_SCOPE_MIN_FIELDS"] == 3,
              json.dumps(values, ensure_ascii=False))

    # ---------- [7] PR2：三层身份 / 版本 / 时间 / 块绑定 / 平台≠评审 ----------
    print("\n[7] PR2：Work/Version/Study 三层分开、三个日期分开、块绑定交付文本、平台≠评审")
    # R03：不同 OpenReview forum 必须是不同作品
    forum_a = S.work_identity({"url": "https://openreview.net/forum?id=paper_A"})
    forum_b = S.work_identity({"url": "https://openreview.net/forum?id=paper_B"})
    check("不同 OpenReview forum id 保持为不同作品",
          forum_a and forum_b and forum_a != forum_b, f"{forum_a} vs {forum_b}")
    # R04：显式 v1 必须在所有抓取候选里保留
    urls_v1 = S.candidate_fulltext_urls("https://arxiv.org/abs/2501.12345v1")
    check("arXiv v1 输入的所有抓取候选都保留 v1",
          bool(urls_v1) and all("2501.12345v1" in u for u in urls_v1), json.dumps(urls_v1))
    # 无版本号的输入**不猜版本**（version_id 由实际抓取地址冻结）
    nover = S.version_identity({"url": "https://arxiv.org/abs/2501.12345"})
    check("无版本号输入不虚构版本（version_id 为 None，等抓取后冻结）",
          nover["version_id"] is None and nover["version_source"] == "unknown", json.dumps(nover))
    ver = S.version_identity({"url": "https://arxiv.org/abs/2501.12345v1",
                              "fulltext_url": "https://arxiv.org/html/2501.12345v1"})
    check("有版本号输入 → version_id 含 v1 且来源可追溯",
          ver["version_id"] == "arxiv:2501.12345v1" and ver["version_source"] == "fetched_url",
          json.dumps(ver))
    # 作品身份跨版本归并（v1/v3 同一作品），但版本不同
    check("同一作品的 v1/v3 归并为同一 work_id、保留不同 version_id",
          S.work_identity({"url": "https://arxiv.org/abs/2501.12345v1"})
          == S.work_identity({"url": "https://arxiv.org/abs/2501.12345v3"})
          and S.version_identity({"url": "https://arxiv.org/abs/2501.12345v1"})["version_id"]
          != S.version_identity({"url": "https://arxiv.org/abs/2501.12345v3"})["version_id"])
    # R05：摘要里的关键词不构成证据块
    abstract = ("This is a synthetic abstract about method selection, dataset choice, "
                "and results reporting. No experimental section is supplied. ")
    check("摘要里的关键词不得认证证据块（结构性判定）", S.detect_blocks(abstract) == [],
          json.dumps(S.detect_blocks(abstract)))
    structured = "1 Introduction\nWe study X.\n2 Method\nWe propose Y.\n3 Experiments\nDataset: D.\n4 Results\nTable 1: main results\n"
    check("真实结构（标题行/表题）仍能识别出三块",
          set(S.detect_blocks(structured)) >= {"method", "experiment_setup", "results"},
          json.dumps(S.detect_blocks(structured)))
    # R09：块可用性必须限定在实际交付文本内
    long_text = "Introductory context. " * 1000 + "\nMethods\nDataset\nResults\n"
    packet = R.source_packets([{"url": "https://arxiv.org/abs/2501.12345v1", "content": long_text,
                                "version_id": "2501.12345v1", "fulltext_blocks": S.detect_blocks(long_text)}],
                              limit=1, excerpt_chars=100)[0]
    check("块可用性不超出交付文本，且优先交付相关章节（而不是无信息的前缀）",
          set(packet["available_blocks"]) <= set(S.detect_blocks(packet["text"]))
          and packet["excerpt_strategy"] == "section_slices"
          and packet["excerpt_truncated"] is True,
          json.dumps({"delivered": packet["text"][:40], "blocks": packet["available_blocks"],
                      "strategy": packet["excerpt_strategy"], "truncated": packet["excerpt_truncated"]},
                     ensure_ascii=False))
    # 无任何结构信号 + 截断 → 头部截断，且**不声明任何块**（这正是 R09 的反例形态）
    flat = R.source_packets([{"url": "https://arxiv.org/abs/2501.12345v1", "content": "Intro. " * 500,
                              "version_id": "2501.12345v1",
                              "fulltext_blocks": ["method", "results"]}], limit=1, excerpt_chars=100)[0]
    check("无结构时头部截断且不继承整篇块标记",
          flat["available_blocks"] == [] and flat["excerpt_strategy"] == "head_truncation"
          and flat["excerpt_truncated"] is True,
          json.dumps({"blocks": flat["available_blocks"], "strategy": flat["excerpt_strategy"]}))
    sliced = R.source_packets([{"url": "https://arxiv.org/abs/2501.12345v1", "content": structured,
                                "version_id": "2501.12345v1"}], limit=1, excerpt_chars=600)[0]
    check("有结构时按相关章节取片，且块由交付文本重新检测",
          sliced["excerpt_strategy"] == "section_slices"
          and set(sliced["available_blocks"]) <= set(S.detect_blocks(sliced["text"]))
          and bool(sliced["block_locators"]),
          json.dumps({"strategy": sliced["excerpt_strategy"], "blocks": sliced["available_blocks"]}))
    # 三个日期分开，且绝不用抓取时间冒充发表日期
    dates = S.parse_dates({"provider_crawled_at": "2026-09-21T00:00:00Z",
                           "provider_published_date": "2026-09-21"},
                          "[Submitted on 25 Jan 2024 (v1), last revised 3 Mar 2026 (this version, v3)]")
    check("首发日期/版本日期/抓取时间分别保存，且抓取时间不冒充发表日期",
          dates["publication_first_date"] == "2024-01-25" and dates["version_date"] == "2026-03-03"
          and dates["provider_crawled_at"] == "2026-09-21T00:00:00Z"
          and dates["date_source"] == "arxiv_submitted_on", json.dumps(dates, ensure_ascii=False))
    # 硬时间窗口：2024 首发 + 2026 更新 在 2022–2025 回溯任务里不接纳；未来论文同样不接纳
    ok_in, why_in = S.within_window({"publication_first_date": "2024-01-25", "version_date": "2024-01-25"}, 2022, 2025)
    ok_rev, why_rev = S.within_window({"publication_first_date": "2024-01-25", "version_date": "2026-03-03"}, 2022, 2025)
    ok_2026, why_2026 = S.within_window({"publication_first_date": "2026-02-13", "version_date": "2026-02-13"}, 2022, 2025)
    ok_unknown, why_unknown = S.within_window({}, 2022, 2025)
    ok_firstonly, why_firstonly = S.within_window({"publication_first_date": "2024-01-25"}, 2022, 2025)
    check("窗口内来源被接纳（首发与版本日期都已确认）", ok_in is True, why_in)
    check("2024 首发但 2026 更新的版本在 2022–2025 任务里不接纳", ok_rev is False, why_rev)
    check("2026 首发不进入 2022–2025 任务", ok_2026 is False, why_2026)
    check("日期未知返回 None（单列，不计入严格窗口结果）", ok_unknown is None, why_unknown)
    check("只知首发、版本时间未确认 → 不算严格窗口内的历史交付",
          ok_firstonly is None and why_firstonly == "version_time_unconfirmed", why_firstonly)
    win_state = {"direction_request": {"start_year": 2022, "end_year": 2025}}
    accepted = R.source_packets([{"url": "https://arxiv.org/abs/2501.1", "content": "x" * 50,
                                  "publication_first_date": "2024-05-01", "version_date": "2024-05-01",
                                  "version_id": "v1"},
                                 {"url": "https://arxiv.org/abs/2602.2", "content": "y" * 50,
                                  "publication_first_date": "2026-02-13", "version_date": "2026-02-13",
                                  "version_id": "v2"},
                                 {"url": "https://arxiv.org/abs/2503.3", "content": "z" * 50,
                                  "version_id": "v3"}],
                                limit=5, excerpt_chars=50, window=R.evidence_window(win_state))
    check("统一入证门：越界与版本时间未确认的来源都不进入交付材料",
          len(accepted) == 1 and accepted[0]["version_id"] == "v1", json.dumps(accepted, ensure_ascii=False)[:200])
    # 域名 ≠ 评审状态
    check("arXiv 预印本不因域名被判 peer_reviewed",
          S.peer_review_status({"url": "https://arxiv.org/abs/2501.12345"}, "")[0] is None)
    check("DOI 解析器不因域名被判 peer_reviewed",
          S.peer_review_status({"url": "https://doi.org/10.1234/x"}, "")[0] is None)
    check("OpenReview 不因域名被判 peer_reviewed",
          S.peer_review_status({"url": "https://openreview.net/forum?id=a",
                                "venue": "Submitted to ICLR 2025"})[0] is False)
    check("普通正文里的 accepted 措辞不构成录用证据（round2 N13）",
          S.peer_review_status({"url": "https://arxiv.org/abs/2501.12345"},
                               "Our parser accepted 100 rows in the benchmark.")[0] is not True)
    check("元数据聚合站不因域名被判 peer_reviewed",
          S.peer_review_status({"url": "https://www.semanticscholar.org/paper/x"}, "")[0] is None)
    check("有 venue 元数据证据才判 peer_reviewed=True",
          S.peer_review_status({"url": "https://aclanthology.org/2024.acl-long.1/"},
                               object_meta={"citation_conference_title": "Proceedings of ACL 2024"})[0] is True)
    # 同论文两个入口不增加独立验证数（作品级归并只算一票）；换一篇作品才会增加
    # PR3：强结论还要求**条件值可比**，故这里给出与目标一致的条件值。
    pr3_setting = {"task": "dialogue QA", "dataset": "D0", "model": "M0", "metric": "accuracy"}

    def _two_entry_state(second_study: str) -> dict:
        st = gate_state([{"id": "H", "scope_id": "S", "conditions": dict(pr3_setting)}])
        st["evidence_links"] = [
            {"id": f"e{i}", "hypothesis_id": "H", "scope_id": "S", "fact_id": f"f{i}", "stance": "supports",
             "quote_bound": True, "contract_complete": True, "study_id": study,
             "independence_verified": True, "evidence_role": "original",
             "material_status": "fetched",
             "conditions": dict(pr3_setting), "reported_conditions": dict(pr3_setting),
             "required_conditions": sorted(pr3_setting),
             # round2：强结论要求比较字段的取值有原文定位（合成前置数据）
             "condition_evidence": {k: {"value": v, "quote": str(v), "quote_bound": True}
                                    for k, v in pr3_setting.items()},
             "conditions_verified": True}
            for i, study in enumerate(("arxiv:2501.12345", second_study))]
        R.reduce_hypotheses(st)
        return st["hypotheses"][0]

    same_work = _two_entry_state("arxiv:2501.12345")
    other_work = _two_entry_state("arxiv:2502.99999")
    check("同一作品的两个入口只算一项独立研究（不因多入口增加票数）",
          same_work["independent_support_count"] == 1, str(same_work["independent_support_count"]))
    check("换成不同作品时才计为两项（计数不是恒为 1）",
          other_work["independent_support_count"] == 2
          and other_work["status"] == "supported_in_scope", json.dumps(
              {"count": other_work["independent_support_count"], "status": other_work["status"]}))
    # 材料状态如实记录（抓不到 ≠ 论文没有报告）
    with patch.object(S, "fetch_document", return_value={"text": "", "final_url": "https://arxiv.org/abs/2501.12345v1",
                                                         "content_type": "", "object_meta": {}, "ok": False}), \
         patch.object(S, "_fetch_cached", side_effect=AssertionError("抓取失败时不应取元数据")):
        failed = S.enrich_result({"url": "https://arxiv.org/abs/2501.12345v1"}, allow_fetch=True)
    check("抓取失败如实记为 fetch_failed，并给出原因码",
          failed.get("material_status") == "fetch_failed" and failed.get("material_reasons"),
          json.dumps({k: failed.get(k) for k in ("material_status", "material_reasons")}, ensure_ascii=False))
    not_attempted = S.enrich_result({"url": "https://arxiv.org/abs/2501.12345v1"}, allow_fetch=False)
    check("未尝试抓取与抓取失败可区分", not_attempted.get("material_status") == "fetch_not_attempted",
          str(not_attempted.get("material_status")))
    # 交付包不得整体继承全文块标记：预算不足以容纳全部块时，结论只能声明交付文本里的块
    # 注意：arXiv 来源在抓取后会取一次**同对象元数据**（用于版本/日期绑定），离线测试必须一并 mock，
    # 否则「离线套件」会真的发网络请求——这正是当年 PR1.1 P11 抓到的同一类问题。
    synthetic_meta = {"text": "Submission history\n[v1] Wed, 01 Jan 2025 00:00:00 UTC (1 KB)\n",
                      "final_url": "https://arxiv.org/abs/2501.12345", "content_type": "html",
                      "object_meta": {}}
    with patch.object(S, "fetch_document",
                      return_value={"text": long_text, "final_url": "https://arxiv.org/html/2501.12345v1",
                                    "content_type": "html", "object_meta": {}, "ok": True}), \
         patch.object(S, "_fetch_cached", return_value=synthetic_meta):
        S._ARXIV_METADATA_CACHE.clear()
        fetched = S.enrich_result({"url": "https://arxiv.org/abs/2501.12345v1"}, allow_fetch=True)
    check("arXiv 正文抓取后必须完成同对象绑定（verified）才可能进入交付",
          fetched.get("identity_binding") == "verified",
          json.dumps({k: fetched.get(k) for k in ("identity_binding", "version_selection",
                                                  "version_date")}, ensure_ascii=False))
    tight = R.source_packets([fetched], limit=1, excerpt_chars=20)[0]
    check("交付包不继承全文块标记（只声明交付文本内可见的块，并记录裁剪）",
          bool(fetched.get("fulltext_blocks"))
          and set(tight["available_blocks"]) <= set(S.detect_blocks(tight["text"]))
          and set(tight["available_blocks"]) < set(fetched["fulltext_blocks"])
          and tight["excerpt_truncated"] is True,
          json.dumps({"fulltext": fetched.get("fulltext_blocks"), "delivered": tight["available_blocks"],
                      "strategy": tight["excerpt_strategy"], "truncated": tight["excerpt_truncated"]},
                     ensure_ascii=False))

    # ---------- [8] PR3：真正的条件化论断（值级可比 + 子命题 + 对称） ----------
    print("\n[8] PR3：条件值可比（不是字段名）、窄命题必须生成 child_claim、支持/反驳对称")
    setting = {"task": "dialogue QA", "dataset": "D0", "model": "M0", "metric": "accuracy"}

    def claim_state(edges_conditions: list[dict], stance: str = "supports") -> dict:
        st = gate_state([{"id": "H", "scope_id": "S", "conditions": dict(setting)}])
        st["evidence_links"] = [
            {"id": f"e{i}", "hypothesis_id": "H", "scope_id": "S", "fact_id": f"f{i}", "stance": stance,
             "quote_bound": True, "contract_complete": True, "study_id": f"study{i}",
             "independence_verified": True, "evidence_role": "original", "material_status": "fetched",
             "conditions": dict(c), "reported_conditions": dict(c), "required_conditions": sorted(setting),
             # 条件取值必须有原文定位才算「核查过的条件」（round2 / N04）
             "condition_evidence": {k: {"value": v, "quote": str(v), "quote_bound": True}
                                    for k, v in c.items()},
             "conditions_verified": sorted(c) == sorted(setting)}
            for i, c in enumerate(edges_conditions)]
        R.reduce_hypotheses(st)
        return st

    same = claim_state([setting, setting])
    check("条件值逐项一致 + 独立 → supported_in_scope", same["hypotheses"][0]["status"] == "supported_in_scope",
          same["hypotheses"][0]["status"])
    mirror = claim_state([setting, setting], stance="refutes")
    check("支持/反驳对称：镜像用例得到 refuted_in_scope",
          mirror["hypotheses"][0]["status"] == "refuted_in_scope", mirror["hypotheses"][0]["status"])
    conflict = claim_state([setting, setting])
    conflict["evidence_links"][1]["stance"] = "refutes"
    R.reduce_hypotheses(conflict)
    check("同向与反向都可比时仍为 conflicted（如实呈现分歧）",
          conflict["hypotheses"][0]["status"] == "conflicted", conflict["hypotheses"][0]["status"])
    # 字段名相同、值完全不同（R06 的形态）
    mismatched = claim_state([dict(setting, task="image classification", metric="AUC"),
                              dict(setting, task="speech recognition", metric="WER")])
    h = mismatched["hypotheses"][0]
    check("同字段名但值不同 → 不得判强结论（值级可比）", h["status"] == "evidence_limited", h["status"])
    check("不可比逐字段留痕（compatible/incompatible/unknown + 两侧值）",
          all(e["condition_compatibility"]["verdict"] == "incompatible" for e in mismatched["evidence_links"])
          and all("task" in e["condition_compatibility"]["fields"] for e in mismatched["evidence_links"]),
          json.dumps(mismatched["evidence_links"][0]["condition_compatibility"], ensure_ascii=False)[:160])
    check("不可比时不得产出旧规则的 *_in_reported_scope",
          h["status"] not in {"supported_in_reported_scope", "refuted_in_reported_scope"})
    # 条件值未知（只给了部分字段）→ unknown，也不放行
    unknown = claim_state([{k: v for k, v in setting.items() if k in {"task"}},
                           {k: v for k, v in setting.items() if k in {"task"}}])
    check("条件值未知 → 不判强结论，并如实记为 comparability_unknown",
          unknown["hypotheses"][0]["status"] == "evidence_limited"
          and "comparability_unknown" in unknown["hypotheses"][0]["limitations"],
          json.dumps(unknown["hypotheses"][0]["limitations"], ensure_ascii=False))
    # 更窄的命题：父命题未给条件值时，两篇一致设置的证据 → child_claim；父命题不升级
    narrow = claim_state([setting, setting])
    narrow["hypotheses"][0]["conditions"] = {}
    R.reduce_hypotheses(narrow)
    children = narrow["hypotheses"][0]["child_claims"]
    check("更窄的命题写成 child_claim（含实际条件值 + 与父命题关系 + 证据映射）",
          len(children) == 1 and children[0]["relation"] == "narrower_scope_of_parent"
          and set(children[0]["pinned_conditions"]) == set(setting)
          and children[0]["evidence_link_ids"] and children[0]["study_ids"],
          json.dumps(children, ensure_ascii=False)[:200])
    check("原命题不因子命题成立而被证明（父状态不升级）",
          narrow["hypotheses"][0]["status"] != "supported_in_scope", narrow["hypotheses"][0]["status"])
    # 条件值的原文定位：只有能在交付文本里逐字核对上的才记 bound
    loc_state = gate_state([{"id": "H", "scope_id": "S", "conditions": dict(setting)}])
    loc_state["resolved_direction"] = {"profile": {
        "claim_types": [{"id": "ct", "required_conditions": sorted(setting),
                         "required_evidence_blocks": ["results"],
                         "comparison_mode": "same_setting"}],
        "source_policy": {"required_source_blocks": []}}}
    loc_state["hypotheses"][0]["claim_type_id"] = "ct"
    # 注意：交付文本是按**相关章节**取的（PR2），所以条件原文必须落在被交付的章节里；
    # 落在章节外的原文**不算**绑定——这正是「块/条件必须绑定实际交付文本」的体现。
    text = "Introductory remarks only.\nResults\nWe evaluate on dialogue QA with model M0. Accuracy is 72."
    raw = [{"url": "https://arxiv.org/abs/2501.12345v1", "content": text, "version_id": "2501.12345v1"}]
    analysis = {"extracted_facts": [{"source_id": None, "quote": "Accuracy is 72",
                                     "content": "accuracy 72", "related_hypothesis": "H",
                                     "hypothesis_support": "supports", "scope_id": "S",
                                     "conditions": dict(setting),
                                     "condition_quotes": {"task": "dialogue QA", "model": "M0",
                                                          "dataset": "NOT IN TEXT", "metric": "72"}}]}
    packet = R.source_packets(raw, limit=1, excerpt_chars=100000)[0]
    analysis["extracted_facts"][0]["source_id"] = packet["source_id"]
    R.ingest_analysis(loc_state, analysis, raw, ["sec1"])
    edge = loc_state["evidence_links"][0]
    check("条件值带原文定位：能核对上的记 bound，核对不上的不假装有依据",
          edge["condition_evidence"]["task"]["quote_bound"] is True
          and edge["condition_evidence"]["dataset"]["quote_bound"] is False
          and edge["conditions_verified"] is False,
          json.dumps(edge["condition_evidence"], ensure_ascii=False)[:200])
    # 旧规则的检查点不得与新语义混用（规则版本升级到 evidence-semantics/4）
    check("证据语义版本已升级（禁止旧投票与新语义混用）",
          R.EVIDENCE_SEMANTICS_VERSION == "evidence-semantics/4", R.EVIDENCE_SEMANTICS_VERSION)
    legacy = {"schema_version": R.STATE_VERSION, "pipeline_version": R.PIPELINE_VERSION,
              "evidence_semantics_version": "evidence-semantics/3",
              "session_id": "sid", "user_id": "uid", "next_stage": "write", "search_local": False}
    try:
        R.validate_resume(legacy, "sid", "uid")
        check("旧规则版本（/2）的检查点必须拒绝续跑", False, "竟然被接受")
    except R.ContractError as exc:
        check("旧规则版本（/2）的检查点必须拒绝续跑", True, str(exc)[:70])

    # ---------- [9] arXiv 同对象元数据绑定（不联网）----------
    print("\n[9] arXiv：正文与版本日期必须来自同一对象，且不得猜版本")
    meta_text = ("Submission history\n[v1] Tue, 23 Jul 2024 20:51:52 UTC (425 KB)\n"
                 "[v2] Thu, 17 Oct 2024 17:51:19 UTC (426 KB)\n")
    body_text = "Abstract\n" + ("We study retrieval. " * 400) + "\nResults\nTable 1: results.\n"

    def bind(url: str, doc_url: str) -> dict:
        doc = {"text": body_text, "final_url": doc_url, "content_type": "html",
               "object_meta": {}, "ok": True, "requested_url": doc_url}
        with patch.object(S, "fetch_document", return_value=doc), \
             patch.object(S, "_fetch_cached",
                          return_value={"text": meta_text, "final_url": "https://arxiv.org/abs/2407.16833",
                                        "content_type": "html", "object_meta": {}}):
            S._ARXIV_METADATA_CACHE.clear()
            return S.enrich_result({"url": url}, allow_fetch=True)

    explicit = bind("https://arxiv.org/abs/2407.16833v1", "https://arxiv.org/html/2407.16833v1")
    check("显式 v1 输入 + 正文带 v1 → 绑定 v1 的日期（不用最新版日期顶替）",
          explicit.get("version_date") == "2024-07-23"
          and explicit.get("version_selection") == "content_url_version"
          and explicit.get("identity_binding") == "verified"
          and explicit.get("version_id") == "arxiv:2407.16833v1",
          json.dumps({k: explicit.get(k) for k in ("version_date", "version_selection",
                                                   "identity_binding", "version_id")},
                     ensure_ascii=False))
    # 请求 v1、正文却是 v2 → 必须拒绝，不能「因为返回的版本很明确」就当它是请求的版本
    v2_returned = bind("https://arxiv.org/abs/2407.16833v1", "https://arxiv.org/html/2407.16833v2")
    check("请求 v1 但正文返回 v2 → 拒绝（version_mismatch，且不写任何日期）",
          v2_returned.get("version_date") is None
          and v2_returned.get("identity_binding") == "version_mismatch"
          and v2_returned.get("version_selection") == "requested_content_version_mismatch",
          json.dumps({k: v2_returned.get(k) for k in ("version_date", "version_selection", "identity_binding")},
                     ensure_ascii=False))
    # 上游已填了最新版日期，若与被确认版本冲突必须校正并记录，不能「非空优先」
    conflict = bind("https://arxiv.org/abs/2407.16833v1", "https://arxiv.org/html/2407.16833v1")
    conflict["version_date"] = "2024-10-17"
    S.bind_arxiv_dates(conflict)
    check("版本日期冲突 → 以被确认版本的官方历史为准并记录校正",
          conflict.get("version_date") == "2024-07-23"
          and (conflict.get("version_date_correction") or {}).get("from") == "2024-10-17",
          json.dumps({k: conflict.get(k) for k in ("version_date", "version_date_correction")},
                     ensure_ascii=False))
    # 无版本输入：正确做法是**先解析 N 再抓显式 vN**；匿名正文（地址无版本）一律不绑定
    unversioned = bind("https://arxiv.org/abs/2407.16833", "https://arxiv.org/html/2407.16833")
    check("无版本输入 + 正文地址无版本 → 不绑定（独立缓存的 latest 不能证明这份匿名正文就是那一版）",
          unversioned.get("version_date") is None
          and unversioned.get("identity_binding") == "unverified_version"
          and unversioned.get("version_selection") == "unversioned_content_unverified",
          json.dumps({k: unversioned.get(k) for k in ("version_date", "version_selection", "identity_binding")},
                     ensure_ascii=False))
    # 先解析 N 再抓显式 vN 的路径：enrich_result 必须用带版本的候选地址抓取
    requested_urls: list[str] = []

    def fake_fetch(url: str) -> dict:
        requested_urls.append(url)
        return {"text": body_text, "final_url": "https://arxiv.org/html/2407.16833v2",
                "content_type": "html", "object_meta": {}, "ok": True, "requested_url": url}

    with patch.object(S, "fetch_document", side_effect=fake_fetch), \
         patch.object(S, "_fetch_cached",
                      return_value={"text": meta_text, "final_url": "https://arxiv.org/abs/2407.16833",
                                    "content_type": "html", "object_meta": {}}):
        S._ARXIV_METADATA_CACHE.clear()
        resolved = S.enrich_result({"url": "https://arxiv.org/abs/2407.16833"}, allow_fetch=True)
    check("无版本输入：先解析 N（最新版）再抓显式 vN 正文，然后才绑定日期",
          resolved.get("version_resolution") == "metadata_latest_before_fetch"
          and requested_urls and requested_urls[0].endswith("v2")
          and resolved.get("version_date") == "2024-10-17"
          and resolved.get("identity_binding") == "verified",
          json.dumps({"resolution": resolved.get("version_resolution"), "fetched": requested_urls,
                      "version_date": resolved.get("version_date"),
                      "binding": resolved.get("identity_binding")}, ensure_ascii=False))
    unconfirmed = bind("https://arxiv.org/abs/2407.16833v1", "https://arxiv.org/html/2407.16833")
    check("请求 v1 但正文地址不带版本 → 拒绝合并（不给无版本地址套 v1 的日期）",
          unconfirmed.get("version_date") is None
          and unconfirmed.get("version_selection") == "version_unconfirmed_in_content",
          json.dumps({k: unconfirmed.get(k) for k in ("version_date", "version_selection")},
                     ensure_ascii=False))
    mismatched = bind("https://arxiv.org/abs/2407.16833v1", "https://arxiv.org/html/2501.00001v1")
    check("元数据与正文不是同一 work → 拒绝合并，且**入证门真的不交付**",
          mismatched.get("version_date") is None
          and mismatched.get("identity_binding") == "work_mismatch"
          and R.source_packets([mismatched], window=(2022, 2025)) == [],
          json.dumps({"binding": mismatched.get("identity_binding"),
                      "delivered": len(R.source_packets([mismatched], window=(2022, 2025)))},
                     ensure_ascii=False))
    # 元数据响应跳到别的 work → 该次元数据无效（不能把别人的提交历史记成本 work 的）
    with patch.object(S, "_fetch_cached",
                      return_value={"text": "[Submitted on 13 Feb 2025]\nSubmission history\n"
                                            "[v1] Thu, 13 Feb 2025 00:00:00 UTC",
                                    "final_url": "https://arxiv.org/abs/2502.09017",
                                    "content_type": "html", "object_meta": {}}):
        S._ARXIV_METADATA_CACHE.clear()
        wrong_work = S._arxiv_metadata("2407.16833")
    check("元数据响应必须属于被请求的 work（跳转即判无效）",
          wrong_work.get("ok") is False and wrong_work.get("reason") == "work_mismatch_response",
          json.dumps({k: wrong_work.get(k) for k in ("ok", "reason", "final_url")}, ensure_ascii=False))
    # 并发合并：同一 work 的进行中请求只发一次
    import threading as _threading
    from concurrent.futures import ThreadPoolExecutor as _Pool
    S._ARXIV_METADATA_CACHE.clear()
    S.METADATA_STATS.update(requests=0, cache_hits=0, failures=0, skipped_budget=0)
    barrier = _threading.Barrier(2)
    fetch_calls: list[str] = []

    def _concurrent_fetch(url: str) -> dict:
        fetch_calls.append(url)
        barrier.wait(timeout=5)
        return {"text": meta_text, "final_url": url, "content_type": "html", "object_meta": {}}

    with patch.object(S, "_fetch_cached", side_effect=_concurrent_fetch):
        with _Pool(max_workers=2) as pool:
            list(pool.map(S._arxiv_metadata, ["2407.16833", "2407.16833"]))
    check("同一 work 的并发元数据请求被合并（只发一次）",
          len(fetch_calls) == 1 and S.METADATA_STATS["cache_hits"] >= 1,
          json.dumps({"fetch_calls": len(fetch_calls), "stats": S.METADATA_STATS}, ensure_ascii=False))
    S._ARXIV_METADATA_CACHE.clear()
    with patch.object(S, "_fetch_cached", side_effect=AssertionError("非 arXiv 不得取元数据")):
        blog = S.enrich_result({"url": "https://example.org/post"}, allow_fetch=False)
    check("非 arXiv 来源不触发任何元数据请求", blog.get("material_status") == "metadata_only")
    # 硬预算：超出上限就不再请求，日期保持未知（由窗口规则延后），并如实记账。
    saved_max, saved_stats = S.METADATA_MAX_REQUESTS, dict(S.METADATA_STATS)
    try:
        S.METADATA_MAX_REQUESTS = S.METADATA_STATS["requests"]  # 上限 = 已经用掉的请求数
        S._ARXIV_METADATA_CACHE.clear()
        over = bind("https://arxiv.org/abs/2407.16833", "https://arxiv.org/html/2407.16833")
        check("元数据请求有硬预算：超限不再发请求，日期保持未知并单独记账",
              over.get("version_date") is None
              and over.get("version_selection") == "metadata_budget_exhausted"
              and S.METADATA_STATS["skipped_budget"] >= 1,
              json.dumps({"selection": over.get("version_selection"),
                          "stats": S.METADATA_STATS}, ensure_ascii=False))
    finally:
        S.METADATA_MAX_REQUESTS = saved_max
        S.METADATA_STATS.clear()
        S.METADATA_STATS.update(saved_stats)
        S._ARXIV_METADATA_CACHE.clear()
    # 窗口语义未被放宽：2020 首发的论文在 2022–2025 窗口内仍被拒
    old = {"publication_first_date": "2020-05-22", "version_date": "2021-04-12"}
    check("窗口语义未放宽：首发 2020 的论文在 2022–2025 仍判越界",
          S.within_window(old, 2022, 2025) == (False, "outside_window:2020-05-22"),
          json.dumps(S.within_window(old, 2022, 2025)))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nPR1/PR1.1/PR2/PR3 可信度收口断言: {passed}/{len(RESULTS)} PASS")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
