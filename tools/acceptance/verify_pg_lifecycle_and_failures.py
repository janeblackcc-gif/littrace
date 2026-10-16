#!/usr/bin/env python3
"""D-01 & D-03 真 PG 阶段恢复、并发拒绝与三种故障注入验收脚本。

依据 MIGRATION_RELEASE_PLAN_v1.md §5 (D-01, D-03):
- D-01:
  - 真实 PostgreSQL (engine / CheckpointGuard)，四大阶段使用固定/Mock Agent 结果做故障注入（显式标注 mock 的是 Agent，不是 PG）；
  - 阶段检查点中断后 resume，恢复后续阶段并完成；
  - stage_history 证明已完成阶段未重复执行；
  - 同 session 并发两执行者被 RunBusy 拒绝，不同 session 不被误锁；
  - 不兼容的旧快照被明确拒绝 (ContractError)。
- D-03:
  - 三种故障：Agent 显式异常、结构化响应无法解析、持久化故障；
  - 必须没有假 research_complete / passed；
  - 终态与错误事件留存。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))
os.environ["ENABLE_DIRECTION_PROFILES"] = "true"

from dotenv import load_dotenv
load_dotenv(BASE / ".env")

from core.runtime_settings import sanitize_no_proxy
sanitize_no_proxy()

from core.database import SessionLocal, engine
from core.security import get_password_hash
from models import User, ResearchCheckpoint
from service.checkpoint_guard import CheckpointGuard, RunBusy
from service.research_integrity import ContractError
from service.research_runtime import execute_stages, clean_snapshot, ui_snapshot
from service.deep_research_v2.state import create_initial_state

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def new_user(tag: str) -> str:
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(username=f"d_{tag}_{s}", email=f"d_{tag}_{s}@example.com",
                 hashed_password=get_password_hash("D-Passw0rd!"))
        db.add(u)
        db.commit()
        db.refresh(u)
        return str(u.id)


def fetch_checkpoint(session_id: str, user_id: str) -> dict:
    from uuid import UUID as _U
    with SessionLocal() as db:
        row = db.query(ResearchCheckpoint).filter(
            ResearchCheckpoint.session_id == session_id,
            ResearchCheckpoint.user_id == _U(user_id)).first()
        return (row.state_json or {}) if row else {}


class MockStageAgent:
    """明确声明：Mock 的是 Agent 推理产物，不是 PostgreSQL 数据库。

    提供确定性的各阶段产出，用于隔离验证状态机、PG 检查点持久化与恢复逻辑。
    """
    def __init__(self, stage_name: str, raises: Exception | None = None, return_invalid_json: bool = False):
        self.stage_name = stage_name
        self.raises = raises
        self.return_invalid_json = return_invalid_json
        self.call_count = 0

    async def process(self, state: dict) -> dict:
        self.call_count += 1
        if self.raises:
            raise self.raises
        if self.stage_name == "architect":
            state["outline"] = [{"id": "s1", "title": "Section 1", "status": "pending", "requires_data": True}]
            state["research_plan"] = {"sections": state["outline"], "hypotheses": []}
            return state
        elif self.stage_name == "scout":
            state["raw_sources"] = [{"url": "https://arxiv.org/abs/2402.11975", "content": "Sample content"}]
            state["facts"] = [{"id": "f1", "statement": "Fact 1", "source_url": "https://arxiv.org/abs/2402.11975"}]
            return state
        elif self.stage_name in {"data_analyst", "wizard"}:
            state["charts"] = []
            return state
        elif self.stage_name == "writer":
            state["final_report"] = "# Final Research Report\n\nVerified findings with empirical evidence."
            return state
        elif self.stage_name == "critic":
            state["review_complete"] = True
            state["quality_status"] = "passed"
            state["quality_score"] = 9.0
            return state
        return state


async def test_d01_stage_recovery():
    print("\n=== [D-01] 1. 真 PG 阶段中断与恢复 ===")
    uid = new_user("rec")
    sid = str(uuid.uuid4())
    guard = CheckpointGuard(engine, sid, uid)

    # 构造初始状态
    st = create_initial_state("恢复验证查询", sid, search_web=True, search_local=False)
    st["user_id"] = uid
    st["direction_request"] = {"choice": {"mode": "preset", "preset_id": "agent_memory"}, "start_year": 2022, "end_year": 2025}
    st["resolved_direction"] = {"profile_hash": "mock_hash_123", "rule_origin": "preset", "profile": {"name": "Test"}}

    # 1. 模拟执行 plan 阶段并中断
    agents_phase1 = {
        "architect": MockStageAgent("architect"),
        "scout": MockStageAgent("scout"),
        "data_analyst": MockStageAgent("data_analyst"),
        "wizard": MockStageAgent("wizard"),
        "writer": MockStageAgent("writer"),
        "critic": MockStageAgent("critic"),
    }
    with guard as g:
        g.save(clean_snapshot(st), ui_snapshot(st))
        async def save_cb(s, u):
            g.save(s, u)
        async def cancelled_cb():
            return False

        # 只执行第一阶段 (plan)，随后模拟进程退出
        events = []
        async for ev in execute_stages(st, agents_phase1, save_cb, cancelled_cb):
            events.append(ev.get("type"))
            if ev.get("type") == "checkpoint_saved":
                # 阶段 1 (plan) 保存完成，立即中断
                break

    check("Plan 阶段产生 checkpoint_saved 并落库", "checkpoint_saved" in events)
    saved_st = fetch_checkpoint(sid, uid)
    check("落库 next_stage 为 research", saved_st.get("next_stage") == "research", f"next_stage={saved_st.get('next_stage')}")
    check("落库 completed_stages 包含 plan", saved_st.get("completed_stages") == ["plan"])

    # 2. 从真实 PG 恢复执行后续阶段
    with guard as g:
        loaded_st = g.load()
        check("真实 PG 成功 load 出中断检查点", loaded_st is not None)
        async def save_cb2(s, u):
            g.save(s, u)
        async def cancelled_cb2():
            return False

        resume_events = []
        async for ev in execute_stages(loaded_st, agents_phase1, save_cb2, cancelled_cb2):
            resume_events.append(ev.get("type"))

    check("恢复后执行至终态 research_complete", "research_complete" in resume_events)
    final_st = fetch_checkpoint(sid, uid)
    check("最终状态 run_status=completed", final_st.get("run_status") == "completed")
    check("所有阶段已完成", set(["plan", "research", "analyze", "write"]).issubset(set(final_st.get("completed_stages", []))))

    # 3. 验证 stage_history：plan 阶段只执行了 1 次，未被重复执行
    plan_runs = [h for h in final_st.get("stage_history", []) if h.get("stage") == "plan"]
    check("stage_history 证明已完成的 plan 阶段未重复执行", len(plan_runs) == 1, f"plan 执行次数={len(plan_runs)}")
    print("  [EVIDENCE] " + json.dumps({
        "case_id": "D01_stage_recovery",
        "session_id": sid,
        "interrupted_checkpoint": {
            "next_stage": saved_st.get("next_stage"),
            "completed_stages": saved_st.get("completed_stages"),
        },
        "resumed_run_id": final_st.get("run_id"),
        "db_run_status": final_st.get("run_status"),
        "last_terminal_event": final_st.get("last_terminal_event"),
        "plan_run_count": len(plan_runs),
    }, ensure_ascii=False, sort_keys=True))


async def test_d01_concurrency_and_compatibility():
    print("\n=== [D-01] 2. 并发拒绝与不兼容快照拒绝 ===")
    uid = new_user("conc")
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())

    guard_a1 = CheckpointGuard(engine, sid_a, uid)
    guard_a2 = CheckpointGuard(engine, sid_a, uid)
    guard_b = CheckpointGuard(engine, sid_b, uid)

    # 1. 同 session 并发拒绝
    with guard_a1:
        runbusy_caught = False
        try:
            with guard_a2:
                pass
        except RunBusy:
            runbusy_caught = True
        check("同 session 第二个执行者被 RunBusy 拒绝", runbusy_caught)

        # 2. 不同 session 不被误锁
        b_success = False
        try:
            with guard_b:
                b_success = True
        except Exception:
            b_success = False
        check("不同 session (sid_b) 未被误锁，正常获得锁", b_success)

    # 3. 不兼容快照拒绝
    from service.research_integrity import validate_resume
    bad_st = create_initial_state("bad query", sid_a)
    bad_st["evidence_semantics_version"] = "outdated_semantics_v0"  # 旧语义
    compat_rejected = False
    try:
        validate_resume(bad_st, sid_a, uid)
    except ContractError as ce:
        compat_rejected = True
        check("旧证据语义版本快照被 validate_resume 明确拒绝", True, str(ce)[:80])
    if not compat_rejected:
        check("旧证据语义版本快照被 validate_resume 明确拒绝", False, "未能拦截旧语义")
    print("  [EVIDENCE] " + json.dumps({
        "case_id": "D01_concurrency_compatibility",
        "locked_session_id": sid_a,
        "independent_session_id": sid_b,
        "same_session_runbusy": runbusy_caught,
        "different_session_lock_acquired": b_success,
        "old_snapshot_rejected": compat_rejected,
    }, ensure_ascii=False, sort_keys=True))


async def test_d03_failure_injections():
    print("\n=== [D-03] 三种故障注入验收 ===")
    uid = new_user("fail")

    async def never_cancelled():
        return False

    def terminal_errors(events: list[dict]) -> list[dict]:
        return [event for event in events if event.get("type") == "error"]

    def check_exact_failure(prefix: str, events: list[dict], state: dict,
                            persisted: dict, expected_error: str) -> None:
        errors = terminal_errors(events)
        check(f"{prefix} 仅产生一个 error 终态事件", len(errors) == 1,
              f"error_count={len(errors)}")
        check(f"{prefix} 命中预定错误原因",
              len(errors) == 1 and errors[0].get("content") == expected_error,
              errors[0].get("content", "") if errors else "缺少 error 事件")
        check(f"{prefix} error 事件带终态与执行身份",
              len(errors) == 1 and errors[0].get("terminal") is True
              and bool(errors[0].get("run_id")) and isinstance(errors[0].get("event_seq"), int))
        check(f"{prefix} 未产生虚假的 research_complete",
              not any(event.get("type") == "research_complete" for event in events))
        check(f"{prefix} 内存终态为 failed", state.get("run_status") == "failed")
        check(f"{prefix} PG 持久化终态为 failed", persisted.get("run_status") == "failed",
              f"run_status={persisted.get('run_status')}")
        check(f"{prefix} PG 留存准确错误原因", expected_error in persisted.get("errors", []),
              repr(persisted.get("errors", [])))

    # 故障 1: Agent 显式异常。先证明 Architect 确实被调用，再核对准确错误原因。
    sid_fail1 = str(uuid.uuid4())
    guard_f1 = CheckpointGuard(engine, sid_fail1, uid)
    st_f1 = create_initial_state("fail query 1", sid_fail1, search_web=True)
    st_f1["user_id"] = uid
    architect_failure = "Simulated LLM API 500 Failure"
    failing_architect = MockStageAgent("architect", raises=RuntimeError(architect_failure))
    agents_f1 = {
        "architect": failing_architect,
        "scout": MockStageAgent("scout"),
        "data_analyst": MockStageAgent("data_analyst"),
        "wizard": MockStageAgent("wizard"),
        "writer": MockStageAgent("writer"),
        "critic": MockStageAgent("critic"),
    }
    with guard_f1 as g:
        g.save(clean_snapshot(st_f1), ui_snapshot(st_f1))
        async def save_f1(s, u):
            g.save(s, u)
        events_f1 = []
        async for ev in execute_stages(st_f1, agents_f1, save_f1, never_cancelled):
            events_f1.append(ev)

    check("Agent 异常注入点确实到达 Architect.process", failing_architect.call_count == 1,
          f"call_count={failing_architect.call_count}")
    check_exact_failure("Agent 显式异常", events_f1, st_f1,
                        fetch_checkpoint(sid_fail1, uid), architect_failure)
    print("  [EVIDENCE] " + json.dumps({
        "case_id": "D03_agent_exception",
        "session_id": sid_fail1,
        "agent_call_count": failing_architect.call_count,
        "terminal_event": terminal_errors(events_f1)[0] if terminal_errors(events_f1) else None,
        "db_state": {"run_status": fetch_checkpoint(sid_fail1, uid).get("run_status"),
                     "errors": fetch_checkpoint(sid_fail1, uid).get("errors", [])},
    }, ensure_ascii=False, sort_keys=True))

    # 故障 2: Agent 正常返回，但结构校验在目标检查点拒绝空规划。
    sid_fail2 = str(uuid.uuid4())
    guard_f2 = CheckpointGuard(engine, sid_fail2, uid)
    st_f2 = create_initial_state("fail query 2", sid_fail2, search_web=True)
    st_f2["user_id"] = uid
    class EmptyPlanAgent:
        def __init__(self):
            self.call_count = 0

        async def process(self, state: dict) -> dict:
            self.call_count += 1
            state["outline"] = []  # 空规划，违反 Contract
            return state
    empty_plan_agent = EmptyPlanAgent()
    agents_f2 = dict(agents_f1, architect=empty_plan_agent)
    with guard_f2 as g:
        g.save(clean_snapshot(st_f2), ui_snapshot(st_f2))
        async def save_f2(s, u):
            g.save(s, u)
        events_f2 = []
        async for ev in execute_stages(st_f2, agents_f2, save_f2, never_cancelled):
            events_f2.append(ev)
    plan_contract_error = "Architect returned an empty plan"
    check("结构故障注入点确实到达 EmptyPlanAgent.process", empty_plan_agent.call_count == 1,
          f"call_count={empty_plan_agent.call_count}")
    check_exact_failure("规划结构不合法", events_f2, st_f2,
                        fetch_checkpoint(sid_fail2, uid), plan_contract_error)
    print("  [EVIDENCE] " + json.dumps({
        "case_id": "D03_structure_rejection",
        "session_id": sid_fail2,
        "agent_call_count": empty_plan_agent.call_count,
        "terminal_event": terminal_errors(events_f2)[0] if terminal_errors(events_f2) else None,
        "db_state": {"run_status": fetch_checkpoint(sid_fail2, uid).get("run_status"),
                     "errors": fetch_checkpoint(sid_fail2, uid).get("errors", [])},
    }, ensure_ascii=False, sort_keys=True))

    # 故障 3: 所有阶段和阶段提交均成功，只在 run_status=completed 的最终提交注入失败。
    sid_fail3 = str(uuid.uuid4())
    guard_f3 = CheckpointGuard(engine, sid_fail3, uid)
    st_f3 = create_initial_state("fail query 3", sid_fail3, search_web=True)
    st_f3["user_id"] = uid
    agents_f3 = {
        name: MockStageAgent(name)
        for name in ("architect", "scout", "data_analyst", "wizard", "writer", "critic")
    }
    final_save_error = "Simulated PostgreSQL failure at final completion commit"
    final_commit_attempts = []
    with guard_f3 as g:
        g.save(clean_snapshot(st_f3), ui_snapshot(st_f3))

        async def fail_only_final_commit(s, u):
            if s.get("run_status") == "completed":
                final_commit_attempts.append({
                    "run_status": s.get("run_status"),
                    "next_stage": s.get("next_stage"),
                    "report_present": bool(s.get("final_report", "").strip()),
                })
                raise ConnectionError(final_save_error)
            g.save(s, u)

        events_f3 = []
        async for ev in execute_stages(st_f3, agents_f3, fail_only_final_commit, never_cancelled):
            events_f3.append(ev)

    reached_agents = {name: agent.call_count for name, agent in agents_f3.items()}
    check("最终保存故障前所有目标 Agent 均已执行",
          all(count >= 1 for count in reached_agents.values()), repr(reached_agents))
    check("最终保存故障只在完成提交点注入一次", final_commit_attempts == [{
        "run_status": "completed", "next_stage": "done", "report_present": True,
    }], repr(final_commit_attempts))
    check_exact_failure("最终完成保存故障", events_f3, st_f3,
                        fetch_checkpoint(sid_fail3, uid), final_save_error)
    print("  [EVIDENCE] " + json.dumps({
        "case_id": "D03_final_commit_failure",
        "session_id": sid_fail3,
        "agent_call_counts": reached_agents,
        "final_commit_attempts": final_commit_attempts,
        "terminal_event": terminal_errors(events_f3)[0] if terminal_errors(events_f3) else None,
        "db_state": {"run_status": fetch_checkpoint(sid_fail3, uid).get("run_status"),
                     "errors": fetch_checkpoint(sid_fail3, uid).get("errors", [])},
    }, ensure_ascii=False, sort_keys=True))


async def main():
    await test_d01_stage_recovery()
    await test_d01_concurrency_and_compatibility()
    await test_d03_failure_injections()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n[D-01 / D-03 验收汇总]: {passed}/{total} PASS")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
