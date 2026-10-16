"""运行与证据结构诊断（**不是**核验质量评测）。

为什么改名（原名 measure_conclusion_quality.py）：
  旧名暗示它能测「结论质量」，于是产出被写成「thinking 质量最好」「成本 3.82×」这类结论——
  那两份总结都不能成立：
    - 报告更长、supported 更多、token 更多 ≠ 核验更正确，且 supported 判定本身当时有
      证据语义缺陷（字段名共同出现即判支持、work_id 直接置独立性）；
    - 差异来自不同模型/输入输出/思考计费，只能叫 token 用量倍数，不能叫实际费用倍数，
      更不能当成效果提升。
  本脚本实际能做的是：真实跑完整链路，记录**运行状态与证据结构**，供复核与复算，
  以及为「同一配置的两份结果才可比」提供配置指纹。它不是质量评测，也不给通过/不通过。

要点：
- 走**生产同一条执行器** `DeepResearchGraph.run_sync`（不是另写一套编排）。
- 每轮用独立 session_id 与独立用户（前缀 `measure_`，见 cleanup_test_data.py）。
- 通过环境变量控制实验条件（思考模式、min_scope），并记录**实际生效值**与配置指纹。
- 指标从 `research_checkpoints.state_json` 读取（与验收记录同一数据来源）。
- 分子/分母必须可复算：n_studies_counted 限定到该结论**实际计入**的同向边与适用范围，
  并注明分母（该论断下被考察的边数）。

用法：
    python tools/acceptance/diagnose_run_and_evidence.py --rounds 2
    python tools/acceptance/diagnose_run_and_evidence.py --rounds 1 --min-scope 3
    python tools/acceptance/diagnose_run_and_evidence.py --rounds 1 --thinking --label thinking-on
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(BASE, "backend", "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE, ".env"))

# 本机 NO_PROXY 含 "[::1]"，httpx 会把它当 URL 解析并抛
# InvalidURL('Invalid port: ":1]"')。应用的 app_main 里有 sanitize_no_proxy，
# 但独立脚本不走那条启动路径——必须在**构造任何客户端之前**自己规范化。
# （同类问题已在 a1i_real_upload.py 上踩过一次，此处是第三处，故统一在此处理。）
from core.runtime_settings import sanitize_no_proxy  # noqa: E402
sanitize_no_proxy()

DEFAULT_QUERY = "LLM Agent 的记忆压缩与上下文管理有哪些实证结论"
# 状态计数用，**不是**质量指标：supported 多不代表核验更正确。
# 其中两个 `*_in_reported_scope` 是旧规则产物（条件可比性未核查），已对称隔离，
# 这里只作运行记录，不得读作结论。
SUPPORTING_STATUSES = {"supported_in_scope", "supported_in_reported_scope", "refuted_in_scope",
                       "refuted_in_reported_scope", "conflicted"}
INDEPENDENCE_NOTE = ("研究身份与研究计数只汇总归约器输出的 adopted_evidence（含规则版本与排除理由）；"
                     "PR1.1 起 independence_verified 一律为 unknown，故 n_studies_counted 目前恒为 0："
                     "该字段**不得**读作「N 篇独立论文」")


def new_user(tag: str) -> str:
    """建一次性用户，返回 user_id（研究必须绑定真实用户）。"""
    from core.database import SessionLocal
    from core.security import get_password_hash
    from models import User
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(username=f"measure_{tag}_{suffix}", email=f"measure_{tag}_{suffix}@example.com",
                 hashed_password=get_password_hash("Measure-Passw0rd!"))
        db.add(u)
        db.commit()
        db.refresh(u)
        return str(u.id)


def counted_studies(hyp: dict, links: list[dict]) -> dict:
    """只汇总归约器**实际采用**的证据边与研究标识，不再自行猜测研究身份。

    旧版按 `study_id or work_id or source_group` 猜身份，于是
    scope_id 不同、`evidence_role="secondary"`、条件不完整、根本没有 study_id 的边
    也会被计成一个研究。现在研究身份只能来自归约器输出的 `adopted_evidence`
    （含规则版本、采用的边、排除理由）；缺失时**拒绝推断**并如实返回 0。

    ⚠️ 本函数必须**自包含**（不引用任何模块级名字）：验收脚本会按 AST 直接取出
    这个函数体单独执行。
    """
    claim_id = hyp.get("id")
    considered = [e for e in links if isinstance(e, dict) and e.get("hypothesis_id") == claim_id]
    adopted = hyp.get("adopted_evidence")
    if not isinstance(adopted, dict):
        return {"status": hyp.get("status"), "conclusive": False, "rule_version": None,
                "scope_fields": None, "n_studies_counted": 0, "n_edges_considered": len(considered),
                "evidence_link_ids": [], "study_ids": [], "excluded_reasons": {},
                "limitations": ["no adopted_evidence recorded by the reducer; "
                                "refusing to infer study identity from work/source ids"]}
    study_ids = [s for s in (adopted.get("study_ids") or []) if s]
    excluded = [x for x in (adopted.get("excluded") or []) if isinstance(x, dict)]
    reasons = sorted({str(x.get("reason")) for x in excluded})
    return {"status": hyp.get("status"),
            "conclusive": bool(adopted.get("conclusive")),
            "rule_version": adopted.get("rule_version"),
            "scope_fields": adopted.get("scope_fields"),
            "n_studies_counted": len(study_ids),
            "n_edges_considered": len(considered),
            "evidence_link_ids": list(adopted.get("evidence_link_ids") or []),
            "study_ids": study_ids,
            "excluded_reasons": {r: len([x for x in excluded if str(x.get("reason")) == r]) for r in reasons},
            "limitations": []}


def collect_metrics(user_id: str, session_id: str) -> dict:
    """从检查点里抽出运行与证据结构指标。"""
    from core.database import SessionLocal
    from models import ResearchCheckpoint
    from uuid import UUID as _UUID
    with SessionLocal() as db:
        row = db.query(ResearchCheckpoint).filter(
            ResearchCheckpoint.session_id == session_id,
            ResearchCheckpoint.user_id == _UUID(user_id)).first()
        if row is None:
            return {"error": "no checkpoint written"}
        state = row.state_json or {}
        ui = row.ui_state_json or {}

    hyps = state.get("hypotheses", []) or []
    by_status: dict[str, int] = {}
    for h in hyps:
        s = h.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    concluded = [h for h in hyps if h.get("status") in SUPPORTING_STATUSES]

    links = state.get("evidence_links", []) or []
    concluded_detail = [{"id": h.get("id"), **counted_studies(h, links)} for h in concluded]

    edges = links
    quote_bound = [e for e in edges if e.get("quote_bound")]
    usage = state.get("usage", {}) or {}
    report = state.get("final_report", "") or ""
    # 哈希必须先确认非空再比较：两个空值相等会得出「报告与审核已绑定」的假结论。
    report_hash = state.get("report_hash")
    reviewed_hash = state.get("reviewed_report_hash")

    return {
        "run_status": state.get("run_status"),
        "quality_status": state.get("quality_status"),
        "next_stage": state.get("next_stage"),
        "iteration": state.get("iteration"),
        "unresolved_issues": state.get("unresolved_issues"),
        "run_config_hash": state.get("run_config_hash"),
        "hypotheses_total": len(hyps),
        "status_breakdown": by_status,
        "concluded_total": len(concluded),
        "concluded_detail": concluded_detail,
        "independence_note": INDEPENDENCE_NOTE,
        "evidence_links_total": len(edges),
        "quote_bound": len(quote_bound),
        "quote_bound_ratio": round(len(quote_bound) / len(edges), 3) if edges else None,
        # 作品标识数（作品级去重），**不是**研究独立数
        "cited_works": len({e.get("source_group") for e in edges if e.get("source_group")}),
        "report_chars": len(report),
        "state_matches_ui": state.get("final_report") == ui.get("streaming_report"),
        "report_hash_present": bool(report_hash),
        "reviewed_hash_present": bool(reviewed_hash),
        "report_hash_eq_reviewed": bool(report_hash) and bool(reviewed_hash) and report_hash == reviewed_hash,
        "usage": usage,
    }


async def one_round(label: str, query: str, thinking: bool, min_scope: int, max_iterations: int) -> dict:
    os.environ["LLM_ENABLE_THINKING"] = "true" if thinking else "false"
    os.environ["REPORTED_SCOPE_MIN_FIELDS"] = str(min_scope)
    from service.deep_research_v2.service import DeepResearchV2Service
    # 确认方向档案可加载（3 个预置方向），加载失败要在这里就暴露
    from service.direction_contracts import load_preset
    load_preset("agent_memory")

    user_id = new_user(label)
    session_id = str(uuid.uuid4())
    print(f"\n{'='*72}\n[{label}] thinking={thinking} min_scope={min_scope} max_iter={max_iterations}")
    print(f"  session={session_id}")
    print(f"  query={query}")
    print(f"{'='*72}")
    t0 = time.time()
    service = DeepResearchV2Service(max_iterations=max_iterations)
    error = None
    try:
        await service.research_sync(
            query=query, session_id=session_id, user_id=user_id,
            search_web=True, search_local=False,
            direction_request={"choice": {"mode": "preset", "preset_id": "agent_memory"},
                               "start_year": 2022, "end_year": 2025, "focus_angles": []},
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - t0
    print(f"  [{label}] 执行结束，耗时 {elapsed/60:.1f} 分钟" + (f"；异常: {error}" if error else ""))

    metrics = collect_metrics(user_id, session_id)
    metrics.update({"label": label, "session_id": session_id, "user_id": user_id,
                    "thinking": thinking, "min_scope": min_scope,
                    "max_iterations": max_iterations, "elapsed_sec": round(elapsed, 1),
                    "error": error, "at": datetime.now().isoformat(timespec="seconds")})
    return metrics


def summarize(m: dict) -> str:
    if m.get("error") and m.get("error") == "no checkpoint written":
        return f"{m['label']:<18} 未写出检查点"
    tok = (m.get("usage") or {}).get("total_tokens")
    return (f"{m.get('label',''):<18} "
            f"已有结论的状态数={m.get('concluded_total')} "
            f"claims={m.get('hypotheses_total')} "
            f"quality={m.get('quality_status')} "
            f"quote比率={m.get('quote_bound_ratio')} "
            f"报告={m.get('report_chars')}字 "
            f"tokens={tok} "
            f"耗时={round((m.get('elapsed_sec') or 0)/60,1)}分 "
            f"issues={m.get('unresolved_issues')} "
            f"cfg={str(m.get('run_config_hash') or '')[:12]}")


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=2, help="同一配置跑几轮（看稳定性）")
    p.add_argument("--min-scope", type=int, default=2)
    p.add_argument("--thinking", action="store_true", help="开启思考模式（默认关闭；实验需固定材料与计划）")
    p.add_argument("--max-iterations", type=int, default=1)
    p.add_argument("--query", default=DEFAULT_QUERY)
    p.add_argument("--label", default=None)
    p.add_argument("--out", default=os.path.join(BASE, "docs", "littrace", "run_evidence_diagnosis.json"))
    args = p.parse_args()

    label_base = args.label or (f"{'think' if args.thinking else 'nothink'}-K{args.min_scope}")
    print(f"诊断配置: rounds={args.rounds} min_scope={args.min_scope} "
          f"thinking={args.thinking} max_iterations={args.max_iterations}")
    print("说明：这是运行与证据结构诊断，不给出质量结论，也不代表核验有效。")

    # 轮次之间**串行**：并发会互相抢 LLM 配额，也会让「稳定性」观察失真
    results = []
    for i in range(args.rounds):
        label = label_base if args.rounds == 1 else f"{label_base}-r{i+1}"
        results.append(await one_round(label, args.query, args.thinking, args.min_scope, args.max_iterations))

    print(f"\n{'='*72}\n运行与证据结构汇总（不是质量评测）\n{'='*72}")
    for m in results:
        print("  " + summarize(m))
        for d in m.get("concluded_detail", []):
            print(f"      · {d['id']}: {d['status']} 已证实={d['conclusive']} "
                  f"采用规则={d['rule_version']} 适用范围={d['scope_fields'] or '-'} "
                  f"计入研究数={d['n_studies_counted']}/{d['n_edges_considered']} 条边 "
                  f"排除原因={d['excluded_reasons'] or '-'}")
    print(f"\n  ⚠️ {INDEPENDENCE_NOTE}")

    # 追加写入，便于多批结果横向对比（不同配置的结果只能作为不同配置分别陈述）
    history = []
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    history.extend(results)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"\n结果已追加到 {os.path.relpath(args.out, BASE)}（共 {len(history)} 条记录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
