"""Queue-streamed stage executor. Agents keep process(state) -> state.

A checkpoint is the last COMMITTED stage boundary. An interrupted stage is replayed;
external LLM/search side effects are not exactly-once.
"""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable
from .research_integrity import (ContractError, RUN_STATE, clean_snapshot, passed_quality,
                                 record_run_config, refresh_usage_totals, report_hash)


class ResearchCancelled(Exception):
    pass


# 终态事件类型：出现其一即表示**这次执行**已经结束，之后不可能再有同一次执行的完成事件。
TERMINAL_EVENT_TYPES = frozenset({"research_complete", "research_cancelled", "error"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mark_run(state: dict, run_id: str, **fields) -> None:
    """把终态写回 `run_history` 里**属于这次执行**的那条记录。

    为什么需要单独做：失败路径会 `state.clear(); state.update(baseline)` 把状态回滚到
    阶段边界，而 baseline 是**快照副本**——之前持有的那条 dict 已经不在 state 里了。
    因此必须按 run_id 在回滚后的列表里重新定位（找不到就补一条），否则终态信息会丢。
    """
    history = state.setdefault("run_history", [])
    entry = next((e for e in history if isinstance(e, dict) and e.get("run_id") == run_id), None)
    if entry is None:
        entry = {"run_id": run_id, "started_at": _now_iso(), "start_stage": state.get("next_stage")}
        history.append(entry)
    entry.update(fields)


def ui_snapshot(state: dict) -> dict:
    return {
        "research_steps": [{"type": x, "status": "completed"} for x in state.get("completed_stages", [])],
        "search_results": [{"id": f["id"], "title": f.get("source_name", ""), "url": f.get("source_url", ""),
                            "snippet": f.get("content", "")[:200]} for f in state.get("facts", [])],
        "charts": state.get("charts", []), "knowledge_graph": state.get("knowledge_graph", {}),
        "streaming_report": state.get("final_report", ""), "references": state.get("references", []),
        "quality_status": state.get("quality_status", "not_reviewed"), "run_status": state.get("run_status", "running"),
    }


async def _run_agent_with_state(agent, state: dict):
    """在**该 Agent 自己的 task/Context 内**设置并复位 RUN_STATE。

    PR4 / 反例 R11：旧实现在生成器函数体里 `RUN_STATE.set()`，token 一直持有到
    `finally: RUN_STATE.reset(token)`——生成器的 aclose / 提前 break / 取消可能发生在
    **另一个 Context**，于是 `reset()` 抛
    `ValueError: <Token ...> was created in a different Context`，
    而该异常发生在清理路径上，只能变成 "Task exception was never retrieved"。
    现在 set/reset 严格局限在同一个 task 内（Agent 调用生命周期），
    生成器本身**不跨 yield 持有 token**。
    """
    token = RUN_STATE.set(state)
    try:
        return await agent.process(state)
    finally:
        RUN_STATE.reset(token)


async def execute_stages(state: dict, agents: dict, save: Callable[[dict, dict], Awaitable[None]],
                         cancelled: Callable[[], Awaitable[bool]]) -> AsyncIterator[dict]:
    queue: asyncio.Queue = asyncio.Queue()
    state["_message_queue"] = queue
    baseline = clean_snapshot(state)
    active_tasks = set()
    current_agent = {"name": None}

    # --- 执行身份与事件序号--------------------------------
    # 为什么需要：真实联调里曾出现「同一 session 既有 error 又有 completed」的两种观测，
    # 事后查明是**两次不同执行**被观测器混在一起。要让这件事**直接可判**，必须给每次执行
    # 一个可辨认的 run_id，并记录事件序号与终态：
    #   - 每次执行有自己的 run_id，写入检查点与每条事件；
    #   - 事件带单调 event_seq，观测器可以按 (run_id, event_seq) 拼接完整序列；
    #   - 终态事件（research_complete / research_cancelled / error）显式带 terminal=True；
    #   - `run_history` 留痕每次执行的起止与终态，恢复/重试是新的一条记录（新 attempt）。
    run_id = uuid.uuid4().hex[:12]
    state["run_id"] = run_id
    seq = {"n": 0}
    run_entry = {"run_id": run_id, "started_at": _now_iso(), "start_stage": state.get("next_stage"),
                 "status": "running", "terminal_event": None, "ended_at": None}
    state.setdefault("run_history", []).append(run_entry)

    def stamp(event: dict) -> dict:
        seq["n"] += 1
        out = dict(event)
        out["run_id"] = run_id
        out["event_seq"] = seq["n"]
        out["terminal"] = out.get("type") in TERMINAL_EVENT_TYPES
        return out

    async def emit_agent(name: str):
        if await cancelled():
            raise ResearchCancelled()
        errors_before = len(state.get("errors", []))
        current_agent["name"] = name
        task = asyncio.create_task(_run_agent_with_state(agents[name], state))
        active_tasks.add(task)
        try:
            while not task.done():
                if await cancelled():
                    raise ResearchCancelled()
                try:
                    yield stamp(await asyncio.wait_for(queue.get(), timeout=0.5))
                except asyncio.TimeoutError:
                    continue
            result = await task  # propagate, never log-and-continue
            if not isinstance(result, dict):
                raise ContractError(f"{name} returned no state")
            if result is not state:
                state.update(result)
                state["_message_queue"] = queue
            if len(state.get("errors", [])) > errors_before:
                raise ContractError(f"{name} recorded a stage error")
            while not queue.empty():
                yield stamp(queue.get_nowait())
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            active_tasks.discard(task)

    async def commit():
        state["messages"] = []
        refresh_usage_totals(state)
        await save(clean_snapshot(state), ui_snapshot(state))

    try:
        while state["next_stage"] != "done":
            if await cancelled():
                raise ResearchCancelled()
            baseline = clean_snapshot(state)
            stage = state["next_stage"]
            state["run_status"] = "running"
            if stage == "plan":
                state["phase"] = "init"
                names, next_stage = ["architect"], "research"
            elif stage in {"research", "supplement"}:
                state["phase"] = "researching" if stage == "research" else "re_researching"
                names, next_stage = ["scout"], "analyze" if stage == "research" else "reanalyze"
            elif stage in {"analyze", "reanalyze"}:
                state["phase"] = "analyzing"
                if stage == "reanalyze":
                    state["charts"] = []  # new facts invalidate derived charts
                names, next_stage = ["data_analyst", "wizard"], "write" if stage == "analyze" else "rewrite"
            elif stage in {"write", "rewrite"}:
                state["phase"] = "writing"
                if stage == "rewrite":
                    state["draft_sections"], state["references"] = {}, []
                    for section in state.get("outline", []):
                        section["status"] = "researched"
                names, next_stage = ["writer"], "review"
            elif stage == "revise":
                state["phase"] = "revising"
                names, next_stage = ["writer"], "review"
            elif stage == "review":
                state["phase"] = "reviewing"
                names, next_stage = ["critic"], "done"
            else:
                raise ContractError(f"unknown stage: {stage}")
            yield stamp({"type": "phase", "phase": state["phase"], "stage": stage, "content": f"执行阶段: {stage}"})
            for name in names:
                async for event in emit_agent(name):
                    yield event
            if stage == "plan" and not state.get("outline"):
                raise ContractError("Architect returned an empty plan")
            if stage in {"write", "rewrite", "revise"}:
                if not state.get("final_report", "").strip():
                    raise ContractError("Writer returned an empty report")
                state["report_hash"] = report_hash(state["final_report"])
                state["review_complete"] = False
                state["quality_status"] = "not_reviewed"
            if stage in {"rewrite", "revise"}:
                state["iteration"] += 1  # completed repair rounds, not review invocations
            if stage == "review":
                if passed_quality(state):
                    state["quality_status"] = "passed"
                elif not state.get("review_complete") or state["iteration"] >= state["max_iterations"]:
                    state["quality_status"] = "needs_review"
                else:
                    next_stage = "supplement" if state.get("review_route") == "research" else "revise"
                    state["quality_status"] = "needs_review"
            if stage in {"plan", "research", "analyze", "write"} and stage not in state["completed_stages"]:
                state["completed_stages"].append(stage)
            state.setdefault("stage_history", []).append({"stage": stage, "iteration": state["iteration"], "status": "completed"})
            state["next_stage"] = next_stage
            record_run_config(state)  # 每个提交点刷新配置指纹（plan 之后才含解析方向）
            await commit()  # phase, next_stage, report, review and UI saved in one DB transaction
            yield stamp({"type": "checkpoint_saved", "session_id": state["session_id"], "phase": state["phase"], "next_stage": next_stage})
        if not state.get("final_report", "").strip():
            raise ContractError("cannot complete an empty report")
        state["run_status"], state["phase"] = "completed", "completed"
        state["quality_status"] = "passed" if passed_quality(state) else "needs_review"
        record_run_config(state)
        run_entry.update(status="completed", terminal_event="research_complete", ended_at=_now_iso())
        _mark_run(state, run_id, status="completed", terminal_event="research_complete", ended_at=_now_iso())
        state["last_terminal_event"] = {"type": "research_complete", "run_id": run_id,
                                        "event_seq": seq["n"] + 1, "at": _now_iso()}
        await commit()
        yield stamp({"type": "research_complete", "final_report": state["final_report"],
               "run_status": state["run_status"], "quality_status": state["quality_status"],
               "quality_score": state.get("quality_score", 0), "report_hash": state.get("report_hash"),
               "reviewed_report_hash": state.get("reviewed_report_hash"), "references": state.get("references", []),
               "facts_count": len(state.get("facts", [])), "charts_count": len(state.get("charts", [])),
               "iterations": state["iteration"], "usage": state.get("usage", {}),
               "run_config_hash": state.get("run_config_hash")})
    except (Exception, asyncio.CancelledError) as exc:
        calls = state.get("llm_calls", [])
        is_cancelled = isinstance(exc, (ResearchCancelled, asyncio.CancelledError))
        # Restore the previous stage boundary; retain cost audit for the failed attempt.
        state.clear()
        state.update(baseline)
        state["llm_calls"] = calls
        state["run_status"] = "paused" if is_cancelled else "failed"
        state["quality_status"] = "needs_review"
        state.setdefault("errors", []).append(type(exc).__name__ if is_cancelled else str(exc))
        terminal_type = "research_cancelled" if is_cancelled else "error"
        run_entry.update(status=state["run_status"], terminal_event=terminal_type, ended_at=_now_iso())
        # 状态已被回滚到阶段边界，必须按 run_id 重新定位这条执行的记录。
        _mark_run(state, run_id, status=state["run_status"], terminal_event=terminal_type,
                  ended_at=_now_iso())
        state["last_terminal_event"] = {"type": terminal_type, "run_id": run_id,
                                        "event_seq": seq["n"] + 1, "at": _now_iso(),
                                        "content": str(exc)}
        try:
            await commit()
        except Exception:
            # Last durable checkpoint remains the recovery source. No false success event.
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        yield stamp({"type": terminal_type, "content": str(exc),
                     "next_stage": state["next_stage"], "run_status": state["run_status"]})
    finally:
        # 所有 Agent task 必须在这里结束；随后清掉队列引用。
        # 注意：**没有** RUN_STATE.reset —— token 不再由生成器持有（见 _run_agent_with_state）。
        for task in active_tasks:
            if not task.done():
                task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        state.pop("_message_queue", None)
